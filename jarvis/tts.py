"""Голос: нейросетевой Microsoft (без ключа, в облаке) и запасной мужской Pavel из самой Windows.

Библиотека edge-tts открывает новое соединение на каждую фразу, а это около секунды до Казахстана.
Поэтому соединение здесь постоянное: один WebSocket обслуживает много реплик и прогревается заранее,
пока человек ещё говорит. Замер: простаивающее соединение живёт не меньше 30 с, через 60 с оно мертво.

Сервис иногда отвечает пустотой на отдельный голос (замер: ru-RU-DmitryNeural и en-US-AndrewMultilingualNeural давали
NoAudioReceived). Поэтому сначала пробуем другие мужские голоса и только потом Windows: Джарвис не должен заговорить женским голосом.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import aiohttp
import edge_tts
from edge_tts import communicate as _cm
from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
from edge_tts.data_classes import TTSConfig
from edge_tts.drm import DRM

from .config import backup_voices, voice_pitch
from .textproc import clean_for_speech

log = logging.getLogger("jarvis.tts")

_SPEECH_CONFIG = (
    "Content-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n"
    '{"context":{"synthesis":{"audio":{"metadataoptions":{'
    '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
    '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
)
_MAX_IDLE = 25.0
_CACHE_TEXT_LIMIT = 60
_CACHE_FILES_LIMIT = 400
_CLOUD_RETRY_AFTER = 30.0
_VOICE_RETRY_AFTER = 120.0  # голос молчит: не дёргаем его на каждой фразе, иначе тембр скакал бы посреди ответа
_CONNECT_ATTEMPTS = 3
_CONNECT_HEDGE = 1.4  # обычное подключение занимает 0,9–1,1 с
_CONNECT_LAST_WAIT = 5.0
MP3_BYTES_PER_SECOND = 6000  # 48 кбит/с, постоянный битрейт

# В классическом списке SAPI этого ноутбука только женские Irina и Zira. Мужской Pavel установлен как голос OneCore,
# и обычный System.Speech его не видит, а SAPI через категорию токенов видит: замер 535 мс на фразу, основной тон 113 Гц.
_SAPI_SCRIPT = (
    "$OutputEncoding=[Text.Encoding]::UTF8;[Console]::InputEncoding=[Text.Encoding]::UTF8;"
    "$v=New-Object -ComObject SAPI.SpVoice;"
    "try{$c=New-Object -ComObject SAPI.SpObjectTokenCategory;"
    "$c.SetId('HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices',$false);"
    "foreach($x in $c.EnumerateTokens()){if($x.GetAttribute('Name') -match 'Pavel'){$v.Voice=$x;break}}}catch{};"
    "if($v.Voice.GetAttribute('Name') -notmatch 'Pavel'){$r=$v.GetVoices('Language=419');if($r.Count){$v.Voice=$r.Item(0)}};"
    "$f=New-Object -ComObject SAPI.SpAudioFormat;$f.Type=22;"
    "$s=New-Object -ComObject SAPI.SpFileStream;$s.Format=$f;$s.Open($args[0],3);$v.AudioOutputStream=$s;"
    "$t=[Console]::In.ReadToEnd();[void]$v.Speak($t);$s.Close()"
)


@dataclass
class SpeechAudio:
    data: bytes
    mime: str
    engine: str
    seconds: float  # сколько ждали звук


class SpeechSynth:
    def __init__(self, home: Path) -> None:
        self.cache_dir = home / "tts-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()
        self._warming: asyncio.Task | None = None
        self._cloud_down_until = 0.0
        self._voice_down: dict[str, float] = {}
        self.failures = 0
        self._prune_cache()

    async def close(self) -> None:
        if self._warming:
            self._warming.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    def warm(self) -> None:
        """Заранее поднимает соединение. Безопасно вызывать сколько угодно раз."""
        if self._warming and not self._warming.done():
            return
        if self._fresh():
            return
        self._warming = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        try:
            async with self._lock:
                await self._ensure()
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
            log.info("Не удалось прогреть голос: %s", type(exc).__name__)

    def _fresh(self) -> bool:
        return (
            self._ws is not None
            and not self._ws.closed
            and time.monotonic() - self._last_used < _MAX_IDLE
        )

    async def _ensure(self) -> aiohttp.ClientWebSocketResponse:
        if self._fresh():
            return self._ws  # type: ignore[return-value]
        await self._drop()
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        ws = await self._race_connect()
        await ws.send_str(f"X-Timestamp:{_cm.date_to_string()}\r\n{_SPEECH_CONFIG}")
        self._ws = ws
        self._last_used = time.monotonic()
        return ws

    async def _open_socket(self) -> aiohttp.ClientWebSocketResponse:
        url = (
            f"{WSS_URL}&ConnectionId={_cm.connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}"
        )
        try:
            return await self._session.ws_connect(  # type: ignore[union-attr]
                url, compress=15, headers=DRM.headers_with_muid(WSS_HEADERS),
                ssl=_cm._SSL_CTX, timeout=aiohttp.ClientWSTimeout(ws_close=2),
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 403:
                # 403 здесь чаще всего означает расхождение часов: библиотека умеет его поправить.
                DRM.handle_client_response_error(exc)
            raise

    async def _race_connect(self) -> aiohttp.ClientWebSocketResponse:
        """Гонка подключений. Замер из Казахстана: обычно 0,95 с, но примерно в 12 % случаев
        рукопожатие TLS с этим сервисом зависает на 9 с (у Groq, Gemini и Open-Meteo такого нет).
        Поэтому, если соединения ещё нет, запускаем параллельно следующую попытку."""
        pending: set[asyncio.Task] = set()
        error: BaseException | None = None
        try:
            for attempt in range(_CONNECT_ATTEMPTS):
                pending.add(asyncio.create_task(self._open_socket()))
                wait = _CONNECT_HEDGE if attempt < _CONNECT_ATTEMPTS - 1 else _CONNECT_LAST_WAIT
                while pending:
                    done, pending = await asyncio.wait(pending, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        break  # время вышло: пора запускать следующую попытку
                    winners = [task for task in done if task.exception() is None]
                    for task in done:
                        error = task.exception() or error
                    if winners:
                        for extra in winners[1:]:
                            _close_socket(extra)
                        return winners[0].result()
            raise error or TimeoutError("голос не подключился")
        finally:
            for task in pending:
                task.add_done_callback(_close_socket)
                task.cancel()

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await asyncio.wait_for(ws.close(), 1)
            except (aiohttp.ClientError, OSError, TimeoutError):
                pass

    async def synthesize(self, text: str, voice: str, rate: int = 0) -> SpeechAudio | None:
        """Возвращает звук для текста или None, если произносить нечего."""
        text = clean_for_speech(text)
        if not text:
            return None
        started = time.perf_counter()
        cache = self._cache_path(text, voice, rate) if len(text) <= _CACHE_TEXT_LIMIT else None
        if cache is not None and cache.exists():
            return SpeechAudio(cache.read_bytes(), "audio/mpeg", "cache", time.perf_counter() - started)

        rate_str = f"{rate:+d}%"
        # Когда облако недоступно, не ждём его на каждой фразе: сразу запасной голос.
        if time.monotonic() >= self._cloud_down_until:
            data, spoken = await self._cloud_speech(text, voice, rate_str)
            if data:
                self.failures = 0
                if cache is not None and spoken == voice:
                    self._cache_put(cache, data)
                return SpeechAudio(data, "audio/mpeg", "edge", time.perf_counter() - started)
            self.failures += 1
            self._cloud_down_until = time.monotonic() + _CLOUD_RETRY_AFTER

        wav = await asyncio.to_thread(_sapi_synth, text)
        if wav:
            return SpeechAudio(wav, "audio/wav", "sapi", time.perf_counter() - started)
        return None

    def _voices_to_try(self, voice: str) -> list[str]:
        now = time.monotonic()
        order = [voice, *backup_voices(voice)]
        alive = [item for item in order if self._voice_down.get(item, 0.0) <= now]
        return alive or order

    async def _cloud_speech(self, text: str, voice: str, rate: str) -> tuple[bytes, str]:
        """Звук и голос, которым он записан. Пустота при живой связи значит «этот голос сейчас молчит»: пробуем следующий."""
        for candidate in self._voices_to_try(voice):
            task = asyncio.create_task(self._ws_synth(text, candidate, rate))
            task.add_done_callback(_consume_exception)
            data, answered = b"", False
            try:
                # Отмена реплики не должна рвать соединение: фоновая задача дочитает ответ сервера,
                # и следующая реплика получит чистый тёплый канал.
                data, answered = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — любой сбой облака ведёт к запасному голосу
                log.warning("Сбой облачного голоса: %s", type(exc).__name__)
            if data:
                self._voice_down.pop(candidate, None)
                return data, candidate
            if not answered:
                break  # сеть, а не голос: остальные голоса только потратили бы время
            log.info("Голос %s ответил пустотой", candidate)
            self._voice_down[candidate] = time.monotonic() + _VOICE_RETRY_AFTER
        return await self._library_synth(text, voice, rate), voice

    def cached(self, text: str, voice: str, rate: int = 0) -> SpeechAudio | None:
        """Звук из кэша или ничего: для фраз-заполнителей ждать сеть нельзя."""
        text = clean_for_speech(text)
        path = self._cache_path(text, voice, rate)
        if text and path.exists():
            return SpeechAudio(path.read_bytes(), "audio/mpeg", "cache", 0.0)
        return None

    async def _ws_synth(self, text: str, voice: str, rate: str) -> tuple[bytes, bool]:
        """(звук, ответил ли сервис до конца). Второе отличает молчащий голос от пропавшей сети."""
        async with self._lock:
            for attempt in range(2):
                try:
                    async with asyncio.timeout(9):
                        ws = await self._ensure()
                        data, finished = await self._exchange(ws, text, voice, rate)
                    if not finished:
                        await self._drop()
                    if data or finished:
                        return data, finished
                except TimeoutError:
                    # Завис сервис или сеть: повтор только удвоит ожидание.
                    await self._drop()
                    return b"", False
                except (aiohttp.ClientError, OSError, ConnectionError) as exc:
                    log.info("Соединение с голосом потеряно (%s), попытка %d", type(exc).__name__, attempt + 1)
                    await self._drop()
            return b"", False

    async def _exchange(
        self, ws: aiohttp.ClientWebSocketResponse, text: str, voice: str, rate: str
    ) -> tuple[bytes, bool]:
        config = TTSConfig(voice, rate, "+0%", voice_pitch(voice), "SentenceBoundary")
        ssml = _cm.mkssml(config, escape(_cm.remove_incompatible_characters(text)))
        await ws.send_str(_cm.ssml_headers_plus_data(_cm.connect_id(), _cm.date_to_string(), ssml))
        audio = bytearray()
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                raw = msg.data.encode("utf-8")
                head, _ = _cm.get_headers_and_data(raw, raw.find(b"\r\n\r\n"))
                if head.get(b"Path") == b"turn.end":
                    self._last_used = time.monotonic()
                    return bytes(audio), True
            elif msg.type == aiohttp.WSMsgType.BINARY:
                if len(msg.data) < 2:
                    continue
                head, data = _cm.get_headers_and_data(msg.data, int.from_bytes(msg.data[:2], "big"))
                if head.get(b"Content-Type") == b"audio/mpeg" and data:
                    audio += data
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        return bytes(audio), False

    async def _library_synth(self, text: str, voice: str, rate: str) -> bytes:
        """Второй шанс через публичный API библиотеки на случай, если изменился протокол."""
        audio = bytearray()
        try:
            async with asyncio.timeout(8):
                async for chunk in edge_tts.Communicate(text, voice, rate=rate, pitch=voice_pitch(voice)).stream():
                    if chunk["type"] == "audio":
                        audio += chunk["data"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.info("Запасной путь edge-tts не сработал: %s", type(exc).__name__)
        return bytes(audio)

    def _cache_path(self, text: str, voice: str, rate: int) -> Path:
        digest = hashlib.sha256(f"{voice}|{voice_pitch(voice)}|{rate}|{text}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.mp3"

    def _cache_put(self, path: Path, data: bytes) -> None:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as exc:
            log.info("Кэш голоса недоступен: %s", exc)

    def _prune_cache(self) -> None:
        files = sorted(self.cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        for path in files[: max(0, len(files) - _CACHE_FILES_LIMIT)]:
            path.unlink(missing_ok=True)


def _consume_exception(task: asyncio.Task) -> None:
    # Брошенная после отмены задача может упасть: это не должно шуметь в журнале.
    if not task.cancelled():
        task.exception()


def _close_socket(task: asyncio.Task) -> None:
    # Проигравшая гонку попытка могла всё же подключиться: не оставляем лишний сокет открытым.
    if not task.cancelled() and task.exception() is None:
        closing = asyncio.ensure_future(task.result().close())
        closing.add_done_callback(_consume_exception)


def _sapi_synth(text: str) -> bytes:
    """Windows SAPI: офлайн и без зависимостей, 0,6 с на фразу по замеру."""
    with tempfile.TemporaryDirectory(prefix="jarvis-tts-") as folder:
        out = Path(folder) / "speech.wav"
        path_arg = str(out).replace("'", "''")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"& {{{_SAPI_SCRIPT}}} '{path_arg}'"],
                input=text.encode("utf-8"), capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return out.read_bytes() if out.exists() else b""
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Запасной голос Windows не сработал: %s", type(exc).__name__)
            return b""


def estimate_seconds(audio: SpeechAudio) -> float:
    if audio.mime == "audio/mpeg":
        return len(audio.data) / MP3_BYTES_PER_SECOND
    return max(0.0, (len(audio.data) - 44) / 44100)  # SAPI по умолчанию: 22 кГц, 16 бит, моно

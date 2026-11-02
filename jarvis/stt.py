"""Распознавание речи: Groq Whisper как основное, Vosk на этом же ноутбуке как запасное."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import wave
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ConfigStore
from .speech_model import SpeechModel
from .textproc import tokens
from .vad import SAMPLE_RATE

log = logging.getLogger("jarvis.stt")

GROQ_BASE = "https://api.groq.com/openai/v1"
WHISPER_MODEL = "whisper-large-v3-turbo"
# Список слов, а не предложение: связную подсказку Whisper на тишине выдавал за речь человека
# («Разговор с голосовым ассистентом Джарвис» в настоящем журнале). Латиница в подсказке заставляет его
# и в русской фразе писать английские слова по-английски: замер на синтетической речи показал
# machine learning, Telegram и deadline вместо «маршин-лернинг», «Телеграм» и «дедлайн».
WHISPER_PROMPT = (
    "Джарвис, VS Code, Chrome, Claude, YouTube, Spotify, Telegram, GitHub, git push, commit, branch, Python, JavaScript, "
    "ESP32, Arduino, Raspberry Pi, machine learning, deep learning, deadline, Steam, Discord."
)
_PROMPT_WORDS = set(tokens(WHISPER_PROMPT))
# Язык определяет сам Whisper, иначе целая английская фраза превращается в кириллицу. На коротких русских
# фразах он может ошибиться и решить, что это другой язык: такой результат переспрашиваем с явным русским.
_EXPECTED_LANGUAGES = {"russian", "english", "ru", "en"}

# Известные «галлюцинации» Whisper на тишине и шуме: это титры из обучающих данных.
_HALLUCINATION = re.compile(
    r"субтитр|dimatorzok|продолжение следует|спасибо за просмотр|подпис(?:ывайтесь|ка)|amara\.org"
    r"|корректор|ставьте лайк|до новых встреч|игорь негода|в следующей серии|thank you|thanks for watching"
    r"|разговор с голосовым ассистентом",
    re.IGNORECASE,
)
_ANY_LETTER = re.compile(r"[^\W\d_]")


class SpeechUnavailable(Exception):
    """Распознать речь нечем."""


@dataclass
class Transcript:
    text: str
    engine: str
    seconds: float


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


def parse_whisper(payload: dict[str, Any]) -> str:
    """Отбрасывает сегменты, в которых сам Whisper не уверен, что слышит речь."""
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        kept = [
            str(seg.get("text", "")).strip()
            for seg in segments
            if not (float(seg.get("no_speech_prob", 0)) >= 0.7 and float(seg.get("avg_logprob", 0)) < -0.6)
        ]
        text = " ".join(part for part in kept if part)
    else:
        text = str(payload.get("text", "")).strip()
    if not _ANY_LETTER.search(text):
        return ""
    if len(text.split()) <= 8 and _HALLUCINATION.search(text):
        return ""
    words = tokens(text)
    if len(words) >= 3 and all(word in _PROMPT_WORDS for word in words):
        return ""  # на шуме модель иногда просто повторяет подсказку
    return text


class Transcriber:
    def __init__(self, config: ConfigStore, http: httpx.AsyncClient, model: SpeechModel) -> None:
        self.config = config
        self.http = http
        self.model = model
        self._test_base = os.environ.get("JARVIS_TEST_STT_BASE")  # только для автотестов

    def _endpoint(self) -> tuple[str, str] | None:
        if self._test_base:
            return self._test_base, "test"
        key = self.config.key("groq")
        return (GROQ_BASE, key) if key else None

    async def transcribe(self, pcm: bytes, language: str = "") -> Transcript:
        """language: язык известен заранее. При проверке имени он нужен: на шуме сам Whisper выбирал албанский и английский."""
        started = time.perf_counter()
        endpoint = self._endpoint()
        if endpoint:
            try:
                text = await self._cloud(pcm, *endpoint, language)
                return Transcript(text, "whisper", time.perf_counter() - started)
            except (httpx.HTTPError, ValueError) as exc:
                # Лимит, сбой сети или сервиса: разговор не должен обрываться из-за этого.
                log.warning("Whisper недоступен (%s), перехожу на локальное распознавание", _describe(exc))
        try:
            text = await self._local(pcm)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            # Нет ни ключа, ни модели: например, первый запуск без интернета.
            raise SpeechUnavailable(type(exc).__name__) from exc
        return Transcript(text, "vosk", time.perf_counter() - started)

    async def _post(self, audio: bytes, filename: str, mime: str, base: str, key: str, language: str = "") -> dict[str, Any]:
        data = {
            "model": WHISPER_MODEL,
            "response_format": "verbose_json",
            "temperature": "0",
            "prompt": WHISPER_PROMPT,
        }
        if language:
            data["language"] = language
        resp = await self.http.post(
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, audio, mime)},
            data=data,
            timeout=httpx.Timeout(connect=5, read=12, write=8, pool=4),
        )
        resp.raise_for_status()
        return resp.json()

    async def _recognize(self, audio: bytes, filename: str, mime: str, base: str, key: str, language: str = "") -> str:
        if language:
            return parse_whisper(await self._post(audio, filename, mime, base, key, language=language))
        payload = await self._post(audio, filename, mime, base, key)
        detected = str(payload.get("language", "")).lower()
        if detected and detected not in _EXPECTED_LANGUAGES:
            payload = await self._post(audio, filename, mime, base, key, language="ru")
        return parse_whisper(payload)

    async def _cloud(self, pcm: bytes, base: str, key: str, language: str = "") -> str:
        return await self._recognize(pcm_to_wav(pcm), "speech.wav", "audio/wav", base, key, language)

    async def transcribe_file(self, audio: bytes, filename: str, mime: str) -> str:
        """Готовый файл, например голосовое сообщение из Telegram. Только облако: Vosk такое не разберёт."""
        endpoint = self._endpoint()
        if endpoint is None:
            raise SpeechUnavailable("нет ключа")
        try:
            return await self._recognize(audio, filename, mime, *endpoint)
        except (httpx.HTTPError, ValueError) as exc:
            raise SpeechUnavailable(_describe(exc)) from exc

    async def _local(self, pcm: bytes) -> str:
        model = await self.model.get()

        def run() -> str:
            from vosk import KaldiRecognizer

            rec = KaldiRecognizer(model, SAMPLE_RATE)
            rec.AcceptWaveform(pcm)
            return json.loads(rec.FinalResult()).get("text", "").strip()

        text = await asyncio.to_thread(run)
        return text[:1].upper() + text[1:] if text else ""


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__

"""Сердце Джарвиса: конечный автомат разговора.

Режимы: «sleep» (ждём слово активации) и «active» (идёт разговор, имя повторять не нужно).
Видимые состояния: sleep, listening, hearing, thinking, speaking, muted.

Звук с микрофона приходит из браузера уже очищенным эхоподавлением WebRTC. Сюда он попадает
кадрами по 32 мс, поэтому on_mic_frame обязан быть быстрым: всё медленное — в отдельных задачах.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

import httpx
import psutil

from . import screen, sysaudio, system, winapi
from .activity import ActivityTracker, human_minutes
from .autostart import set_autostart
from .config import ConfigStore, SettingsPatch
from .desktop import Desktop
from .intents import instant_answer, local_answer
from .llm import Finished, LLMRouter, LLMUnavailable, TextDelta, ToolCalls
from .media import MediaSessions
from .memory import Memory
from .persona import CREATIVE_GUIDE, SILENCE_MARK, SYSTEM_PROMPT, Address, context_message, greeting, has_address
from .laptop import Laptop
from .mascot import Mascot
from .phone import PhoneBridge
from .speech_model import SpeechModel
from .stt import SpeechUnavailable, Transcriber, pcm_to_wav
from .textproc import (
    GenderFilter, SpeechChunker, claims_done, clean_for_speech, echo_like, final_message, is_goodbye, is_no,
    is_stop_command, is_yes, looks_like_action,
)
from .tools import (
    CHAIN_TOOLS, CONFIRM_PREFIX, DATA_MARK, SLOW_TOOLS, TOOL_GROUPS, ToolBox, group_of, schemas_for, speaks_directly, wanted_groups,
)
from .tts import SpeechAudio, SpeechSynth, estimate_seconds
from .uia import UiaClient
from .vad import FRAME_BYTES, SAMPLE_RATE, Segmenter, SileroVad, VadEvent
from .wake import WakeSpotter, check_addressed
from .watcher import WatcherState, came_back, check_presence, check_status
from .window_host import WindowHost

log = logging.getLogger("jarvis.assistant")

MAX_TOOL_ROUNDS = 6
TEXT_LIMIT = 2000
LATENCY_FILLER_AFTER = 1.6
ECHO_STRIKES_LIMIT = 3
RECENT_ACTION_SECONDS = 300
FILLERS = {
    "ack": ["Да, сэр?", "Слушаю, сэр."],
    "wait": ["Секунду, сэр.", "Минуту, сэр.", "Одну секунду."],
    "search": ["Сейчас посмотрю, сэр.", "Секунду, проверю.", "Уже уточняю, сэр."],
    "bye": ["Как пожелаете, сэр. Я рядом.", "Хорошо, сэр. Если что, зовите."],
}
_LLM_DOWN = {
    "no_key": "Чтобы думать, мне нужен бесплатный ключ Groq. Откройте настройки, сэр, это займёт минуту.",
    "bad_key": "Сервис не принимает ключ. Проверьте его в настройках, сэр.",
    "rate_limit": "Бесплатный лимит на сейчас исчерпан, сэр. Простые команды я выполню, а на сложные вопросы отвечу чуть позже.",
    "busy": "Модель сейчас не смогла ответить. Попробуйте ещё раз через минуту, сэр.",
    "network": "Нет связи с сервером, сэр. Простые команды работают, остальное после восстановления сети.",
}
_ACTION_TOOLS = {"open_website", "open_app", "play_youtube", "play_video", "click_item", "click_text", "type_text", "press_keys",
                 "window_control", "media_control", "system_control", "open_path", "take_screenshot", "telegram", "radio", "open_link"}
_CAMERA_ERRORS = {"NotAllowedError": "камера запрещена: разрешите её в окне Джарвиса", "NotFoundError": "на ноутбуке не нашлось камеры",
                  "NotReadableError": "камеру сейчас занимает другая программа"}
_NO_TOOL_NUDGE = ("Ты сказал, что сделал, но инструмент не вызывался, значит, ничего не сделано. "
                  "Вызови подходящий инструмент прямо сейчас.")
_NO_TOOL_ANSWER = "Этого я пока сделать не могу: у меня нет подходящего инструмента."
_SOFT_HOLD_CHARS = 60
_AMBIENT_SECONDS = 1200
_STICKY_GROUPS_SECONDS = 180
_CREATIVE_MEMORY_SECONDS = 150
_PHONE_FORWARD_IDLE = 300.0
_REOPEN_PAUSE = 1.2  # столько ждём после закрытия окна: вдруг человек сам открывает его снова
_PAGE_WAIT = 10.0
_CAMERA_WAIT = 15.0
_NAME_CHECK_SECONDS = 12
_NAME_CHECKS_PER_HOUR = 120
_WATCH_EVERY = 60.0
_MASCOT_RETRIES = 3
_DICTATION_STOP = re.compile(r"^(?:стоп(?:\s+диктовк\w*)?|конец\s+диктовк\w*|хватит(?:\s+диктовать)?|выключи\s+диктовку|закончи\s+диктовку|останови\s+диктовку)[.!]*$", re.IGNORECASE)
_ALL_GROUPS = set(TOOL_GROUPS)
_CREATIVE = re.compile(
    r"(?:расскажи|придумай|сочини|напиши|давай|хочу|можешь)\b.*\b(?:истори\w*|сказк\w*|стих\w*|шутк\w*|анекдот\w*|байк\w*|рассказ\w*)|"
    r"\bпошути\b|\bрассмеши\b|\bчто-?(?:нибудь|то) смешное\b",
    re.IGNORECASE,
)
_JOKE_WORDS = re.compile(r"шутк|шути|анекдот|рассмеши|смешн|прикол", re.IGNORECASE)
_STORY_WORDS = re.compile(r"истори|сказк|рассказ|стих|байк", re.IGNORECASE)
# «Можешь очистить историю браузера» это просьба о действии, а не о рассказе.
_NOT_CREATIVE = re.compile(r"браузер|телеграм|\bтг\b|\bчат|очист|удал|откр[оы]|посмотр|покаж|файл|сообщени|отправ", re.IGNORECASE)
# После шутки или истории человек часто просит продолжения без повторения темы.
_CREATIVE_FOLLOW_UP = re.compile(r"\b(?:ещ[её]|смешнее|веселее|страшнее|интереснее|озорн\w*|весел\w*|смешн\w*|продолж\w*|другую|другой|длиннее|дальше)\b", re.IGNORECASE)
_MULTI_STEP = re.compile(
    r"\b(?:и|а|потом|затем|после этого)\s+(?:потом\s+|затем\s+|сразу\s+|там\s+)?"
    r"(?:открой|включи|закрой|напиши|позвони|запусти|поставь|найди|нажми|сделай|покажи|отправь|скажи|сверни|переключи|прочитай)",
    re.IGNORECASE,
)
_THINK_HARD = re.compile(
    r"почему|объясни|сравни|реши|докажи|составь|спланируй|план\b|проанализируй|в ч[её]м разница|как работает|"
    r"посоветуй|что лучше|напиши (?:код|функци|скрипт|письмо|текст|программ)|разбери|подробно",
    re.IGNORECASE,
)


@dataclass
class Turn:
    id: int
    user_text: str = ""
    seq: int = 0
    estimated: float = 0.0
    spoken: list[str] = field(default_factory=list)
    text: str = ""
    filler_used: bool = False
    playing: bool = False
    playback_done: asyncio.Event = field(default_factory=asyncio.Event)
    started: float = field(default_factory=time.perf_counter)
    speech_end: float = 0.0
    stt_seconds: float = 0.0
    stt_engine: str = ""
    first_token: float | None = None
    first_audio: float | None = None
    provider: str = ""
    model: str = ""
    remote: bool = False
    addressed: bool = False  # «сэр» уже прозвучал: в сценарии из пяти шагов он иначе звучал бы в каждом

    def known_text(self) -> str:
        return " ".join(self.spoken) + " " + self.text


@dataclass
class Pending:
    """Человек сделал паузу, Джарвис начал думать, а он продолжил: склеиваем в одну реплику."""

    audio: bytes = b""
    text: str = ""
    via_wake: bool = False


class Assistant:
    def __init__(self, config: ConfigStore, out: Any) -> None:
        self.config = config
        self.out = out
        self.http = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=6, keepalive_expiry=90), timeout=15
        )
        self.llm = LLMRouter(config, self.http)
        self.speech_model = SpeechModel(config.home, self._on_model_status)
        self.stt = Transcriber(config, self.http, self.speech_model)
        self.tts = SpeechSynth(config.home)
        self.memory = Memory(config.home / "jarvis.db")
        if config.upgraded:
            # В истории прошлой версии есть «Песня включена» без всякой песни и мужской род. Подражать этому незачем.
            self.memory.retire_history()
        self.com = UiaClient()
        self.media = MediaSessions()
        self.desktop = Desktop(self.com)
        self.activity = ActivityTracker(
            config, self.memory, self.media, self.desktop.is_jarvis, self.announce, self._send_activity,
            check_in=self._check_in,
        )
        self.tools = ToolBox(
            config, self.memory, self.http, self._on_timers_changed,
            desktop=self.desktop, media=self.media, tracker=self.activity, vision=self.llm.vision,
            on_notes_changed=self._send_notes, on_event=self._on_tool_event, on_ui=self._on_ui,
        )
        self.tools.show_browser_after_open = True
        self.tools.on_self = self._self_window
        self.tools.on_speak = self.speak_aloud
        self.tools.on_camera = self.grab_camera
        self.tools.on_dictation = self._set_dictation
        self.tools.on_phone_text = self._phone_text
        self.tools.on_phone_file = self._phone_file
        self.tools.on_phone_screen = self._phone_screen
        self.laptop = Laptop(self)
        self.phone = PhoneBridge(config, self.http, self.remote_turn, self._phone_screenshot, self.stt.transcribe_file, self._send_phone,
                                 laptop=self.laptop, vision=self.llm.vision)
        self.host: WindowHost | None = None
        self.mascot: Mascot | None = None
        self.request_exit: Callable[[], None] | None = None
        self.restart_wanted = False
        self.address = Address()
        self.meter: system.LiveMeter | None = None
        self._mascot_failures = 0
        self.dictating = False
        self._camera_wait: dict[str, asyncio.Future[bytes]] = {}
        self._watch = WatcherState()
        self._reopening = False
        self._name_checks: deque[float] = deque()
        self._mic_wanted = True
        self._told_about_closing = False
        self.ambient: dict[str, Any] = {}
        self._recent_actions: deque[tuple[float, str]] = deque(maxlen=4)
        # Голос и телефон могут заговорить одновременно, а инструменты помнят состояние текущей реплики.
        self._brain_lock = asyncio.Lock()
        self._tool_turn = 0
        self._last_groups: set[str] = set()
        self._last_groups_at = 0.0
        self._creative_at = 0.0
        self.seg = Segmenter()
        self.vad: Any = None
        self.spotter: WakeSpotter | None = None
        self.model_status: dict[str, Any] = {"status": "idle", "progress": 0.0}

        self.mode = "sleep"
        self.state = "sleep"
        self.mic_on = False
        self.connected = False
        self.visible = True
        self.ptt = False
        self.browser_pid: int | None = None

        self.work: asyncio.Task | None = None
        self.turn: Turn | None = None
        self._turn_counter = 0
        self._barge = False
        self._barge_task: asyncio.Task | None = None
        self._ignore_segment = False
        self._wake_hit = False
        self._echo_strikes = 0
        self._barge_off = False
        self._pending = Pending()
        self._stt_audio = b""
        self._stt_via_wake = False
        self._speech_end = 0.0
        self._ptt_frames: list[bytes] = []
        self._followup: asyncio.TimerHandle | None = None
        self._announcements: list[str] = []
        self._background: list[asyncio.Task] = []
        self._timers_changed = asyncio.Event()
        self._greeted = False
        self._output_checked = False
        self._closing = False

    # ---------- Жизненный цикл ----------

    async def start(self) -> None:
        self.vad = await asyncio.to_thread(SileroVad)
        self._background = [
            asyncio.create_task(self._timer_loop()),
            asyncio.create_task(self._stats_loop()),
            asyncio.create_task(self._prepare()),
            asyncio.create_task(self.activity.run()),
            asyncio.create_task(self._ambient_loop()),
            asyncio.create_task(self.phone.run()),
            asyncio.create_task(self._watch_loop()),
        ]
        self._apply_keep_awake()

    async def close(self) -> None:
        self._closing = True
        self._stop_mascot()
        if self.host is not None:
            self.host.close()
        self._cancel_followup()
        self._cancel_work()
        self._cancel_barge()
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self.activity.flush()
        self.com.close()
        await self.tts.close()
        await self.http.aclose()
        self.memory.close()

    async def _prepare(self) -> None:
        """Фоновая подготовка, чтобы окно появлялось сразу, а не через несколько секунд."""
        try:
            await self.llm.discover()
        except Exception as exc:  # noqa: BLE001
            log.info("Не удалось узнать список моделей: %s", type(exc).__name__)
        self.send_hello()
        if self.config.settings.wake_word:
            await self._load_spotter()
        await self._precache_fillers()

    async def _load_spotter(self) -> None:
        try:
            model = await self.speech_model.get()
            self.spotter = await asyncio.to_thread(WakeSpotter, model)
            self._send_state()
        except Exception as exc:  # noqa: BLE001 — без слова активации Джарвис всё равно работает
            log.warning("Слово активации недоступно: %s", type(exc).__name__)
            self._on_model_status("failed", 0.0)

    async def _precache_fillers(self) -> None:
        settings = self.config.settings
        for phrases in FILLERS.values():
            for phrase in phrases:
                if self.tts.cached(phrase, settings.voice, settings.speech_rate) is None:
                    await self.tts.synthesize(phrase, settings.voice, settings.speech_rate)

    def _on_model_status(self, status: str, progress: float) -> None:
        self.model_status = {"status": status, "progress": round(progress, 3)}
        self._send({"type": "model_status", **self.model_status})

    # ---------- Подключение интерфейса ----------

    def on_connected(self) -> None:
        self.connected = True
        self.send_hello()

    def attach_host(self, host: WindowHost) -> None:
        """Настоящий запуск: окно и талисман. В тестах сюда не приходят, поэтому на экране ничего не появляется."""
        self.host = host
        if self.config.settings.mascot:
            self._start_mascot()

    def keeps_running(self) -> bool:
        """Закрытие окна не выключает Джарвис: остаются талисман и телефон, а значит, и слух."""
        return self.host is not None and (self.config.settings.mascot or self.phone.active)

    def on_window_gone(self) -> None:
        if self._reopening or self._closing:
            return
        if not self._told_about_closing:
            # Раньше крестик выключал Джарвис совсем: новое поведение стоит объяснить один раз.
            self._told_about_closing = True
            self._mascot_say("Я остался здесь и слышу вас, сэр. Выключить совсем: правая кнопка мыши.", 8.0, "listening", force=True)
        self._reopening = True
        task = asyncio.create_task(self._reopen_hidden())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _reopen_hidden(self) -> None:
        try:
            await asyncio.sleep(_REOPEN_PAUSE)
            if self.connected or self._closing or self.host is None:
                return
            await self.host.start_hidden()
        finally:
            self._reopening = False

    def quit_later(self) -> None:
        """Сначала ответ человеку, потом выключение."""
        if self.request_exit is not None:
            asyncio.get_running_loop().call_later(0.5, self.request_exit)

    def restart_later(self) -> None:
        """Новый процесс запускает __main__, когда этот уже освободил порт и файл запуска."""
        self.restart_wanted = True
        self.quit_later()

    def ask_page_mic(self, on: bool) -> None:
        self._send({"type": "mic_set", "on": on})

    def mascot_caption(self, text: str) -> None:
        self._mascot_say(text, 6.0, "thinking", force=True)

    def mascot_note(self, text: str) -> None:
        self._mascot_say(text, 15.0, "alert", force=True)
        self._mascot_emote("alert")

    # ---------- Талисман ----------

    def _start_mascot(self) -> None:
        if self.mascot is not None:
            return
        settings = self.config.settings
        loop = asyncio.get_running_loop()
        position = (settings.mascot_x, settings.mascot_y) if settings.mascot_x is not None and settings.mascot_y is not None else None

        def on_action(action: str) -> None:
            loop.call_soon_threadsafe(self._mascot_action, action)

        def on_moved(x: int, y: int) -> None:
            loop.call_soon_threadsafe(self._mascot_moved, x, y)

        self.mascot = Mascot(
            on_action=on_action,
            on_moved=on_moved,
            size=settings.mascot_size, position=position, captions=settings.mascot_captions,
            hide_in_fullscreen=settings.mascot_hide_fullscreen,
        )
        self.mascot.start()
        if not self.mascot.alive:
            # Без этого сбой молчалив: человек видит включённый переключатель и пустой экран. Причина уже в журнале.
            log.error("Талисман не поднялся")
            self.mascot = None
            self._mascot_failures += 1
            self._send({"type": "mascot", "alive": False})
            return
        self._mascot_failures = 0
        log.info("Талисман на экране")
        self.mascot.set_mic(self.mic_on)
        self.mascot.set_state(self.state if self.connected else "muted")
        self._send({"type": "mascot", "alive": True})

    def _heal_mascot(self) -> None:
        """Поток окна мог упасть уже во время работы: поднимаем заново, но не бесконечно."""
        if self.mascot is not None and not self.mascot.alive:
            log.warning("Окно талисмана пропало, поднимаю заново")
            self.mascot = None
            self._mascot_failures += 1
        if self.mascot is None and self.host is not None and self.config.settings.mascot and self._mascot_failures < _MASCOT_RETRIES:
            self._start_mascot()

    def _stop_mascot(self) -> None:
        if self.mascot is not None:
            self.mascot.stop()
            self.mascot = None

    def _mascot_moved(self, x: int, y: int) -> None:
        self.config.update(mascot_x=x, mascot_y=y)

    def _mascot_action(self, action: str) -> None:
        if action == "open":
            task = asyncio.create_task(self._toggle_window())
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        elif action == "mic":
            self._send({"type": "mic_set", "on": not self.mic_on})
        elif action == "size" and self.mascot is not None:
            self.config.update(mascot_size=self.mascot.size_name)
            self.send_hello()
        elif action == "captions" and self.mascot is not None:
            self.config.update(mascot_captions=self.mascot.captions)
            self.send_hello()
        elif action == "hide":
            self.config.update(mascot=False)
            self._stop_mascot()
            self.send_hello()
            # Иначе от Джарвиса на экране не останется ничего: большое окно могло быть скрыто.
            shown = asyncio.create_task(self._self_window("show"))
            shown.add_done_callback(lambda t: t.cancelled() or t.exception())
        elif action == "quit":
            self.quit_later()
        elif action == "restart":
            self.restart_later()

    async def _toggle_window(self) -> None:
        if self.host is None:
            return
        window = winapi.foreground()
        ours = self.host.find()
        if ours and window is not None and window.hwnd == ours and self.host.is_visible():
            self.host.hide()
        else:
            await self._self_window("show")

    def _mascot_say(self, text: str, seconds: float = 5.0, tone: str = "speaking", force: bool = False) -> None:
        if self.mascot is not None:
            self.mascot.say(text, seconds, tone, force)

    def _mascot_emote(self, name: str) -> None:
        if self.mascot is not None:
            self.mascot.emote(name)

    async def _self_window(self, action: str) -> str:
        if action == "show":
            if self.host is not None:
                done = await self.host.show()
            else:
                done = await asyncio.to_thread(self.desktop.show_jarvis)
            self._mascot_emote("wave")
            return "Я здесь, сэр." if done else "Ошибка: окно не открылось. Откройте меня ярлыком на рабочем столе."
        if action == "hide":
            if self.host is None:
                return "Ошибка: спрятать окно сейчас нельзя."
            self.host.hide()
            return "Скрываюсь, сэр. Я рядом: просто позовите по имени."
        if action == "mascot_on":
            self.config.update(mascot=True)
            if self.host is not None:
                self._start_mascot()
            self.send_hello()
            return "Я снова на экране."
        if action == "quit":
            self.quit_later()
            return "Отключаюсь, сэр. До связи."
        if action == "mascot_off":
            self.config.update(mascot=False)
            self._stop_mascot()
            self.send_hello()
            return "Убрал талисман. Вернуть можно голосом или в настройках."
        return "Ошибка: неизвестное действие."

    async def _ensure_page(self) -> bool:
        """Голос и камера живут в странице: если её сейчас нет, открываем скрытой и ждём."""
        if self.connected:
            return True
        if self.host is None or not await self.host.start_hidden():
            return False
        deadline = time.monotonic() + _PAGE_WAIT
        while time.monotonic() < deadline:
            if self.connected:
                return True
            await asyncio.sleep(0.1)
        return False

    async def speak_aloud(self, text: str) -> str:
        if not await self._ensure_page():
            return "Ошибка: голос сейчас недоступен, окно Джарвиса не открылось."
        if self._busy() or self.seg.in_speech or self.ptt:
            self._announcements.append(text)
        else:
            self._start_work(self._run_script(text, stay_asleep=True))
        return "Сказал вслух на ноутбуке."

    async def grab_camera(self) -> bytes:
        if not await self._ensure_page():
            raise RuntimeError("окно Джарвиса не открылось")
        request = secrets.token_hex(4)
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._camera_wait[request] = future
        self._send({"type": "camera", "id": request})
        try:
            return await asyncio.wait_for(future, _CAMERA_WAIT)
        except TimeoutError as exc:
            raise RuntimeError("камера не ответила") from exc
        finally:
            self._camera_wait.pop(request, None)

    def _on_camera_frame(self, msg: dict[str, Any]) -> None:
        future = self._camera_wait.get(str(msg.get("id")))
        if future is None or future.done():
            return
        data = msg.get("data")
        if not isinstance(data, str) or not data:
            future.set_exception(RuntimeError(_CAMERA_ERRORS.get(str(msg.get("error")), "камера недоступна")))
            return
        try:
            future.set_result(base64.b64decode(data, validate=True))
        except ValueError:
            future.set_exception(RuntimeError("камера прислала непонятный снимок"))

    async def _set_dictation(self, on: bool) -> str:
        self.dictating = on
        if on:
            self.mode = "active"
            self._mascot_say("Диктовка: всё, что скажете, напечатаю.", 5, "listening", force=True)
            if self.connected:
                self._arm_followup()
            return "Включил диктовку. Всё, что скажете, напечатаю в активном окне. Скажите «стоп диктовка», когда закончите."
        return "Выключил диктовку."

    async def _dictate(self, text: str) -> None:
        """Режим диктовки: речь сразу становится текстом в окне, без модели и без ответов."""
        if _DICTATION_STOP.match(text.strip()):
            self.dictating = False
            await self._run_script("Диктовка выключена.", show=False)
            return
        window = await self.desktop.focus_target()
        if window is None or await self.desktop.uia.focused_is_password():
            self._mascot_say("Не могу напечатать: нет окна или это поле пароля.", 4, "error", force=True)
            self._enter_listening()
            return
        await asyncio.to_thread(winapi.send_text, text.strip() + " ")
        self._mascot_say(text, 3, "listening")
        self._enter_listening()

    async def _phone_text(self, text: str) -> str:
        if not self.phone.owner:
            return "Ошибка: телефон не подключён."
        await self.phone.notify(text)
        return "Отправил на телефон."

    async def _phone_file(self, path: Path) -> str:
        return await self.phone.push_file(path)

    async def _phone_screen(self) -> str:
        return await self.phone.push_screen()

    async def _watch_loop(self) -> None:
        """Батарея, диск, память, интернет и возвращение человека. Раз в минуту, а простой чаще: он дешевле."""
        passed = 0.0
        while True:
            await asyncio.sleep(5.0)
            passed += 5.0
            self._heal_mascot()
            await self._phone_alerts()
            if not self.config.settings.proactive:
                continue
            if came_back(self._watch, winapi.idle_seconds()) and self.mascot is not None:
                self.mascot.emote("wave")
                self.mascot.say("С возвращением, сэр.", 4, "happy", force=True)
            if passed < _WATCH_EVERY:
                continue
            passed = 0.0
            try:
                status = await asyncio.to_thread(system.read_status)
                heaviest = ""
                if status.ram_total and status.ram_used / status.ram_total >= 0.97:
                    groups = await asyncio.to_thread(system.top_processes, 1)
                    heaviest = groups[0].name.removesuffix(".exe") if groups else ""
                for notice in check_status(self._watch, status, time.monotonic(), heaviest):
                    self.announce(notice.text, soft=not notice.urgent)
            except Exception as exc:  # noqa: BLE001 — наблюдение не должно мешать работе
                log.info("Наблюдение за ноутбуком не сработало: %s", type(exc).__name__)

    async def _phone_alerts(self) -> None:
        """Кто-то вошёл, ноутбук проснулся, выдернули зарядку: хозяин узнаёт об этом на телефоне."""
        try:
            battery = psutil.sensors_battery()
            alerts = check_presence(self._watch, winapi.session_locked(), bool(battery.power_plugged) if battery else True, battery is not None, time.monotonic())
        except (OSError, psutil.Error) as exc:
            log.info("Проверка присутствия не сработала: %s", type(exc).__name__)
            return
        if self.config.settings.phone_alerts and self.phone.owner:
            for text in alerts:
                await self.phone.notify(text)

    def on_disconnected(self) -> None:
        self.connected = False
        self.mic_on = False
        self.ptt = False
        self.seg.reset()
        self._interrupt()
        self.mode = "sleep"
        self.state = "sleep"
        if self.mascot is not None:
            self.mascot.set_state("muted")

    def _send(self, message: dict[str, Any]) -> None:
        self.out.send_json(message)

    def send_hello(self) -> None:
        self._send({
            "type": "hello",
            "settings": self.config.public(),
            "state": self.state,
            "mode": self.mode,
            "wake_ready": self.spotter is not None,
            "needs_key": not self.llm.has_any_key(),
            "brain": self.llm.active_label(),
            "model": self.model_status,
            "timers": self._timers_payload(),
            "notes": self.memory.notes(),
            "activity": self.activity.report(),
            "ambient": self.ambient,
            "phone": self.phone.status(),
            "mic_wanted": self._mic_wanted,
            # None: окна нет вообще (тесты, отладка). False: талисман должен быть, но не поднялся, и это надо показать.
            "mascot_alive": None if self.host is None else self.mascot is not None and self.mascot.alive,
        })

    def _timers_payload(self) -> list[dict[str, Any]]:
        return self.memory.active_timers()

    def _send_notes(self) -> None:
        self._send({"type": "notes", "items": self.memory.notes()})

    def _send_activity(self) -> None:
        if self.connected:
            self._send({"type": "activity", **self.activity.report()})

    def _send_phone(self) -> None:
        self._send({"type": "phone", **self.phone.status()})
        self._apply_keep_awake()

    def _apply_keep_awake(self) -> None:
        winapi.set_keep_awake(bool(self.config.settings.keep_awake and self.phone.active))

    def _on_ui(self, kind: str) -> None:
        if kind == "mic_off":
            self._send({"type": "mic_set", "on": False})

    def _check_in(self, text: str) -> None:
        """Человек работает: без сигнала, тихо и редко."""
        self.announce(text, soft=True)

    async def _phone_screenshot(self) -> bytes | None:
        image, _ = await self.desktop.capture()
        if image is None or screen.is_blank(image):
            return None
        return await asyncio.to_thread(screen.to_jpeg, image)

    async def remote_turn(self, text: str) -> tuple[str, bool]:
        """Реплика с телефона: всё то же самое, только без голоса на ноутбуке. Возвращает (ответ, ждёт ли подтверждения)."""
        text = text.strip()[:TEXT_LIMIT]
        self._turn_counter += 1
        # self.turn не трогаем: дома в этот момент может идти голосовой разговор.
        turn = Turn(id=self._turn_counter, user_text=text, remote=True)
        self._send({"type": "user_text", "turn": turn.id, "text": text, "source": "phone"})
        self._send({"type": "assistant_start", "turn": turn.id})
        # Кто сидит за ноутбуком, должен видеть, что команда пришла с телефона.
        self._mascot_say(f"С телефона: {text}", 5, "thinking")
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        finished = False
        calm = self.turn is None and not self._busy()
        if calm:
            self._set_state("thinking")
        try:
            await self._produce(turn, text, True, queue, SpeechChunker())
            finished = True
        except Exception:
            log.exception("Сбой при обработке реплики с телефона")
            return "Что-то пошло не так на ноутбуке. Подробности в журнале Джарвиса.", False
        finally:
            self._save_turn(turn, finished)
            self._send({"type": "assistant_end", "turn": turn.id})
            if calm and self.turn is None and not self._busy():
                self._set_state(self._idle_state())
        return turn.text.strip(), self.tools.pending is not None

    def _on_tool_event(self, name: str, status: str, ok: bool) -> None:
        turn_id = self._tool_turn
        if status == "look":
            # Снимок экрана уходит в облако: человек должен это видеть прямо в момент съёмки.
            self._send({"type": "looking"})
            return
        self._send({"type": "tool", "turn": turn_id, "name": name, "status": status, "ok": ok})
        if status == "end" and ok and name in _ACTION_TOOLS:
            self._recent_actions.append((time.monotonic(), self.tools.last_result[:90]))
            self._mascot_emote("happy")
        elif status == "end" and not ok and not self.tools.pending:
            self._mascot_emote("sad")

    def _set_state(self, state: str) -> None:
        self.state = state
        if self.mascot is not None:
            self.mascot.set_state(state)
        self._send_state()

    def _send_state(self) -> None:
        self._send({
            "type": "state", "state": self.state, "mode": self.mode,
            "wake_ready": self.spotter is not None and self.config.settings.wake_word,
        })

    def _idle_state(self) -> str:
        if self.mode == "active":
            return "listening"
        return "sleep" if self.mic_on else "muted"

    # ---------- Сообщения от интерфейса ----------

    async def on_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ready":
            self.mic_on = bool(msg.get("mic"))
            self.visible = bool(msg.get("visible", True))
            self._set_state(self._idle_state())
            await self._maybe_greet()
            if not self._output_checked:
                self._output_checked = True
                self._background.append(asyncio.create_task(self._warn_if_silent()))
        elif kind == "mic":
            self._mic_wanted = bool(msg.get("wanted", msg.get("on")))
            self._set_mic(bool(msg.get("on")))
        elif kind == "text":
            text = str(msg.get("text", "")).strip()[:TEXT_LIMIT]
            if text:
                self._interrupt()
                self._pending = Pending()
                self.mode = "active"
                self._start_work(self._run_turn(text, addressed=True))
        elif kind == "ptt":
            self._on_ptt(bool(msg.get("down")))
        elif kind == "activate":
            self._on_activate()
        elif kind == "stop":
            self._interrupt()
            self._pending = Pending()
            self._enter_listening()
        elif kind == "playback":
            self._on_playback(msg)
        elif kind == "visibility":
            self.visible = bool(msg.get("visible"))
        elif kind == "level":
            if self.mascot is not None:
                self.mascot.set_level(float(msg.get("v") or 0.0))
        elif kind == "camera_frame":
            self._on_camera_frame(msg)
        elif kind == "onboarded":
            self.config.update(onboarded=True)
            self.send_hello()
            await self._maybe_greet()

    async def _warn_if_silent(self) -> None:
        notice = sysaudio.silence_notice(await self.com.run(sysaudio.read_output_state))
        if notice:
            self._send({"type": "notice", "text": notice})

    async def _maybe_greet(self) -> None:
        if self._greeted or not self.config.settings.onboarded or self._busy():
            return
        self._greeted = True
        if not self.visible:
            return  # запустился вместе с Windows: здороваться вслух в пустой комнате незачем
        settings = self.config.settings
        first_run = self.memory.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        text = greeting(datetime.now(), settings.user_name, first_run) + self._greeting_tail()
        self._start_work(self._run_script(text, stay_asleep=True))

    def _greeting_tail(self) -> str:
        """Одна фраза о дне: без запроса к модели и только если есть что сказать."""
        now = datetime.now()
        timers = [timer for timer in self.memory.active_timers() if datetime.fromtimestamp(timer["due"]).date() == now.date()]
        notes = self.memory.notes()
        parts = []
        if timers:
            first = datetime.fromtimestamp(timers[0]["due"])
            parts.append(f"Ближайшее напоминание в {first:%H:%M}: {timers[0]['label'][:40]}.")
        if notes:
            parts.append(f"Дел в списке: {len(notes)}.")
        return " " + " ".join(parts) if parts else ""

    def _set_mic(self, on: bool) -> None:
        self.mic_on = on
        if self.mascot is not None:
            self.mascot.set_mic(on)
        if not on:
            self.seg.reset()
            self.ptt = False
            self._barge = False
            self._ignore_segment = False
            if self.mode == "active" and not self._busy():
                self.mode = "sleep"
        if not self._busy():
            self._set_state(self._idle_state())

    def _on_ptt(self, down: bool) -> None:
        if down:
            self._interrupt()
            self._pending = Pending()
            self.mode = "active"
            self.ptt = True
            self._ptt_frames = []
            self.seg.reset()
            self._set_state("hearing")
            self._prewarm()
            return
        if not self.ptt:
            return
        self.ptt = False
        audio = b"".join(self._ptt_frames)
        self._ptt_frames = []
        if len(audio) < SAMPLE_RATE * 2 * 0.25:
            self._enter_listening()
            return
        self._speech_end = time.perf_counter()
        self._start_work(self._handle_utterance(audio, via_wake=False, addressed=True))

    def _on_activate(self) -> None:
        if self._busy() or self.seg.in_speech:
            self._interrupt()
            self._pending = Pending()
            self._enter_listening()
        elif self.mode == "active":
            self._go_sleep()
        else:
            self._send({"type": "chime", "kind": "wake"})
            self._prewarm()
            self._enter_listening()

    def _on_playback(self, msg: dict[str, Any]) -> None:
        turn = self.turn
        if turn is None or msg.get("turn") != turn.id:
            return
        event = msg.get("event")
        if event == "start" and not turn.playing:
            turn.playing = True
            self._set_state("speaking")
        elif event == "done":
            turn.playback_done.set()

    # ---------- Поток с микрофона ----------

    def on_mic_frame(self, frame: bytes) -> None:
        if not self.mic_on or len(frame) != FRAME_BYTES or self.vad is None:
            return
        if self.ptt:
            self._ptt_frames.append(frame)
            return
        prob = self.vad.process_chunk(frame)
        event = self.seg.push(frame, prob, strict=self._audio_active())
        if self.mode == "sleep":
            self._feed_sleep(frame, event)
        else:
            self._feed_active(event)

    def _audio_active(self) -> bool:
        return self.turn is not None and self.turn.seq > 0

    def _feed_sleep(self, frame: bytes, event: VadEvent | None) -> None:
        if self.spotter is None or not self.config.settings.wake_word:
            if event in (VadEvent.END, VadEvent.CANCEL):
                self.seg.take_audio()
            return
        if event is VadEvent.START:
            self.spotter.begin(self.seg.current_audio())
            self._wake_hit = False
        elif self.seg.in_speech:
            if self.spotter.feed(frame) and not self._wake_hit:
                self._wake_hit = True
                self._send({"type": "chime", "kind": "wake"})
                self._set_state("hearing")
                self._prewarm()
        elif event is VadEvent.END:
            audio = self.seg.take_audio()
            heard = self._wake_hit or self.spotter.finish()
            self._wake_hit = False
            if heard:
                # Режим меняем сразу: следующая фраза не должна потеряться, пока идёт проверка имени.
                self.mode = "active"
                self._speech_end = time.perf_counter()
                self._start_work(self._handle_utterance(audio, via_wake=True, addressed=True))
            elif getattr(self.spotter, "maybe", False) and self._may_check_name(audio):
                # Похоже на имя: тихо проверяем точным распознаванием. Человек заметит только, что его услышали.
                self._speech_end = time.perf_counter()
                self._start_work(self._handle_utterance(audio, via_wake=True, addressed=True, quiet=True))
        elif event is VadEvent.CANCEL:
            self.seg.take_audio()
            if self._wake_hit:
                self._wake_hit = False
                self._set_state(self._idle_state())

    def _may_check_name(self, audio: bytes) -> bool:
        """Похожее на имя уходит в облако только коротким и не слишком часто: телевизор не должен весь день ехать в Groq."""
        if not self.config.settings.wake_cloud_check or self._busy():
            return False
        if len(audio) > _NAME_CHECK_SECONDS * SAMPLE_RATE * 2:
            return False
        now = time.monotonic()
        while self._name_checks and now - self._name_checks[0] > 3600:
            self._name_checks.popleft()
        if len(self._name_checks) >= _NAME_CHECKS_PER_HOUR:
            return False
        self._name_checks.append(now)
        return True

    def _feed_active(self, event: VadEvent | None) -> None:
        if event is VadEvent.START:
            self._on_speech_start()
        elif event is VadEvent.END:
            audio = self.seg.take_audio()
            self._on_speech_end(audio)
        elif event is VadEvent.CANCEL:
            self.seg.take_audio()
            self._ignore_segment = False
            if self._barge:
                self._barge = False
                self._send({"type": "duck", "on": False})
            if not self._busy():
                self._set_state(self._idle_state())
                self._arm_followup()

    def _on_speech_start(self) -> None:
        self._cancel_followup()
        self._prewarm()
        if self._audio_active():
            if self.config.settings.barge_in and not self._barge_off:
                self._barge = True
                self._send({"type": "duck", "on": True})
            else:
                self._ignore_segment = True
            return
        if self._busy():
            self._merge_into_pending()
            self._cancel_work()
        self._set_state("hearing")

    def _merge_into_pending(self) -> None:
        if self.turn is not None:
            self._pending.text = self.turn.user_text
            self._send({"type": "user_retract", "turn": self.turn.id})
        elif self._stt_audio:
            self._pending.audio = self._stt_audio
            self._pending.via_wake = self._stt_via_wake

    def _on_speech_end(self, audio: bytes) -> None:
        self._speech_end = time.perf_counter()
        if self._ignore_segment:
            self._ignore_segment = False
            return
        if self._barge:
            self._barge = False
            self._cancel_barge()
            self._barge_task = asyncio.create_task(self._check_barge_in(audio))
            return
        self._start_work(self._handle_utterance(audio, via_wake=False, addressed=False))

    # ---------- Управление задачами ----------

    def _busy(self) -> bool:
        return self.work is not None and not self.work.done()

    def _start_work(self, coro: Coroutine[Any, Any, None]) -> None:
        self._cancel_work()
        self._cancel_followup()
        self.work = asyncio.create_task(self._guard(coro))

    async def _guard(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Сбой в обработке реплики")
            self._send({"type": "error", "text": "Внутренняя ошибка. Подробности в журнале."})
            self.turn = None
            self._enter_listening()

    def _cancel_work(self) -> None:
        if self.work is not None and not self.work.done():
            self.work.cancel()

    def _cancel_barge(self) -> None:
        if self._barge_task is not None and not self._barge_task.done():
            self._barge_task.cancel()

    def _interrupt(self) -> None:
        self._cancel_followup()
        self._cancel_barge()
        self._barge = False
        self._ignore_segment = False
        had_audio = self._audio_active()
        self._cancel_work()
        if had_audio:
            self._send({"type": "audio_stop"})

    def _prewarm(self) -> None:
        self.tts.warm()
        task = asyncio.create_task(self.llm.prewarm())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _enter_listening(self) -> None:
        if self._closing:
            return
        self.mode = "active"
        if self._announcements and not self._busy_other():
            self._start_work(self._run_script(self._announcements.pop(0)))
            return
        if not self.mic_on:
            # Ответили на текст при выключенном микрофоне: слушать всё равно нечем.
            self._go_sleep()
            return
        self._set_state("hearing" if (self.seg.in_speech or self.ptt) else "listening")
        self._arm_followup()

    def _busy_other(self) -> bool:
        return self._busy() and self.work is not asyncio.current_task()

    def _go_sleep(self) -> None:
        self._cancel_followup()
        self.mode = "sleep"
        self._set_state(self._idle_state())

    def _arm_followup(self) -> None:
        self._cancel_followup()
        delay = self.config.settings.followup_seconds
        self._followup = asyncio.get_running_loop().call_later(delay, self._followup_expired)

    def _cancel_followup(self) -> None:
        if self._followup is not None:
            self._followup.cancel()
            self._followup = None

    def _followup_expired(self) -> None:
        self._followup = None
        if self.seg.in_speech or self.ptt or self._busy() or self.dictating:
            self._arm_followup()
        else:
            self._go_sleep()

    # ---------- Обработка реплики ----------

    async def _handle_utterance(self, audio: bytes, via_wake: bool, addressed: bool, quiet: bool = False) -> None:
        pending = self._pending
        self._pending = Pending()
        if pending.audio:
            audio = pending.audio + b"\0" * int(SAMPLE_RATE * 2 * 0.2) + audio
            via_wake = via_wake or pending.via_wake
        speech_end = self._speech_end
        if not quiet:
            self._set_state("thinking")
        self._stt_audio = audio
        self._stt_via_wake = via_wake
        try:
            result = await self.stt.transcribe(audio, language="ru") if via_wake else await self.stt.transcribe(audio)
        except SpeechUnavailable:
            if quiet:
                return
            self._send({
                "type": "notice",
                "text": "Распознавание речи пока недоступно: нет ключа Groq и не загрузился модуль слуха. Можно писать текстом.",
            })
            self._enter_listening()
            return
        finally:
            self._stt_audio = b""
        text = result.text.strip()

        if self.dictating and text:
            await self._dictate(text)
            return

        if via_wake:
            confirmed, cleaned = check_addressed(text, lenient=True)
            if not confirmed:
                # Vosk ошибся: имени не было. Молча возвращаемся ко сну.
                log.info("Ложное срабатывание слова активации отсеяно%s", ", проверка была тихой" if quiet else "")
                if quiet:
                    return
                self._go_sleep()
                return
            if quiet:
                log.info("Имя подтверждено тихой проверкой")
                self.mode = "active"
                self._send({"type": "chime", "kind": "wake"})
                self._set_state("thinking")
            addressed = True
            text = cleaned
            if not text and not pending.text:
                await self._run_script(random.choice(FILLERS["ack"]), show=False)
                return
        elif text:
            confirmed, cleaned = check_addressed(text)
            if confirmed:
                # В разговоре без имени могли звучать чужие голоса: просьба начинается после обращения.
                addressed = True
                text = cleaned or text

        if pending.text:
            text = f"{pending.text} {text}".strip()
            addressed = True
        if not text:
            self._enter_listening()
            return
        if is_stop_command(text):
            self._enter_listening()
            return
        if is_goodbye(text):
            self._send({"type": "user_text", "turn": 0, "text": text})
            await self._run_script(random.choice(FILLERS["bye"]), stay_asleep=True)
            return
        await self._run_turn(text, addressed, result.seconds, result.engine, speech_end)

    async def _check_barge_in(self, audio: bytes) -> None:
        """Человек заговорил поверх Джарвис. Голос уже приглушён, теперь решаем: это он или эхо?"""
        try:
            result = await self.stt.transcribe(audio)
        except SpeechUnavailable:
            self._send({"type": "duck", "on": False})
            return
        text = result.text.strip()
        turn = self.turn
        if turn is None or not self._audio_active():
            # Пока распознавали, Джарвис договорил: это обычная реплика.
            if text and not self._busy():
                self._start_work(self._run_turn(text, addressed=False, stt_seconds=result.seconds, stt_engine=result.engine))
            return
        if not text or echo_like(text, turn.known_text()):
            self._send({"type": "duck", "on": False})
            self._echo_strikes += 1
            log.info("Перебивание оказалось эхом (%d подряд)", self._echo_strikes)
            if self._echo_strikes >= ECHO_STRIKES_LIMIT and not self._barge_off:
                self._barge_off = True
                self._send({
                    "type": "notice",
                    "text": "Похоже, микрофон слышит динамики, поэтому перебивание голосом выключено до перезапуска. "
                            "Помогут наушники, а остановить меня можно клавишей Esc.",
                })
            return
        self._echo_strikes = 0
        self._cancel_followup()
        self._cancel_work()
        self._send({"type": "audio_stop"})
        if is_stop_command(text):
            self.mode = "active"
            # Дождёмся завершения отменённой задачи, чтобы она не перезаписала состояние.
            await asyncio.sleep(0)
            self._enter_listening()
            return
        self._start_work(self._run_turn(text, addressed=True, stt_seconds=result.seconds, stt_engine=result.engine))

    def _new_turn(self, user_text: str = "") -> Turn:
        self._turn_counter += 1
        turn = Turn(id=self._turn_counter, user_text=user_text)
        self.turn = turn
        return turn

    async def _run_script(self, text: str, show: bool = True, stay_asleep: bool = False,
                          voice: str | None = None, rate: int | None = None) -> None:
        """Реплика без языковой модели: приветствие, напоминание, короткий ответ."""
        was_asleep = self.mode == "sleep"
        turn = self._new_turn()
        self.mode = "active"
        try:
            if show:
                self._send({"type": "assistant_start", "turn": turn.id})
                self._send({"type": "assistant_delta", "turn": turn.id, "text": text})
            await self._speak(turn, text, voice, rate)
            await self._finish_audio(turn)
        finally:
            if self.turn is turn:
                self.turn = None
            if show:
                self._send({"type": "assistant_end", "turn": turn.id})
        if stay_asleep or (was_asleep and voice is not None):
            self._go_sleep()
        else:
            self._enter_listening()

    async def _speak(self, turn: Turn, text: str, voice: str | None = None, rate: int | None = None) -> None:
        settings = self.config.settings
        audio = await self.tts.synthesize(
            text, voice or settings.voice, settings.speech_rate if rate is None else rate
        )
        if audio is not None:
            self._send_audio(turn, audio, text)

    def _send_audio(self, turn: Turn, audio: SpeechAudio, text: str) -> None:
        if turn.first_audio is None:
            turn.first_audio = time.perf_counter()
        header = {"turn": turn.id, "seq": turn.seq, "text": text, "mime": audio.mime, "engine": audio.engine}
        self.out.send_audio(header, audio.data)
        self._mascot_say(text, estimate_seconds(audio) + 1.2)
        turn.seq += 1
        turn.spoken.append(text)
        turn.estimated += estimate_seconds(audio)

    async def _finish_audio(self, turn: Turn) -> None:
        if turn.seq == 0:
            return
        self._send({"type": "audio_done", "turn": turn.id, "count": turn.seq})
        try:
            await asyncio.wait_for(turn.playback_done.wait(), timeout=turn.estimated + 8)
        except TimeoutError:
            log.info("Интерфейс не подтвердил конец воспроизведения")

    def _maybe_filler(self, turn: Turn, kind: str) -> None:
        if turn.remote or turn.seq > 0 or turn.filler_used or self.turn is not turn:
            return
        settings = self.config.settings
        text = random.choice(FILLERS[kind])
        audio = self.tts.cached(text, settings.voice, settings.speech_rate)
        if audio is not None:
            turn.filler_used = True
            self._send_audio(turn, audio, text)

    def _build_messages(self, text: str, addressed: bool, creative: bool = False, remote: bool = False) -> list[dict[str, Any]]:
        settings = self.config.settings
        facts = [fact["text"] for fact in self.memory.facts()]
        now = time.monotonic()
        recent = [line for stamp, line in self._recent_actions if now - stamp < RECENT_ACTION_SECONDS]
        context = context_message(
            datetime.now(), settings.user_name, settings.city, facts, addressed,
            active_window=self.desktop.describe_active() if settings.allow_screen else "",
            recent_actions=recent, activity=self._activity_line(), agenda=self._agenda_line(), creative=creative,
            open_apps=self.desktop.open_apps() if settings.allow_screen else "", remote=remote,
        )
        # Постоянная подсказка первой, а изменчивый контекст в конце: так работает кэш начала запроса.
        guide = [{"role": "system", "content": CREATIVE_GUIDE}] if creative else []
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.memory.recent_messages(),
            *guide,
            {"role": "system", "content": context},
            {"role": "user", "content": text},
        ]

    def _agenda_line(self) -> str:
        """Настоящий случай: на «что у меня в три часа» Джарвис ответил «ничего», хотя запись была в делах."""
        parts = []
        notes = self.memory.notes()
        if notes:
            parts.append("Дела: " + "; ".join(note["text"][:60] for note in notes[:4]) + ".")
        timers = self.memory.active_timers()
        if timers:
            parts.append("Напоминания: " + "; ".join(
                f"{datetime.fromtimestamp(timer['due']):%d.%m %H:%M} {timer['label'][:40]}" for timer in timers[:3]
            ) + ".")
        return " ".join(parts)

    def _is_creative(self, text: str) -> bool:
        now = time.monotonic()
        if _NOT_CREATIVE.search(text):
            return False
        if _CREATIVE.search(text):
            self._creative_at = now
            return True
        if now - self._creative_at < _CREATIVE_MEMORY_SECONDS and len(text) < 60 and _CREATIVE_FOLLOW_UP.search(text):
            self._creative_at = now
            return True
        return False

    def _sticky_groups(self) -> set[str]:
        # «Открой ютуб», потом «а теперь сверни»: следующая реплика обычно про то же самое.
        return self._last_groups if time.monotonic() - self._last_groups_at < _STICKY_GROUPS_SECONDS else set()

    def _activity_line(self) -> str:
        kind = self.activity.current_kind
        if kind not in ("game", "video"):
            return ""
        spent = self.activity.sessions[kind].seconds
        what = "играет" if kind == "game" else "смотрит видео"
        return f"Сейчас человек {what}: {self.activity.current_app}, подряд {human_minutes(spent)}."

    async def _run_turn(
        self,
        text: str,
        addressed: bool,
        stt_seconds: float = 0.0,
        stt_engine: str = "",
        speech_end: float = 0.0,
    ) -> None:
        turn = self._new_turn(text)
        turn.stt_seconds = stt_seconds
        turn.stt_engine = stt_engine
        turn.speech_end = speech_end
        self.mode = "active"
        self._set_state("thinking")
        self._send({"type": "user_text", "turn": turn.id, "text": text})
        self._send({"type": "assistant_start", "turn": turn.id})
        self.tts.warm()

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        speaker = asyncio.create_task(self._speech_worker(turn, queue))
        loop = asyncio.get_running_loop()
        filler_timer = loop.call_later(LATENCY_FILLER_AFTER, self._maybe_filler, turn, "wait")
        chunker = SpeechChunker()
        silent = False
        finished_ok = False
        try:
            silent = await self._produce(turn, text, addressed, queue, chunker)
            filler_timer.cancel()
            if silent:
                speaker.cancel()
                self._send({"type": "user_retract", "turn": turn.id})
                self._send({"type": "assistant_end", "turn": turn.id, "discard": True})
                self._enter_after_silence(turn)
                return
            for chunk in chunker.flush():
                queue.put_nowait(chunk)
            queue.put_nowait(None)
            await speaker
            await self._finish_audio(turn)
            finished_ok = True
        finally:
            filler_timer.cancel()
            if not speaker.done():
                speaker.cancel()
            if not silent:
                self._save_turn(turn, finished_ok)
                self._send({
                    "type": "assistant_end", "turn": turn.id, "interrupted": not finished_ok,
                    # Отменили до первого звука: Джарвис ничего не сказал, значит, и показывать нечего.
                    "discard": not finished_ok and turn.seq == 0,
                })
            if self.turn is turn:
                self.turn = None
        self._send_metrics(turn)
        self._enter_listening()

    def _enter_after_silence(self, turn: Turn) -> None:
        if self.turn is turn:
            self.turn = None
        self._enter_listening()

    async def _speech_worker(self, turn: Turn, queue: asyncio.Queue[str | None]) -> None:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            await self._speak(turn, chunk)

    def _say(self, turn: Turn, text: str, queue: asyncio.Queue[str | None], chunker: SpeechChunker) -> None:
        if not text:
            return
        if turn.first_token is None:
            turn.first_token = time.perf_counter()
        turn.text += text
        self._send({"type": "assistant_delta", "turn": turn.id, "text": text})
        if turn.remote:
            return  # ответ уйдёт на телефон текстом: говорить в пустой комнате незачем
        for chunk in chunker.feed(text):
            queue.put_nowait(chunk)

    async def _produce(
        self,
        turn: Turn,
        text: str,
        addressed: bool,
        queue: asyncio.Queue[str | None],
        chunker: SpeechChunker,
    ) -> bool:
        """Сначала подтверждение, потом мгновенные команды, и только затем модель."""
        async with self._brain_lock:
            self.tools.remote = turn.remote
            silent = await self._answer_once(turn, text, addressed, queue, chunker)
            for step in self.tools.take_routine():
                # Сценарий: каждый шаг как отдельная просьба. Шаг с вопросом останавливает остальные: человек ещё не ответил.
                if self.tools.pending is not None:
                    self._say(turn, " Остальные шаги подождут вашего ответа.", queue, chunker)
                    break
                self._say(turn, " ", queue, chunker)
                await self._answer_once(turn, step, True, queue, chunker)
            return silent

    async def _answer_once(
        self, turn: Turn, text: str, addressed: bool, queue: asyncio.Queue[str | None], chunker: SpeechChunker,
    ) -> bool:
        self.tools.begin_turn()
        self._tool_turn = turn.id
        joke_before = self.tools.last_joke_at
        answer = await self._confirmation_answer(text)
        if answer is None:
            answer = await instant_answer(text, self.tools)
        if self.tools.last_joke_at != joke_before:
            self._mascot_emote("laugh")
        if answer is not None:
            # Шутка уже закончена как есть: обращение после концовки её только портит.
            told_joke = self.tools.last_joke_at != joke_before
            self._say(turn, answer if told_joke else self._with_address(turn, answer), queue, chunker)
            return False
        return await self._converse(turn, text, addressed, queue, chunker)

    async def _confirmation_answer(self, text: str) -> str | None:
        """Подтверждение решает код, а не модель: страница в интернете не сможет сказать «да» вместо человека."""
        pending = self.tools.take_pending()
        if pending is None:
            return None
        if is_yes(text):
            try:
                return await asyncio.wait_for(pending.run(), pending.seconds)
            except Exception as exc:  # noqa: BLE001
                log.warning("Подтверждённое действие не сработало: %s", type(exc).__name__)
                return "Не получилось это сделать."
        if is_no(text):
            return "Как скажете, сэр. Не буду."
        return None

    async def _converse(
        self,
        turn: Turn,
        text: str,
        addressed: bool,
        queue: asyncio.Queue[str | None],
        chunker: SpeechChunker,
    ) -> bool:
        """Диалог с моделью, включая вызовы инструментов. Возвращает True, если Джарвис решил промолчать."""
        creative = self._is_creative(text)
        messages = self._build_messages(text, addressed, creative, turn.remote)
        gate = "" if not addressed else None
        # В истории рассказчик не Джарвис, поэтому глагол без «я» там может быть про героя.
        gender = GenderFilter(own_voice=not creative)
        # Замер: qwen с рассуждениями шутит заметно лучше основной модели, но у неё лимит 1000 токенов ответа в минуту,
        # и на длинной истории всё уходит в рассуждения. Поэтому шутки туда, а истории в основную модель.
        joke = creative and bool(_JOKE_WORDS.search(text)) and not _STORY_WORDS.search(text)
        action = looks_like_action(text)
        hard = len(text) > 140 or bool(_THINK_HARD.search(text))
        # Просьбу о действии модель выполняет быстро, а над ответом и историей стоит подумать.
        # Высокая настройка не годится: основная модель тратит на обдумывание весь запас и возвращает пустой ответ.
        effort = "low" if action and not hard and not creative else "medium"
        max_tokens = 900 if joke else 1400 if hard or creative else 700
        temperature = 0.95 if creative else 0.6
        multi_step = bool(_MULTI_STEP.search(text))
        groups = wanted_groups(text) | self._sticky_groups()
        keep_tools = not creative
        # Просьбу о действии держим до конца первого ответа модели: так ложное «включила» не успеет прозвучать.
        hold = action
        force_tool = False
        nudged = False
        round_number = 0
        while round_number < MAX_TOOL_ROUNDS:
            tools = None
            if force_tool:
                tools = schemas_for(_ALL_GROUPS)
            elif keep_tools and round_number < MAX_TOOL_ROUNDS - 1:
                tools = schemas_for(groups | self.tools.requested)
            calls: list[dict[str, Any]] = []
            round_text = ""
            held = ""
            released = False
            try:
                async for event in self.llm.stream(
                    messages, tools, max_tokens, tool_choice="required" if force_tool else None, effort=effort,
                    temperature=temperature, prefer="qwen" if joke else "",
                ):
                    if isinstance(event, TextDelta):
                        delta = event.text
                        if gate is not None:
                            gate += delta
                            head = gate.lstrip()
                            if head.startswith(SILENCE_MARK):
                                return True
                            if SILENCE_MARK.startswith(head):
                                continue
                            delta, gate = gate, None
                        if not released:
                            # Модель любит сказать «Закрыла.» прямо перед вызовом инструмента, а потом повторить.
                            # Такие реплики короткие, поэтому немного придерживаем начало: задержка незаметна.
                            held += delta
                            if hold or len(held) < _SOFT_HOLD_CHARS:
                                continue
                            released = True
                            delta, held = held, ""
                        delta = gender.feed(delta)
                        round_text += delta
                        self._say(turn, delta, queue, chunker)
                    elif isinstance(event, ToolCalls):
                        calls = event.calls
                    elif isinstance(event, Finished):
                        turn.provider, turn.model = event.provider, event.model
                        self._send({"type": "brain", "label": self._brain_label(event)})
            except LLMUnavailable as exc:
                if force_tool:
                    # Обязательный вызов не удался: честно говорим, что не можем, а не повторяем выдумку.
                    self._say(turn, _NO_TOOL_ANSWER, queue, chunker)
                    return False
                await self._on_llm_down(turn, text, exc, queue, chunker)
                return False

            force_tool = False
            if gate:
                held, gate = held + gate, None
            if hold and not calls and claims_done(held) and self.tools.actions_done == 0 and not nudged:
                nudged = True
                log.info("Модель заявила о действии без инструмента: прошу вызвать его")
                messages.append({"role": "assistant", "content": held})
                messages.append({"role": "system", "content": _NO_TOOL_NUDGE})
                force_tool = True
                hold = False
                round_number += 1
                continue
            if held and not calls:
                delta = gender.feed(final_message(held) if hold else held)
                round_text += delta
                self._say(turn, delta, queue, chunker)
            tail = gender.flush()
            round_text += tail
            self._say(turn, tail, queue, chunker)
            if not calls:
                return False
            # Ответ после инструмента короткий: придержать его целиком почти ничего не стоит,
            # зато слипшиеся варианты не успеют прозвучать.
            hold = True
            messages.append({"role": "assistant", "content": round_text, "tool_calls": calls})
            results: list[tuple[str, str, str]] = []
            for call in calls:
                name = call["function"]["name"]
                raw_args = call["function"].get("arguments", "{}")
                if name in SLOW_TOOLS:
                    self._maybe_filler(turn, "search")
                result = await self.tools.execute(name, raw_args)
                results.append((name, raw_args, result))
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": result})
            self._last_groups = {group_of(name) for name, _, _ in results} - {"core"} | self.tools.requested
            self._last_groups_at = time.monotonic()
            direct = [speaks_directly(name, raw, result) for name, raw, result in results]
            # Простое действие готово: говорим его результат сами, без второго запроса к модели.
            # «И» в описании сценария это его шаги, а не наша работа: настоящая проверка показала лишний снимок экрана на этом месте.
            if (not multi_step or any(name == "routines" for name, _, _ in results)) and all(direct):
                self._say(turn, self._with_address(turn, self._spoken_results(results)), queue, chunker)
                return False
            gender.after_comma = any(direct)
            keep_tools = multi_step or any(name in CHAIN_TOOLS or result.startswith("Ошибка") for name, _, result in results)
            round_number += 1
        return False

    def _spoken_results(self, results: list[tuple[str, str, str]]) -> str:
        phrases = []
        for _, _, result in results:
            if result.startswith(CONFIRM_PREFIX) and self.tools.pending is not None:
                phrases.append(self.tools.pending.question)
            else:
                phrases.append(result.replace(DATA_MARK, ""))
        return " ".join(phrases)

    def _with_address(self, turn: Turn, text: str) -> str:
        if turn.addressed:
            return text
        said = self.address.apply(text)
        turn.addressed = said != text or has_address(text)
        return said

    @staticmethod
    def _brain_label(event: Finished) -> str:
        return f"{event.provider} · {event.model.split('/')[-1]}"

    async def _on_llm_down(
        self, turn: Turn, text: str, exc: LLMUnavailable,
        queue: asyncio.Queue[str | None], chunker: SpeechChunker,
    ) -> None:
        log.info("Языковая модель недоступна: %s", exc.kind)
        tools_ran = self.tools.actions_done > 0
        # Инструменты уже сработали: повторная локальная команда поставила бы второй таймер.
        answer = None if tools_ran else await local_answer(text, self.tools)
        if answer is None:
            if tools_ran:
                # Модель не успела пересказать результат, но он есть: говорим его как есть.
                answer = clean_for_speech(self.tools.last_result)[:240] or "Готово."
            else:
                answer = _LLM_DOWN[exc.kind]
                self._send({"type": "brain_down", "kind": exc.kind})
        self._say(turn, answer, queue, chunker)

    def _save_turn(self, turn: Turn, finished: bool) -> None:
        if not turn.user_text or not turn.text.strip():
            return
        if not finished and turn.seq == 0:
            # Реплику склеят со следующей: в истории она появится уже целиком.
            return
        self.memory.add_message("user", turn.user_text)
        said = turn.text.strip()
        if not finished:
            said = (" ".join(turn.spoken) or said) + " [перебили]"
        self.memory.add_message("assistant", said)

    def _send_metrics(self, turn: Turn) -> None:
        first_audio = turn.first_audio
        total = first_audio - turn.speech_end if first_audio and turn.speech_end else None
        metrics = {
            "stt_ms": round(turn.stt_seconds * 1000) if turn.stt_seconds else None,
            "stt": turn.stt_engine,
            "llm_ms": round((turn.first_token - turn.started) * 1000) if turn.first_token else None,
            "first_audio_ms": round((first_audio - turn.started) * 1000) if first_audio else None,
            "total_ms": round(total * 1000) if total else None,
            "provider": turn.provider,
            "model": turn.model,
        }
        log.info("Метрики реплики: %s", metrics)
        self._send({"type": "metrics", **metrics})

    # ---------- Таймеры и объявления ----------

    def _on_timers_changed(self) -> None:
        self.notify_timers_changed()

    def notify_timers_changed(self) -> None:
        self._timers_changed.set()
        self._send({"type": "timers", "items": self._timers_payload()})

    async def _timer_loop(self) -> None:
        while True:
            now = time.time()
            timers = self.memory.active_timers()
            for timer in [t for t in timers if t["due"] <= now]:
                if timer.get("repeat") == "daily":
                    next_due = timer["due"]
                    while next_due <= now:
                        next_due += 86400
                    self.memory.reschedule_timer(timer["id"], next_due)
                else:
                    self.memory.finish_timer(timer["id"])
                self.announce(f"Напоминаю, сэр: {timer['label']}.")
                self._send({"type": "timers", "items": self._timers_payload()})
            remaining = [t["due"] - now for t in timers if t["due"] > now]
            wait = min(remaining) if remaining else 30.0
            self._timers_changed.clear()
            try:
                await asyncio.wait_for(self._timers_changed.wait(), max(0.3, min(wait, 30.0)))
            except TimeoutError:
                pass

    def announce(self, text: str, soft: bool = False) -> None:
        if not soft:
            self._send({"type": "chime", "kind": "timer"})
            self._mascot_emote("alert")
        self._mascot_say(text, 9.0, "alert" if not soft else "speaking", force=True)
        # Человек может ответить на напоминание: Джарвис должен помнить, что сам его сказал.
        self.memory.add_message("assistant", text)
        # Человека нет за ноутбуком: напоминание пригодится на телефоне. Вопрос о самочувствии туда не шлём.
        if not soft and self.phone.owner and (not self.connected or winapi.idle_seconds() > _PHONE_FORWARD_IDLE):
            forward = asyncio.create_task(self.phone.notify(text))
            forward.add_done_callback(lambda t: t.cancelled() or t.exception())
        if not self.connected:
            return  # окно закрыто: звук играть негде, и синтез речи только потратил бы время
        if self._busy() or self.seg.in_speech or self.ptt:
            self._announcements.append(text)
        else:
            self._start_work(self._run_script(text))

    def _apply_mascot_settings(self, patch: SettingsPatch) -> None:
        settings = self.config.settings
        if patch.mascot is True and self.host is not None:
            self._start_mascot()
        elif patch.mascot is False:
            self._stop_mascot()
        if self.mascot is None:
            return
        if patch.mascot_size is not None:
            self.mascot.set_size(settings.mascot_size)
        if patch.mascot_captions is not None:
            self.mascot.captions = settings.mascot_captions
        if patch.mascot_hide_fullscreen is not None:
            self.mascot.hide_in_fullscreen = settings.mascot_hide_fullscreen

    def preview_voice(self, voice: str, rate: int) -> None:
        self._interrupt()
        self._start_work(self._run_script(
            "Добрый день, сэр. Так будет звучать мой голос.", show=False, voice=voice, rate=rate,
        ))

    def on_settings_changed(self, patch: SettingsPatch) -> None:
        if patch.wake_word is False:
            # Модель занимает 166 МБ. Раз слово активации не нужно, отдаём память системе.
            self.spotter = None
            self.speech_model.unload()
        if patch.wake_word and self.spotter is None:
            task = asyncio.create_task(self._load_spotter())
            self._background.append(task)
        if patch.voice or patch.speech_rate is not None:
            task = asyncio.create_task(self._precache_fillers())
            self._background.append(task)
        if patch.barge_in:
            self._barge_off = False
            self._echo_strikes = 0
        if patch.autostart is not None:
            shortcut = asyncio.create_task(asyncio.to_thread(set_autostart, patch.autostart))
            shortcut.add_done_callback(lambda t: t.cancelled() or t.exception())
        if patch.keep_awake is not None:
            self._apply_keep_awake()
        self._apply_mascot_settings(patch)
        self.send_hello()

    # ---------- Самопроверка и статистика ----------

    async def self_check(self) -> list[dict[str, Any]]:
        """Реальные запросы к каждому сервису: честный ответ на вопрос «всё ли работает»."""
        results = []

        async def probe(name: str, coro: Coroutine[Any, Any, str]) -> None:
            started = time.perf_counter()
            try:
                detail = await asyncio.wait_for(coro, 30)
                results.append({"name": name, "ok": True, "detail": detail,
                                "ms": round((time.perf_counter() - started) * 1000)})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "ok": False, "detail": _error_text(exc),
                                "ms": round((time.perf_counter() - started) * 1000)})

        await probe("Голос", self._check_voice())
        if self.config.key("groq"):
            await probe("Мозг: Groq", self._check_brain("groq"))
            await probe("Распознавание: Whisper", self._check_whisper())
        else:
            results.append({"name": "Мозг: Groq", "ok": False, "detail": "Ключ не задан", "ms": 0})
        if self.config.key("gemini"):
            await probe("Запасной мозг: Gemini", self._check_brain("gemini"))
        await probe("Слух: слово «Джарвис»", self._check_wake())
        await probe("Погода и воздух", self._check_weather())
        await probe("Поиск в интернете", self._check_search())
        await probe("Курс Нацбанка", self.tools.currency_rate("USD"))
        await probe("Управление окнами", self._check_desktop())
        await probe("Медиасессии", self._check_media())
        if self.config.key("groq") and self.config.settings.allow_screen:
            await probe("Зрение: Groq", self._check_vision())
        return results

    async def _check_desktop(self) -> str:
        window = self.desktop.target()
        if window is None:
            return "окон нет, проверять нечего"
        items = await self.com.run(self.com.snapshot_sync, window.hwnd, False)
        return f"окно «{window.title[:40]}»: элементов {len(items)}"

    async def _check_media(self) -> str:
        sessions = await self.media.current()
        return f"источников звука: {len(sessions)}"

    async def _check_vision(self) -> str:
        from PIL import Image

        from . import screen

        # Сплошной цвет вместо настоящего снимка: проверка не должна отправлять экран в облако.
        jpeg = await asyncio.to_thread(screen.to_jpeg, Image.new("RGB", (64, 64), (200, 30, 30)))
        return (await self.llm.vision(jpeg, "Какого цвета картинка? Одно слово."))[:40]

    async def _check_voice(self) -> str:
        settings = self.config.settings
        # Уникальный текст обходит кэш: иначе проверялся бы диск, а не сервис.
        audio = await self.tts.synthesize(f"Проверка голоса номер {int(time.time()) % 100000}, связь есть.",
                                          settings.voice, settings.speech_rate)
        if audio is None:
            raise RuntimeError("синтез не вернул звук")
        return f"движок {audio.engine}" if audio.engine != "sapi" else "облако недоступно, работает запасной голос Windows"

    async def _check_brain(self, provider: str) -> str:
        result = await self.llm.check(provider)
        if not result.ok:
            raise RuntimeError(result.message)
        answer = ""
        async for event in self.llm.stream([{"role": "user", "content": "Ответь одним словом: столица Казахстана?"}]):
            if isinstance(event, TextDelta):
                answer += event.text
            elif isinstance(event, Finished):
                return f"{event.model}, ответ: {answer.strip()[:40]}"
        return answer.strip()[:40]

    async def _check_whisper(self) -> str:
        silence = pcm_to_wav(b"\0" * SAMPLE_RATE)
        resp = await self.http.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.config.key('groq')}"},
            files={"file": ("check.wav", silence, "audio/wav")},
            data={"model": "whisper-large-v3-turbo", "language": "ru"},
            timeout=15,
        )
        resp.raise_for_status()
        return "сервис отвечает"

    async def _check_wake(self) -> str:
        if not self.config.settings.wake_word:
            return "выключено в настройках"
        if self.spotter is None:
            await self._load_spotter()
        if self.spotter is None:
            raise RuntimeError("модель не загружена")
        return "модель загружена"

    async def _check_weather(self) -> str:
        return (await self.tools.get_weather(self.config.settings.city or "Алматы"))[:80]

    async def _check_search(self) -> str:
        result = await self.tools.web_search("столица Казахстана")
        if not result.startswith("Результаты"):
            raise RuntimeError("поиск не дал результатов")
        return "результаты получены"

    async def _ambient_loop(self) -> None:
        """Погода и воздух в углу окна: живая приборная панель. Раз в 20 минут, этого хватает с запасом."""
        while True:
            if self.connected:
                try:
                    self.ambient = await self.tools.ambient()
                    self._send({"type": "ambient", **self.ambient})
                except Exception as exc:  # noqa: BLE001 — без погоды в углу окна можно жить
                    log.info("Погода для панели недоступна: %s", type(exc).__name__)
                await asyncio.sleep(_AMBIENT_SECONDS)
            else:
                await asyncio.sleep(2)

    async def _stats_loop(self) -> None:
        own = psutil.Process()
        cores = psutil.cpu_count() or 1
        browser: list[psutil.Process] = []
        refreshed = 0.0
        self.meter = await asyncio.to_thread(system.LiveMeter)
        while True:
            await asyncio.sleep(3)
            if not (self.connected and self.visible):
                continue
            try:
                if time.monotonic() - refreshed > 30:
                    refreshed = time.monotonic()
                    browser = await asyncio.to_thread(self._browser_tree)
                # У сервера считаем рабочий набор: модель резервирует сотни мегабайт адресов, почти их не занимая.
                # У браузера — личную память: рабочие наборы его процессов считают общие страницы по нескольку раз.
                cpu = own.cpu_percent()
                ram = own.memory_info().rss
                for proc in list(browser):
                    try:
                        cpu += proc.cpu_percent()
                        ram += proc.memory_info().private
                    except psutil.Error:
                        browser.remove(proc)
                numbers = await asyncio.to_thread(self.meter.read)
                self._send({"type": "stats", "cpu": round(cpu / cores, 1), "ram_mb": round(ram / 2**20), "system": numbers})
            except (psutil.Error, OSError):
                continue

    def _browser_tree(self) -> list[psutil.Process]:
        if not self.browser_pid:
            return []
        try:
            root = psutil.Process(self.browser_pid)
            return [root, *root.children(recursive=True)]
        except psutil.Error:
            return []


def _error_text(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"сервис ответил кодом {exc.response.status_code}"
    if isinstance(exc, TimeoutError):
        return "нет ответа"
    if isinstance(exc, LLMUnavailable):
        return _LLM_DOWN.get(exc.kind, exc.kind)
    return str(exc) or type(exc).__name__

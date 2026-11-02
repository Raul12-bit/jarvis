"""Контроль времени: сколько человек играет, смотрит и работает, и когда пора остановиться.

Раз в несколько секунд смотрим на активное окно. Данные остаются на этом компьютере в SQLite.
Игру Джарвис просит закрыться так же, как крестик в углу окна, и заранее предупреждает голосом:
убивать процесс нельзя, можно потерять несохранённое.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from . import winapi
from .config import ConfigStore
from .media import MediaSessions
from .memory import Memory
from .winapi import WindowInfo

log = logging.getLogger("jarvis.activity")

TICK_SECONDS = 5.0
FLUSH_SECONDS = 60.0
IDLE_LIMIT = 300.0
VIDEO_IDLE_LIMIT = 2400.0  # Кино смотрят, не трогая мышь: простой короче фильма считать нельзя.
SESSION_GAP = 600.0
WORK_GAP = 300.0
WARN_BEFORE = 300.0
GAME_GRACE = 60.0
CLOSE_CHECK_AFTER = 20.0
VIDEO_NAG_EVERY = 90.0
VIDEO_NAG_LIMIT = 3
WORK_NAG_EVERY = 3600.0
FOCUS_NAG_EVERY = 120.0
FOCUS_GRACE = 15.0
KINDS = ("game", "video", "work", "other")

_GAME_PATHS = re.compile(
    r"\\(?:steamapps\\common|epic games|riot games|battle\.net|gog games|gog galaxy\\games|xboxgames|"
    r"ubisoft game launcher\\games|ubisoft\\.*?\\games|ea games|electronic arts|rockstar games|wargaming\.net|"
    r"lesta|roblox\\versions|miniconda\\.*minecraft|\.minecraft)\\",
    re.I,
)
_LAUNCHERS = {"steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "upc.exe", "ubisoftconnect.exe",
              "riotclientux.exe", "riotclientservices.exe", "battle.net.exe", "galaxyclient.exe", "eadesktop.exe",
              "origin.exe", "rockstarlauncher.exe", "lgc.exe", "wgc.exe"}
_GAME_EXES = {
    "cs2.exe", "csgo.exe", "dota2.exe", "valorant-win64-shipping.exe", "league of legends.exe", "robloxplayerbeta.exe",
    "minecraft.windows.exe", "gta5.exe", "gta5_enhanced.exe", "fortniteclient-win64-shipping.exe", "rocketleague.exe",
    "genshinimpact.exe", "starrail.exe", "zenlesszonezero.exe", "overwatch.exe", "tslgame.exe", "r5apex.exe",
    "rainbowsix.exe", "rainbowsix_be.exe", "eldenring.exe", "witcher3.exe", "cyberpunk2077.exe", "rdr2.exe",
    "worldoftanks.exe", "wowclassic.exe", "wow.exe", "hearthstone.exe", "pathofexile_x64.exe", "terraria.exe",
    "stardew valley.exe", "standoff2.exe", "cod.exe", "fc25.exe", "fc26.exe", "fifa23.exe",
}
_PLAYER_EXES = {"vlc.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be64.exe", "potplayer.exe", "potplayermini.exe",
                "potplayermini64.exe", "wmplayer.exe", "microsoft.media.player.exe", "video.ui.exe", "kmplayer.exe",
                "kmplayer64.exe", "mpv.exe", "smplayer.exe", "stremio.exe"}
_WORK_EXES = {
    "code.exe", "cursor.exe", "devenv.exe", "pycharm64.exe", "idea64.exe", "clion64.exe", "webstorm64.exe",
    "rider64.exe", "goland64.exe", "antigravity.exe", "windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
    "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe", "notepad++.exe", "obsidian.exe", "notion.exe",
    "figma.exe", "arduino ide.exe", "matlab.exe", "postman.exe", "dbeaver.exe", "claude.exe", "devin.exe",
    "sublime_text.exe", "zed.exe", "kicad.exe", "fusion360.exe",
}
_VIDEO_TITLES = re.compile(
    r"(?:[-–—]\s*youtube\b|\bnetflix\b|кинопоиск|kinopoisk|hdrezka|\brezka\b|смотреть\s+(?:онлайн|фильм|сериал)|"
    r"\bсерия\b|\bсезон\b|онлайн\s+кинотеатр|\bivi\b|\bokko\b|\bwink\b|amediateka|\btwitch\b|anilibria|animego|jut\.su|"
    r"disney\+|prime video|\bhulu\b|\bmax\b\s*[-–—]|\btiktok\b|rutube|vk видео)",
    re.I,
)
_VIDEO_SITE_NAMES = (
    ("youtube", "YouTube"), ("netflix", "Netflix"), ("кинопоиск", "Кинопоиск"), ("kinopoisk", "Кинопоиск"),
    ("rezka", "HDRezka"), ("twitch", "Twitch"), ("tiktok", "TikTok"), ("rutube", "Rutube"), ("ivi", "ivi"),
)
_WORK_TITLES = re.compile(r"\b(?:github|gitlab|stack overflow|stackoverflow|google docs|документы google|notion|chatgpt|claude|jira|figma|overleaf|colab)\b", re.I)
_APP_NAMES = {"code.exe": "VS Code", "cursor.exe": "Cursor", "devenv.exe": "Visual Studio", "winword.exe": "Word",
              "excel.exe": "Excel", "powerpnt.exe": "PowerPoint", "windowsterminal.exe": "Терминал",
              "pycharm64.exe": "PyCharm", "arduino ide.exe": "Arduino IDE", "claude.exe": "Claude", "chrome.exe": "Chrome",
              "msedge.exe": "Edge", "firefox.exe": "Firefox"}
# Рабочие программы и браузеры Джарвис сам не закрывает никогда, что бы ни показывал контроль времени.
PROTECTED_EXES = _WORK_EXES | winapi.BROWSER_EXES
_CHECK_INS = (
    "Вы давно работаете, сэр. Как самочувствие? Может, что-нибудь нужно?",
    "Как вы там, сэр? Если что-то нужно, скажите. Вода и пара минут без экрана тоже считаются.",
    "Не отвлекаю, сэр, просто проверяю: как самочувствие? Нужно что-нибудь?",
)


def is_protected(window: WindowInfo) -> bool:
    return window.exe in PROTECTED_EXES


def app_name(window: WindowInfo) -> str:
    return _APP_NAMES.get(window.exe, window.exe.removesuffix(".exe"))


def closable_game(window: WindowInfo, custom: dict[str, str] | None = None) -> bool:
    """Закрыть можно только то, что точно игра. Полноэкранная программа считается игрой для подсчёта времени,
    но закрывать её по догадке нельзя: это может оказаться редактор, презентация или звонок."""
    if is_protected(window):
        return False
    if window.exe in _GAME_EXES or _GAME_PATHS.search(window.path):
        return True
    if window.exe == "javaw.exe" and "minecraft" in window.title.lower():
        return True
    return any(kind == "game" and (needle in window.exe or needle in window.title.lower())
               for needle, kind in (custom or {}).items())


def classify(window: WindowInfo | None, custom: dict[str, str] | None = None, fullscreen: bool = False) -> str:
    """game, video, work или other. Свои правила человека важнее встроенных."""
    if window is None:
        return "other"
    exe = window.exe
    title = window.title
    for needle, kind in (custom or {}).items():
        if needle in exe or needle in title.lower():
            return kind if kind in KINDS else "other"
    if exe in _PLAYER_EXES:
        return "video"
    if window.is_browser:
        if _VIDEO_TITLES.search(title):
            return "video"
        return "work" if _WORK_TITLES.search(title) else "other"
    if exe in _LAUNCHERS:
        return "other"
    if exe in _GAME_EXES or _GAME_PATHS.search(window.path):
        return "game"
    if exe == "javaw.exe" and "minecraft" in title.lower():
        return "game"
    if exe in _WORK_EXES:
        return "work"
    # Полноэкранная программа не из офисных и не браузер почти всегда игра. Презентации и плееры отсеяны выше.
    if fullscreen and not window.path.lower().startswith(r"c:\windows"):
        return "game"
    return "other"


def app_label(window: WindowInfo, kind: str) -> str:
    if window.is_browser:
        lowered = window.title.lower()
        for needle, name in _VIDEO_SITE_NAMES:
            if needle in lowered:
                return name
        return "Браузер"
    if window.exe in _APP_NAMES:
        return _APP_NAMES[window.exe]
    if kind == "game":
        return window.title.strip()[:40] or window.exe.removesuffix(".exe")
    return window.exe.removesuffix(".exe")[:40]


def human_minutes(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours, rest = divmod(minutes, 60)
    if hours == 1 and rest == 30:
        return "полтора часа"
    text = f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"
    return f"{text} {rest} {_plural(rest, 'минуту', 'минуты', 'минут')}" if rest else text


def _plural(number: int, one: str, few: str, many: str) -> str:
    tail = number % 100
    if 11 <= tail <= 14:
        return many
    last = tail % 10
    return one if last == 1 else few if 2 <= last <= 4 else many


def rest_advice(moment: datetime) -> str:
    if moment.hour >= 23 or moment.hour < 5:
        return "Уже поздно, сэр, пора спать."
    if moment.hour < 20:
        return "Пора поработать или немного отдохнуть."
    return "Пора отдохнуть или заняться делами."


@dataclass
class Session:
    kind: str
    seconds: float = 0.0
    last_seen: float = 0.0
    extra: float = 0.0
    warned: bool = False
    fired: bool = False
    nags: int = 0
    next_nag: float = 0.0

    def reset(self) -> None:
        self.seconds = self.extra = self.next_nag = 0.0
        self.warned = self.fired = False
        self.nags = 0


@dataclass
class ClosePlan:
    hwnd: int
    name: str
    at: float
    checked: bool = False
    check_at: float = 0.0


class ActivityTracker:
    def __init__(
        self,
        config: ConfigStore,
        memory: Memory,
        media: MediaSessions,
        is_jarvis: Callable[[WindowInfo], bool],
        announce: Callable[[str], None],
        on_update: Callable[[], None],
        check_in: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.media = media
        self.is_jarvis = is_jarvis
        self.announce = announce
        self.on_update = on_update
        self.check_in = check_in or announce
        self.sessions = {kind: Session(kind) for kind in ("game", "video", "work")}
        self.close_plan: ClosePlan | None = None
        self.focus_until = 0.0
        self.focus_next_nag = 0.0
        self.focus_started = 0.0
        self.current_kind = "other"
        self.current_app = ""
        self._pending: dict[tuple[str, str], float] = {}
        self._last_tick = time.monotonic()
        self._last_flush = time.monotonic()
        self._last_update = 0.0
        self._last_window: WindowInfo | None = None

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — сбой слежения не должен трогать разговор
                log.exception("Сбой контроля времени")

    def flush(self) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        for (kind, app), seconds in self._pending.items():
            self.memory.add_activity(day, kind, app, seconds)
        self._pending.clear()
        self._last_flush = time.monotonic()

    def _sample(self) -> WindowInfo | None:
        window = winapi.foreground()
        if window is None or self.is_jarvis(window) or window.exe in ("lockapp.exe", "logonui.exe"):
            # Человек говорит с Джарвисом или разблокирует экран: окно под ним остаётся текущим занятием.
            return self._last_window if window is not None and self.is_jarvis(window) else None
        return window

    async def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        dt = min(now - self._last_tick, TICK_SECONDS * 3)
        self._last_tick = now
        settings = self.config.settings
        if not settings.track_time:
            return
        window = self._sample()
        self._last_window = window if window is not None and winapi.window_exists(window.hwnd) else None
        window = self._last_window
        locked = winapi.session_locked()
        fullscreen = window is not None and not window.is_browser and winapi.fills_monitor(window)
        kind = classify(window, settings.custom_kinds, fullscreen)
        idle = winapi.idle_seconds()
        active = window is not None and not locked and idle < (VIDEO_IDLE_LIMIT if kind == "video" else IDLE_LIMIT)
        if active and kind == "video" and await self.media.is_video_playing() is False:
            # Видео на паузе: страница открыта, но человек его не смотрит.
            active = False
        self.current_kind = kind if active else "other"
        self.current_app = app_label(window, kind) if active and window else ""
        if active and window is not None:
            key = (kind, self.current_app)
            self._pending[key] = self._pending.get(key, 0.0) + dt
            if kind in self.sessions:
                session = self.sessions[kind]
                gap = WORK_GAP if kind == "work" else SESSION_GAP
                if session.last_seen and now - session.last_seen > gap:
                    session.reset()
                session.seconds += dt
                session.last_seen = now
        for session in self.sessions.values():
            if session.last_seen and now - session.last_seen > (WORK_GAP if session.kind == "work" else SESSION_GAP):
                session.reset()
                session.last_seen = 0.0
        await self._apply_rules(window, active, kind, now)
        if now - self._last_flush >= FLUSH_SECONDS:
            self.flush()
        if now - self._last_update >= 20.0:
            self._last_update = now
            self.on_update()

    # ---------- Правила ----------

    def _limit(self, kind: str) -> float:
        settings = self.config.settings
        minutes = {"game": settings.limit_game_minutes, "video": settings.limit_video_minutes,
                   "work": settings.limit_work_minutes}[kind]
        return minutes * 60.0

    async def _apply_rules(self, window: WindowInfo | None, active: bool, kind: str, now: float) -> None:
        await self._run_close_plan(now)
        if self.focus_until:
            await self._focus_rules(window, active, kind, now)
        if not active or window is None or kind not in self.sessions:
            return
        session = self.sessions[kind]
        limit = self._limit(kind)
        if limit <= 0:
            return
        moment = datetime.now()
        target = limit + session.extra
        if kind == "work":
            # Работу Джарвис не прерывает и ничего не требует: только изредка спрашивает, как дела.
            if session.seconds >= target and now >= session.next_nag:
                session.next_nag = now + WORK_NAG_EVERY
                self.check_in(random.choice(_CHECK_INS))
            return
        if not session.warned and target - WARN_BEFORE <= session.seconds < target:
            session.warned = True
            if kind == "video":
                self.announce(f"Вы смотрите уже {human_minutes(session.seconds)}. Через пять минут поставлю на паузу.")
            elif self._will_close_game() and closable_game(window, self.config.settings.custom_kinds):
                self.announce(f"Вы играете уже {human_minutes(session.seconds)}. Сохраните игру, сэр: через пять минут я её закрою.")
            else:
                self.announce(f"Вы играете уже {human_minutes(session.seconds)}. Через пять минут лимит.")
        if session.seconds >= target:
            if kind == "game":
                await self._game_limit(window, session, now, moment)
            else:
                await self._video_limit(session, now, moment)

    def _will_close_game(self) -> bool:
        return self.config.settings.game_action == "close"

    async def _game_limit(self, window: WindowInfo, session: Session, now: float, moment: datetime) -> None:
        if session.fired:
            return
        session.fired = True
        spent = human_minutes(session.seconds)
        if not self._will_close_game() or not closable_game(window, self.config.settings.custom_kinds):
            self.announce(f"Вы играете уже {spent}, это ваш лимит. {rest_advice(moment)}")
            return
        name = app_label(window, "game")
        self.close_plan = ClosePlan(window.hwnd, name, now + GAME_GRACE)
        self.announce(f"Вы играете уже {spent}, лимит вышел. Через минуту закрою игру, сохранитесь. "
                      "Если очень нужно, скажите: ещё пятнадцать минут.")

    async def _run_close_plan(self, now: float) -> None:
        plan = self.close_plan
        if plan is None:
            return
        if not winapi.window_exists(plan.hwnd):
            self.close_plan = None
            if plan.checked:
                self.announce(f"Игра закрыта. {rest_advice(datetime.now())}")
            return
        if not plan.check_at and now >= plan.at:
            # Перед закрытием проверяем заново: за минуту окно могло смениться.
            current = winapi.window_info(plan.hwnd)
            if current is not None and not closable_game(current, self.config.settings.custom_kinds):
                self.close_plan = None
                return
            winapi.close_window(plan.hwnd)
            plan.check_at = now + CLOSE_CHECK_AFTER
            plan.checked = True
            return
        if plan.check_at and now >= plan.check_at:
            self.close_plan = None
            # Закрыть окно могло запретить само приложение (диалог сохранения) или защита прав Windows.
            self.announce(f"{plan.name} не закрывается сама, возможно, ждёт подтверждения. Закройте, пожалуйста, вручную.")

    async def _video_limit(self, session: Session, now: float, moment: datetime) -> None:
        if not session.fired:
            session.fired = True
            session.next_nag = now + VIDEO_NAG_EVERY
            paused = await self.media.pause_all(only_video=True)
            state = "Поставил на паузу." if paused else "Поставить на паузу не получилось, остановите видео, пожалуйста."
            self.announce(f"Вы смотрите уже {human_minutes(session.seconds)}. {state} {rest_advice(moment)} "
                          "Если хотите досмотреть, скажите: ещё двадцать минут.")
            return
        # Человек снова включил видео, не договорившись о продлении.
        if now >= session.next_nag and session.nags < VIDEO_NAG_LIMIT:
            session.nags += 1
            session.next_nag = now + VIDEO_NAG_EVERY
            await self.media.pause_all(only_video=True)
            if session.nags == VIDEO_NAG_LIMIT:
                self.announce("Как пожелаете, сэр, это ваше решение. Больше останавливать не буду, просто напомню о времени.")
            else:
                self.announce(f"Мы договаривались, сэр. Снова поставил на паузу. {rest_advice(moment)}")

    def extend(self, minutes: int) -> str:
        """Продление: человек сказал «ещё пятнадцать минут»."""
        minutes = max(1, min(int(minutes), 240))
        self.close_plan = None
        touched = False
        for kind in ("game", "video"):
            session = self.sessions[kind]
            if session.seconds >= self._limit(kind) - WARN_BEFORE and self._limit(kind) > 0:
                session.extra = max(session.seconds - self._limit(kind), 0.0) + minutes * 60.0
                session.fired = False
                session.warned = minutes * 60.0 <= WARN_BEFORE
                session.nags = 0
                touched = True
        if not touched:
            return "Сейчас лимит не сработал, продлевать нечего."
        return f"Хорошо, ещё {minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}."

    # ---------- Режим фокуса ----------

    def start_focus(self, minutes: int) -> str:
        minutes = max(1, min(int(minutes), 240))
        now = time.monotonic()
        self.focus_started = now
        self.focus_until = now + minutes * 60.0
        self.focus_next_nag = now
        self.on_update()
        return f"Режим фокуса на {minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}: игры и видео буду останавливать."

    def stop_focus(self) -> str:
        if not self.focus_until:
            return "Режим фокуса и так выключен."
        self.focus_until = 0.0
        self.on_update()
        return "Режим фокуса выключен."

    def focus_left(self) -> float:
        return max(self.focus_until - time.monotonic(), 0.0) if self.focus_until else 0.0

    async def _focus_rules(self, window: WindowInfo | None, active: bool, kind: str, now: float) -> None:
        if now >= self.focus_until:
            spent = now - self.focus_started
            self.focus_until = 0.0
            self.on_update()
            self.announce(f"Фокус окончен: {human_minutes(spent)} позади. Отдохните пять минут, сэр, это время честно заработано.")
            return
        if not active or kind not in ("game", "video") or now - self.focus_started < FOCUS_GRACE:
            return
        if now < self.focus_next_nag:
            return
        self.focus_next_nag = now + FOCUS_NAG_EVERY
        left = human_minutes(self.focus_until - now)
        if kind == "video":
            await self.media.pause_all(only_video=True)
            self.announce(f"Мы договорились работать ещё {left}. Поставил видео на паузу.")
        else:
            self.announce(f"Мы договорились работать ещё {left}. Вернёмся к делу, сэр, игра подождёт.")

    # ---------- Отчёты ----------

    def report(self, day: str | None = None) -> dict[str, Any]:
        self.flush()
        day = day or datetime.now().strftime("%Y-%m-%d")
        rows = self.memory.activity_for(day)
        totals = dict.fromkeys(KINDS, 0.0)
        apps: list[dict[str, Any]] = []
        for row in rows:
            totals[row["kind"]] = totals.get(row["kind"], 0.0) + row["seconds"]
            if row["kind"] != "other":
                apps.append({"kind": row["kind"], "app": row["app"], "seconds": round(row["seconds"])})
        apps.sort(key=lambda item: -item["seconds"])
        settings = self.config.settings
        return {
            "day": day,
            "totals": {kind: round(value) for kind, value in totals.items()},
            "apps": apps[:8],
            "current": {"kind": self.current_kind, "app": self.current_app},
            "sessions": {kind: round(session.seconds) for kind, session in self.sessions.items()},
            "limits": {"game": settings.limit_game_minutes, "video": settings.limit_video_minutes,
                       "work": settings.limit_work_minutes},
            "focus_left": round(self.focus_left()),
            "track": settings.track_time,
        }

    def speakable_report(self, day: str | None = None) -> str:
        data = self.report(day)
        totals = data["totals"]
        parts = []
        if totals["game"] >= 60:
            parts.append(f"игры {human_minutes(totals['game'])}")
        if totals["video"] >= 60:
            parts.append(f"видео {human_minutes(totals['video'])}")
        if totals["work"] >= 60:
            parts.append(f"работа {human_minutes(totals['work'])}")
        if not parts:
            return "Пока почти ничего не набежало: ни игр, ни видео, ни работы дольше минуты."
        top = [app for app in data["apps"] if app["seconds"] >= 60][:3]
        tail = "Больше всего: " + ", ".join(f"{app['app']} {human_minutes(app['seconds'])}" for app in top) + "." if top else ""
        return "Сегодня: " + ", ".join(parts) + ". " + tail

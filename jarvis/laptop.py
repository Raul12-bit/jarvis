"""Пульт ноутбука для кнопок в телеграме: нажатие сразу становится действием, без языковой модели и без токенов."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import __version__, screen, sysaudio, system, winapi
from .activity import app_name, is_protected
from .tools import _DANGEROUS_FILES, known_folder, normalize_url

if TYPE_CHECKING:
    from .assistant import Assistant

log = logging.getLogger("jarvis.laptop")

FOLDERS = (("Рабочий стол", "desktop"), ("Загрузки", "downloads"), ("Документы", "documents"),
           ("Изображения", "pictures"), ("Видео", "videos"), ("Музыка", "music"))
_HAS_SCHEME = re.compile(r"^https?://", re.I)
_LOOKS_LIKE_SITE = re.compile(r"^[\w-]+(\.[\w-]+)+(/\S*)?$")
SHELL_SECONDS = 900  # установки идут долго, а бот всё это время отвечает, и команду можно остановить кнопкой
_LIVE_SIDE = 1280  # живой экран обновляется часто: снимок поменьше не съест мобильный интернет
_LOG_LINES = 25
_GRID_MIN_WIDTH = 1000
_BROWSER_SAFE_COMBOS = {"alt+left", "alt+right", "f5", "ctrl+t", "ctrl+w", "ctrl+shift+t", "ctrl+tab", "ctrl+shift+tab", "ctrl+=", "ctrl+-", "ctrl+0",
                        "f11", "pageup", "pagedown", "home", "end"}


@dataclass
class WindowRow:
    hwnd: int
    title: str
    app: str
    minimized: bool
    protected: bool


class Laptop:
    def __init__(self, assistant: Assistant) -> None:
        self.a = assistant
        self.session = system.ShellSession()
        self.started_at = time.monotonic()

    # ---------- Экран ----------

    async def screenshot(self, kind: str = "screen", hwnd: int = 0) -> bytes | None:
        """JPEG экрана или окна. None: снимать нельзя или вышел чёрный кадр."""
        if not self.a.config.settings.allow_screen:
            return None

        def shoot() -> Any:
            if kind in ("screen", "live", "cursor"):
                return screen.capture_rect(winapi.virtual_screen_rect())
            window = winapi.window_info(hwnd) if hwnd else self.a.desktop.target()
            if window is None:
                return screen.capture_rect(winapi.virtual_screen_rect())
            if window.minimized:
                winapi.restore(window.hwnd)
                window = winapi.window_info(window.hwnd)
            return screen.capture_window(window) if window else None

        image = await asyncio.to_thread(shoot)
        if image is None or screen.is_blank(image):
            return None
        if kind == "cursor":
            image = await asyncio.to_thread(_mark_cursor, image, winapi.virtual_screen_rect(), winapi.cursor_position())
        return await asyncio.to_thread(screen.to_jpeg, image, _LIVE_SIDE if kind == "live" else 1920, 72 if kind == "live" else 82)

    async def grid_shot(self, rect: tuple[int, int, int, int], columns: int, rows: int) -> bytes | None:
        """Снимок части экрана с сеткой: так с телефона можно точно навести мышь, а модели зрения координаты не доверяем."""
        if not self.a.config.settings.allow_screen:
            return None

        def shoot() -> bytes | None:
            image = screen.capture_rect(rect)
            if image is None or screen.is_blank(image):
                return None
            return screen.to_jpeg(_draw_grid(image, rect, columns, rows, winapi.cursor_position()), 1920, 82)

        return await asyncio.to_thread(shoot)

    @staticmethod
    def screen_rect() -> tuple[int, int, int, int]:
        return winapi.virtual_screen_rect()

    async def read_screen(self) -> str:
        return await self.a.tools.read_screen_text(whole_screen=True)

    async def click_text(self, text: str) -> str:
        return await self.a.tools.click_text(text)

    async def look(self, question: str) -> str:
        return await self.a.tools.look_at_screen(question)

    # ---------- Мышь ----------

    @staticmethod
    def locked() -> bool:
        """На заблокированном экране ввод идёт в защищённый рабочий стол: Windows его не пропустит."""
        return winapi.session_locked()

    @staticmethod
    def mouse_move(dx: int, dy: int) -> tuple[int, int]:
        return winapi.mouse_move_by(dx, dy)

    @staticmethod
    def mouse_to(x: int, y: int) -> tuple[int, int]:
        return winapi.mouse_move_to(x, y)

    async def mouse_click(self, button: str = "left") -> None:
        if button == "double":
            await asyncio.to_thread(winapi.mouse_press, "left")
            await asyncio.sleep(0.05)
            await asyncio.to_thread(winapi.mouse_press, "left")
        else:
            await asyncio.to_thread(winapi.mouse_press, button)

    async def mouse_scroll(self, notches: int) -> None:
        await asyncio.to_thread(winapi.mouse_scroll, notches)

    # ---------- Окна ----------

    def windows(self) -> list[WindowRow]:
        return [
            WindowRow(window.hwnd, window.title, app_name(window), window.minimized, is_protected(window))
            for window in self.a.desktop.others()[:12]
        ]

    def window_row(self, hwnd: int) -> WindowRow | None:
        window = winapi.window_info(hwnd)
        if window is None or not winapi.window_exists(hwnd):
            return None
        return WindowRow(window.hwnd, window.title, app_name(window), window.minimized, is_protected(window))

    async def window_action(self, hwnd: int, action: str) -> str:
        window = winapi.window_info(hwnd)
        if window is None:
            return "Этого окна уже нет."
        title = window.title[:40]
        if action == "focus":
            done = await asyncio.to_thread(winapi.activate, hwnd)
            return f"На переднем плане: {title}." if done else "Windows не дала переключиться."
        if action == "minimize":
            winapi.minimize(hwnd)
            return f"Свернул: {title}."
        if action == "maximize":
            winapi.maximize(hwnd)
            await asyncio.to_thread(winapi.activate, hwnd)
            return f"Развернул: {title}."
        if action in ("snap_left", "snap_right"):
            winapi.snap_window(hwnd, action.removeprefix("snap_"))
            return f"Половина экрана: {title}."
        if action == "close":
            return await self.a.tools._close_window(window)  # noqa: SLF001
        return "Неизвестное действие."

    # ---------- Система ----------

    async def status_text(self) -> str:
        status = await asyncio.to_thread(system.read_status)
        lines = system.status_lines(status)
        window = self.a.desktop.target()
        if window is not None:
            lines.append(f"Сейчас: {window.title[:60]}")
        idle = winapi.idle_seconds()
        lines.append(f"За ноутбуком никого {round(idle / 60)} мин" if idle >= 120 else "Сейчас кто-то за ноутбуком")
        if winapi.session_locked():
            lines.append("Экран заблокирован")
        return "\n".join(lines)

    async def processes(self, sort: str = "memory") -> list[system.ProcessGroup]:
        return await asyncio.to_thread(system.top_processes, 8, sort)

    async def kill(self, name: str) -> str:
        count, why = await asyncio.to_thread(system.kill_process, name)
        return f"Завершил {name}." if count else f"Не получилось: {why}."

    async def power(self, action: str) -> str:
        return await self.a.tools.power_now(action)

    async def shutdown_in(self, minutes: int) -> str:
        return await self.a.tools.shutdown_in(minutes)

    async def brightness(self, level: int) -> str:
        return await self.a.tools.system_control("brightness", str(level))

    async def theme(self, dark: bool) -> str:
        return await self.a.tools.system_control("dark_theme" if dark else "light_theme")

    # ---------- Сеть ----------

    async def net_text(self) -> str:
        wifi = await asyncio.to_thread(system.wifi_name)
        ip = await asyncio.to_thread(system.local_ip)
        ping = await asyncio.to_thread(system.ping_ms)
        lines = [f"Wi-Fi: {wifi}" if wifi else "Wi-Fi не подключён"]
        lines.append(f"Адрес в сети: {ip}" if ip else "Адреса в сети нет")
        lines.append(f"Отклик интернета: {ping:.0f} мс" if ping is not None else "Интернета нет")
        try:
            radios = await system.list_radios()
            lines += [f"{title}: {'включён' if radios[key] else 'выключен'}" for key, title in (("wifi", "Модуль Wi-Fi"), ("bluetooth", "Bluetooth")) if key in radios]
        except (OSError, ImportError) as exc:
            log.info("Радио недоступно: %s", type(exc).__name__)
        return "\n".join(lines)

    async def public_ip(self) -> str:
        # Только по нажатию: сервис узнаёт адрес ноутбука, и больше ничего.
        try:
            resp = await self.a.http.get("https://api.ipify.org", timeout=httpx.Timeout(6))
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.info("Внешний адрес недоступен: %s", type(exc).__name__)
            return "Внешний адрес узнать не удалось."
        return f"Внешний адрес: {resp.text.strip()[:45]}"

    async def radio(self, kind: str, on: bool) -> str:
        return await self.a.tools.radio(kind, on)

    # ---------- Звук ----------

    async def media_text(self) -> str:
        playing = await self.a.tools.media_control("now_playing")
        state = await self.a.com.run(sysaudio.read_output_state)
        volume = f"\nГромкость {state[0]}%" + (", звук выключен" if state[1] else "") if state else ""
        return playing + volume

    async def media_action(self, action: str) -> str:
        if action == "toggle":
            sessions = await self.a.media.current()
            action = "pause" if any(item.playing for item in sessions) else "play"
        return await self.a.tools.media_control(action)

    async def volume(self, level: int) -> str:
        return await self.a.tools.media_control("set_volume", level)

    # ---------- Файлы ----------

    @staticmethod
    def folder(key: str) -> Path | None:
        path = known_folder(key)
        return path if path is not None and path.is_dir() else None

    @property
    def any_path(self) -> bool:
        return self.a.config.settings.phone_any_path

    @staticmethod
    def drives() -> list[Path]:
        return system.drives()

    def list_dir(self, path: Path, everything: bool = False) -> list[Path]:
        if not everything and system.blocked_path(path):
            return []
        entries = system.list_folder(path, 300, everything)
        return sorted(entries, key=lambda item: not item.is_dir())

    @staticmethod
    def is_dangerous(path: Path) -> bool:
        return path.suffix.lower() in _DANGEROUS_FILES

    async def open_file(self, path: Path) -> str:
        if system.blocked_path(path) and not self.any_path:
            return "Этот файл открывать нельзя."
        await asyncio.to_thread(os.startfile, str(path))  # noqa: S606
        return f"Открыл на ноутбуке: {path.name}."

    async def trash(self, path: Path) -> str:
        if system.blocked_path(path) and not self.any_path:
            return "Этот файл трогать нельзя."
        done = await asyncio.to_thread(system.to_recycle_bin, path)
        return f"В корзине: {path.name}." if done else f"Не получилось удалить {path.name}."

    async def find(self, query: str) -> list[Path]:
        await self.a.tools.find_files(query)
        return [path for path in self.a.tools._found_files if not system.blocked_path(path)]  # noqa: SLF001

    @staticmethod
    def incoming_folder() -> Path:
        base = known_folder("downloads") or Path.home() / "Downloads"
        folder = base / "От телефона"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def resolve(self, raw: str) -> Path:
        """Путь из команды: относительный считается от текущей папки терминала, как и ждёт человек, который только что сделал cd."""
        path = Path(raw.strip().strip('"')).expanduser()
        return path if path.is_absolute() else self.session.cwd / path

    @staticmethod
    async def read_text(path: Path) -> str | None:
        return await asyncio.to_thread(system.read_text_file, path)

    @staticmethod
    async def make_folder(path: Path) -> str:
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        return f"Папка готова: {path}"

    @staticmethod
    async def transfer(source: Path, target: Path, copy: bool) -> str:
        """Папка назначения: кладём внутрь. Новое имя: это переименование. Занятое имя не перезаписываем."""
        import shutil

        def run() -> Path:
            place = target / source.name if target.is_dir() else target
            place = system.unique_path(place)
            place.parent.mkdir(parents=True, exist_ok=True)
            if copy:
                if source.is_dir():
                    shutil.copytree(source, place)
                else:
                    shutil.copy2(source, place)
            else:
                shutil.move(str(source), str(place))
            return place

        moved = await asyncio.to_thread(run)
        return f"{'Скопировал' if copy else 'Переместил'}: {moved}"

    @staticmethod
    async def zip_folder(path: Path, limit: int) -> Path | None:
        return await asyncio.to_thread(system.zip_folder, path, limit)

    # ---------- Ввод ----------

    async def type_text(self, text: str) -> str:
        return await self.a.tools.type_text(text)

    async def press(self, combo: str) -> str:
        return await self.a.tools.press_keys(combo)

    async def browser_key(self, combo: str) -> str:
        """Клавиша в браузере, даже если сверху другое окно. Только известные сочетания: они не закрывают браузер целиком."""
        if combo not in _BROWSER_SAFE_COMBOS:
            return "Неизвестная кнопка."
        window = self.a.desktop.target_browser()
        if window is None:
            return "Браузер не открыт."
        if not await asyncio.to_thread(winapi.activate, window.hwnd):
            return "Windows не дала переключиться на браузер."
        codes = winapi.parse_combo(combo)
        if codes is None:
            return "Неизвестная кнопка."
        await asyncio.to_thread(winapi.send_combo, codes)
        return f"Браузер: {combo}."

    async def clipboard(self) -> str:
        return await self.a.tools.clipboard("read")

    async def set_clipboard(self, text: str) -> str:
        return await self.a.tools.clipboard("write", text)

    async def open_target(self, target: str) -> str:
        target = target.strip()
        if _HAS_SCHEME.match(target) or _LOOKS_LIKE_SITE.match(target):
            return await self.a.tools.open_website(target if normalize_url(target) else "https://" + target)
        return await self.a.tools.open_app(target)

    async def say(self, text: str) -> str:
        return await self.a.tools.speak_aloud(text)

    async def popup(self, text: str) -> str:
        """Записка поверх всех окон. Своим потоком, а не общим пулом: окно может ждать нажатия часами, и десяток записок занял бы весь пул."""
        message = text[:800]
        self.a.mascot_note(message)

        def show() -> None:
            ctypes.windll.user32.MessageBoxW(None, message, "Сообщение от хозяина", 0x40 | 0x1000 | 0x40000)

        threading.Thread(target=show, name="jarvis-note", daemon=True).start()
        return "Показал на экране ноутбука."

    # ---------- Дела и напоминания ----------

    def notes(self) -> list[dict[str, Any]]:
        return self.a.memory.notes()

    def timers(self) -> list[dict[str, Any]]:
        return self.a.memory.active_timers()

    def note_done(self, note_id: int) -> None:
        self.a.memory.set_note_done(note_id)
        self.a.tools.on_notes_changed()

    def timer_cancel(self, timer_id: int) -> None:
        self.a.memory.finish_timer(timer_id)
        self.a.notify_timers_changed()

    async def add_note(self, text: str) -> str:
        return await self.a.tools.notes("add", text)

    async def add_reminder(self, label: str, seconds: int | None = None, at: str | None = None) -> str:
        return await self.a.tools.set_timer(label, seconds, at)

    # ---------- Сильные команды ----------

    @property
    def shell_allowed(self) -> bool:
        return self.a.config.settings.phone_shell

    @property
    def camera_allowed(self) -> bool:
        return self.a.config.settings.allow_camera

    @property
    def pin(self) -> str:
        return self.a.config.settings.phone_pin

    async def shell(self, command: str) -> system.CommandResult:
        # Кто сидит за ноутбуком, должен видеть, что им управляют. Текст команды в журнал не пишем: там могут быть пароли.
        self.a.mascot_caption(f"Команда с телефона: {command[:80]}")
        result = await asyncio.to_thread(self.session.run, command, SHELL_SECONDS)
        log.info("Команда с телефона завершилась, код %s", result.code)
        return result

    def shell_stop(self) -> bool:
        return self.session.stop()

    async def camera(self) -> bytes:
        return await self.a.grab_camera()

    # ---------- Джарвис ----------

    def routines(self) -> list[dict[str, Any]]:
        return self.a.memory.routines()

    async def show_jarvis(self) -> str:
        return await self.a.tools.jarvis_window("show")

    async def hide_jarvis(self) -> str:
        return await self.a.tools.jarvis_window("hide")

    def mic_on(self) -> bool:
        return self.a.mic_on

    def set_mic(self, on: bool) -> str:
        if not self.a.connected:
            return "Страницы Джарвиса сейчас нет, микрофон переключить нельзя."
        self.a.ask_page_mic(on)
        return "Включаю микрофон." if on else "Выключаю микрофон."

    def mascot_on(self) -> bool:
        return self.a.mascot is not None

    async def set_mascot(self, on: bool) -> str:
        return await self.a.tools.jarvis_window("mascot_on" if on else "mascot_off")

    async def jarvis_text(self) -> str:
        """Честная сводка: что работает прямо сейчас. Если талисман не поднялся, здесь это будет видно и с телефона."""
        a = self.a
        settings = a.config.settings
        minutes = round((time.monotonic() - self.started_at) / 60)
        lines = [
            f"J.A.R.V.I.S. {__version__}, работаю {minutes} мин",
            f"Мозг: {a.llm.active_label() or 'нет ключа'}",
            f"Микрофон: {'включён' if a.mic_on else 'выключен'}",
            f"Слово активации: {'слышу' if a.spotter is not None and settings.wake_word else 'не слушаю'}",
            f"Талисман: {'на экране' if a.mascot is not None else 'выключен'}",
            f"Окно: {'страница работает' if a.connected else 'страницы нет'}",
            f"Голос: {settings.voice}",
        ]
        return "\n".join(lines)

    def log_tail(self) -> str:
        path = self.a.config.home / "logs" / "jarvis.log"
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines = deque(handle, maxlen=_LOG_LINES)
        except OSError:
            return "Журнал пока пуст."
        return "".join(lines).strip() or "Журнал пока пуст."

    async def self_check(self) -> str:
        results = await self.a.self_check()
        return "\n".join(f"{'✓' if item['ok'] else '✕'} {item['name']}: {item['detail']}" for item in results)

    def restart_jarvis(self) -> None:
        self.a.restart_later()

    def quit_jarvis(self) -> None:
        self.a.quit_later()

    def take_pending(self) -> Any:
        return self.a.tools.take_pending()

    def pending_question(self) -> str:
        pending = self.a.tools.pending
        return pending.question if pending is not None else ""


def _mark_cursor(image: Any, rect: tuple[int, int, int, int], cursor: tuple[int, int]) -> Any:
    """Снимок экрана курсора не содержит: рисуем метку сами, иначе с телефона непонятно, куда он наведён."""
    from PIL import ImageDraw

    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    x, y = cursor[0] - rect[0], cursor[1] - rect[1]
    radius = max(10, marked.width // 110)
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=(255, 70, 60), width=3)
    draw.line((x - radius * 2, y, x + radius * 2, y), fill=(255, 70, 60), width=2)
    draw.line((x, y - radius * 2, x, y + radius * 2), fill=(255, 70, 60), width=2)
    return marked


def _draw_grid(image: Any, rect: tuple[int, int, int, int], columns: int, rows: int, cursor: tuple[int, int]) -> Any:
    from PIL import Image, ImageDraw

    from .mascot_art import _font

    # Маленькую часть экрана увеличиваем: на телефоне её иначе не разглядеть, и сетка потеряла бы смысл.
    width, height = image.size
    scale = max(1.0, _GRID_MIN_WIDTH / max(width, 1))
    canvas = image.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS).convert("RGBA") if scale > 1 else image.convert("RGBA")
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cell_w, cell_h = canvas.width / columns, canvas.height / rows
    font = _font(max(16, round(min(cell_w, cell_h) * 0.2)))
    for index in range(1, columns):
        draw.line((index * cell_w, 0, index * cell_w, canvas.height), fill=(255, 190, 60, 170), width=2)
    for index in range(1, rows):
        draw.line((0, index * cell_h, canvas.width, index * cell_h), fill=(255, 190, 60, 170), width=2)
    for row in range(rows):
        for column in range(columns):
            label = f"{chr(65 + column)}{row + 1}"
            x, y = (column + 0.5) * cell_w, (row + 0.5) * cell_h
            box = draw.textbbox((x, y), label, font=font, anchor="mm")
            draw.rectangle((box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4), fill=(2, 10, 18, 190))
            draw.text((x, y), label, font=font, fill=(255, 220, 130, 255), anchor="mm")
    composed = Image.alpha_composite(canvas, layer).convert("RGB")
    left, top, right, bottom = rect
    if left <= cursor[0] < right and top <= cursor[1] < bottom:
        return _mark_cursor(composed, (0, 0, 0, 0), (round((cursor[0] - left) * scale), round((cursor[1] - top) * scale)))
    return composed

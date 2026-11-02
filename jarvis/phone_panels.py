"""Кнопочные панели бота: экран, окна, система, звук, файлы, питание, мышь, браузер, терминал, дела, сеть, пульт.

Кнопки работают без языковой модели. Так надёжнее и быстрее, и ни один токен не тратится на то, что и так однозначно.
Опасные кнопки всегда спрашивают второй раз, и вопрос живёт всего минуту: старая кнопка в переписке не сработает.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

from . import system
from .laptop import FOLDERS, Laptop
from .tools import CONFIRM_PREFIX

if TYPE_CHECKING:
    from .phone import PhoneBridge

log = logging.getLogger("jarvis.panels")

_PAGE = 8
_CONFIRM_SECONDS = 60.0
_ID_LIMIT = 400
_TOO_BIG = 49 * 2**20  # предел Bot API для файла, который отправляет бот
_TEXT_LIMIT = 3500
_UNLOCK_SECONDS = 900.0
_PIN_ATTEMPTS = 5
_PIN_LOCKOUT = 900.0
_LIVE_SECONDS = 120.0
_LIVE_STEP = 3.0
_SHELL_NOTICE_AFTER = 4.0
_GRID_COLUMNS, _GRID_ROWS = 6, 4
_MOUSE_STEPS = (10, 50, 200)
_SCROLL_NOTCHES = 4
_NOT_ALLOWED = "выключено. Включить можно только на самом ноутбуке: настройки Джарвиса, раздел «Полный доступ»."
QUICK_KEYS = (
    (("Enter", "enter"), ("Esc", "esc"), ("Tab", "tab"), ("Пробел", "space")),
    (("←", "left"), ("↑", "up"), ("↓", "down"), ("→", "right")),
    (("PgUp", "pageup"), ("PgDn", "pagedown"), ("Alt+Tab", "alt+tab"), ("Win+D", "win+d")),
    (("Ctrl+C", "ctrl+c"), ("Ctrl+V", "ctrl+v"), ("Ctrl+S", "ctrl+s"), ("Ctrl+Z", "ctrl+z")),
    (("Новая вкладка", "ctrl+t"), ("Закрыть вкладку", "ctrl+w"), ("F5", "f5"), ("На весь экран", "f11")),
)
_BROWSER_KEYS = (
    (("⬅️ Назад", "alt+left"), ("🔄 Обновить", "f5"), ("➡️ Вперёд", "alt+right")),
    (("◀️ Вкладка", "ctrl+shift+tab"), ("➕ Новая", "ctrl+t"), ("Вкладка ▶️", "ctrl+tab")),
    (("✖️ Закрыть", "ctrl+w"), ("↩️ Вернуть", "ctrl+shift+t"), ("⤢ Экран", "f11")),
    (("⏫ PgUp", "pageup"), ("🔍 +", "ctrl+="), ("🔍 −", "ctrl+-"), ("⏬ PgDn", "pagedown")),
)
_POWER = (("🔒 Заблокировать", "lock"), ("🖥 Погасить экран", "screen_off"), ("💤 Сон", "sleep"), ("🔁 Перезагрузка", "restart"),
          ("⏻ Выключение", "shutdown"), ("↩️ Отменить выключение", "cancel_shutdown"))
_POWER_NO_QUESTION = {"lock", "screen_off", "cancel_shutdown"}
_POWER_QUESTION = {"sleep": "Отправить ноутбук в сон, сэр? С телефона до него будет не достучаться.",
                   "restart": "Перезагрузить ноутбук, сэр?", "shutdown": "Выключить ноутбук, сэр? С телефона включить его обратно нельзя."}
_REMIND_IN = re.compile(r"^(\d{1,4})\s*(м|мин|m|min|ч|час|h|с|сек|s)?\s+(.+)$", re.I | re.S)
_REMIND_AT = re.compile(r"^(\d{1,2}[:.]\d{2})\s+(.+)$", re.S)
_UNITS = {"м": 60, "мин": 60, "m": 60, "min": 60, "ч": 3600, "час": 3600, "h": 3600, "с": 1, "сек": 1, "s": 1, "": 60}
_TERMINAL_QUICK = (("📋 Процессы", "Get-Process | Sort-Object CPU -Descending | Select-Object -First 12 Name,Id,CPU | Format-Table -AutoSize"),
                   ("🌐 Сеть", "ipconfig"), ("💾 Диски", "Get-PSDrive -PSProvider FileSystem | Format-Table -AutoSize"))


@dataclass
class Panel:
    text: str
    rows: list[list[tuple[str, str]]] = field(default_factory=list)

    def keyboard(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in self.rows]}


HOME = Panel(
    "Пульт ноутбука, сэр. Можно и просто написать, что сделать, как дома голосом.",
    [[("📸 Экран", "scr:full"), ("🪟 Окна", "win"), ("📊 Система", "sys")],
     [("🎵 Звук", "med"), ("📁 Файлы", "fil"), ("⚡ Питание", "pow")],
     [("🖱 Мышь", "mou"), ("⌨️ Клавиши", "key"), ("🌐 Браузер", "bro")],
     [("💻 Терминал", "ter"), ("📝 Дела", "tod"), ("🚀 Сценарии", "rou")],
     [("🤖 Джарвис", "jar"), ("📡 Сеть", "net"), ("📋 Буфер", "clip")]],
)
_BACK = [("⬅️ Назад", "home")]
_PANELS = ("home", "win", "sys", "med", "fil", "pow", "rou", "key", "clip", "mou", "bro", "ter", "tod", "net", "jar", "drv")


class Panels:
    def __init__(self, bridge: PhoneBridge, laptop: Laptop) -> None:
        self.bridge = bridge
        self.laptop = laptop
        self._items: dict[str, Any] = {}
        # Счёт начинается заново при каждом запуске, поэтому у запуска своя приставка:
        # иначе «Да» под старым вопросом подтвердило бы совсем другой новый вопрос с тем же номером.
        self._run = secrets.token_hex(2)
        self._counter = 0
        self._asks: dict[str, tuple[float, Callable[[], Awaitable[str]], str]] = {}
        self.terminal_on = False
        self.upload_dir: Path | None = None
        self._mouse_step = _MOUSE_STEPS[1]
        self._grid: list[tuple[int, int, int, int]] = []
        self._live_until = 0.0
        self._live_running = False
        self._unlocked_until = 0.0
        self._pin_misses = 0
        self._pin_blocked_until = 0.0

    def _new_key(self) -> str:
        self._counter += 1
        return f"{self._run}{self._counter}"

    def _remember(self, value: Any) -> str:
        """В кнопку влезает 64 байта, поэтому путь или окно хранится здесь, а в кнопке только номер."""
        key = self._new_key()
        self._items[key] = value
        if len(self._items) > _ID_LIMIT:
            self._items.pop(next(iter(self._items)))
        return key

    # ---------- Вторая защита: ПИН ----------

    def lock(self) -> None:
        self._unlocked_until = 0.0
        self.terminal_on = False

    def _pin_ok(self) -> bool:
        return not self.laptop.pin or time.monotonic() < self._unlocked_until

    async def _need_pin(self, chat: int) -> bool:
        """True: команда закрыта ПИНом, человеку уже всё объяснили."""
        if self._pin_ok():
            return False
        await self.bridge.send(chat, "Нужен ПИН, сэр: /unlock ПИН. Сообщение с ним я сразу сотру.")
        return True

    async def _unlock(self, chat: int, pin: str) -> None:
        # Сообщение с ПИНом не должно остаться в переписке: её увидит любой, кто получит аккаунт.
        await self.bridge.delete_last(chat)
        now = time.monotonic()
        if not self.laptop.pin:
            await self.bridge.send(chat, "ПИН не задан. Его можно задать в настройках на самом ноутбуке.")
            return
        if now < self._pin_blocked_until:
            await self.bridge.send(chat, "Слишком много неверных попыток. Попробуйте через 15 минут.")
            return
        if secrets.compare_digest(pin.encode("utf-8"), self.laptop.pin.encode("utf-8")):
            self._pin_misses = 0
            self._unlocked_until = now + _UNLOCK_SECONDS
            await self.bridge.send(chat, "Открыто на 15 минут, сэр. Закрыть раньше: /lockbot")
            return
        self._pin_misses += 1
        if self._pin_misses >= _PIN_ATTEMPTS:
            self._pin_misses = 0
            self._pin_blocked_until = now + _PIN_LOCKOUT
            await self.bridge.send(chat, "Неверный ПИН. Попытки закрыты на 15 минут.")
            return
        await self.bridge.send(chat, f"Неверный ПИН. Осталось попыток: {_PIN_ATTEMPTS - self._pin_misses}.")

    def _any_path(self) -> bool:
        return bool(getattr(self.laptop, "any_path", False)) and self._pin_ok()

    # ---------- Открыть панель ----------

    async def show(self, chat: int, name: str, message: int = 0) -> None:
        builders = {"home": self._home, "win": self._windows, "sys": self._system, "med": self._media, "fil": self._folders,
                    "pow": self._power, "rou": self._routines, "key": self._keys, "clip": self._clipboard, "mou": self._mouse,
                    "bro": self._browser, "ter": self._terminal, "tod": self._todo, "net": self._net, "jar": self._jarvis, "drv": self._drives}
        panel = await builders[name]()
        await self.bridge.present(chat, message, panel.text, panel.keyboard())

    async def _home(self) -> Panel:
        return HOME

    def _screen_keyboard(self, kind: str) -> dict[str, Any] | None:
        return Panel("", [
            [("🔄 Обновить", f"scr:{kind}"), ("🪟 Активное окно", "scr:active"), ("🖥 Весь экран", "scr:full")],
            [("🔍 Прочитать текст", "scr:read"), ("📺 Живой экран", "liv:start"), ("🎯 Навести", "gr:home")],
            [("🖱 Мышь", "mou"), ("⌨️ Клавиши", "key"), ("🏠 Пульт", "home")],
        ]).keyboard()

    async def screen(self, chat: int, kind: str = "full", message: int = 0, hwnd: int = 0) -> None:
        await self.bridge.typing(chat, "upload_photo")
        jpeg = await self.laptop.screenshot("screen" if kind == "full" else "window", hwnd)
        if jpeg is None:
            await self.bridge.send(chat, "Снимок не получился, сэр: экран заблокирован, просмотр выключен в настройках или это защищённое видео.")
            return
        keyboard = self._screen_keyboard(kind)
        if message:
            await self.bridge.edit_photo(chat, message, jpeg, keyboard)
        else:
            await self.bridge.send_photo(chat, jpeg, keyboard)

    async def _windows(self) -> Panel:
        rows: list[list[tuple[str, str]]] = []
        for window in self.laptop.windows():
            label = f"{'🗕 ' if window.minimized else ''}{window.app}: {window.title[:28]}" if window.app.lower() not in window.title.lower() else f"{'🗕 ' if window.minimized else ''}{window.title[:38]}"
            rows.append([(label, f"wi:{self._remember(window.hwnd)}")])
        rows.append([("🔄 Обновить", "win"), *_BACK])
        return Panel("Открытые окна. Нажмите, чтобы управлять." if len(rows) > 1 else "Открытых окон нет.", rows)

    async def _window(self, hwnd: int) -> Panel:
        row = self.laptop.window_row(hwnd)
        if row is None:
            return Panel("Этого окна уже нет.", [[("🪟 К списку", "win")]])
        key = self._remember(hwnd)
        return Panel(f"{row.title[:120]}\nПрограмма: {row.app}", [
            [("⬆️ На передний план", f"wa:{key}:focus"), ("📸 Снимок", f"ws:{key}")],
            [("🗕 Свернуть", f"wa:{key}:minimize"), ("🗖 Развернуть", f"wa:{key}:maximize")],
            [("◧ Влево", f"wa:{key}:snap_left"), ("◨ Вправо", f"wa:{key}:snap_right")],
            [("✖️ Закрыть", f"wa:{key}:close"), ("🪟 К списку", "win")],
        ])

    async def _system(self) -> Panel:
        return Panel(await self.laptop.status_text(), [
            [("🔄 Обновить", "sys"), ("🔥 Что грузит", "pro:cpu"), ("🧠 Что ест память", "pro:memory")],
            [("🔅 25 %", "br:25"), ("🔆 50 %", "br:50"), ("☀️ 75 %", "br:75"), ("💡 100 %", "br:100")],
            [("🌙 Тёмная тема", "th:dark"), ("🌞 Светлая тема", "th:light")],
            [*_BACK],
        ])

    async def _processes(self, sort: str) -> Panel:
        groups = await self.laptop.processes(sort)
        rows = [[(f"✖️ {group.name.removesuffix('.exe')}", f"kl:{self._remember(group.name)}")] for group in groups[:6]]
        rows.append([("🔄 Обновить", f"pro:{sort}"), ("📊 К сводке", "sys")])
        return Panel(("Больше всего грузят процессор:" if sort == "cpu" else "Больше всего занимают памяти:") + "\n" + system.describe_processes(groups), rows)

    async def _media(self) -> Panel:
        return Panel(await self.laptop.media_text(), [
            [("⏮", "ma:previous"), ("⏯", "ma:toggle"), ("⏭", "ma:next")],
            [("🔉 Тише", "ma:volume_down"), ("🔇", "ma:mute"), ("🔊 Громче", "ma:volume_up")],
            [("🔈 25 %", "vo:25"), ("🔉 50 %", "vo:50"), ("🔊 75 %", "vo:75"), ("📢 100 %", "vo:100")],
            [("🔄 Обновить", "med"), *_BACK],
        ])

    async def _folders(self) -> Panel:
        rows: list[list[tuple[str, str]]] = []
        row: list[tuple[str, str]] = []
        for title, key in FOLDERS:
            path = self.laptop.folder(key)
            if path is not None:
                row.append((title, f"di:{self._remember(path)}:0"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if self._any_path():
            rows.append([("💽 Диски", "drv"), ("💻 Папка терминала", f"di:{self._remember(self.laptop.session.cwd)}:0")])
        rows.append(_BACK)
        where = self.upload_dir or "Загрузки, От телефона"
        return Panel(f"Файлы на ноутбуке. Присланное сюда попадёт в папку: {where}. Поиск: /find название. Путь: /ls C:\\Папка", rows)

    async def _drives(self) -> Panel:
        rows = [[(f"💽 {path}", f"di:{self._remember(path)}:0")] for path in self.laptop.drives()]
        rows.append([("🏠 Папки", "fil")])
        return Panel("Диски ноутбука.", rows)

    async def _directory(self, path: Path, page: int) -> Panel:
        entries = self.laptop.list_dir(path, True) if self._any_path() else self.laptop.list_dir(path)
        pages = max(1, (len(entries) + _PAGE - 1) // _PAGE)
        page = max(0, min(page, pages - 1))
        rows = self._entry_rows(entries[page * _PAGE:(page + 1) * _PAGE])
        key = self._remember(path)
        moves = []
        if page > 0:
            moves.append(("◀️", f"di:{key}:{page - 1}"))
        if page < pages - 1:
            moves.append(("▶️", f"di:{key}:{page + 1}"))
        if path.parent != path and (self._any_path() or not system.blocked_path(path.parent)):
            moves.append(("⬆️ Выше", f"di:{self._remember(path.parent)}:0"))
        moves.append(("🏠 Папки", "fil"))
        rows.append(moves)
        rows.append([("📦 Архивом сюда", f"fz:{key}"), ("📥 Принимать файлы сюда", f"fu:{key}")])
        title = f"{path}\nВсего: {len(entries)}" + (f", страница {page + 1} из {pages}" if pages > 1 else "")
        return Panel(title, rows)

    def _entry_rows(self, entries: list[Path]) -> list[list[tuple[str, str]]]:
        rows = []
        for entry in entries:
            key = self._remember(entry)
            rows.append([(f"📁 {entry.name[:40]}", f"di:{key}:0") if entry.is_dir() else (f"📄 {entry.name[:40]}", f"fi:{key}")])
        return rows

    async def _file(self, path: Path) -> Panel:
        key = self._remember(path)
        try:
            size = system.human_size((await asyncio.to_thread(path.stat)).st_size)
        except OSError:
            return Panel("Этого файла уже нет.", [[("🏠 Папки", "fil")]])
        return Panel(f"{path.name}\n{size}\nПапка: {path.parent}", [
            [("📤 Прислать сюда", f"fg:{key}"), ("▶️ Открыть на ноутбуке", f"fo:{key}")],
            [("👁 Показать текст", f"fr:{key}"), ("🗑 В корзину", f"fd:{key}")],
            [("⬅️ К папке", f"di:{self._remember(path.parent)}:0")],
        ])

    async def _power(self) -> Panel:
        rows = [[(label, f"pa:{action}")] for label, action in _POWER]
        rows.append([("⏲ Выключить через 15", "pt:15"), ("30 мин", "pt:30"), ("60 мин", "pt:60")])
        rows.append([("❌ Выключить самого Джарвиса", "pa:quit"), *_BACK])
        return Panel("Питание, сэр. Опасное я сначала переспрошу.", rows)

    async def _routines(self) -> Panel:
        saved = self.laptop.routines()
        if not saved:
            return Panel("Сценариев пока нет, сэр. Напишите, например: создай сценарий рабочий режим: открой VS Code, открой Chrome, поставь громкость 30.", [_BACK])
        rows = [[(f"🚀 {item['name'][:40]}", f"ro:{self._remember(item['name'])}")] for item in saved[:12]]
        rows.append(_BACK)
        return Panel("Сценарии. Нажмите, и я выполню все шаги по порядку.", rows)

    async def _keys(self) -> Panel:
        rows = [[(label, f"pr:{combo}") for label, combo in line] for line in QUICK_KEYS]
        rows.append([("📸 Посмотреть результат", "scr:full"), *_BACK])
        return Panel("Клавиши нажимаются в окне, которое сейчас сверху.\n/type текст напечатает текст\n/keys ctrl+shift+t нажмёт сочетание\n/click Сохранить нажмёт надпись на экране", rows)

    async def _clipboard(self) -> Panel:
        return Panel(f"{await self.laptop.clipboard()}\n\nПоложить в буфер: /clip текст", [[("🔄 Обновить", "clip"), *_BACK]])

    async def _mouse(self) -> Panel:
        return Panel(f"Мышь. Шаг {self._mouse_step} пикселей. «🎯 Навести» точнее: снимок с сеткой, нажимайте на клетки.", [
            [("↖️", "mm:-1:-1"), ("⬆️", "mm:0:-1"), ("↗️", "mm:1:-1")],
            [("⬅️", "mm:-1:0"), ("🖱 Клик", "mc:left"), ("➡️", "mm:1:0")],
            [("↙️", "mm:-1:1"), ("⬇️", "mm:0:1"), ("↘️", "mm:1:1")],
            [("Дважды", "mc:double"), ("Правой", "mc:right"), ("Средней", "mc:middle")],
            [("🔼 Вверх", "ms:up"), ("🔽 Вниз", "ms:down")],
            [(f"{'• ' if step == self._mouse_step else ''}{step} px", f"mst:{step}") for step in _MOUSE_STEPS],
            [("📸 Где курсор", "scr:cursor"), ("🎯 Навести", "gr:home"), *_BACK],
        ])

    async def _browser(self) -> Panel:
        rows = [[(label, f"pb:{combo}") for label, combo in line] for line in _BROWSER_KEYS]
        rows.append([("📸 Посмотреть", "scr:full"), *_BACK])
        return Panel("Браузер. Кнопка сама переключит на него, если сверху другое окно. Открыть адрес: /open github.com", rows)

    async def _terminal(self) -> Panel:
        session = self.laptop.session
        if not self.laptop.shell_allowed:
            return Panel(f"Команды с телефона {_NOT_ALLOWED}", [_BACK])
        mode = "Режим терминала включён: каждое сообщение выполняется как команда." if self.terminal_on else "Одна команда: /cmd git status. Режим терминала: каждое сообщение становится командой."
        rows = [[("⏹ Выйти из терминала", "tm:off") if self.terminal_on else ("▶️ Режим терминала", "tm:on")]]
        if session.running:
            rows.append([("⛔ Остановить команду", "tm:stop")])
        rows.append([(label, f"tq:{index}") for index, (label, _) in enumerate(_TERMINAL_QUICK)])
        rows.append([("🏠 Домашняя папка", "tm:home"), ("🖥 Рабочий стол", "tm:desktop")])
        rows.append(_BACK)
        return Panel(f"{mode}\nПапка: {session.cwd}", rows)

    async def _todo(self) -> Panel:
        rows: list[list[tuple[str, str]]] = []
        for note in self.laptop.notes()[:8]:
            rows.append([(f"✅ {note['text'][:44]}", f"nd:{note['id']}")])
        now = time.time()
        for timer in self.laptop.timers()[:6]:
            left = system.human_span(max(timer["due"] - now, 0))
            rows.append([(f"⏰ {timer['label'][:30]}, через {left}", f"td:{timer['id']}")])
        rows.append([("🔄 Обновить", "tod"), *_BACK])
        empty = "Дел и напоминаний нет, сэр." if len(rows) == 1 else "Нажатие на дело отмечает его сделанным, на напоминание отменяет."
        return Panel(f"{empty}\nДобавить: /note купить молоко\nНапомнить: /remind 10м позвонить или /remind 18:30 встреча", rows)

    async def _net(self) -> Panel:
        return Panel(await self.laptop.net_text(), [
            [("🌍 Внешний адрес", "net:ip"), ("🔄 Обновить", "net")],
            [("📶 Wi-Fi вкл", "ra:wifi:on"), ("Wi-Fi выкл", "ra:wifi:off")],
            [("🦷 Bluetooth вкл", "ra:bluetooth:on"), ("Bluetooth выкл", "ra:bluetooth:off")],
            [*_BACK],
        ])

    async def _jarvis(self) -> Panel:
        mic = self.laptop.mic_on()
        mascot = self.laptop.mascot_on()
        return Panel(await self.laptop.jarvis_text(), [
            [("🎙 Выключить микрофон" if mic else "🎙 Включить микрофон", f"ja:mic_{'off' if mic else 'on'}")],
            [("🔵 Убрать талисман" if mascot else "🔵 Вернуть талисман", f"ja:mascot_{'off' if mascot else 'on'}")],
            [("🪟 Показать окно", "ja:show"), ("🙈 Спрятать окно", "ja:hide")],
            [("📜 Журнал", "ja:log"), ("🩺 Самопроверка", "ja:check")],
            [("🔁 Перезапустить", "ja:restart"), ("❌ Выключить", "pa:quit")],
            [("🔄 Обновить", "jar"), *_BACK],
        ])

    # ---------- Подтверждения ----------

    async def ask(self, chat: int, message: int, question: str, action: Callable[[], Awaitable[str]], back: str = "home") -> None:
        now = time.monotonic()
        self._asks = {key: value for key, value in self._asks.items() if value[0] > now}
        key = self._new_key()
        self._asks[key] = (now + _CONFIRM_SECONDS, action, back)
        await self.bridge.present(chat, message, question, Panel("", [[("✅ Да", f"ok:{key}"), ("Нет", f"no:{key}")]]).keyboard())

    async def _after_tool(self, chat: int, message: int, result: str, back: str) -> None:
        """Инструмент решил, что нужно подтверждение: задаём его кнопками, а не текстом."""
        if result.startswith(CONFIRM_PREFIX):
            question = self.laptop.pending_question()
            pending = self.laptop.take_pending()
            if pending is not None:
                await self.ask(chat, message, question, pending.run, back)
                return
        panel = Panel(result, [[("📸 Посмотреть", "scr:full"), ("🏠 Пульт", "home")]])
        await self.bridge.present(chat, message, panel.text, panel.keyboard())

    # ---------- Нажатия ----------

    async def callback(self, chat: int, message: int, data: str) -> str:
        """Выполняет нажатие. Возвращает короткую всплывающую подпись или пустую строку."""
        kind, _, arg = data.partition(":")
        if kind == "net" and arg == "ip":
            return (await self.laptop.public_ip())[:190]
        if kind in _PANELS:
            await self.show(chat, kind, message)
            return ""
        if kind == "scr":
            if arg == "read":
                await self.bridge.send(chat, await self.laptop.read_screen())
                return ""
            if arg == "cursor":
                await self._cursor_shot(chat)
                return ""
            # Обновляем ту же фотографию, только если нажали кнопку под фотографией.
            await self.screen(chat, "full" if arg == "full" else "active", message if self.bridge.is_photo(message) else 0)
            return ""
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            return "Эта кнопка устарела."
        return await handler(chat, message, arg)

    def _item(self, key: str) -> Any:
        return self._items.get(key)

    async def _on_ok(self, chat: int, message: int, key: str) -> str:
        entry = self._asks.pop(key, None)
        if entry is None or entry[0] < time.monotonic():
            await self.bridge.present(chat, message, "Вопрос устарел, ничего не сделал.", Panel("", [[("🏠 Пульт", "home")]]).keyboard())
            return ""
        try:
            result = await entry[1]()
        except Exception as exc:  # noqa: BLE001 — кнопка не должна ронять бота
            log.warning("Действие с кнопки не сработало: %s", type(exc).__name__)
            result = "Не получилось это сделать."
        await self.bridge.present(chat, message, result, Panel("", [[("⬅️ Назад", entry[2])]]).keyboard())
        return ""

    async def _on_no(self, chat: int, message: int, key: str) -> str:
        entry = self._asks.pop(key, None)
        await self.bridge.present(chat, message, "Как скажете, сэр. Не буду.", Panel("", [[("⬅️ Назад", entry[2] if entry else "home")]]).keyboard())
        return ""

    async def _on_wi(self, chat: int, message: int, key: str) -> str:
        hwnd = self._item(key)
        if hwnd is None:
            return "Кнопка устарела."
        panel = await self._window(hwnd)
        await self.bridge.present(chat, message, panel.text, panel.keyboard())
        return ""

    async def _on_ws(self, chat: int, message: int, key: str) -> str:
        hwnd = self._item(key)
        if hwnd is None:
            return "Кнопка устарела."
        await self.screen(chat, "active", 0, hwnd)
        return ""

    async def _on_wa(self, chat: int, message: int, arg: str) -> str:
        key, _, action = arg.partition(":")
        hwnd = self._item(key)
        if hwnd is None:
            return "Кнопка устарела."
        if action == "close":
            row = self.laptop.window_row(hwnd)
            if row is None:
                return "Этого окна уже нет."
            question = f"Закрыть {row.app}, сэр? Там может быть несохранённая работа." if row.protected else f"Закрыть окно {row.title[:50]}?"
            await self.ask(chat, message, question, lambda: self.laptop.window_action(hwnd, "close"), "win")
            return ""
        result = await self.laptop.window_action(hwnd, action)
        panel = await self._window(hwnd)
        await self.bridge.present(chat, message, panel.text, panel.keyboard())
        return result

    async def _on_pro(self, chat: int, message: int, sort: str) -> str:
        panel = await self._processes("cpu" if sort == "cpu" else "memory")
        await self.bridge.present(chat, message, panel.text, panel.keyboard())
        return ""

    async def _on_kl(self, chat: int, message: int, key: str) -> str:
        name = self._item(key)
        if name is None:
            return "Кнопка устарела."
        await self.ask(chat, message, f"Завершить {name.removesuffix('.exe')}, сэр? Несохранённое пропадёт.", lambda: self.laptop.kill(name), "sys")
        return ""

    async def _on_ma(self, chat: int, message: int, action: str) -> str:
        result = await self.laptop.media_action(action)
        panel = await self._media()
        await self.bridge.present(chat, message, panel.text, panel.keyboard())
        return result[:190]

    async def _on_vo(self, chat: int, message: int, level: str) -> str:
        result = await self.laptop.volume(int(level))
        panel = await self._media()
        await self.bridge.present(chat, message, panel.text, panel.keyboard())
        return result[:190]

    async def _on_br(self, chat: int, message: int, level: str) -> str:
        return (await self.laptop.brightness(int(level)))[:190]

    async def _on_th(self, chat: int, message: int, kind: str) -> str:
        return (await self.laptop.theme(kind == "dark"))[:190]

    async def _on_ra(self, chat: int, message: int, arg: str) -> str:
        kind, _, state = arg.partition(":")
        await self._after_tool(chat, message, await self.laptop.radio(kind, state == "on"), "net")
        return ""

    async def _on_di(self, chat: int, message: int, arg: str) -> str:
        key, _, page = arg.partition(":")
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        panel = await self._directory(path, int(page or 0))
        await self.bridge.present(chat, message, panel.text, panel.keyboard())
        return ""

    async def _on_fi(self, chat: int, message: int, key: str) -> str:
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        panel = await self._file(path)
        await self.bridge.present(chat, message, panel.text, panel.keyboard())
        return ""

    async def _on_fg(self, chat: int, message: int, key: str) -> str:
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        await self.send_file(chat, path)
        return ""

    async def _on_fr(self, chat: int, message: int, key: str) -> str:
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        await self._send_text_of(chat, path)
        return ""

    async def _on_fz(self, chat: int, message: int, key: str) -> str:
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        self.bridge.spawn(self._send_zip(chat, path))
        return "Собираю архив."

    async def _on_fu(self, chat: int, message: int, key: str) -> str:
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        self.upload_dir = path
        return f"Файлы теперь попадут сюда: {path.name or path}"

    async def send_file(self, chat: int, path: Path) -> None:
        if system.blocked_path(path) and not self._any_path():
            await self.bridge.send(chat, "Этот файл на телефон я не отправлю: там могут быть пароли и ключи.")
            return
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
        except OSError:
            await self.bridge.send(chat, "Этого файла уже нет.")
            return
        if size > _TOO_BIG:
            await self.bridge.send(chat, f"Файл {path.name} весит {system.human_size(size)}, а бот может отправить только до 49 МБ.")
            return
        await self.bridge.typing(chat, "upload_document")
        await self.bridge.send_document(chat, path)

    async def _send_text_of(self, chat: int, path: Path) -> None:
        if system.blocked_path(path) and not self._any_path():
            await self.bridge.send(chat, "Этот файл я показывать не буду: там могут быть пароли и ключи.")
            return
        text = await self.laptop.read_text(path)
        if text is None:
            await self.bridge.send(chat, f"{path.name} не текстовый файл. Прислать его можно кнопкой «Прислать сюда».")
            return
        await self.bridge.send(chat, text[:_TEXT_LIMIT] or "Файл пуст.")
        if len(text) > _TEXT_LIMIT:
            await self.bridge.send(chat, f"Показал начало: файл длиннее. Целиком: /get {path}")

    async def _send_zip(self, chat: int, path: Path) -> None:
        if system.blocked_path(path) and not self._any_path():
            await self.bridge.send(chat, "Эту папку я не отправлю: там могут быть пароли и ключи.")
            return
        await self.bridge.typing(chat, "upload_document")
        archive = await self.laptop.zip_folder(path, _TOO_BIG)
        if archive is None:
            await self.bridge.send(chat, f"Папка {path.name} не помещается в архив до 49 МБ: это предел Telegram для ботов.")
            return
        try:
            await self.bridge.send_document(chat, archive)
        finally:
            await asyncio.to_thread(shutil.rmtree, archive.parent, True)

    async def _on_fo(self, chat: int, message: int, key: str) -> str:
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        if self.laptop.is_dangerous(path):
            await self.ask(chat, message, f"Запустить программу {path.name}, сэр? Это может быть опасно.", lambda: self.laptop.open_file(path), "fil")
            return ""
        return (await self.laptop.open_file(path))[:190]

    async def _on_fd(self, chat: int, message: int, key: str) -> str:
        path = self._item(key)
        if path is None:
            return "Кнопка устарела."
        await self.ask(chat, message, f"Отправить в корзину {path.name}?", lambda: self.laptop.trash(path), "fil")
        return ""

    async def _on_pa(self, chat: int, message: int, action: str) -> str:
        if action == "quit":
            await self.ask(chat, message, "Выключить меня полностью, сэр? С телефона включить обратно нельзя.", self._quit, "pow")
            return ""
        if action in _POWER_NO_QUESTION:
            return (await self.laptop.power(action))[:190]
        await self.ask(chat, message, _POWER_QUESTION.get(action, "Точно, сэр?"), lambda: self.laptop.power(action), "pow")
        return ""

    async def _on_pt(self, chat: int, message: int, minutes: str) -> str:
        count = int(minutes)
        await self.ask(chat, message, f"Выключить ноутбук через {count} минут, сэр?", lambda: self.laptop.shutdown_in(count), "pow")
        return ""

    async def _quit(self) -> str:
        self.laptop.quit_jarvis()
        return "Отключаюсь, сэр. До связи."

    async def _restart(self) -> str:
        self.laptop.restart_jarvis()
        return "Перезапускаюсь, сэр. Буду на связи через несколько секунд."

    async def _on_ro(self, chat: int, message: int, key: str) -> str:
        name = self._item(key)
        if name is None:
            return "Кнопка устарела."
        await self.bridge.run_text(chat, name)
        return ""

    async def _on_pr(self, chat: int, message: int, combo: str) -> str:
        result = await self.laptop.press(combo)
        if result.startswith(CONFIRM_PREFIX):
            await self._after_tool(chat, 0, result, "key")
            return ""
        return result[:190]

    async def _on_pb(self, chat: int, message: int, combo: str) -> str:
        return (await self.laptop.browser_key(combo))[:190]

    # ---------- Мышь и сетка ----------

    def _locked_note(self) -> str:
        return "Экран заблокирован: Windows не пропускает мышь и клавиши." if self.laptop.locked() else ""

    async def _on_mm(self, chat: int, message: int, arg: str) -> str:
        if note := self._locked_note():
            return note
        dx, _, dy = arg.partition(":")
        x, y = self.laptop.mouse_move(int(dx) * self._mouse_step, int(dy) * self._mouse_step)
        return f"Курсор: {x}, {y}"

    async def _on_mc(self, chat: int, message: int, button: str) -> str:
        if note := self._locked_note():
            return note
        await self.laptop.mouse_click(button)
        return "Нажал."

    async def _on_ms(self, chat: int, message: int, direction: str) -> str:
        if note := self._locked_note():
            return note
        await self.laptop.mouse_scroll(_SCROLL_NOTCHES if direction == "up" else -_SCROLL_NOTCHES)
        return "Прокрутил."

    async def _on_mst(self, chat: int, message: int, step: str) -> str:
        if int(step) in _MOUSE_STEPS:
            self._mouse_step = int(step)
        await self.show(chat, "mou", message)
        return ""

    async def _cursor_shot(self, chat: int) -> None:
        await self.bridge.typing(chat, "upload_photo")
        jpeg = await self.laptop.screenshot("cursor")
        if jpeg is None:
            await self.bridge.send(chat, "Снимок не получился: экран заблокирован или просмотр выключен в настройках.")
            return
        await self.bridge.send_photo(chat, jpeg, Panel("", [[("🖱 Мышь", "mou"), ("🎯 Навести", "gr:home"), ("🏠 Пульт", "home")]]).keyboard())

    def _grid_keyboard(self) -> dict[str, Any] | None:
        rows = [[(f"{chr(65 + column)}{row + 1}", f"gr:{column}:{row}") for column in range(_GRID_COLUMNS)] for row in range(_GRID_ROWS)]
        rows.append([("🖱 Клик в центр", "gc:left"), ("Дважды", "gc:double"), ("Правой", "gc:right")])
        rows.append([("🔎 Отдалить", "gr:back"), ("🖥 Весь экран", "gr:home"), ("🏠 Пульт", "home")])
        return Panel("", rows).keyboard()

    async def _show_grid(self, chat: int, message: int) -> None:
        await self.bridge.typing(chat, "upload_photo")
        jpeg = await self.laptop.grid_shot(self._grid[-1], _GRID_COLUMNS, _GRID_ROWS)
        if jpeg is None:
            await self.bridge.send(chat, "Снимок не получился: экран заблокирован или просмотр выключен в настройках.")
            return
        if message and self.bridge.is_photo(message):
            await self.bridge.edit_photo(chat, message, jpeg, self._grid_keyboard())
        else:
            await self.bridge.send_photo(chat, jpeg, self._grid_keyboard())

    async def _on_gr(self, chat: int, message: int, arg: str) -> str:
        """Нажатие на клетку приближает её: за три нажатия точность около десяти пикселей, и всё это без координат от модели."""
        if arg == "home" or not self._grid:
            self._grid = [self.laptop.screen_rect()]
        elif arg == "back":
            if len(self._grid) > 1:
                self._grid.pop()
        else:
            column, _, row = arg.partition(":")
            left, top, right, bottom = self._grid[-1]
            width, height = (right - left) / _GRID_COLUMNS, (bottom - top) / _GRID_ROWS
            rect = (round(left + int(column) * width), round(top + int(row) * height),
                    round(left + (int(column) + 1) * width), round(top + (int(row) + 1) * height))
            if rect[2] - rect[0] < _GRID_COLUMNS or rect[3] - rect[1] < _GRID_ROWS:
                return "Точнее уже некуда."
            self._grid.append(rect)
        left, top, right, bottom = self._grid[-1]
        if not self.laptop.locked():
            self.laptop.mouse_to((left + right) // 2, (top + bottom) // 2)
        await self._show_grid(chat, message)
        return ""

    async def _on_gc(self, chat: int, message: int, button: str) -> str:
        if note := self._locked_note():
            return note
        if not self._grid:
            return "Сначала откройте сетку."
        left, top, right, bottom = self._grid[-1]
        self.laptop.mouse_to((left + right) // 2, (top + bottom) // 2)
        await self.laptop.mouse_click(button)
        # Покажем результат: иначе с телефона непонятно, попал ли клик.
        await asyncio.sleep(0.4)
        await self._show_grid(chat, message)
        return "Нажал."

    # ---------- Живой экран ----------

    async def _on_liv(self, chat: int, message: int, arg: str) -> str:
        if arg == "stop":
            self._live_until = 0.0
            return "Остановил."
        self._live_until = time.monotonic() + _LIVE_SECONDS
        if self._live_running:
            return "Уже показываю."
        self._live_running = True
        self.bridge.spawn(self._live_loop(chat, message if self.bridge.is_photo(message) else 0))
        return f"Показываю {_LIVE_SECONDS:.0f} секунд."

    async def _live_loop(self, chat: int, message: int) -> None:
        """Одна фотография, которая обновляется на месте: без роликов и лишних программ, а чат не засоряется."""
        keyboard = Panel("", [[("⏹ Стоп", "liv:stop"), ("🖱 Мышь", "mou"), ("⌨️ Клавиши", "key")]]).keyboard()
        try:
            if not message:
                jpeg = await self.laptop.screenshot("live")
                if jpeg is None:
                    await self.bridge.send(chat, "Снимок не получился: экран заблокирован или просмотр выключен в настройках.")
                    return
                message = await self.bridge.send_photo(chat, jpeg, keyboard)
            while time.monotonic() < self._live_until:
                await asyncio.sleep(_LIVE_STEP)
                jpeg = await self.laptop.screenshot("live")
                if jpeg is None:
                    break
                await self.bridge.edit_photo(chat, message, jpeg, keyboard)
            await self.bridge.edit_keyboard(chat, message, self._screen_keyboard("full"))
        except httpx.HTTPError as exc:
            log.info("Живой экран остановлен: %s", type(exc).__name__)
        finally:
            self._live_running = False

    # ---------- Терминал ----------

    async def _on_tm(self, chat: int, message: int, action: str) -> str:
        if not self.laptop.shell_allowed:
            return "Команды с телефона выключены."
        if await self._need_pin(chat):
            return ""
        note = ""
        if action == "on":
            self.terminal_on = True
            note = "Каждое сообщение теперь команда. Выйти: /exit"
        elif action == "off":
            self.terminal_on = False
            note = "Вышел из терминала."
        elif action == "stop":
            note = "Остановил команду." if self.laptop.shell_stop() else "Сейчас ничего не выполняется."
        elif action == "home":
            self.laptop.session.cwd = Path.home()
        elif action == "desktop":
            self.laptop.session.cwd = self.laptop.folder("desktop") or Path.home()
        await self.show(chat, "ter", message)
        return note

    async def _on_tq(self, chat: int, message: int, index: str) -> str:
        if not 0 <= int(index) < len(_TERMINAL_QUICK):
            return "Кнопка устарела."
        await self._shell(chat, _TERMINAL_QUICK[int(index)][1])
        return ""

    # ---------- Дела ----------

    async def _on_nd(self, chat: int, message: int, note_id: str) -> str:
        self.laptop.note_done(int(note_id))
        await self.show(chat, "tod", message)
        return "Отметил."

    async def _on_td(self, chat: int, message: int, timer_id: str) -> str:
        self.laptop.timer_cancel(int(timer_id))
        await self.show(chat, "tod", message)
        return "Отменил."

    async def _remind(self, chat: int, arg: str) -> None:
        at = _REMIND_AT.match(arg)
        delay = _REMIND_IN.match(arg)
        if at is not None:
            result = await self.laptop.add_reminder(at[2].strip(), at=at[1].replace(".", ":"))
        elif delay is not None:
            unit = (delay[2] or "").lower()
            result = await self.laptop.add_reminder(delay[3].strip(), seconds=int(delay[1]) * _UNITS.get(unit, 60))
        else:
            result = "Напишите так: /remind 10м позвонить или /remind 18:30 встреча."
        await self.bridge.send(chat, result)

    # ---------- Джарвис ----------

    async def _on_ja(self, chat: int, message: int, action: str) -> str:
        if action in ("mic_on", "mic_off"):
            note = self.laptop.set_mic(action == "mic_on")
            # Страница переключит микрофон и пришлёт ответ, но кнопка на телефоне должна отозваться сразу.
            await asyncio.sleep(0.5)
            await self.show(chat, "jar", message)
            return note[:190]
        if action in ("mascot_on", "mascot_off"):
            note = await self.laptop.set_mascot(action == "mascot_on")
            await self.show(chat, "jar", message)
            return note[:190]
        if action == "show":
            return (await self.laptop.show_jarvis())[:190]
        if action == "hide":
            return (await self.laptop.hide_jarvis())[:190]
        if action == "log":
            await self.bridge.send(chat, self.laptop.log_tail())
            return ""
        if action == "check":
            self.bridge.spawn(self._self_check(chat))
            return "Проверяю, это до минуты."
        if action == "restart":
            await self.ask(chat, message, "Перезапустить меня, сэр? Это займёт несколько секунд.", self._restart, "jar")
            return ""
        return "Кнопка устарела."

    async def _self_check(self, chat: int) -> None:
        await self.bridge.typing(chat)
        await self.bridge.send(chat, await self.laptop.self_check())

    # ---------- Команды со словами ----------

    async def command(self, chat: int, name: str, arg: str) -> bool:
        """Команды вроде /say привет. True: команда наша и обработана."""
        panel = {"menu": "home", "pult": "home", "пульт": "home", "windows": "win", "status": "sys", "media": "med", "files": "fil",
                 "power": "pow", "routines": "rou", "mouse": "mou", "browser": "bro", "terminal": "ter", "term": "ter", "todo": "tod",
                 "net": "net", "jarvis": "jar"}.get(name)
        if panel is not None:
            await self.show(chat, panel)
            return True
        words: dict[str, Callable[[int, str], Awaitable[None]]] = {
            "screen": self._cmd_screen, "active": self._cmd_active, "read": self._cmd_read, "keys": self._cmd_keys, "lock": self._cmd_lock,
            "find": self._find, "camera": self._cmd_camera, "cmd": self._shell, "show": self._cmd_show, "say": self._cmd_say, "type": self._cmd_type,
            "click": self._cmd_click, "open": self._cmd_open, "clip": self._cmd_clip, "live": self._cmd_live, "tap": self._cmd_tap, "exit": self._cmd_exit,
            "note": self._cmd_note, "remind": self._cmd_remind, "ip": self._cmd_ip, "log": self._cmd_log, "check": self._cmd_check,
            "restart": self._cmd_restart, "mic": self._cmd_mic, "vol": self._cmd_vol, "bright": self._cmd_bright, "kill": self._cmd_kill,
            "ps": self._cmd_ps, "ls": self._cmd_ls, "get": self._cmd_get, "cat": self._cmd_cat, "mkdir": self._cmd_mkdir, "rm": self._cmd_rm,
            "mv": self._cmd_mv, "cp": self._cmd_cp, "zip": self._cmd_zip, "notify": self._cmd_notify, "look": self._cmd_look,
            "unlock": self._unlock, "lockbot": self._cmd_lockbot, "shutdown": self._cmd_shutdown,
        }
        handler = words.get(name)
        if handler is None:
            return False
        await handler(chat, arg)
        return True

    async def _need_text(self, chat: int, name: str, arg: str, example: str = "текст") -> bool:
        if arg:
            return False
        await self.bridge.send(chat, f"Напишите так: /{name} {example}.")
        return True

    async def _cmd_screen(self, chat: int, arg: str) -> None:
        await self.screen(chat, "full")

    async def _cmd_active(self, chat: int, arg: str) -> None:
        await self.screen(chat, "active")

    async def _cmd_read(self, chat: int, arg: str) -> None:
        await self.bridge.send(chat, await self.laptop.read_screen())

    async def _cmd_keys(self, chat: int, arg: str) -> None:
        if not arg:
            await self.show(chat, "key")
            return
        await self._after_tool(chat, 0, await self.laptop.press(arg), "key")

    async def _cmd_lock(self, chat: int, arg: str) -> None:
        await self.bridge.send(chat, await self.laptop.power("lock"))

    async def _cmd_show(self, chat: int, arg: str) -> None:
        await self.bridge.send(chat, await self.laptop.show_jarvis())

    async def _cmd_say(self, chat: int, arg: str) -> None:
        if not await self._need_text(chat, "say", arg):
            await self.bridge.send(chat, await self.laptop.say(arg))

    async def _cmd_type(self, chat: int, arg: str) -> None:
        if not await self._need_text(chat, "type", arg):
            await self._after_tool(chat, 0, await self.laptop.type_text(arg), "key")

    async def _cmd_click(self, chat: int, arg: str) -> None:
        if not await self._need_text(chat, "click", arg, "надпись на экране"):
            await self._after_tool(chat, 0, await self.laptop.click_text(arg), "key")

    async def _cmd_open(self, chat: int, arg: str) -> None:
        if not await self._need_text(chat, "open", arg, "github.com или название программы"):
            await self._after_tool(chat, 0, await self.laptop.open_target(arg), "home")

    async def _cmd_clip(self, chat: int, arg: str) -> None:
        if arg:
            await self.bridge.send(chat, await self.laptop.set_clipboard(arg))
        else:
            await self.show(chat, "clip")

    async def _cmd_live(self, chat: int, arg: str) -> None:
        note = await self._on_liv(chat, 0, "start")
        await self.bridge.send(chat, f"{note} Остановить: кнопка «Стоп».")

    async def _cmd_tap(self, chat: int, arg: str) -> None:
        await self._on_gr(chat, 0, "home")

    async def _cmd_exit(self, chat: int, arg: str) -> None:
        self.terminal_on = False
        await self.bridge.send(chat, "Вышел из терминала, сэр.")

    async def _cmd_note(self, chat: int, arg: str) -> None:
        if not await self._need_text(chat, "note", arg, "купить молоко"):
            await self.bridge.send(chat, await self.laptop.add_note(arg))

    async def _cmd_remind(self, chat: int, arg: str) -> None:
        await self._remind(chat, arg)

    async def _cmd_ip(self, chat: int, arg: str) -> None:
        await self.bridge.send(chat, await self.laptop.public_ip())

    async def _cmd_log(self, chat: int, arg: str) -> None:
        await self.bridge.send(chat, self.laptop.log_tail())

    async def _cmd_check(self, chat: int, arg: str) -> None:
        self.bridge.spawn(self._self_check(chat))

    async def _cmd_restart(self, chat: int, arg: str) -> None:
        await self.ask(chat, 0, "Перезапустить меня, сэр? Это займёт несколько секунд.", self._restart, "jar")

    async def _cmd_mic(self, chat: int, arg: str) -> None:
        wanted = arg.lower() in ("on", "вкл", "включи", "1") if arg else not self.laptop.mic_on()
        await self.bridge.send(chat, self.laptop.set_mic(wanted))

    async def _cmd_vol(self, chat: int, arg: str) -> None:
        if not arg.isdigit():
            await self.show(chat, "med")
            return
        await self.bridge.send(chat, await self.laptop.volume(min(int(arg), 100)))

    async def _cmd_bright(self, chat: int, arg: str) -> None:
        if not arg.isdigit():
            await self.bridge.send(chat, "Напишите так: /bright 50.")
            return
        await self.bridge.send(chat, await self.laptop.brightness(min(int(arg), 100)))

    async def _cmd_kill(self, chat: int, arg: str) -> None:
        if await self._need_text(chat, "kill", arg, "chrome"):
            return
        name = arg.strip()
        await self.ask(chat, 0, f"Завершить {name.removesuffix('.exe')}, сэр? Несохранённое пропадёт.", lambda: self.laptop.kill(name), "sys")

    async def _cmd_ps(self, chat: int, arg: str) -> None:
        panel = await self._processes("cpu" if arg.lower() == "cpu" else "memory")
        await self.bridge.send(chat, panel.text, panel.keyboard())

    async def _cmd_shutdown(self, chat: int, arg: str) -> None:
        if arg.isdigit():
            await self._on_pt(chat, 0, arg)
        else:
            await self.show(chat, "pow")

    def _open_for_owner(self, path: Path) -> bool:
        return self._any_path() or not system.blocked_path(path)

    async def _cmd_ls(self, chat: int, arg: str) -> None:
        path = self.laptop.resolve(arg) if arg else self.laptop.session.cwd
        if not path.is_dir():
            await self.bridge.send(chat, f"Такой папки нет: {path}")
            return
        if not self._open_for_owner(path):
            await self.bridge.send(chat, f"Эта папка закрыта. Открыть можно на ноутбуке: настройки Джарвиса, раздел «Полный доступ». {path}")
            return
        panel = await self._directory(path, 0)
        await self.bridge.send(chat, panel.text, panel.keyboard())

    async def _cmd_get(self, chat: int, arg: str) -> None:
        if await self._need_text(chat, "get", arg, "C:\\Папка\\файл.txt"):
            return
        path = self.laptop.resolve(arg)
        if path.is_dir():
            self.bridge.spawn(self._send_zip(chat, path))
        else:
            await self.send_file(chat, path)

    async def _cmd_cat(self, chat: int, arg: str) -> None:
        if not await self._need_text(chat, "cat", arg, "C:\\Папка\\файл.txt"):
            await self._send_text_of(chat, self.laptop.resolve(arg))

    async def _cmd_mkdir(self, chat: int, arg: str) -> None:
        if await self._need_text(chat, "mkdir", arg, "C:\\Новая папка"):
            return
        path = self.laptop.resolve(arg)
        if not self._open_for_owner(path):
            await self.bridge.send(chat, "Эта папка закрыта настройками полного доступа.")
            return
        try:
            await self.bridge.send(chat, await self.laptop.make_folder(path))
        except OSError as exc:
            await self.bridge.send(chat, f"Не получилось: {exc.strerror or type(exc).__name__}")

    async def _cmd_rm(self, chat: int, arg: str) -> None:
        if await self._need_text(chat, "rm", arg, "C:\\Папка\\файл.txt"):
            return
        path = self.laptop.resolve(arg)
        if not path.exists():
            await self.bridge.send(chat, f"Такого нет: {path}")
            return
        if not self._open_for_owner(path):
            await self.bridge.send(chat, "Это закрыто настройками полного доступа.")
            return
        await self.ask(chat, 0, f"Отправить в корзину {path}?", lambda: self.laptop.trash(path), "fil")

    async def _transfer(self, chat: int, name: str, arg: str, copy: bool) -> None:
        source_text, _, target_text = arg.partition("|")
        if not source_text.strip() or not target_text.strip():
            await self.bridge.send(chat, f"Напишите так: /{name} откуда | куда.")
            return
        source, target = self.laptop.resolve(source_text), self.laptop.resolve(target_text)
        if not source.exists():
            await self.bridge.send(chat, f"Такого нет: {source}")
            return
        if not self._open_for_owner(source) or not self._open_for_owner(target):
            await self.bridge.send(chat, "Это закрыто настройками полного доступа.")
            return
        await self.ask(chat, 0, f"{'Скопировать' if copy else 'Переместить'} {source.name} в {target}?", lambda: self.laptop.transfer(source, target, copy), "fil")

    async def _cmd_mv(self, chat: int, arg: str) -> None:
        await self._transfer(chat, "mv", arg, False)

    async def _cmd_cp(self, chat: int, arg: str) -> None:
        await self._transfer(chat, "cp", arg, True)

    async def _cmd_zip(self, chat: int, arg: str) -> None:
        if await self._need_text(chat, "zip", arg, "C:\\Папка"):
            return
        path = self.laptop.resolve(arg)
        if not path.is_dir():
            await self.bridge.send(chat, f"Такой папки нет: {path}")
            return
        self.bridge.spawn(self._send_zip(chat, path))

    async def _cmd_notify(self, chat: int, arg: str) -> None:
        if not await self._need_text(chat, "notify", arg):
            await self.bridge.send(chat, await self.laptop.popup(arg))

    async def _cmd_look(self, chat: int, arg: str) -> None:
        # Зрение думает до 25 секунд: всё это время бот должен отвечать на остальное.
        self.bridge.spawn(self._look(chat, arg or "Что сейчас на экране? Коротко."))

    async def _look(self, chat: int, question: str) -> None:
        await self.bridge.typing(chat)
        await self.bridge.send(chat, await self.laptop.look(question))

    async def _cmd_lockbot(self, chat: int, arg: str) -> None:
        self.lock()
        await self.bridge.send(chat, "Закрыто, сэр. Команды и камера снова требуют ПИН." if self.laptop.pin else "ПИН не задан, закрывать нечего.")

    async def _find(self, chat: int, query: str) -> None:
        if not query:
            await self.bridge.send(chat, "Напишите так: /find название файла.")
            return
        await self.bridge.typing(chat)
        paths = await self.laptop.find(query)
        if not paths:
            await self.bridge.send(chat, f"Файлов с «{query}» в названии не нашёл.")
            return
        rows = [[(f"📄 {path.name[:44]}", f"fi:{self._remember(path)}")] for path in paths[:8]]
        await self.bridge.send(chat, f"Нашёл, самые свежие первыми: {len(paths)}", Panel("", rows).keyboard())

    async def _cmd_camera(self, chat: int, arg: str) -> None:
        await self._camera(chat)

    async def _camera(self, chat: int) -> None:
        if not self.laptop.camera_allowed:
            await self.bridge.send(chat, f"Камера с телефона {_NOT_ALLOWED}")
            return
        if await self._need_pin(chat):
            return
        await self.bridge.typing(chat, "upload_photo")
        try:
            jpeg = await self.laptop.camera()
        except Exception as exc:  # noqa: BLE001 — камеры может не быть, она может быть занята или запрещена
            log.info("Камера не сработала: %s", exc)
            await self.bridge.send(chat, f"С камеры снять не получилось: {exc}")
            return
        await self.bridge.send_photo(chat, jpeg, None)

    async def _shell(self, chat: int, command: str) -> None:
        if not self.laptop.shell_allowed:
            await self.bridge.send(chat, f"Команды с телефона {_NOT_ALLOWED}")
            return
        if await self._need_pin(chat):
            return
        if not command:
            await self.bridge.send(chat, "Напишите так: /cmd git status")
            return
        self.bridge.spawn(self._run_shell(chat, command))

    async def terminal_line(self, chat: int, command: str) -> None:
        """Сообщение в режиме терминала. Без разрешения или ПИНа режим сам выключается: иначе команда ушла бы в модель."""
        if not self.laptop.shell_allowed or not self._pin_ok():
            self.terminal_on = False
        await self._shell(chat, command)

    async def _run_shell(self, chat: int, command: str) -> None:
        await self.bridge.typing(chat)
        running = asyncio.ensure_future(self.laptop.shell(command))
        try:
            # Долгая команда не должна выглядеть как зависший бот: через несколько секунд предлагаем её остановить.
            done, _ = await asyncio.wait({running}, timeout=_SHELL_NOTICE_AFTER)
            if not done:
                await self.bridge.send(chat, "Команда ещё выполняется, сэр.", Panel("", [[("⛔ Остановить", "tm:stop")]]).keyboard())
            result = await running
        except OSError as exc:
            await self.bridge.send(chat, f"Команда не запустилась: {type(exc).__name__}")
            return
        head = "Не успела за 15 минут, остановил." if result.timed_out else f"Код {result.code}."
        output = result.output or "Вывода нет."
        try:
            if len(output) > _TEXT_LIMIT:
                await self.bridge.send_text_file(chat, "вывод.txt", output)
                await self.bridge.send(chat, f"{head} Вывод длинный, отправил файлом. Начало:\n{output[:600]}")
            else:
                await self.bridge.send(chat, f"{head}\n{output}")
        except httpx.HTTPError as exc:
            log.info("Вывод команды не ушёл: %s", type(exc).__name__)

"""Руки и глаза Джарвис: окна, элементы интерфейса, видео на странице, снимок экрана.

Сначала Джарвис читает окно через UI Automation: это точно, бесплатно и не покидает компьютер.
Снимок экрана уходит в облако только когда человек просит посмотреть, а читать больше нечего.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from . import screen, winapi
from .uia import UiaClient, UiItem
from .winapi import WindowInfo

log = logging.getLogger("jarvis.desktop")

_ITEMS_LIMIT = 30
_NAME_LIMIT = 70
_ITEMS_FRESH_SECONDS = 180
_ROW_TOLERANCE = 60
_NAVIGATION_WAIT = 3.0
_VIDEO_PAGE_WAIT = 8.0
_OTHER_PAGE_WAIT = 2.0
_MAX_BROWSERS = 3
_LINK_MIN_NAME = 12
_KIND_ROLES = {
    "link": {"ссылка"}, "button": {"кнопка", "флажок", "переключатель"}, "input": {"поле ввода", "выпадающий список"},
    "tab": {"вкладка"}, "menu": {"пункт меню"}, "list": {"пункт списка", "строка", "пункт дерева"},
}
_VIDEO_LINK = re.compile(r"(?:youtube\.com/watch\?|youtu\.be/|youtube\.com/shorts/|vimeo\.com/\d|rutube\.ru/video/|vk\.com/video|twitch\.tv/videos/)", re.I)
_VIDEO_KEY = re.compile(r"(?:[?&]v=|youtu\.be/|shorts/|video/|videos/|vimeo\.com/)([\w-]+)")
_VIEWS_TAIL = re.compile(r"\s+[\d\s,.]+\s*(?:тыс\.?|млн\.?|млрд\.?|K|M|B)?\s*(?:просмотр\w*|views?)\b.*$", re.I)
_AUTHOR_TAIL = re.compile(r"\s+(?:автор:?|от|by)\s+\S.*$", re.I)
_DURATION_TAIL = re.compile(r"\s+(?:\d+\s+(?:час\w*|минут\w*|секунд\w*)\s*)+$", re.I)
_ADDRESS_BAR = re.compile(r"адресн|address", re.I)
_AD_LINK = re.compile(r"googleadservices|doubleclick|/pagead/|[?&]adurl=|[?&]ad_", re.I)
_BUTTON_WORD = re.compile(r"смотреть|watch|реклама|подробнее|sponsored|ad|learn more|перейти", re.I)
_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})
APP_ALIASES = {
    "телеграм": "telegram", "телега": "telegram", "дискорд": "discord", "хром": "chrome", "гугл хром": "chrome",
    "эдж": "edge", "файрфокс": "firefox", "мозилла": "firefox", "спотифай": "spotify",
    "ворд": "word", "эксель": "excel", "иксель": "excel", "павер поинт": "powerpoint", "поверпоинт": "powerpoint",
    "вс код": "visual studio code", "вскод": "visual studio code", "визуал студио код": "visual studio code",
    "vs code": "visual studio code", "vscode": "visual studio code",
    "стим": "steam", "зум": "zoom", "обс": "obs", "ватсап": "whatsapp", "вацап": "whatsapp", "капкат": "capcut",
    "курсор": "cursor", "клод": "claude", "чат гпт": "chatgpt", "чатгпт": "chatgpt", "ардуино": "arduino",
    "пейнт": "paint", "паинт": "paint", "камера": "camera", "часы": "clock", "ютуб": "youtube", "браузер": "browser",
    "тимспик": "teamspeak", "юбисофт": "ubisoft", "проводник": "explorer", "блокнот": "notepad",
    "клауд": "claude", "cloud": "claude", "вс кот": "visual studio code", "вижуал студио": "visual studio code",
}


def latin(text: str) -> str:
    return text.lower().translate(_TRANSLIT)


def clean_video_title(name: str) -> str:
    """YouTube складывает в подпись ссылки название, автора, просмотры и длительность. Вслух нужно только название."""
    title, cut = _VIEWS_TAIL.subn("", name)
    if cut:
        # Автора ищем, только когда нашлись просмотры: иначе «от» в самом названии отрезало бы его половину.
        title = _AUTHOR_TAIL.sub("", title)
    title = _DURATION_TAIL.sub("", title)
    return " ".join(title.split())[:110]


@dataclass
class Video:
    title: str
    url: str
    item: UiItem


def pick_videos(items: list[UiItem]) -> list[Video]:
    """Видео в том порядке, в каком человек видит их на странице: по рядам сверху вниз, в ряду слева направо."""
    named: dict[str, UiItem] = {}
    for item in items:
        if item.role != "ссылка" or not item.rect[2] > item.rect[0] or not _VIDEO_LINK.search(item.value):
            continue
        # Настоящий случай: реклама в выдаче с кнопкой «Смотреть» оказалась бы первым видео.
        if _AD_LINK.search(item.value) or _BUTTON_WORD.fullmatch(item.name.strip()):
            continue
        match = _VIDEO_KEY.search(item.value)
        key = match[1] if match else item.value
        # На одно видео обычно две ссылки: картинка без подписи и заголовок. Нужен заголовок.
        if key not in named or (item.name and not named[key].name):
            named[key] = item
    # Короткие ролики показываем, лишь когда обычных видео на странице нет.
    regular = {key: item for key, item in named.items() if "/shorts/" not in item.value}
    chosen = [item for item in (regular or named).values() if item.name]
    chosen.sort(key=lambda item: item.rect[1])
    rows: list[list[UiItem]] = []
    for item in chosen:
        if rows and item.rect[1] - rows[-1][0].rect[1] <= _ROW_TOLERANCE:
            rows[-1].append(item)
        else:
            rows.append([item])
    ordered = [item for row in rows for item in sorted(row, key=lambda it: it.rect[0])]
    return [Video(clean_video_title(item.name), item.value, item) for item in ordered]


def pick_links(items: list[UiItem]) -> list[UiItem]:
    """Ссылки самой страницы в порядке чтения: заголовки в выдаче поисковика, статьи. Меню и кнопки браузера не нужны."""
    ceiling = max((i.rect[3] for i in items if i.role == "поле ввода" and _ADDRESS_BAR.search(i.name)), default=0)
    seen: set[str] = set()
    links = []
    for item in sorted(items, key=lambda it: (it.rect[1], it.rect[0])):
        name = " ".join(item.name.split())
        if item.role != "ссылка" or len(name) < _LINK_MIN_NAME or item.rect[1] < ceiling or item.rect[2] <= item.rect[0]:
            continue
        key = item.value or name
        if key not in seen and not _AD_LINK.search(item.value):
            seen.add(key)
            links.append(item)
    return links


def best_window(query: str, windows: list[WindowInfo]) -> WindowInfo | None:
    """Находит окно по тому, как его назвали голосом: «телеграм», «хром», кусок заголовка."""
    query = " ".join(query.lower().split())
    if not query:
        return None
    wanted = APP_ALIASES.get(query, query)
    needles = list(dict.fromkeys((wanted, latin(wanted))))
    if wanted == "visual studio code":
        needles.append("code.exe")
    for needle in needles:
        for window in windows:
            if needle == window.exe.removesuffix(".exe") or needle == window.exe:
                return window
    for needle in needles:
        if len(needle) < 3:
            continue
        for window in windows:
            if needle in window.title.lower() or needle in window.exe:
                return window
    names = {window.exe.removesuffix(".exe"): window for window in windows}
    close = difflib.get_close_matches(needles[-1], names, n=1, cutoff=0.75)
    return names[close[0]] if close else None


class Desktop:
    def __init__(self, uia: UiaClient) -> None:
        self.uia = uia
        self.jarvis_pid: int | None = None
        self.items: list[UiItem] = []
        self.items_hwnd = 0
        self.items_at = 0.0
        self.videos_at = 0.0

    def is_jarvis(self, window: WindowInfo) -> bool:
        if window.pid == self.jarvis_pid:
            return True
        # Окно Джарвиса показывает Edge: отличить его от обычного браузера можно по заголовку без адреса.
        return window.cls == "Chrome_WidgetWin_1" and window.title.strip() == "Джарвис"

    def others(self) -> list[WindowInfo]:
        return [window for window in winapi.top_windows() if not self.is_jarvis(window)]

    def target(self) -> WindowInfo | None:
        """С каким окном работает человек: верхнее, не считая самого Джарвиса и свёрнутых."""
        for window in self.others():
            if not window.minimized:
                return window
        return None

    def target_browser(self) -> WindowInfo | None:
        return next((window for window in self.others() if window.is_browser), None)

    def jarvis_on_top(self) -> bool:
        window = winapi.foreground()
        return window is not None and self.is_jarvis(window)

    def describe_active(self) -> str:
        window = self.target()
        if window is None:
            return ""
        return f"{window.title[:90]} ({window.exe.removesuffix('.exe')})"

    def open_apps(self, limit: int = 7) -> str:
        """Что сейчас открыто, только названия программ: заголовки пишут чужие сайты, а здесь хватит и этого."""
        names: list[str] = []
        for window in self.others():
            name = window.exe.removesuffix(".exe")
            if name and name not in names:
                names.append(name)
        return ", ".join(names[:limit])

    # ---------- Видео на странице ----------

    def browsers(self) -> list[WindowInfo]:
        """Окна браузеров: сначала те, где открыт YouTube, затем по порядку наложения."""
        found = [window for window in self.others() if window.is_browser]
        return sorted(found, key=lambda window: "youtu" not in window.title.lower())

    async def _videos_in(self, window: WindowInfo, wanted: int, patient: bool) -> list[Video]:
        # Человек говорит «включи второе» сразу после «открой ютуб», пока страница ещё грузится.
        # На видеосайте ждать имеет смысл дольше: там видео точно будут.
        started = time.monotonic()
        current: WindowInfo | None = window
        found: list[Video] = []
        while current is not None:
            found = pick_videos(await self.uia.video_snapshot(window.hwnd, wanted))
            patience = _VIDEO_PAGE_WAIT if "youtu" in current.title.lower() else _OTHER_PAGE_WAIT
            if len(found) >= wanted or not patient or time.monotonic() - started >= patience:
                return found
            await asyncio.sleep(0.5)
            current = winapi.window_info(window.hwnd)
        return found

    async def videos(self, wanted: int = 8) -> tuple[WindowInfo | None, list[Video]]:
        windows = self.browsers()[:_MAX_BROWSERS]
        if not windows:
            return None, []
        # Настоящий случай: «на странице только одно видео», хотя YouTube был открыт в другом окне браузера.
        best_window_found = windows[0]
        best: list[Video] = []
        for index, window in enumerate(windows):
            found = await self._videos_in(window, wanted, patient=index == 0)
            log.info("Видео на странице: %d (%s)", len(found), window.exe)
            if len(found) >= wanted:
                return window, found
            if len(found) > len(best):
                best_window_found, best = window, found
        return best_window_found, best

    # ---------- Ссылки на странице ----------

    async def open_link(self, position: int = 1) -> str:
        windows = self.browsers()[:_MAX_BROWSERS]
        if not windows:
            return "Ошибка: браузер не открыт."
        window = windows[0]
        links = pick_links(await self.uia.snapshot(window.hwnd))
        if not links:
            return f"Ошибка: на странице «{window.title[:60]}» не вижу ссылок."
        if not 1 <= position <= len(links):
            return f"Ошибка: на странице вижу только {len(links)} ссылок."
        link = links[position - 1]
        winapi.activate(window.hwnd)
        if await self.uia.invoke(link) is None:
            await self._physical_click(window, link)
        await self._wait_title_change(window)
        return f"Открыл ссылку номер {position}: {link.name[:70]}."

    def show_jarvis(self) -> bool:
        """Окно самого Джарвиса на передний план: «Джарвис, покажись»."""
        for window in winapi.top_windows():
            if self.is_jarvis(window):
                return winapi.activate(window.hwnd)
        return False

    async def play_video(self, position: int = 1) -> str:
        window, found = await self.videos(position)
        if window is None:
            return "Ошибка: браузер не открыт."
        if not found:
            return f"Ошибка: на странице «{window.title[:60]}» не вижу видео."
        if not 1 <= position <= len(found):
            return f"Ошибка: на странице вижу только {len(found)} видео."
        video = found[position - 1]
        self.videos_at = time.monotonic()
        winapi.activate(window.hwnd)
        if await self.uia.invoke(video.item) is None:
            await self._physical_click(window, video.item)
        changed = await self._wait_title_change(window)
        if not changed:
            return f"Ошибка: нажал на «{video.title}», но страница не изменилась."
        return f"Включил видео номер {position}: {video.title}."

    async def _wait_title_change(self, window: WindowInfo) -> bool:
        deadline = time.monotonic() + _NAVIGATION_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            now = winapi.window_info(window.hwnd)
            if now is None or now.title != window.title:
                return True
        return False

    async def _physical_click(self, window: WindowInfo, item: UiItem) -> None:
        await self.uia.scroll_into_view(item)
        await asyncio.sleep(0.25)
        winapi.activate(window.hwnd)
        if item.msaa:
            x, y = item.center
        else:
            fresh = await self.uia.run(lambda: item.element.CurrentBoundingRectangle)
            x, y = (fresh.left + fresh.right) // 2, (fresh.top + fresh.bottom) // 2
        winapi.mouse_click(x, y)

    # ---------- Элементы окна ----------

    async def list_items(self, kind: str = "", query: str = "") -> tuple[WindowInfo | None, list[UiItem]]:
        window = self.target()
        if window is None:
            return None, []
        items = await self.uia.snapshot(window.hwnd)
        self.items = self._arrange(window, items, kind, query)
        self.items_hwnd = window.hwnd
        self.items_at = time.monotonic()
        return window, self.items

    def _arrange(self, window: WindowInfo, items: list[UiItem], kind: str, query: str) -> list[UiItem]:
        roles = _KIND_ROLES.get(kind)
        needle = query.lower().strip()
        unique: dict[tuple[str, str], UiItem] = {}
        for item in items:
            if not item.visible or not item.enabled or (roles and item.role not in roles):
                continue
            label = (item.name or item.value).lower()
            if not label and item.role != "поле ввода":
                continue
            if needle and needle not in label:
                continue
            unique.setdefault((item.role, label), item)
        result = list(unique.values())
        if window.is_browser:
            # Сначала страница, а кнопки самого браузера потом: люди просят нажать то, что на странице.
            ceiling = max((i.rect[3] for i in items if i.role == "поле ввода" and _ADDRESS_BAR.search(i.name)), default=0)
            result.sort(key=lambda item: item.rect[1] < ceiling)
        return result[:_ITEMS_LIMIT]

    @staticmethod
    def format_items(items: list[UiItem]) -> str:
        lines = []
        for number, item in enumerate(items, 1):
            name = item.name or item.value or "без подписи"
            lines.append(f"{number}. {item.role}: {name[:_NAME_LIMIT]}")
        return "\n".join(lines)

    def item_by_number(self, number: int) -> UiItem | None:
        if time.monotonic() - self.items_at > _ITEMS_FRESH_SECONDS or not 1 <= number <= len(self.items):
            return None
        return self.items[number - 1]

    async def click_item(self, item: UiItem) -> str:
        window = winapi.window_info(self.items_hwnd)
        if window is None:
            return "Ошибка: окно уже закрыто."
        name = (item.name or item.value or item.role)[:_NAME_LIMIT]
        if await self.uia.invoke(item) is None:
            await self._physical_click(window, item)
        await asyncio.sleep(0.6)
        after = winapi.window_info(self.items_hwnd)
        where = f" Окно сейчас: {after.title[:70]}." if after and after.title != window.title else ""
        return f"Нажал: {name}.{where}"

    async def focus_target(self) -> WindowInfo | None:
        window = self.target()
        if window is None or not await asyncio.to_thread(winapi.activate, window.hwnd):
            return None
        return window

    # ---------- Снимок экрана ----------

    async def capture(self) -> tuple[Any, WindowInfo | None]:
        target = self.target()
        on_top = self.jarvis_on_top()

        def shoot() -> Any:
            return screen.snapshot(target, on_top)

        return await asyncio.to_thread(shoot), target

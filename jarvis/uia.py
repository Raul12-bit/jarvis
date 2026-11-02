"""UI Automation: Джарвис читает, что есть в окне, и нажимает элементы как программа чтения с экрана.

Такой способ не требует ни мыши, ни снимков, ни облака. Замер на настоящем Edge и странице YouTube:
снимок дерева 70 мс, у ссылки есть её адрес, вызов Invoke открыл ровно то видео, что просили.

Все обращения к COM идут из одного отдельного потока: библиотека привязана к потоку, а зависшее
приложение не должно останавливать цикл событий, через который идёт микрофон.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from . import winapi

log = logging.getLogger("jarvis.uia")

T = TypeVar("T")

_NAME, _TYPE, _RECT, _OFFSCREEN, _ENABLED, _PASSWORD, _VALUE, _AUTOMATION_ID = 30005, 30003, 30001, 30022, 30010, 30019, 30045, 30011
_INVOKE, _VALUE_PATTERN, _TOGGLE, _SELECTION_ITEM, _EXPAND, _SCROLL_ITEM, _TEXT, _LEGACY = 10000, 10002, 10015, 10010, 10005, 10017, 10014, 10018
_TREE_DESCENDANTS = 4
_DOCUMENT = 50030
_ROLES = {
    50000: "кнопка", 50002: "флажок", 50003: "выпадающий список", 50004: "поле ввода", 50005: "ссылка",
    50007: "пункт списка", 50011: "пункт меню", 50013: "переключатель", 50019: "вкладка", 50024: "пункт дерева",
    50029: "строка", 50031: "кнопка",
}
# Дерево вкладки браузер строит лениво: сразу после первого обращения в нём лишь рамка окна.
_COM_TIMEOUT = 10.0
_SETTLE_DEADLINE = 3.5
_SETTLE_PAUSE = 0.35
_ENOUGH_ITEMS = 25


# Firefox по умолчанию не отдаёт страницу через UI Automation, но всегда отдаёт её через старый MSAA.
_MSAA_ROLES = {0x1E: "ссылка", 0x2B: "кнопка", 0x2A: "поле ввода", 0x2C: "флажок", 0x2D: "переключатель",
               0x2E: "выпадающий список", 0x22: "пункт списка", 0x25: "вкладка", 0x0C: "пункт меню", 0x24: "пункт дерева"}
_MSAA_TEXT = 0x29
_MSAA_EDIT = 0x2A
_MSAA_DOCUMENT = 0x0F
_STATE_FOCUSABLE = 0x100000
_STATE_UNAVAILABLE = 0x1
_STATE_INVISIBLE = 0x8000
_STATE_OFFSCREEN = 0x10000
_STATE_PROTECTED = 0x20000000
_OBJID_CLIENT = 0xFFFFFFFC
_MSAA_MAX_NODES = 9000
_MSAA_MAX_SECONDS = 5.0
_MSAA_MAX_DEPTH = 60
_SELFLAG_TAKEFOCUS = 1
_MSAA_APPS = {"firefox.exe", "zen.exe", "waterfox.exe", "librewolf.exe"}


@dataclass
class UiItem:
    role: str
    name: str
    value: str
    rect: tuple[int, int, int, int]
    offscreen: bool
    enabled: bool
    password: bool
    element: Any = None
    msaa: bool = False

    @property
    def visible(self) -> bool:
        left, top, right, bottom = self.rect
        return not self.offscreen and right > left and bottom > top

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


def _uses_msaa(hwnd: int) -> bool:
    window = winapi.window_info(hwnd)
    return window is not None and window.exe in _MSAA_APPS


def _prepare_com() -> None:
    # comtypes инициализирует COM прямо при импорте: нужен многопоточный режим, а не режим окна.
    sys.coinit_flags = 0  # type: ignore[attr-defined]
    import comtypes  # noqa: F401


class UiaClient:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-com", initializer=_prepare_com)
        self._automation: Any = None
        self._accessibility: Any = None
        self._module: Any = None
        self._request: Any = None
        self._condition: Any = None
        self._warm: set[int] = set()

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(self._pool, fn, *args), _COM_TIMEOUT)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _ensure(self) -> None:
        if self._automation is not None:
            return
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as module

        self._module = module
        # Настоящий журнал: у старого класса CUIAutomation нет IUIAutomation2, и таймауты молча не применялись.
        automation = comtypes.client.CreateObject(module.CUIAutomation8, interface=module.IUIAutomation)
        try:
            # Зависшее приложение не должно держать нас 20 секунд, как по умолчанию.
            second = automation.QueryInterface(module.IUIAutomation2)
            second.ConnectionTimeout = 2000
            second.TransactionTimeout = 4000
        except comtypes.COMError:
            log.info("Таймауты UI Automation недоступны")
        request = automation.CreateCacheRequest()
        for prop in (_NAME, _TYPE, _RECT, _OFFSCREEN, _ENABLED, _PASSWORD, _VALUE):
            request.AddProperty(prop)
        conditions = [automation.CreatePropertyCondition(_TYPE, kind) for kind in _ROLES]
        self._condition = automation.CreateOrConditionFromArray(conditions)
        self._request = request
        self._automation = automation

    # ---------- Вызывается только в потоке COM ----------

    def _find(self, hwnd: int) -> list[UiItem]:
        root = self._automation.ElementFromHandle(hwnd)
        found = root.FindAllBuildCache(_TREE_DESCENDANTS, self._condition, self._request)
        items: list[UiItem] = []
        for index in range(found.Length):
            element = found.GetElement(index)
            rect = element.CachedBoundingRectangle
            value = element.GetCachedPropertyValue(_VALUE)
            items.append(UiItem(
                role=_ROLES.get(element.CachedControlType, "элемент"),
                name=(element.CachedName or "").strip(),
                value=value if isinstance(value, str) else "",
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                offscreen=bool(element.CachedIsOffscreen),
                enabled=bool(element.CachedIsEnabled),
                password=bool(element.CachedIsPassword),
                element=element,
            ))
        return items

    def _msaa_root(self, hwnd: int) -> Any:
        import comtypes.client

        if self._accessibility is None:
            comtypes.client.GetModule("oleacc.dll")
            from comtypes.gen import Accessibility

            self._accessibility = Accessibility
        pointer = ctypes.POINTER(self._accessibility.IAccessible)()
        ctypes.oledll.oleacc.AccessibleObjectFromWindow(
            ctypes.wintypes.HWND(hwnd), ctypes.c_ulong(_OBJID_CLIENT),
            ctypes.byref(self._accessibility.IAccessible._iid_), ctypes.byref(pointer),
        )
        return pointer

    def _msaa_children(self, node: Any) -> list[Any]:
        from comtypes.automation import VARIANT

        count = node.accChildCount
        if not count:
            return []
        array = (VARIANT * count)()
        obtained = ctypes.c_long()
        ctypes.oledll.oleacc.AccessibleChildren(node, 0, count, array, ctypes.byref(obtained))
        children = []
        for index in range(obtained.value):
            value = array[index].value
            # Простые элементы приходят номером, у них нет своего объекта: нам такие не нужны.
            if not isinstance(value, int) and value is not None:
                children.append(value.QueryInterface(self._accessibility.IAccessible))
        return children

    def _walk_msaa(self, hwnd: int, want_text: bool = False, limit: int = 6000, enough_links: int = 0,
                   link_needle: str = "") -> tuple[list[UiItem], list[str]]:
        """Обход дерева с ограничениями: на тяжёлой странице важнее уложиться в секунды, чем дойти до конца.

        enough_links останавливает обход, как только нашлось столько подходящих ссылок: страница YouTube
        огромна, а человеку обычно нужно одно из первых видео."""
        root = self._msaa_root(hwnd)
        items: list[UiItem] = []
        text: list[str] = []
        used = 0
        visited = 0
        links = 0
        deadline = time.monotonic() + _MSAA_MAX_SECONDS
        stack: list[tuple[Any, int]] = [(root, 0)]
        while stack and visited < _MSAA_MAX_NODES and time.monotonic() < deadline:
            if enough_links and links >= enough_links:
                break
            node, depth = stack.pop()
            visited += 1
            try:
                role = node.accRole(0)
                state = node.accState(0)
                if state & _STATE_INVISIBLE and role != _MSAA_DOCUMENT:
                    continue
                # Firefox обозначает одной ролью и обычный текст, и поле ввода: различаются только фокусом.
                plain_text = role == _MSAA_TEXT or (role == _MSAA_EDIT and not state & _STATE_FOCUSABLE)
                if plain_text:
                    if want_text and not state & _STATE_OFFSCREEN and used < limit:
                        name = (node.accName(0) or "").strip()
                        if name:
                            text.append(name)
                            used += len(name) + 1
                elif role in _MSAA_ROLES:
                    x, y, width, height = node.accLocation(0)
                    items.append(UiItem(
                        role=_MSAA_ROLES[role], name=(node.accName(0) or "").strip(), value=node.accValue(0) or "",
                        rect=(x, y, x + width, y + height), offscreen=bool(state & _STATE_OFFSCREEN),
                        enabled=not state & _STATE_UNAVAILABLE, password=bool(state & _STATE_PROTECTED),
                        element=node, msaa=True,
                    ))
                    if link_needle and link_needle in items[-1].value:
                        links += 1
                if depth < _MSAA_MAX_DEPTH:
                    stack.extend((child, depth + 1) for child in reversed(self._msaa_children(node)))
            except OSError as exc:  # COMError тоже наследует Exception, но ctypes бросает OSError
                log.debug("Элемент MSAA недоступен: %s", type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 — элемент мог исчезнуть прямо во время обхода
                log.debug("Элемент MSAA недоступен: %s", type(exc).__name__)
        return items, text

    def _snapshot_msaa(self, hwnd: int, settle: bool, enough_links: int = 0, link_needle: str = "") -> list[UiItem]:
        # При первом обращении Firefox ещё только включает поддержку: дерево появляется через секунду-другую.
        deadline = time.monotonic() + _SETTLE_DEADLINE
        while True:
            items = self._walk_msaa(hwnd, enough_links=enough_links, link_needle=link_needle)[0]
            if items or not settle or time.monotonic() >= deadline:
                return items
            time.sleep(_SETTLE_PAUSE)

    def video_snapshot_sync(self, hwnd: int, wanted: int) -> list[UiItem]:
        """Для поиска видео: в Firefox читает дерево только до нужного ролика, в остальных браузерах всё сразу."""
        self._ensure()
        if _uses_msaa(hwnd):
            # На видео обычно две ссылки (картинка и заголовок), плюс запас на порядок в сетке.
            return self._snapshot_msaa(hwnd, True, enough_links=(wanted + 4) * 2, link_needle="/watch")
        return self.snapshot_sync(hwnd)

    def snapshot_sync(self, hwnd: int, settle: bool = True) -> list[UiItem]:
        self._ensure()
        if _uses_msaa(hwnd):
            return self._snapshot_msaa(hwnd, settle)
        items = self._find(hwnd)
        if not settle:
            return items
        if hwnd in self._warm and len(items) >= _ENOUGH_ITEMS:
            return items
        deadline = time.monotonic() + _SETTLE_DEADLINE
        previous = len(items)
        while time.monotonic() < deadline:
            time.sleep(_SETTLE_PAUSE)
            items = self._find(hwnd)
            if len(items) == previous and len(items) >= _ENOUGH_ITEMS:
                break
            previous = len(items)
        self._warm.add(hwnd)
        return items

    def invoke_sync(self, item: UiItem) -> str | None:
        """Нажимает элемент. Возвращает способ либо None, если программно нажать не получилось."""
        self._ensure()
        if item.msaa:
            try:
                item.element.accDoDefaultAction(0)
                return "DoDefaultAction"
            except Exception as exc:  # noqa: BLE001
                log.debug("Действие MSAA не сработало: %s", type(exc).__name__)
                return None
        module = self._module
        element = item.element
        for pattern_id, interface, action in (
            (_INVOKE, module.IUIAutomationInvokePattern, "Invoke"),
            (_TOGGLE, module.IUIAutomationTogglePattern, "Toggle"),
            (_SELECTION_ITEM, module.IUIAutomationSelectionItemPattern, "Select"),
            (_EXPAND, module.IUIAutomationExpandCollapsePattern, "Expand"),
            (_LEGACY, module.IUIAutomationLegacyIAccessiblePattern, "DoDefaultAction"),
        ):
            try:
                raw = element.GetCurrentPattern(pattern_id)
                if raw:
                    getattr(raw.QueryInterface(interface), action)()
                    return action
            except Exception as exc:  # noqa: BLE001 — COMError и отсутствие паттерна: пробуем следующий
                log.debug("Способ %s не сработал: %s", action, type(exc).__name__)
        return None

    def current_name_sync(self, item: UiItem) -> str:
        """Подпись прямо сейчас, а не на момент снимка. Пустая строка: элемента больше нет."""
        try:
            return (item.element.accName(0) if item.msaa else item.element.CurrentName) or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("Подпись элемента недоступна: %s", type(exc).__name__)
            return ""

    def scroll_into_view_sync(self, item: UiItem) -> None:
        if item.msaa:
            return
        try:
            raw = item.element.GetCurrentPattern(_SCROLL_ITEM)
            if raw:
                raw.QueryInterface(self._module.IUIAutomationScrollItemPattern).ScrollIntoView()
        except Exception as exc:  # noqa: BLE001
            log.debug("Прокрутка к элементу не сработала: %s", type(exc).__name__)

    def set_value_sync(self, item: UiItem, text: str) -> bool:
        try:
            raw = item.element.GetCurrentPattern(_VALUE_PATTERN)
            if not raw:
                return False
            raw.QueryInterface(self._module.IUIAutomationValuePattern).SetValue(text)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("Запись значения не сработала: %s", type(exc).__name__)
            return False

    def focus_sync(self, item: UiItem) -> bool:
        try:
            if item.msaa:
                item.element.accSelect(_SELFLAG_TAKEFOCUS, 0)
            else:
                item.element.SetFocus()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("Фокус не поставлен: %s", type(exc).__name__)
            return False

    def focused_is_password_sync(self) -> bool:
        self._ensure()
        try:
            return bool(self._automation.GetFocusedElement().CurrentIsPassword)
        except Exception:  # noqa: BLE001 — не знаем, значит, считаем поле обычным: проверка не единственная
            return False

    def read_text_sync(self, hwnd: int, limit: int = 6000) -> str:
        """Текст страницы или документа. Сначала документ целиком, иначе подписи элементов окна."""
        self._ensure()
        if _uses_msaa(hwnd):
            deadline = time.monotonic() + _SETTLE_DEADLINE
            while True:
                lines = self._walk_msaa(hwnd, want_text=True, limit=limit)[1]
                if lines or time.monotonic() >= deadline:
                    return "\n".join(lines)
                time.sleep(_SETTLE_PAUSE)
        automation = self._automation
        module = self._module
        root = automation.ElementFromHandle(hwnd)
        document = root.FindFirst(_TREE_DESCENDANTS, automation.CreatePropertyCondition(_TYPE, _DOCUMENT))
        if document:
            try:
                raw = document.GetCurrentPattern(_TEXT)
                if raw:
                    text = raw.QueryInterface(module.IUIAutomationTextPattern).DocumentRange.GetText(limit)
                    if text and text.strip():
                        return text
            except Exception as exc:  # noqa: BLE001
                log.debug("Текст документа недоступен: %s", type(exc).__name__)
        found = root.FindAllBuildCache(_TREE_DESCENDANTS, automation.CreatePropertyCondition(_TYPE, 50020), self._request)
        labels: list[str] = []
        used = 0
        for index in range(found.Length):
            element = found.GetElement(index)
            name = (element.CachedName or "").strip()
            if name and not element.CachedIsOffscreen:
                labels.append(name)
                used += len(name) + 1
                if used > limit:
                    break
        return "\n".join(labels)

    # ---------- Для кода на asyncio ----------

    async def snapshot(self, hwnd: int, settle: bool = True) -> list[UiItem]:
        return await self.run(self.snapshot_sync, hwnd, settle)

    async def video_snapshot(self, hwnd: int, wanted: int) -> list[UiItem]:
        return await self.run(self.video_snapshot_sync, hwnd, wanted)

    async def invoke(self, item: UiItem) -> str | None:
        return await self.run(self.invoke_sync, item)

    async def current_name(self, item: UiItem) -> str:
        return await self.run(self.current_name_sync, item)

    async def scroll_into_view(self, item: UiItem) -> None:
        await self.run(self.scroll_into_view_sync, item)

    async def set_value(self, item: UiItem, text: str) -> bool:
        return await self.run(self.set_value_sync, item, text)

    async def focus(self, item: UiItem) -> bool:
        return await self.run(self.focus_sync, item)

    async def focused_is_password(self) -> bool:
        return await self.run(self.focused_is_password_sync)

    async def read_text(self, hwnd: int) -> str:
        return await self.run(self.read_text_sync, hwnd)

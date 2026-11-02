"""Тонкая обёртка над Win32 через ctypes: окна, клавиатура, мышь, буфер обмена, простой пользователя.

Вызовы здесь занимают микросекунды, поэтому их можно делать прямо из цикла событий.
Окна процессов с правами администратора Windows защищает: Джарвис не сможет их закрыть или нажать в них.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("jarvis.winapi")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shell32 = ctypes.WinDLL("shell32")

ULONG_PTR = ctypes.c_size_t
EnumWindowsProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KeybdInput(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeybdInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _InputUnion)]


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsWindow.argtypes = [w.HWND]
user32.IsIconic.argtypes = [w.HWND]
user32.IsZoomed.argtypes = [w.HWND]
user32.GetWindow.argtypes = [w.HWND, w.UINT]
user32.GetWindow.restype = w.HWND
user32.GetForegroundWindow.restype = w.HWND
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(_Input), ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.EnumChildWindows.argtypes = [w.HWND, EnumWindowsProc, w.LPARAM]
user32.EnumWindows.argtypes = [EnumWindowsProc, w.LPARAM]
user32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
user32.MonitorFromWindow.restype = w.HMONITOR
user32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(_MonitorInfo)]
user32.MonitorFromPoint.argtypes = [w.POINT, w.DWORD]
user32.MonitorFromPoint.restype = w.HMONITOR
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
user32.OpenClipboard.argtypes = [w.HWND]
user32.GetClipboardData.argtypes = [w.UINT]
user32.GetClipboardData.restype = w.HANDLE
user32.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
user32.SetClipboardData.restype = w.HANDLE
user32.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
user32.OpenInputDesktop.restype = w.HANDLE
user32.CloseDesktop.argtypes = [w.HANDLE]
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = w.HGLOBAL
kernel32.GlobalLock.argtypes = [w.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
kernel32.GlobalFree.argtypes = [w.HGLOBAL]
kernel32.GlobalFree.restype = w.HGLOBAL
dwmapi.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]

_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x80
_WS_EX_APPWINDOW = 0x40000
_DWMWA_CLOAKED = 14
_WM_CLOSE = 0x0010
_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_SW_RESTORE = 9
_PROCESS_QUERY_LIMITED = 0x1000
_MONITOR_DEFAULTTONEAREST = 2
_MONITOR_DEFAULTTOPRIMARY = 1
_SW_HIDE = 0
_SW_SHOW = 5
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_KEYUP = 0x0002
_KEY_UNICODE = 0x0004
_KEY_EXTENDED = 0x0001
_MOUSE_LEFTDOWN = 0x0002
_MOUSE_LEFTUP = 0x0004
_MOUSE_RIGHTDOWN = 0x0008
_MOUSE_RIGHTUP = 0x0010
_MOUSE_MIDDLEDOWN = 0x0020
_MOUSE_MIDDLEUP = 0x0040
_MOUSE_WHEEL = 0x0800
_WHEEL_NOTCH = 120
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_DESKTOP_SWITCHDESKTOP = 0x0100

# Служебные окна оболочки: для человека это не «окно», и закрывать их нельзя.
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow",
                  "XamlExplorerHostIslandWindow", "TopLevelWindowForOverflowXamlIsland"}
BROWSER_EXES = {"msedge.exe", "chrome.exe", "firefox.exe", "browser.exe", "opera.exe", "brave.exe", "vivaldi.exe",
                "arc.exe", "zen.exe"}
_TITLE_LIMIT = 512


def enable_dpi_awareness() -> None:
    """Иначе на экранах с масштабом Windows подсовывает пересчитанные координаты и снимки становятся мыльными."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    cls: str
    pid: int
    exe: str
    path: str
    minimized: bool
    maximized: bool
    rect: tuple[int, int, int, int]

    @property
    def is_browser(self) -> bool:
        return self.exe in BROWSER_EXES


def process_path(pid: int) -> str:
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_text(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(_TITLE_LIMIT)
    user32.GetWindowTextW(hwnd, buffer, _TITLE_LIMIT)
    # Edge вставляет в название невидимый символ: «Microsoft​ Edge».
    return buffer.value.replace("​", "")


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buffer, 128)
    return buffer.value


def _window_pid(hwnd: int) -> int:
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _uwp_pid(hwnd: int, host_pid: int) -> int:
    """Приложения из Store живут в окне-рамке чужого процесса: настоящий процесс у дочернего окна."""
    found = [host_pid]

    def check(child: int, _: int) -> bool:
        pid = _window_pid(child)
        if pid and pid != host_pid:
            found[0] = pid
            return False
        return True

    user32.EnumChildWindows(hwnd, EnumWindowsProc(check), 0)
    return found[0]


def window_info(hwnd: int) -> WindowInfo | None:
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    cls = _class_name(hwnd)
    pid = _window_pid(hwnd)
    if cls == "ApplicationFrameWindow":
        pid = _uwp_pid(hwnd, pid)
    path = process_path(pid)
    rect = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return WindowInfo(
        hwnd=hwnd, title=_window_text(hwnd), cls=cls, pid=pid,
        exe=os.path.basename(path).lower(), path=path,
        minimized=bool(user32.IsIconic(hwnd)), maximized=bool(user32.IsZoomed(hwnd)),
        rect=(rect.left, rect.top, rect.right, rect.bottom),
    )


def _is_app_window(hwnd: int) -> bool:
    if not user32.IsWindowVisible(hwnd) or _class_name(hwnd) in _SHELL_CLASSES:
        return False
    exstyle = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    if exstyle & _WS_EX_TOOLWINDOW and not exstyle & _WS_EX_APPWINDOW:
        return False
    if user32.GetWindow(hwnd, _GW_OWNER) and not exstyle & _WS_EX_APPWINDOW:
        return False
    cloaked = w.DWORD()
    # Спрятанные окна фоновых приложений Store и других виртуальных рабочих столов.
    if dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
        return False
    return bool(_window_text(hwnd).strip())


def top_windows() -> list[WindowInfo]:
    """Окна приложений в порядке наложения: самое верхнее первое."""
    handles: list[int] = []

    def collect(hwnd: int, _: int) -> bool:
        if _is_app_window(hwnd):
            handles.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(collect), 0)
    return [info for info in map(window_info, handles) if info is not None]


def foreground() -> WindowInfo | None:
    return window_info(user32.GetForegroundWindow())


def idle_seconds() -> float:
    info = _LastInputInfo(cbSize=ctypes.sizeof(_LastInputInfo))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # Счётчик 32-битный и обнуляется раз в 49 дней: маска даёт верную разницу и в этот момент.
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def session_locked() -> bool:
    desktop = user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
    if not desktop:
        return True
    user32.CloseDesktop(desktop)
    return False


def windows_of_class(cls: str) -> list[tuple[int, str]]:
    """Все окна верхнего уровня этого класса с заголовком, в том числе скрытые. top_windows() скрытых не видит."""
    found: list[tuple[int, str]] = []

    def collect(hwnd: int, _: int) -> bool:
        if _class_name(hwnd) == cls:
            title = _window_text(hwnd)
            if title.strip():
                found.append((hwnd, title))
        return True

    user32.EnumWindows(EnumWindowsProc(collect), 0)
    return found


def is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd)) and not user32.IsIconic(hwnd)


def fills_monitor(window: WindowInfo) -> bool:
    """Окно на весь экран: так выглядят игры в режиме «без рамки» и плеер в полноэкранном режиме."""
    monitor = user32.MonitorFromWindow(window.hwnd, _MONITOR_DEFAULTTONEAREST)
    info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return False
    area = info.rcMonitor
    left, top, right, bottom = window.rect
    return left <= area.left and top <= area.top and right >= area.right and bottom >= area.bottom


def monitor_rect(hwnd: int) -> tuple[int, int, int, int]:
    monitor = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        area = info.rcMonitor
        return area.left, area.top, area.right, area.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _work_rect(monitor: int) -> tuple[int, int, int, int]:
    info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        area = info.rcWork
        return area.left, area.top, area.right, area.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def primary_work_area() -> tuple[int, int, int, int]:
    return _work_rect(user32.MonitorFromPoint(w.POINT(0, 0), _MONITOR_DEFAULTTOPRIMARY))


def window_work_area(hwnd: int) -> tuple[int, int, int, int]:
    return _work_rect(user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST))


def point_on_monitor(x: int, y: int) -> bool:
    return bool(user32.MonitorFromPoint(w.POINT(x, y), 0))


def system_dpi() -> int:
    try:
        return int(user32.GetDpiForSystem()) or 96
    except (AttributeError, OSError):
        return 96


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """Все мониторы разом: левый верхний угол может быть и в минусе."""
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    return left, top, left + user32.GetSystemMetrics(78), top + user32.GetSystemMetrics(79)


# ---------- Управление окнами ----------


def _send(inputs: list[_Input]) -> int:
    array = (_Input * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), array, ctypes.sizeof(_Input))


def activate(hwnd: int) -> bool:
    """Выводит окно на передний план. Windows разрешает это лишь тому, кто дал последний ввод,
    поэтому сначала отправляем пустое событие мыши: оно ничего не двигает и ничего не нажимает."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True
    _send([_Input(type=_INPUT_MOUSE)])
    user32.SetForegroundWindow(hwnd)
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return False


def show_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, _SW_RESTORE if user32.IsIconic(hwnd) else _SW_SHOW)


def hide_window(hwnd: int) -> None:
    user32.ShowWindow(hwnd, _SW_HIDE)


def move_window(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    if user32.IsZoomed(hwnd):
        # Развёрнутое окно Windows двигать не даёт: без этого прикрепление к половине экрана не сработает.
        user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetWindowPos(hwnd, None, x, y, width, height, _SWP_NOZORDER | _SWP_NOACTIVATE)


def snap_window(hwnd: int, side: str) -> bool:
    """Половина или четверть экрана, на котором стоит окно."""
    left, top, right, bottom = window_work_area(hwnd)
    width, height = right - left, bottom - top
    half_w, half_h = width // 2, height // 2
    places = {
        "left": (left, top, half_w, height), "right": (left + half_w, top, width - half_w, height),
        "top": (left, top, width, half_h), "bottom": (left, top + half_h, width, height - half_h),
        "top_left": (left, top, half_w, half_h), "top_right": (left + half_w, top, width - half_w, half_h),
        "bottom_left": (left, top + half_h, half_w, height - half_h),
        "bottom_right": (left + half_w, top + half_h, width - half_w, height - half_h),
    }
    place = places.get(side)
    if place is None:
        return False
    move_window(hwnd, *place)
    return True


def set_always_on_top(hwnd: int, on: bool) -> None:
    user32.SetWindowPos(hwnd, w.HWND(-1 if on else -2), 0, 0, 0, 0, 0x0001 | 0x0002 | _SWP_NOACTIVATE)


def minimize(hwnd: int) -> None:
    user32.ShowWindow(hwnd, _SW_MINIMIZE)


def maximize(hwnd: int) -> None:
    user32.ShowWindow(hwnd, _SW_MAXIMIZE)


def restore(hwnd: int) -> None:
    user32.ShowWindow(hwnd, _SW_RESTORE)


def close_window(hwnd: int) -> bool:
    """Вежливо просит окно закрыться: приложение успеет предложить сохранение. Процесс не убиваем."""
    return bool(user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0))


def window_exists(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


# ---------- Клавиатура и мышь ----------

_NAMED_KEYS = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "pgup": 0x21, "pgdn": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "printscreen": 0x2C,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1,
    ",": 0xBC, ".": 0xBE, "-": 0xBD, "=": 0xBB, "/": 0xBF, ";": 0xBA, "[": 0xDB, "]": 0xDD,
}
_RU_KEYS = {"ввод": "enter", "энтер": "enter", "пробел": "space", "вверх": "up", "вниз": "down", "влево": "left",
            "вправо": "right", "контрол": "ctrl", "шифт": "shift", "таб": "tab", "эскейп": "esc", "вин": "win"}
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B}


def parse_combo(combo: str) -> list[int] | None:
    """«ctrl+shift+t» в коды клавиш. None, если какой-то клавиши не существует."""
    codes: list[int] = []
    for raw in str(combo).lower().replace(" ", "").split("+"):
        name = _RU_KEYS.get(raw, raw)
        if name in _NAMED_KEYS:
            codes.append(_NAMED_KEYS[name])
        elif len(name) == 1 and ("a" <= name <= "z" or "0" <= name <= "9"):
            codes.append(ord(name.upper()))
        elif name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
            codes.append(0x6F + int(name[1:]))
        else:
            return None
    return codes or None


def _key_event(code: int, up: bool) -> _Input:
    flags = (_KEYUP if up else 0) | (_KEY_EXTENDED if code in _EXTENDED else 0)
    return _Input(type=_INPUT_KEYBOARD, u=_InputUnion(ki=_KeybdInput(wVk=code, dwFlags=flags)))


def send_combo(codes: list[int], repeat: int = 1) -> None:
    for _ in range(max(1, repeat)):
        _send([_key_event(code, False) for code in codes] + [_key_event(code, True) for code in reversed(codes)])
        time.sleep(0.02)


def send_text(text: str) -> None:
    """Печатает кодами символов: работает при любой раскладке, а буфер обмена не трогает."""
    events: list[_Input] = []
    for char in text:
        if char == "\n":
            events += [_key_event(0x0D, False), _key_event(0x0D, True)]
            continue
        units = char.encode("utf-16-le")
        for index in range(0, len(units), 2):
            unit = int.from_bytes(units[index:index + 2], "little")
            events.append(_Input(type=_INPUT_KEYBOARD, u=_InputUnion(ki=_KeybdInput(wScan=unit, dwFlags=_KEY_UNICODE))))
            events.append(_Input(type=_INPUT_KEYBOARD, u=_InputUnion(ki=_KeybdInput(wScan=unit, dwFlags=_KEY_UNICODE | _KEYUP))))
    # Порциями: слишком длинная очередь событий теряет символы в медленных приложениях.
    for start in range(0, len(events), 60):
        _send(events[start:start + 60])
        time.sleep(0.01)


def _mouse_event(flags: int, data: int = 0) -> _Input:
    return _Input(type=_INPUT_MOUSE, u=_InputUnion(mi=_MouseInput(mouseData=data, dwFlags=flags)))


def mouse_click(x: int, y: int, button: str = "left") -> None:
    user32.SetCursorPos(int(x), int(y))
    mouse_press(button)


def mouse_press(button: str = "left") -> None:
    down, up = {"right": (_MOUSE_RIGHTDOWN, _MOUSE_RIGHTUP), "middle": (_MOUSE_MIDDLEDOWN, _MOUSE_MIDDLEUP)}.get(button, (_MOUSE_LEFTDOWN, _MOUSE_LEFTUP))
    _send([_mouse_event(down), _mouse_event(up)])


def cursor_position() -> tuple[int, int]:
    point = w.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def mouse_move_to(x: int, y: int) -> tuple[int, int]:
    """Курсор на точку, не дальше границ всех экранов: иначе Windows сама поставит его не туда, куда просили."""
    left, top, right, bottom = virtual_screen_rect()
    user32.SetCursorPos(max(left, min(right - 1, int(x))), max(top, min(bottom - 1, int(y))))
    return cursor_position()


def mouse_move_by(dx: int, dy: int) -> tuple[int, int]:
    x, y = cursor_position()
    return mouse_move_to(x + dx, y + dy)


def mouse_scroll(notches: int) -> None:
    """Положительное вверх. Поле беззнаковое, а прокрутка вниз отрицательная: Windows ждёт её как дополнительный код."""
    _send([_mouse_event(_MOUSE_WHEEL, (notches * _WHEEL_NOTCH) & 0xFFFFFFFF)])


def set_keep_awake(enabled: bool) -> None:
    """Не даёт ноутбуку уснуть, пока Джарвис запущен: иначе с телефона до него не достучаться.
    Действует на поток, поэтому вызывается из основного потока программы. Закрытую крышку это не отменяет."""
    continuous, system_required = 0x80000000, 0x00000001
    kernel32.SetThreadExecutionState(continuous | system_required if enabled else continuous)


# ---------- Буфер обмена ----------


def _open_clipboard() -> bool:
    # Буфер могут держать другие приложения, обычно считаные миллисекунды.
    for _ in range(10):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def get_clipboard() -> str | None:
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard(text: str) -> bool:
    data = text.encode("utf-16-le") + b"\0\0"
    handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not _open_clipboard():
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if user32.SetClipboardData(_CF_UNICODETEXT, handle):
            return True  # Теперь памятью владеет система.
        kernel32.GlobalFree(handle)
        return False
    finally:
        user32.CloseClipboard()

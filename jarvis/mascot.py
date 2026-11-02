"""Живой талисман поверх всех окон: когда большое окно скрыто, это и есть Джарвис.

Окно настоящее, без браузера: слоистое окно Windows с попиксельной прозрачностью. Браузер здесь не подошёл бы: его окно
не бывает прозрачным, и ради маленькой фигурки пришлось бы держать ещё один процесс рендера. Здесь кадр собирается за 2 мс.

Окно не забирает фокус и не мешает кликать: прозрачным для мыши оно становится всякий раз, когда курсор не над телом.
Снимки экрана его не видят (SetWindowDisplayAffinity): модели зрения незачем описывать саму Джарвис.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import logging
import math
import random
import threading
import time
from typing import Callable

from . import winapi
from .mascot_art import _CENTER, LOGICAL_SIZE, PALETTE, MascotArt, Pose, premultiplied_bgra, render_bubble

log = logging.getLogger("jarvis.mascot")

SIZES = {"small": 0.72, "medium": 0.9, "large": 1.15}
_SIZE_NAMES = {"small": "маленький", "medium": "средний", "large": "большой"}
DOZE_AFTER = 420.0  # человека нет семь минут: талисман задрёмывает
_EMOTES = {"happy": 2.0, "laugh": 2.4, "sad": 2.6, "surprise": 0.9, "wave": 1.8, "alert": 4.0, "yawn": 1.9}
_STATE_COLORS = {"sleep": "idle", "listening": "listening", "hearing": "hearing", "thinking": "thinking",
                 "speaking": "speaking", "muted": "muted"}
_EMOTE_COLORS = {"happy": "happy", "laugh": "happy", "sad": "error", "alert": "alert"}
_RING_SPEED = {"sleep": 0.38, "muted": 0.12, "listening": 0.75, "hearing": 1.7, "thinking": 2.6, "speaking": 1.25}


class MascotMind:
    """Характер: что талисман делает в эту долю секунды. Без окон и настоящего времени, поэтому проверяется тестами."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.state = "sleep"
        self.mic_on = True
        self.dragging = False
        self.now = 0.0
        self.level = 0.0
        self.level_at = -10.0
        self.color = [float(c) for c in PALETTE["idle"]]
        self.glow = 0.4
        self.gaze = [0.0, 0.0]
        self.gaze_target = [0.0, 0.0]
        self.next_glance = 1.2
        self.next_blink = 1.8
        self.blink_left = 0.0
        self.breath = self.rng.random() * math.tau
        self.ring = self.rng.random() * math.tau
        self.orbit = self.waves = self.zzz = self.sparkle = 0.0
        self.stretch = self.stretch_v = 0.0
        self.antenna = self.antenna_v = 0.0
        self.hop_left = 0.0
        self.hop_length = 0.5
        self.hop_height = 0.0
        self.mouth = 0.0
        self.emote_name = ""
        self.emote_until = 0.0
        self.next_yawn = 90.0 + self.rng.random() * 120
        self.dozing = False

    # ---------- События ----------

    def set_state(self, state: str) -> None:
        if state == self.state:
            return
        previous, self.state = self.state, state
        if state == "hearing":
            self.hop(0.34, 6.0)
        elif state == "listening" and previous in ("sleep", "muted"):
            self.hop(0.3, 4.0)

    def set_level(self, value: float) -> None:
        self.level = max(0.0, min(1.0, value))
        self.level_at = self.now

    def hop(self, seconds: float = 0.42, height: float = 9.0) -> None:
        self.hop_length = self.hop_left = seconds
        self.hop_height = height
        self.stretch_v += 1.5

    def emote(self, name: str) -> None:
        length = _EMOTES.get(name)
        if length is None:
            return
        self.emote_name = name
        self.emote_until = self.now + length
        if name in ("happy", "laugh", "wave"):
            self.hop(0.42, 9.0)
        elif name == "alert":
            self.hop(0.34, 7.0)
        elif name == "surprise":
            self.stretch_v += 2.4
            self.antenna_v += 8.0

    def _current_emote(self) -> str:
        return self.emote_name if self.now < self.emote_until else ""

    # ---------- Кадр ----------

    def tick(self, dt: float, cursor: tuple[float, float] = (0.0, 0.0), cursor_moved: bool = False, near: bool = False,
             user_idle: float = 0.0, drag_speed: float = 0.0) -> Pose:
        self.now += dt
        emote = self._current_emote()
        was_dozing = self.dozing
        self.dozing = self.state == "sleep" and user_idle >= DOZE_AFTER and not emote and not near and not self.dragging
        if was_dozing and not self.dozing:
            self.emote("surprise")
            emote = "surprise"
        self._maybe_yawn(near, emote)
        emote = self._current_emote()
        self._follow_color(emote, dt)
        self._move_gaze(dt, cursor, cursor_moved, emote)
        self._blink(dt)
        self._springs(dt, drag_speed)
        self._voice(dt, emote)
        self.ring += _RING_SPEED.get(self.state, 0.4) * (0.35 if self.dozing else 1.0) * (1 + self.mouth * 0.7) * dt
        self.orbit = (self.orbit + dt * 0.62) % 1.0
        self.waves = (self.waves + dt * 0.95) % 1.0
        self.zzz = (self.zzz + dt * 0.2) % 1.0
        self.sparkle = (self.sparkle + dt * 0.8) % 1.0
        breath_speed = 0.7 if self.dozing else 1.9 if self.state == "speaking" else 1.35
        self.breath += breath_speed * dt
        return self._pose(emote, drag_speed)

    def _maybe_yawn(self, near: bool, emote: str) -> None:
        if self.state != "sleep" or emote or near or self.dozing or self.now < self.next_yawn:
            return
        self.next_yawn = self.now + 90.0 + self.rng.random() * 170
        self.emote("yawn")

    def _follow_color(self, emote: str, dt: float) -> None:
        if self.dozing:
            target = (70, 130, 178)
        else:
            name = _EMOTE_COLORS.get(emote) or _STATE_COLORS.get(self.state, "idle")
            target = PALETTE[name]
        rate = min(1.0, dt * 6.0)
        for index in range(3):
            self.color[index] += (target[index] - self.color[index]) * rate

    def _move_gaze(self, dt: float, cursor: tuple[float, float], cursor_moved: bool, emote: str) -> None:
        if self.state == "thinking":
            self.gaze_target = [0.55 * math.sin(self.now * 1.1), -0.85]
        elif self.state in ("speaking", "hearing"):
            self.gaze_target = [cursor[0] * 0.25, cursor[1] * 0.15]
        elif emote == "sad":
            self.gaze_target = [0.0, 0.7]
        elif cursor_moved:
            self.gaze_target = [cursor[0], cursor[1]]
            self.next_glance = self.now + 2.2 + self.rng.random() * 2.5
        elif self.now >= self.next_glance:
            look_away = self.rng.random() < 0.55
            self.gaze_target = [self.rng.uniform(-0.9, 0.9), self.rng.uniform(-0.55, 0.45)] if look_away else [0.0, 0.0]
            self.next_glance = self.now + 1.8 + self.rng.random() * 4.0
        rate = min(1.0, dt * 9.0)
        self.gaze[0] += (self.gaze_target[0] - self.gaze[0]) * rate
        self.gaze[1] += (self.gaze_target[1] - self.gaze[1]) * rate

    def _blink(self, dt: float) -> None:
        if self.blink_left > 0:
            self.blink_left -= dt
        elif self.now >= self.next_blink:
            self.blink_left = 0.13
            # Иногда моргает дважды: ровный ритм выглядит механически.
            self.next_blink = self.now + (0.32 if self.rng.random() < 0.18 else 1.8 + self.rng.random() * 4.2)

    def _springs(self, dt: float, drag_speed: float) -> None:
        # Шаг не больше 1/60 с: при редких кадрах пружины иначе начинают раскачиваться сами.
        steps = max(1, min(5, math.ceil(dt * 60)))
        step = dt / steps
        for _ in range(steps):
            target = max(-0.16, min(0.16, abs(drag_speed) * 0.0035)) if self.dragging else 0.0
            self.stretch_v += (-190.0 * (self.stretch - target) - 15.0 * self.stretch_v) * step
            self.stretch += self.stretch_v * step
            sway = 0.1 * math.sin(self.now * 0.9) - max(-1.6, min(1.6, drag_speed * 0.02))
            self.antenna_v += (-85.0 * (self.antenna - sway) - 5.0 * self.antenna_v) * step
            self.antenna += self.antenna_v * step
        if self.hop_left > 0:
            self.hop_left -= dt
            if self.hop_left <= 0:
                self.stretch_v -= 2.1
                self.antenna_v += 5.0

    def _voice(self, dt: float, emote: str) -> None:
        if self.state == "speaking":
            if self.now - self.level_at < 0.35:
                target = self.level
            else:
                # Окно скрыто и уровень звука не приходит: похожий на речь ритм, чтобы рот всё равно двигался.
                syllable = abs(math.sin(self.now * 8.3)) * (0.55 + 0.45 * math.sin(self.now * 2.7) ** 2)
                target = 0.15 + 0.62 * syllable
        elif emote == "laugh":
            target = 0.35 + 0.4 * abs(math.sin(self.now * 14.0))
        elif emote == "yawn":
            left = (self.emote_until - self.now) / _EMOTES["yawn"]
            target = 0.95 * math.sin(max(0.0, min(1.0, left)) * math.pi)
        elif emote == "happy":
            target = 0.22
        else:
            target = 0.0
        self.mouth += (target - self.mouth) * min(1.0, dt * 24.0)

    def _pose(self, emote: str, drag_speed: float) -> Pose:
        state = self.state
        muted = state == "muted"
        shape, opened, smile = "oval", 1.0, 0.5
        if self.dozing:
            shape = "closed"
        elif emote in ("happy", "laugh"):
            shape, smile = "happy", 1.0
        elif emote == "sad":
            shape, opened, smile = "sad", 0.85, -0.8
        elif emote == "surprise" or self.dragging:
            opened, smile = 1.3, 0.1
        elif emote == "yawn":
            opened = 0.42
        elif emote == "alert":
            opened, smile = 1.2, 0.2
        elif state == "thinking":
            shape, smile = "focus", 0.0
        elif state == "hearing":
            opened, smile = 1.18, 0.15
        elif muted:
            opened, smile = 0.55, 0.1
        if self.blink_left > 0 and shape in ("oval", "focus", "sad"):
            opened = 0.08
        amplitude = 1.1 if self.dozing else 1.3 if state == "speaking" else 2.3
        bob = math.sin(self.breath) * amplitude
        if self.hop_left > 0:
            bob -= math.sin((1 - self.hop_left / self.hop_length) * math.pi) * self.hop_height
        lean = max(-5.0, min(5.0, drag_speed * 0.05))
        if emote == "laugh":
            lean += math.sin(self.now * 17.0) * 1.6
        if emote == "wave":
            self.antenna_v += math.sin(self.now * 15.0) * 1.6
        return Pose(
            color=(self.color[0], self.color[1], self.color[2]),
            glow=self._glow_level(emote),
            bob=bob,
            stretch=self.stretch + math.sin(self.breath) * 0.012,
            lean=lean,
            gaze=(self.gaze[0], self.gaze[1]),
            eye_open=opened,
            eye_shape=shape,
            mouth=self.mouth,
            smile=smile,
            ring_angle=self.ring,
            ring_bright=0.26 if self.dozing or muted else 0.5 + 0.5 * min(1.0, self.glow),
            antenna=self.antenna,
            tip=0.2 if self.dozing else 0.5 + 0.5 * min(1.0, self.mouth + (0.5 if state == "hearing" else 0.0)),
            orbit=self.orbit if state == "thinking" and not emote else -1.0,
            waves=self.waves if state == "hearing" else -1.0,
            sleep_z=self.zzz if self.dozing else -1.0,
            sparkle=self.sparkle if emote in ("happy", "laugh") else -1.0,
            muted_mark=muted,
        )

    def _glow_level(self, emote: str) -> float:
        if self.dozing:
            target = 0.18
        elif emote == "alert":
            target = 0.85 + 0.12 * math.sin(self.now * 8.0)
        elif emote in ("happy", "laugh"):
            target = 0.8
        else:
            target = {
                "muted": 0.16, "sleep": 0.38 + 0.05 * math.sin(self.breath), "listening": 0.58, "hearing": 0.82,
                "thinking": 0.6 + 0.14 * math.sin(self.now * 3.2), "speaking": 0.68 + 0.3 * self.mouth,
            }.get(self.state, 0.4)
        self.glow += (target - self.glow) * 0.18
        return self.glow

    @property
    def lively(self) -> bool:
        """Нужны ли сейчас частые кадры: в покое хватает меньшего числа, и это заметно по батарее."""
        return bool(self.state in ("hearing", "thinking", "speaking") or self._current_emote() or self.dragging or self.hop_left > 0)


# ---------- Windows ----------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32")
_kernel32 = ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class _WndClassEx(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]


class _Blend(ctypes.Structure):
    _fields_ = [("BlendOp", w.BYTE), ("BlendFlags", w.BYTE), ("SourceConstantAlpha", w.BYTE), ("AlphaFormat", w.BYTE)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD), ("biBitCount", w.WORD),
                ("biCompression", w.DWORD), ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


_kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
_kernel32.GetModuleHandleW.restype = w.HMODULE
_user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WndClassEx)]
_user32.RegisterClassExW.restype = w.ATOM
_user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
_user32.CreateWindowExW.restype = w.HWND
_user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.DestroyWindow.argtypes = [w.HWND]
_user32.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
_user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
_user32.GetDC.argtypes = [w.HWND]
_user32.GetDC.restype = w.HDC
_user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
_user32.UpdateLayeredWindow.argtypes = [w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE), w.HDC, ctypes.POINTER(w.POINT),
                                        w.COLORREF, ctypes.POINTER(_Blend), w.DWORD]
_user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
_user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
_user32.SetCapture.argtypes = [w.HWND]
_user32.SetCapture.restype = w.HWND
_user32.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.LoadCursorW.argtypes = [w.HINSTANCE, w.LPVOID]
_user32.LoadCursorW.restype = w.HANDLE
_user32.GetDpiForWindow.argtypes = [w.HWND]
_user32.GetDpiForWindow.restype = w.UINT
_user32.SetWindowDisplayAffinity.argtypes = [w.HWND, w.DWORD]
_user32.CreatePopupMenu.restype = w.HMENU
_user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
_user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID]
_user32.TrackPopupMenu.restype = ctypes.c_int
_user32.DestroyMenu.argtypes = [w.HMENU]
_user32.SetForegroundWindow.argtypes = [w.HWND]
_user32.MonitorFromPoint.argtypes = [w.POINT, w.DWORD]
_user32.MonitorFromPoint.restype = w.HMONITOR
_user32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(_MonitorInfo)]
_gdi32.CreateCompatibleDC.argtypes = [w.HDC]
_gdi32.CreateCompatibleDC.restype = w.HDC
_gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(_BitmapInfoHeader), w.UINT, ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
_gdi32.CreateDIBSection.restype = w.HBITMAP
_gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
_gdi32.SelectObject.restype = w.HGDIOBJ
_gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [w.HDC]

_WS_POPUP = 0x80000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TRANSPARENT = 0x00000020
_GWL_EXSTYLE = -20
_HWND_TOPMOST = w.HWND(-1)
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE, _SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0040
_SW_HIDE, _SW_SHOWNOACTIVATE = 0, 4
_WM_DESTROY, _WM_CLOSE, _WM_NULL, _WM_TIMER, _WM_MOUSEMOVE = 0x0002, 0x0010, 0x0000, 0x0113, 0x0200
_WM_LBUTTONDOWN, _WM_LBUTTONUP, _WM_RBUTTONUP, _WM_MOUSEACTIVATE = 0x0201, 0x0202, 0x0205, 0x0021
_WM_DPICHANGED = 0x02E0
_MA_NOACTIVATE = 3
_ULW_ALPHA = 2
_AC_SRC_ALPHA = 1
_IDC_HAND = 32649
_IDC_ARROW = 32512
_MF_STRING, _MF_SEPARATOR = 0x0000, 0x0800
_TPM_RETURNCMD, _TPM_NONOTIFY, _TPM_RIGHTBUTTON = 0x0100, 0x0080, 0x0002
_MONITOR_DEFAULTTONULL, _MONITOR_DEFAULTTONEAREST = 0, 2
_WDA_EXCLUDEFROMCAPTURE = 0x11
_TIMER_ID = 1
_CLICK_DISTANCE = 5
_MENU = ("open", "mic", None, "size", "captions", "hide", None, "restart", "quit")
_NEAR_RADIUS = 36  # логические пиксели: внешнее кольцо реактора
_CLASS_NAMES = ("JarvisMascot", "JarvisBubble")


class _Surface:
    """Память под кадр и всё, что нужно, чтобы показать его в слоистом окне."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.screen = _user32.GetDC(None)
        self.dc = _gdi32.CreateCompatibleDC(self.screen)
        header = _BitmapInfoHeader(biSize=ctypes.sizeof(_BitmapInfoHeader), biWidth=width, biHeight=-height,
                                   biPlanes=1, biBitCount=32, biCompression=0)
        self.bits = ctypes.c_void_p()
        self.bitmap = _gdi32.CreateDIBSection(self.dc, ctypes.byref(header), 0, ctypes.byref(self.bits), None, 0)
        if not self.bitmap or not self.bits:
            raise OSError("CreateDIBSection не сработал")
        self.previous = _gdi32.SelectObject(self.dc, self.bitmap)

    def present(self, hwnd: int, x: int, y: int, data: bytes | None, alpha: int = 255) -> None:
        if data is not None:
            ctypes.memmove(self.bits, data, min(len(data), self.width * self.height * 4))
        blend = _Blend(0, 0, alpha, _AC_SRC_ALPHA)
        position, size, origin = w.POINT(x, y), w.SIZE(self.width, self.height), w.POINT(0, 0)
        _user32.UpdateLayeredWindow(hwnd, self.screen, ctypes.byref(position), ctypes.byref(size), self.dc, ctypes.byref(origin),
                                    0, ctypes.byref(blend), _ULW_ALPHA)

    def close(self) -> None:
        _gdi32.SelectObject(self.dc, self.previous)
        _gdi32.DeleteObject(self.bitmap)
        _gdi32.DeleteDC(self.dc)
        _user32.ReleaseDC(None, self.screen)


def _work_area(x: int, y: int) -> tuple[int, int, int, int]:
    monitor = _user32.MonitorFromPoint(w.POINT(x, y), _MONITOR_DEFAULTTONEAREST)
    info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
    if monitor and _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        area = info.rcWork
        return area.left, area.top, area.right, area.bottom
    return 0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


def _on_some_monitor(x: int, y: int) -> bool:
    return bool(_user32.MonitorFromPoint(w.POINT(x, y), _MONITOR_DEFAULTTONULL))


class Mascot:
    """Окно талисмана в своём потоке. Методы можно звать откуда угодно: они только оставляют записку для потока окна."""

    def __init__(
        self,
        on_action: Callable[[str], None],
        on_moved: Callable[[int, int], None],
        size: str = "medium",
        position: tuple[int, int] | None = None,
        captions: bool = True,
        hide_in_fullscreen: bool = False,
        hide_from_capture: bool = True,
    ) -> None:
        self.mind = MascotMind()
        self.on_action = on_action
        self.on_moved = on_moved
        self.size_name = size if size in SIZES else "medium"
        self.captions = captions
        self.hide_in_fullscreen = hide_in_fullscreen
        self.hide_from_capture = hide_from_capture
        self._position = position
        self._thread: threading.Thread | None = None
        self.hwnd = 0
        self._bubble_hwnd = 0
        self._ready = threading.Event()
        self._art: MascotArt | None = None
        self._surface: _Surface | None = None
        self._bubble_surface: _Surface | None = None
        self._bubble_pixels: bytes | None = None
        self._bubble_alpha = 0
        self._bubble_until = 0.0
        self._bubble_note: tuple[str, float, str] | None = None
        self._bubble_tail_right = True
        self._x = self._y = 0
        self._dpi = 96
        self._last_tick = 0.0
        self._interval = 0
        self._cursor = (0, 0)
        self._cursor_moved_at = 0.0
        self._clickthrough = False
        self._press: tuple[int, int, int, int] | None = None
        self._dragged = False
        self._drag_speed = 0.0
        self._drag_previous = 0
        self._wanted_visible = True
        self._shown = True
        self._next_check = 0.0
        self._paused = False
        self._rebuild = False
        self._mic_label_on = True
        self.failed = False
        self._proc = _WNDPROC(self._window_proc)  # ссылка обязательна: иначе обратный вызов соберёт сборщик мусора

    # ---------- Записки от остального Джарвиса ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="jarvis-mascot", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)

    @property
    def alive(self) -> bool:
        return not self.failed and self._thread is not None and self._thread.is_alive() and bool(self.hwnd)

    def stop(self) -> None:
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, _WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(2.0)

    def set_state(self, state: str) -> None:
        self.mind.set_state(state)

    def set_mic(self, on: bool) -> None:
        self.mind.mic_on = on
        self._mic_label_on = on

    def set_level(self, value: float) -> None:
        self.mind.set_level(value)

    def emote(self, name: str) -> None:
        self.mind.emote(name)

    def say(self, text: str, seconds: float = 5.0, tone: str = "speaking", force: bool = False) -> None:
        """Облачко рядом с Джарвис. force: показать, даже если подписи выключены, например напоминание."""
        text = " ".join(str(text).split())
        if text and (self.captions or force):
            self._bubble_note = (text, seconds, tone)

    def set_visible(self, visible: bool) -> None:
        self._wanted_visible = visible

    def set_size(self, name: str) -> None:
        if name in SIZES and name != self.size_name:
            self.size_name = name
            self._rebuild = True

    # ---------- Поток окна ----------

    def _run(self) -> None:
        try:
            self._create_windows()
            self._ready.set()
            message = w.MSG()
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(message))
                _user32.DispatchMessageW(ctypes.byref(message))
        except Exception:  # noqa: BLE001 — без талисмана Джарвис всё равно должен работать
            self.failed = True
            log.exception("Талисман не запустился")
        finally:
            self._ready.set()
            for surface in (self._surface, self._bubble_surface):
                if surface is not None:
                    surface.close()
            # Класс окна живёт, пока жив процесс, и помнит обратный вызов этого объекта. Если его не снять,
            # следующий талисман получил бы чужой, уже мёртвый обратный вызов: включить снова значило бы упасть.
            instance = _kernel32.GetModuleHandleW(None)
            for name in _CLASS_NAMES:
                _user32.UnregisterClassW(name, instance)
            self.hwnd = 0

    def _create_windows(self) -> None:
        instance = _kernel32.GetModuleHandleW(None)
        for name, cursor in zip(_CLASS_NAMES, (_IDC_HAND, _IDC_ARROW)):
            wc = _WndClassEx(cbSize=ctypes.sizeof(_WndClassEx), lpfnWndProc=self._proc, hInstance=instance,
                             hCursor=_user32.LoadCursorW(None, ctypes.c_void_p(cursor)), lpszClassName=name)
            _user32.RegisterClassExW(ctypes.byref(wc))
        style = _WS_EX_LAYERED | _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
        self.hwnd = _user32.CreateWindowExW(style, "JarvisMascot", "Талисман Джарвиса", _WS_POPUP, 0, 0, 64, 64, None, None, instance, None)
        self._bubble_hwnd = _user32.CreateWindowExW(style | _WS_EX_TRANSPARENT, "JarvisBubble", "Подпись Джарвиса", _WS_POPUP, 0, 0, 16, 16,
                                                    None, None, instance, None)
        if not self.hwnd or not self._bubble_hwnd:
            raise OSError("Окно талисмана не создалось")
        if self.hide_from_capture:
            for hwnd in (self.hwnd, self._bubble_hwnd):
                _user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE)
        self._build_art()
        self._place_initially()
        self._draw_frame(0.0)
        _user32.ShowWindow(self.hwnd, _SW_SHOWNOACTIVATE)
        self._set_interval(50)
        self._last_tick = time.monotonic()

    def _build_art(self) -> None:
        self._dpi = _user32.GetDpiForWindow(self.hwnd) or 96
        self._art = MascotArt(SIZES[self.size_name] * self._dpi / 96)
        if self._surface is not None:
            self._surface.close()
        self._surface = _Surface(self._art.size, self._art.size)

    def _place_initially(self) -> None:
        assert self._art is not None
        size = self._art.size
        if self._position is not None and _on_some_monitor(self._position[0] + size // 2, self._position[1] + size // 2):
            self._x, self._y = self._position
            return
        # Дом талисмана по умолчанию: правый нижний угол, над панелью задач.
        left, top, right, bottom = _work_area(0, 0)
        self._x, self._y = right - size - 12, bottom - size - 4

    def _set_interval(self, milliseconds: int) -> None:
        if milliseconds != self._interval:
            self._interval = milliseconds
            _user32.SetTimer(self.hwnd, _TIMER_ID, milliseconds, None)

    def _body_center(self) -> tuple[float, float]:
        assert self._art is not None
        k = self._art.size / LOGICAL_SIZE
        return self._x + _CENTER[0] * k, self._y + _CENTER[1] * k

    def _draw_frame(self, dt: float, drag: float = 0.0) -> None:
        assert self._art is not None and self._surface is not None
        cursor = w.POINT()
        _user32.GetCursorPos(ctypes.byref(cursor))
        now = time.monotonic()
        if (cursor.x, cursor.y) != self._cursor:
            self._cursor = (cursor.x, cursor.y)
            self._cursor_moved_at = now
        k = self._art.size / LOGICAL_SIZE
        cx, cy = self._body_center()
        dx, dy = cursor.x - cx, cursor.y - cy
        near = math.hypot(dx, dy) <= _NEAR_RADIUS * k
        self._update_clickthrough(near)
        vector = (max(-1.0, min(1.0, dx / (360 * k))), max(-1.0, min(1.0, dy / (260 * k))))
        pose = self.mind.tick(dt, vector, now - self._cursor_moved_at < 1.2, near, winapi.idle_seconds(), drag)
        self._surface.present(self.hwnd, self._x, self._y, premultiplied_bgra(self._art.render(pose)))
        if self.mind.dozing:
            self._set_interval(125)
        elif self.mind.lively or near:
            self._set_interval(33)
        else:
            self._set_interval(50)

    def _update_clickthrough(self, near: bool) -> None:
        through = not near and self._press is None
        if through == self._clickthrough:
            return
        self._clickthrough = through
        style = _user32.GetWindowLongPtrW(self.hwnd, _GWL_EXSTYLE)
        style = style | _WS_EX_TRANSPARENT if through else style & ~_WS_EX_TRANSPARENT
        _user32.SetWindowLongPtrW(self.hwnd, _GWL_EXSTYLE, style)

    def _on_timer(self) -> None:
        now = time.monotonic()
        dt = min(0.25, now - self._last_tick)
        self._last_tick = now
        if self._rebuild:
            self._rebuild = False
            self._build_art()
        if now >= self._next_check:
            self._next_check = now + 1.5
            self._periodic_checks()
        if self._paused or not self._shown:
            self._set_interval(500)
            self._bubble_note = None
            return
        drag = self._drag_speed
        self._drag_speed *= 0.6
        self._draw_frame(dt, drag)
        self._update_bubble(dt)

    def _periodic_checks(self) -> None:
        """Раз в полторы секунды: не накрыло ли окно другое поверх всех и не пора ли спрятаться."""
        self._paused = winapi.session_locked()
        visible = self._wanted_visible and not self._fullscreen_blocks()
        if visible != self._shown:
            self._shown = visible
            _user32.ShowWindow(self.hwnd, _SW_SHOWNOACTIVATE if visible else _SW_HIDE)
            if not visible:
                self._hide_bubble()
        if self._shown and self._press is None:
            _user32.SetWindowPos(self.hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)

    def _fullscreen_blocks(self) -> bool:
        if not self.hide_in_fullscreen:
            return False
        window = winapi.foreground()
        return window is not None and window.exe != "explorer.exe" and winapi.fills_monitor(window)

    # ---------- Облачко ----------

    def _update_bubble(self, dt: float) -> None:
        now = time.monotonic()
        note, self._bubble_note = self._bubble_note, None
        if note is not None:
            self._show_bubble(*note)
        if self._bubble_surface is None:
            return
        target = 255 if now < self._bubble_until else 0
        if self._bubble_alpha == target:
            if target == 0:
                self._hide_bubble()
            elif self._press is not None:
                self._place_bubble()
            return
        step = int(dt * 1400)
        self._bubble_alpha = min(target, self._bubble_alpha + step) if target else max(0, self._bubble_alpha - step)
        self._place_bubble()

    def _show_bubble(self, text: str, seconds: float, tone: str) -> None:
        assert self._art is not None
        area = _work_area(self._x, self._y)
        self._bubble_tail_right = self._x + self._art.size / 2 > (area[0] + area[2]) / 2
        color = PALETTE.get(tone, PALETTE["speaking"])
        image = render_bubble(text, color, self._dpi / 96, tail_right=self._bubble_tail_right)
        if self._bubble_surface is not None:
            self._bubble_surface.close()
        self._bubble_surface = _Surface(*image.size)
        self._bubble_pixels = premultiplied_bgra(image)
        self._bubble_until = time.monotonic() + seconds
        self._place_bubble()
        _user32.ShowWindow(self._bubble_hwnd, _SW_SHOWNOACTIVATE)

    def _place_bubble(self) -> None:
        assert self._art is not None
        surface = self._bubble_surface
        if surface is None:
            return
        cx, cy = self._body_center()
        k = self._art.size / LOGICAL_SIZE
        gap = 45 * k
        x = int(cx - gap - surface.width) if self._bubble_tail_right else int(cx + gap)
        left, top, right, bottom = _work_area(self._x, self._y)
        y = max(top + 4, min(bottom - surface.height - 4, int(cy - surface.height / 2)))
        x = max(left + 4, min(right - surface.width - 4, x))
        surface.present(self._bubble_hwnd, x, y, self._bubble_pixels, self._bubble_alpha)
        self._bubble_pixels = None

    def _hide_bubble(self) -> None:
        _user32.ShowWindow(self._bubble_hwnd, _SW_HIDE)
        if self._bubble_surface is not None:
            self._bubble_surface.close()
            self._bubble_surface = None
        self._bubble_alpha = 0

    # ---------- Мышь и меню ----------

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == _WM_TIMER and hwnd == self.hwnd:
                self._on_timer()
                return 0
            if message == _WM_MOUSEACTIVATE:
                return _MA_NOACTIVATE
            if hwnd == self.hwnd:
                if message == _WM_LBUTTONDOWN:
                    self._on_press()
                    return 0
                if message == _WM_MOUSEMOVE and self._press is not None:
                    self._on_drag()
                    return 0
                if message == _WM_LBUTTONUP:
                    self._on_release()
                    return 0
                if message == _WM_RBUTTONUP:
                    self._show_menu()
                    return 0
                if message == _WM_DPICHANGED:
                    self._rebuild = True
                    return 0
                if message == _WM_CLOSE:
                    _user32.KillTimer(hwnd, _TIMER_ID)
                    _user32.DestroyWindow(self._bubble_hwnd)
                    _user32.DestroyWindow(hwnd)
                    return 0
                if message == _WM_DESTROY:
                    _user32.PostQuitMessage(0)
                    return 0
        except Exception:  # noqa: BLE001 — исключение нельзя выпускать в обратный вызов Windows
            log.exception("Сбой в окне талисмана")
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _on_press(self) -> None:
        cursor = w.POINT()
        _user32.GetCursorPos(ctypes.byref(cursor))
        self._press = (cursor.x, cursor.y, self._x, self._y)
        self._drag_previous = cursor.x
        self._dragged = False
        _user32.SetCapture(self.hwnd)

    def _on_drag(self) -> None:
        assert self._press is not None
        cursor = w.POINT()
        _user32.GetCursorPos(ctypes.byref(cursor))
        start_x, start_y, window_x, window_y = self._press
        if not self._dragged and math.hypot(cursor.x - start_x, cursor.y - start_y) < _CLICK_DISTANCE:
            return
        self._dragged = True
        self.mind.dragging = True
        self._x, self._y = window_x + cursor.x - start_x, window_y + cursor.y - start_y
        self._drag_speed = float(cursor.x - self._drag_previous) * 4
        self._drag_previous = cursor.x

    def _on_release(self) -> None:
        _user32.ReleaseCapture()
        was_drag = self._dragged
        self._press = None
        self._dragged = False
        self.mind.dragging = False
        if was_drag:
            self.mind.stretch_v -= 3.0  # приземлился
            self._keep_on_screen()
            self.on_moved(self._x, self._y)
        else:
            self.mind.hop(0.36, 7.0)
            self.on_action("open")

    def _keep_on_screen(self) -> None:
        """Талисмана нельзя потерять: если его унесли за край, он возвращается целиком на ближайший экран."""
        assert self._art is not None
        cx, cy = self._body_center()
        left, top, right, bottom = _work_area(int(cx), int(cy))
        size = self._art.size
        self._x = max(left - size // 4, min(right - size * 3 // 4, self._x))
        self._y = max(top - size // 4, min(bottom - size * 3 // 4, self._y))

    def _menu_label(self, item: str) -> str:
        return {
            "open": "Открыть Джарвиса",
            "mic": "Выключить микрофон" if self._mic_label_on else "Включить микрофон",
            "size": f"Размер: {_SIZE_NAMES[self.size_name]}",
            "captions": "Подписи: включены" if self.captions else "Подписи: выключены",
            "hide": "Спрятать талисман",
            "restart": "Перезапустить Джарвиса",
            "quit": "Выключить Джарвиса полностью",
        }[item]

    def _show_menu(self) -> None:
        menu = _user32.CreatePopupMenu()
        for index, item in enumerate(_MENU, 1):
            if item is None:
                _user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            else:
                _user32.AppendMenuW(menu, _MF_STRING, index, self._menu_label(item))
        cursor = w.POINT()
        _user32.GetCursorPos(ctypes.byref(cursor))
        previous = winapi.user32.GetForegroundWindow()
        # Без этого меню не закрывается по клику мимо него: таково требование Windows.
        _user32.SetForegroundWindow(self.hwnd)
        command = _user32.TrackPopupMenu(menu, _TPM_RETURNCMD | _TPM_NONOTIFY | _TPM_RIGHTBUTTON, cursor.x, cursor.y, 0, self.hwnd, None)
        _user32.PostMessageW(self.hwnd, _WM_NULL, 0, 0)
        _user32.DestroyMenu(menu)
        if previous:
            winapi.activate(previous)
        if command and _MENU[command - 1]:
            self._menu_command(str(_MENU[command - 1]))

    def _menu_command(self, item: str) -> None:
        if item == "size":
            names = list(SIZES)
            self.set_size(names[(names.index(self.size_name) + 1) % len(names)])
        elif item == "captions":
            self.captions = not self.captions
        self.on_action(item)

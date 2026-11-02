"""Снимки экрана. Снимок делается только по запросу и уходит только в модель зрения Groq.

Джарвис не должен видеть сама себя, поэтому, когда её окно сверху, снимаем окно под ним:
PrintWindow рисует его содержимое, даже если оно чем-то перекрыто.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import io
from typing import Any

from . import winapi

gdi32 = ctypes.WinDLL("gdi32")
user32 = winapi.user32

gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = w.HBITMAP
gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.BitBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD]
gdi32.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT]
gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
gdi32.DeleteDC.argtypes = [w.HDC]
user32.GetDC.argtypes = [w.HWND]
user32.GetDC.restype = w.HDC
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_PW_RENDERFULLCONTENT = 2
_MAX_SIDE = 1600
_BLANK_LEVEL = 10


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD), ("biBitCount", w.WORD),
        ("biCompression", w.DWORD), ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD),
    ]


def _grab(width: int, height: int, draw: Any) -> Any:
    from PIL import Image  # Импорт при первом снимке: большинству запусков Pillow не понадобится.

    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        if not draw(memory, screen):
            return None
        header = _BitmapInfoHeader(biSize=ctypes.sizeof(_BitmapInfoHeader), biWidth=width, biHeight=-height,
                                   biPlanes=1, biBitCount=32, biCompression=0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(header), 0):
            return None
        return Image.frombuffer("RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)


def capture_rect(rect: tuple[int, int, int, int]) -> Any:
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    def draw(memory: int, screen: int) -> bool:
        return bool(gdi32.BitBlt(memory, 0, 0, width, height, screen, left, top, _SRCCOPY | _CAPTUREBLT))

    return _grab(width, height, draw)


def capture_window(window: winapi.WindowInfo) -> Any:
    left, top, right, bottom = window.rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0 or window.minimized:
        return None

    def draw(memory: int, _: int) -> bool:
        return bool(user32.PrintWindow(window.hwnd, memory, _PW_RENDERFULLCONTENT))

    return _grab(width, height, draw)


def is_blank(image: Any) -> bool:
    """Чёрный снимок: так выглядят игры в эксклюзивном полноэкранном режиме и защищённое видео."""
    extrema = image.resize((64, 36)).getextrema()
    return all(high < _BLANK_LEVEL for _, high in extrema)


def to_jpeg(image: Any, max_side: int = _MAX_SIDE, quality: int = 78) -> bytes:
    from PIL import Image

    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def snapshot(target: winapi.WindowInfo | None, jarvis_on_top: bool) -> Any:
    """Что видит человек. Возвращает картинку или None, если снять не удалось."""
    if target is None:
        return capture_rect((0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)))
    if jarvis_on_top:
        image = capture_window(target)
        if image is not None and not is_blank(image):
            return image
    return capture_rect(winapi.monitor_rect(target.hwnd))

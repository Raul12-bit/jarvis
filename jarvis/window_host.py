"""Большое окно Джарвиса и его скрытый двойник.

Микрофон и эхоподавление живут в браузере, поэтому страница нужна всегда, даже когда окно не видно. Закрыть окно крестиком
значит уничтожить страницу, а перехватить это нельзя. Поэтому после закрытия та же страница открывается заново, но скрытой:
человек видит только талисман, а Джарвис продолжает слышать имя. Нажал на талисман — окно появилось мгновенно.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import psutil

from . import winapi
from .browser import launch_window

log = logging.getLogger("jarvis.host")

_CLASS = "Chrome_WidgetWin_1"
_TITLE = "Джарвис"
_APPEAR_SECONDS = 12.0
_HIDDEN_ARGS = ["--window-position=-32000,-32000"]
_DEFAULT_SIZE = (1240, 800)


def _looks_like_ours(title: str, port_text: str) -> bool:
    # Пока страница загружается, в заголовке окна стоит её адрес.
    title = title.strip()
    return title == _TITLE or port_text in title


def _same_profile(hwnd: int, profile: str) -> bool:
    """Окно принадлежит именно этому профилю браузера. Заголовка мало: чужое окно закрыть нельзя ни в коем случае."""
    info = winapi.window_info(hwnd)
    if info is None:
        return False
    try:
        return any(profile in part for part in psutil.Process(info.pid).cmdline())
    except psutil.Error:
        return False


class WindowHost:
    def __init__(self, url: str, home: Path, port: int, extra_args: list[str] | None = None) -> None:
        self.url = url
        self.home = home
        self.port_text = f"127.0.0.1:{port}"
        self.profile = str(home / "browser")
        self.extra_args = list(extra_args or [])
        self.pid: int | None = None

    def find_all(self) -> list[int]:
        return [hwnd for hwnd, title in winapi.windows_of_class(_CLASS)
                if _looks_like_ours(title, self.port_text) and _same_profile(hwnd, self.profile)]

    def find(self) -> int:
        found = self.find_all()
        return found[0] if found else 0

    def is_visible(self) -> bool:
        hwnd = self.find()
        return bool(hwnd) and winapi.is_visible(hwnd)

    def _launch(self, args: list[str]) -> bool:
        process = launch_window(self.url, self.home, [*self.extra_args, *args])
        if process is None:
            log.error("Нет браузера для окна Джарвиса")
            return False
        self.pid = process.pid
        return True

    def start_visible(self) -> bool:
        x, y, _, _ = self._normal_rect()
        return self._launch([f"--window-position={x},{y}"])

    async def start_hidden(self) -> bool:
        """Открывает страницу за краем экрана и сразу прячет её. Вспышки на экране быть не должно."""
        hwnd = self.find()
        if hwnd:
            return True
        if not self._launch(_HIDDEN_ARGS):
            return False
        deadline = time.monotonic() + _APPEAR_SECONDS
        while time.monotonic() < deadline:
            hwnd = self.find()
            if hwnd:
                winapi.hide_window(hwnd)
                return True
            await asyncio.sleep(0.04)
        log.info("Скрытое окно не появилось")
        return False

    async def show(self) -> bool:
        hwnd = self.find()
        if not hwnd:
            if not self.start_visible():
                return False
            deadline = time.monotonic() + _APPEAR_SECONDS
            while time.monotonic() < deadline and not hwnd:
                await asyncio.sleep(0.1)
                hwnd = self.find()
            if not hwnd:
                return False
        self._bring_back(hwnd)
        return await asyncio.to_thread(winapi.activate, hwnd)

    def _bring_back(self, hwnd: int) -> None:
        info = winapi.window_info(hwnd)
        off_screen = info is not None and not winapi.point_on_monitor((info.rect[0] + info.rect[2]) // 2, (info.rect[1] + info.rect[3]) // 2)
        if off_screen:
            winapi.move_window(hwnd, *self._normal_rect())
        winapi.show_window(hwnd)

    def hide(self) -> bool:
        hwnd = self.find()
        if not hwnd:
            return False
        winapi.hide_window(hwnd)
        return True

    def close(self) -> None:
        for hwnd in self.find_all():
            winapi.close_window(hwnd)

    def _normal_rect(self) -> tuple[int, int, int, int]:
        left, top, right, bottom = winapi.primary_work_area()
        scale = winapi.system_dpi() / 96
        width = min(round(_DEFAULT_SIZE[0] * scale), int((right - left) * 0.94))
        height = min(round(_DEFAULT_SIZE[1] * scale), int((bottom - top) * 0.94))
        return left + (right - left - width) // 2, top + (bottom - top - height) // 2, width, height

"""Окно Джарвиса: Edge (есть в любой Windows 11) или Chrome в режиме приложения.

Браузер здесь не прихоть: только он даёт бесплатное качественное эхоподавление WebRTC,
без которого живой разговор с перебиванием на динамиках невозможен.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("jarvis.browser")


def find_browser() -> str | None:
    program_files = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")]
    tails = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
    ]
    for tail in tails:
        for base in program_files:
            if base and (Path(base) / tail).exists():
                return str(Path(base) / tail)
    return shutil.which("msedge") or shutil.which("chrome")


def launch_window(url: str, home: Path, extra_args: list[str] | None = None) -> subprocess.Popen | None:
    browser = find_browser()
    if browser is None:
        log.error("Не найден ни Edge, ни Chrome")
        return None
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={home / 'browser'}",
        "--window-size=1240,800",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-breakpad",
        "--disable-features=Translate,msEdgeSidebarV2,msHubApps,msEdgeShoppingUI,msEdgeWalletCheckout,OptimizationHints,MediaRouter",
        "--autoplay-policy=no-user-gesture-required",
        "--renderer-process-limit=1",
        # На ноутбуках с двумя видеокартами не будим дискретную ради небольшой анимации.
        "--force_low_power_gpu",
        # Проверка самой первой установки: своего аргумента у Jarvis.cmd нет, а звучать и спрашивать микрофон ей нельзя.
        *os.environ.get("JARVIS_TEST_BROWSER_ARGS", "").split(),
        *(extra_args or []),
    ]
    return subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)

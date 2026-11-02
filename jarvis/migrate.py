"""Смена версии без участия человека: какой код работает, переезд с Карен, замена старого процесса.

Настоящий случай, из-за которого это появилось: Карен версии 3 работала сутки, потому что телефон держит её в фоне. Ярлык видел
живой процесс и просто просил его показать окно, поэтому новый код с талисманом так ни разу и не запустился. А страница бралась
с диска уже новая: человек видел переключатель талисмана, а старый сервер отвергал незнакомую настройку. Отсюда правило: запуск
обязан сверять, какой код сейчас в памяти, и заменять устаревший процесс.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import urllib.request
from pathlib import Path

import psutil

from .config import PROJECT_DIR

log = logging.getLogger("jarvis.migrate")

_APP_MODULES = ("jarvis", "karen")
_STOP_SECONDS = 8.0
_OLD_HOME_NAME = "Karen"
_OLD_DB = "karen.db"
_NEW_DB = "jarvis.db"
_OLD_RUNTIME = "runtime.json"


def build_id() -> str:
    """Отпечаток кода на диске. По содержимому, а не по времени: OneDrive меняет дату файла и без правок."""
    forced = os.environ.get("JARVIS_TEST_BUILD")  # только для сквозной проверки: иначе «старую версию» пришлось бы хранить рядом
    if forced:
        return forced
    digest = hashlib.sha1(usedforsecurity=False)
    files = [*sorted((PROJECT_DIR / "jarvis").glob("*.py")), *sorted((PROJECT_DIR / "web").glob("*")), PROJECT_DIR / "requirements.txt"]
    for path in files:
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _our_process(pid: int) -> psutil.Process | None:
    """Процесс Джарвиса или Карен. Номер из файла мог достаться чужой программе, а чужое завершать нельзя."""
    try:
        process = psutil.Process(pid)
        command = process.cmdline()
    except psutil.Error:
        return None
    for index, part in enumerate(command[:-1]):
        if part == "-m" and command[index + 1] in _APP_MODULES:
            return process
    return None


def _ask_to_exit(port: int, token: str) -> None:
    request = urllib.request.Request(  # noqa: S310 — только свой локальный сервер
        f"http://127.0.0.1:{port}/api/shutdown", method="POST",
        headers={"x-jarvis-token": token, "x-karen-token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=2):  # noqa: S310
            pass
    except (OSError, ValueError) as exc:
        log.info("Прежняя версия не ответила на просьбу выключиться: %s", type(exc).__name__)


def stop_instance(runtime: Path) -> bool:
    """Выключает работающую копию из этого файла: сначала вежливо, затем принудительно. True: теперь её нет."""
    try:
        info = json.loads(runtime.read_text(encoding="utf-8"))
        pid, port, token = int(info["pid"]), int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    process = _our_process(pid)
    if process is None:
        runtime.unlink(missing_ok=True)
        return True
    _ask_to_exit(port, token)
    # На Windows python.exe из виртуального окружения только запускает настоящий процесс и ждёт его: завершать надо обоих.
    try:
        parent = process.parent()
    except psutil.Error:
        parent = None
    family = [item for item in (process, parent) if item is not None and _our_process(item.pid) is not None]
    deadline = time.monotonic() + _STOP_SECONDS
    while time.monotonic() < deadline and process.is_running():
        time.sleep(0.1)
    for item in family:
        try:
            if item.is_running():
                log.info("Прежняя версия не выключилась сама, завершаю процесс %d", item.pid)
                item.kill()
        except psutil.Error:
            continue
    psutil.wait_procs(family, timeout=4)
    runtime.unlink(missing_ok=True)
    return not process.is_running()


def close_browser_profile(profile: Path) -> int:
    """Окно старой версии: без сервера оно бесконечно переподключалось бы с включённым микрофоном. Трогаем только свой профиль."""
    closed = 0
    needle = f"--user-data-dir={profile}".lower()
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if (process.info["name"] or "").lower() not in ("msedge.exe", "chrome.exe"):
                continue
            if any(part.lower() == needle for part in process.info["cmdline"] or []):
                process.kill()
                closed += 1
        except psutil.Error:
            continue
    return closed


def adopt_old_data(old: Path, new: Path) -> bool:
    """Ключи, привязка телефона, память, дела и модель слуха переезжают копией: папка Карен остаётся нетронутой на случай отката."""
    if not (old / "config.json").exists() or (new / "config.json").exists():
        return False
    new.mkdir(parents=True, exist_ok=True)
    if (old / _OLD_DB).exists() and not (new / _NEW_DB).exists():
        shutil.copy2(old / _OLD_DB, new / _NEW_DB)
    if (old / "models").is_dir() and not (new / "models").exists():
        shutil.copytree(old / "models", new / "models")
    # Настройки последними: по ним видно, что переезд закончен, а прерванный начнётся заново.
    temporary = new / "config.json.tmp"
    shutil.copy2(old / "config.json", temporary)
    temporary.replace(new / "config.json")
    log.info("Настройки и память перенесены из %s", old)
    return True


def leave_karen(new_home: Path) -> bool:
    """Первый запуск после переименования. True: Карен была и данные перенесены."""
    if os.environ.get("JARVIS_HOME"):
        return False
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return False
    old = Path(base) / _OLD_HOME_NAME
    if not old.is_dir():
        return False
    # Иначе два процесса забирали бы сообщения одного бота, и Telegram отвечал бы обоим ошибкой 409.
    stop_instance(old / _OLD_RUNTIME)
    close_browser_profile(old / "browser")
    return adopt_old_data(old, new_home)

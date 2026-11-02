"""Самая первая установка на чистую машину, где раньше работала Карен. Ровно то, что случится при двойном нажатии на Jarvis.cmd.

Настоящие: Jarvis.cmd, создание окружения, pip, pythonw, Edge, талисман, замена живого старого процесса, перенос данных.
Подменены: папка AppData (своя, поэтому настоящие данные не затрагиваются), микрофон и звук (поддельные, ничего не звучит).
Ярлыки на настоящем рабочем столе не создаются и не удаляются.

Запуск: python tests/e2e/first_run_check.py <короткая_папка_вне_AppData>   (нужна модель Vosk в JARVIS_E2E_MODEL)
Первый прогон качает зависимости и занимает несколько минут.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import psutil

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
WORK = Path(sys.argv[1]).resolve()
LOCAL = WORK / "Local"
OLD = LOCAL / "Karen"
NEW = LOCAL / "Jarvis"
results: list[bool] = []

FAKE_OLD_KAREN = '''
"""Старая Карен, как в настоящем случае: живёт в фоне, отвечает на /health и держит файл запуска."""
import http.server, json, os, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"app": "karen", "version": "3.0.0"}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        # У старой версии могло не быть этого адреса: выключить её тогда можно только принудительно.
        self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), H)
home = sys.argv[1]
with open(os.path.join(home, "runtime.json"), "w", encoding="utf-8") as fh:
    json.dump({"pid": os.getpid(), "port": server.server_address[1], "token": "old-token"}, fh)
server.serve_forever()
'''


def check(name: str, ok: Any, note: str = "") -> None:
    results.append(bool(ok))
    print(("ОК   " if ok else "СБОЙ ") + name + (f" ({note})" if note else ""), flush=True)


def wait_for(predicate: Callable[[], Any], seconds: float = 30.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except (OSError, ValueError, KeyError):
            value = None
        if value:
            return value
        time.sleep(0.25)
    return None


def build_old_karen() -> subprocess.Popen:
    (OLD / "logs").mkdir(parents=True)
    source = Path(os.environ["JARVIS_E2E_MODEL"])
    shutil.copytree(source, OLD / "models" / source.name)
    (OLD / "config.json").write_text(json.dumps({
        "voice": "en-US-EmmaMultilingualNeural", "user_name": "Сэр", "speech_rate": 10, "onboarded": True, "profile_version": 2,
        "shortcut_created": True, "shortcut_target": "старый путь", "city": "Алматы", "autostart": True, "wake_word": True,
        "groq_api_key": "gsk_not_a_real_key_for_this_check",
    }, ensure_ascii=False), encoding="utf-8")
    db = sqlite3.connect(OLD / "karen.db")
    db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL UNIQUE, ts REAL NOT NULL)")
    db.execute("INSERT INTO facts(text, ts) VALUES ('пью кофе без сахара', 1)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL)")
    db.execute("INSERT INTO messages(role, content, ts) VALUES ('assistant', 'Я проверила почту.', ?)", (time.time(),))
    db.commit()
    db.close()
    package = WORK / "old" / "karen"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", "utf-8")
    (package / "__main__.py").write_text(FAKE_OLD_KAREN, "utf-8")
    # Командная строка та же, что у настоящей старой версии: по ней новая версия понимает, что это своё и его можно завершить.
    return subprocess.Popen([sys.executable, "-m", "karen", str(OLD)], cwd=WORK / "old", creationflags=subprocess.CREATE_NO_WINDOW)


def api(runtime: dict, method: str, path: str) -> tuple[int, Any]:
    request = urllib.request.Request(f"http://127.0.0.1:{runtime['port']}{path}", method=method, headers={"x-jarvis-token": runtime["token"]})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:  # noqa: S310
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, None


def mascots(pid: int) -> list[int]:
    from jarvis import winapi

    try:
        root = psutil.Process(pid)
        pids = {root.pid, root.ppid(), *(child.pid for child in root.children(recursive=True))}
    except psutil.Error:
        return []
    return [hwnd for hwnd, _ in winapi.windows_of_class("JarvisMascot") if winapi._window_pid(hwnd) in pids and ctypes.windll.user32.IsWindowVisible(hwnd)]  # noqa: SLF001


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    LOCAL.mkdir(parents=True)
    old = build_old_karen()
    check("Старая Карен работает", wait_for(lambda: (OLD / "runtime.json").exists(), 15))

    env = {
        **os.environ, "LOCALAPPDATA": str(LOCAL), "PIP_CACHE_DIR": str(Path(os.environ["LOCALAPPDATA"]) / "pip" / "Cache"),
        "JARVIS_TEST_NO_SHORTCUTS": "1",
        "JARVIS_TEST_BROWSER_ARGS": "--use-fake-device-for-media-stream --use-fake-ui-for-media-stream --mute-audio",
    }
    env.pop("JARVIS_HOME", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    started = time.monotonic()
    # Как двойное нажатие: своё окно консоли и текущая папка не важна.
    subprocess.Popen(["cmd", "/c", "start", "", "/wait", "cmd", "/c", str(PROJECT / "Jarvis.cmd")], cwd=os.environ.get("SystemRoot", "C:\\"), env=env)
    runtime_file = NEW / "runtime.json"
    runtime = wait_for(lambda: json.loads(runtime_file.read_text("utf-8")), 900)
    check("Установка закончилась и Джарвис запустился", runtime, f"{time.monotonic() - started:.0f} с")
    if not runtime:
        old.kill()
        return 1
    try:
        check("Окружение создано в своей папке", (NEW / "venv" / "Scripts" / "pythonw.exe").exists() and (NEW / "venv" / "requirements.lock").exists())
        check("Процесс запущен без консоли", "pythonw" in " ".join(psutil.Process(int(runtime["pid"])).cmdline()).lower())
        check("Старая Карен завершена", wait_for(lambda: old.poll() is not None, 5))

        config = json.loads((NEW / "config.json").read_text("utf-8"))
        check("Ключ и настройки перенесены", config["groq_api_key"].startswith("gsk_") and config["speech_rate"] == 10 and config["onboarded"])
        check("Женский голос заменён мужским", config["voice"] == "fr-FR-RemyMultilingualNeural", config["voice"])
        check("Полный доступ и талисман включены", config["phone_shell"] and config["allow_camera"] and config["mascot"])
        check("Папка Карен на месте для отката", (OLD / "config.json").exists() and (OLD / "karen.db").exists())
        check("Модель слуха перенесена без скачивания", wait_for(lambda: (NEW / "models").is_dir()))

        db = sqlite3.connect(NEW / "jarvis.db")
        facts = [row[0] for row in db.execute("SELECT text FROM facts")]
        stale = db.execute("SELECT COUNT(*) FROM messages WHERE ts > 1000 AND content LIKE '%проверила%'").fetchone()[0]
        db.close()
        check("Память перенесена", facts == ["пью кофе без сахара"], str(facts))
        check("Женская история больше не пример для модели", stale == 0, str(stale))

        pid = int(runtime["pid"])
        check("Страница подключилась", wait_for(lambda: api(runtime, "GET", "/api/presence")[1]["connected"], 40))
        check("Микрофон работает", wait_for(lambda: api(runtime, "GET", "/api/presence")[1]["mic"], 20))
        check("ТАЛИСМАН НА ЭКРАНЕ", wait_for(lambda: mascots(pid), 15))
        log = (NEW / "logs" / "jarvis.log").read_text("utf-8")
        check("В журнале нет ошибок запуска", "ERROR" not in log and "Traceback" not in log, log[-300:] if "ERROR" in log else "")
        check("Журнал показывает версию кода", "Запуск Джарвиса" in log)

        # Второй запуск ярлыка: окно то же, новый процесс не нужен, а установки больше нет.
        second = subprocess.Popen(["cmd", "/c", str(PROJECT / "Jarvis.cmd")], cwd=PROJECT, env=env)
        check("Второй запуск сразу, без установки", wait_for(lambda: second.poll() is not None, 20))
        time.sleep(4)
        check("Второй запуск не заменил работающий процесс", json.loads(runtime_file.read_text("utf-8"))["pid"] == runtime["pid"])

        status, _ = api(runtime, "POST", "/api/shutdown")
        check("Выключение принято", status == 200)
        check("Джарвис выключился", wait_for(lambda: not psutil.pid_exists(pid), 25))
        check("Файл запуска убран", wait_for(lambda: not runtime_file.exists(), 10))
    finally:
        for process in psutil.process_iter(["cmdline"]):
            try:
                line = " ".join(process.info["cmdline"] or [])
                if str(LOCAL) in line:
                    process.kill()
            except psutil.Error:
                continue
        if old.poll() is None:
            old.kill()
    print(f"Итог: {sum(results)} из {len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

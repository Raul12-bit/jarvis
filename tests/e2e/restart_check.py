"""Перезапуск с телефона на настоящем процессе. Если новый процесс не поднимется, удалённо ноутбук уже не вернуть.

Настоящие: процесс Джарвиса и новый процесс после перезапуска, Edge, талисман, опрос бота по HTTP.
Подменены: сервер Telegram (fake_telegram.py), микрофон и звук браузера. Настоящий бот не нужен, в сеть ничего не уходит.

Запуск: python tests/e2e/restart_check.py <рабочая_папка>
"""
from __future__ import annotations

import json
import os
import shutil
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
HOME = WORK / "home-restart"
PORT, TELEGRAM_PORT = 47415, 47416
TOKEN = "restart-check"
BOT_TOKEN = "123456:FakeTokenForLocalCheckOnly"
OWNER = 111
ROUNDS = 3

from jarvis import winapi  # noqa: E402
from jarvis.window_host import WindowHost  # noqa: E402

results: list[bool] = []


def check(name: str, ok: Any, note: str = "") -> None:
    results.append(bool(ok))
    print(("ОК   " if ok else "СБОЙ ") + name + (f" ({note})" if note else ""), flush=True)


def request(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", **(headers or {})})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except OSError:
        return 0, None


def runtime() -> dict:
    try:
        return json.loads((HOME / "runtime.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def jarvis(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    now = runtime()
    return request(method, f"http://127.0.0.1:{now.get('port', PORT)}{path}", body, {"x-jarvis-token": now.get("token", TOKEN)})


def push(text: str) -> None:
    request("POST", f"http://127.0.0.1:{TELEGRAM_PORT}/control/push", {"text": text, "sender": OWNER})


def press(data: str) -> None:
    request("POST", f"http://127.0.0.1:{TELEGRAM_PORT}/control/press", {"sender": OWNER, "data": data, "message_id": 7, "photo": False})


def sent() -> list[dict]:
    items = request("GET", f"http://127.0.0.1:{TELEGRAM_PORT}/control/sent")[1] or []
    return [item for item in items if item["method"] not in ("setMyCommands", "sendChatAction", "answerCallbackQuery", "deleteMessage", "editMessageReplyMarkup")]


def said(needle: str, start: int = 0) -> dict | None:
    return next((item for item in sent()[start:] if needle in item.get("text", "")), None)


def button_data(item: dict | None, label: str) -> str:
    rows = ((item or {}).get("markup") or {}).get("inline_keyboard", [])
    return next((button["callback_data"] for row in rows for button in row if label in button["text"]), "")


def wait_for(predicate: Callable[[], Any], seconds: float = 20.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def stop_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
        for process in [*root.children(recursive=True), root]:
            process.kill()
    except psutil.Error:
        pass


def stop_ours() -> None:
    # Новый процесс не потомок этой проверки: по дереву его не найти.
    for process in psutil.process_iter(["cmdline"]):
        try:
            if "jarvis" in (process.info["cmdline"] or []) and process.environ().get("JARVIS_HOME") == str(HOME):
                process.kill()
        except psutil.Error:
            continue


def mascot_of(pid: int) -> bool:
    return any(winapi._window_pid(hwnd) == pid and winapi.user32.IsWindowVisible(hwnd) for hwnd, _ in winapi.windows_of_class("JarvisMascot"))  # noqa: SLF001


def main() -> int:
    shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(parents=True)
    (HOME / "config.json").write_text(json.dumps({
        "onboarded": True, "wake_word": False, "user_name": "Тест", "shortcut_created": True, "mascot": True, "profile_version": 3,
    }), encoding="utf-8")
    env = {
        **os.environ, "JARVIS_HOME": str(HOME), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "JARVIS_TEST_TELEGRAM_BASE": f"http://127.0.0.1:{TELEGRAM_PORT}",
        "JARVIS_TEST_BROWSER_ARGS": "--use-fake-device-for-media-stream --use-fake-ui-for-media-stream --mute-audio",
    }
    python = sys.executable
    telegram = subprocess.Popen([python, str(PROJECT / "tests" / "e2e" / "fake_telegram.py"), str(TELEGRAM_PORT)])
    server = subprocess.Popen([python, "-m", "jarvis", f"--port={PORT}", f"--token={TOKEN}"], cwd=PROJECT, env=env)
    host = WindowHost(f"http://127.0.0.1:{PORT}/?t={TOKEN}", HOME, PORT)
    try:
        check("Джарвис запустился и страница подключилась", wait_for(lambda: (jarvis("GET", "/api/presence")[1] or {}).get("connected"), 40))
        status, body = jarvis("POST", "/api/phone/token", {"token": BOT_TOKEN})
        check("Бот подключён", status == 200 and body["ok"])
        push(body["phone"]["code"])
        check("Владелец привязан", wait_for(lambda: said("К вашим услугам")))

        # Несколько раз подряд: старый Edge закрывается как раз тогда, когда новый процесс открывает своё окно.
        for attempt in range(1, ROUNDS + 1):
            before = runtime()
            count = len(sent())
            push("/restart")
            question = wait_for(lambda: said("Перезапустить меня", count))
            answer_yes = button_data(question, "Да")
            check(f"{attempt}: перезапуск сначала спрашивает", answer_yes)
            press(answer_yes)
            pressed = time.monotonic()
            check(f"{attempt}: ответ ушёл на телефон до выключения", wait_for(lambda: said("Перезапускаюсь", count)))
            check(f"{attempt}: старый процесс завершился", wait_for(lambda: not psutil.pid_exists(before["pid"]), 20))
            check(f"{attempt}: новый процесс записал файл запуска", wait_for(lambda: runtime().get("pid") not in (None, before["pid"]), 30))
            after = runtime()
            connected = wait_for(lambda: (jarvis("GET", "/api/presence")[1] or {}).get("connected"), 40)
            check(f"{attempt}: новая страница подключилась", connected, f"{time.monotonic() - pressed:.1f} с после нажатия")
            check(f"{attempt}: большое окно не открылось", wait_for(host.find, 10) and not host.is_visible())
            check(f"{attempt}: талисман вернулся", wait_for(lambda: mascot_of(after["pid"]), 15))
            check(f"{attempt}: бот сам сообщил, что вернулся", wait_for(lambda: said("на связи", count), 20))

            count = len(sent())
            push("/status")
            check(f"{attempt}: бот снова слушает команды", wait_for(lambda: len(sent()) > count, 15))

        # Настоящая поломка этой проверки при первом запуске: новый процесс сам перезапускался каждые 4 секунды.
        time.sleep(8)
        log = (HOME / "logs" / "jarvis.log").read_text("utf-8")
        check("Цикла перезапусков нет", log.count("Запуск Джарвиса") == ROUNDS + 1 and runtime().get("pid") == after["pid"], f"запусков {log.count('Запуск Джарвиса')}")

        status, _ = jarvis("POST", "/api/shutdown")
        check("Выключение нового процесса принято", status == 200)
        check("Новый процесс завершился", wait_for(lambda: not psutil.pid_exists(after["pid"]), 20))
        check("Файл запуска убран", wait_for(lambda: not (HOME / "runtime.json").exists(), 5))
        check("Окно Edge закрылось", wait_for(lambda: not host.find_all(), 10))
        log = (HOME / "logs" / "jarvis.log").read_text("utf-8")
        check("В журнале нет ошибок", "ERROR" not in log and "Traceback" not in log, log[-400:] if "ERROR" in log or "Traceback" in log else "")
        check("Выход обошёлся без принудительного завершения", "задерживают" not in log)
    finally:
        stop_ours()
        for pid in (server.pid, telegram.pid):
            stop_tree(pid)
        host.close()
    print(f"Итог: {sum(results)} из {len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

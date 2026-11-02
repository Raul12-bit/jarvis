"""Настоящая проверка жизни в фоне: окно Edge, скрытая страница, талисман, слух с закрытым окном, повторный запуск, выключение.

Настоящие: Edge и его аудиоконвейер, Silero, Vosk, окна Windows. Подменён только микрофон (WAV через поддельное устройство),
звук приглушён. Отдельная папка данных и отдельный порт: настоящий Джарвис, если запущен, не затрагивается.

Запуск: python tests/e2e/background_check.py <рабочая_папка>   (нужна модель Vosk в JARVIS_E2E_MODEL)
"""
from __future__ import annotations

import asyncio
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
import wave
from pathlib import Path
from typing import Any, Callable

import edge_tts
import miniaudio
import psutil

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
WORK = Path(sys.argv[1]).resolve()
HOME = WORK / "home-bg"
MIC = WORK / "mic-bg.wav"
PORT = 47405
TOKEN = "bg-check"

from jarvis import winapi  # noqa: E402
from jarvis.window_host import WindowHost  # noqa: E402

results: list[bool] = []
_user32 = ctypes.WinDLL("user32")


def check(name: str, ok: Any, note: str = "") -> None:
    results.append(bool(ok))
    print(("ОК   " if ok else "СБОЙ ") + name + (f" ({note})" if note else ""), flush=True)


def api(method: str, path: str) -> tuple[int, Any]:
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, headers={"x-jarvis-token": TOKEN})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:  # noqa: S310
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except OSError:
        return 0, None


def wait_for(predicate: Callable[[], Any], seconds: float = 25.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.15)
    return None


async def build_mic() -> None:
    buf = bytearray()
    for voice in ("ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"):
        try:
            async for chunk in edge_tts.Communicate("Джарвис, который час?", voice).stream():
                if chunk["type"] == "audio":
                    buf += chunk["data"]
            break
        except edge_tts.exceptions.NoAudioReceived:
            continue  # сервис иногда отказывает одному из голосов
    pcm = miniaudio.decode(bytes(buf), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=16000).samples.tobytes()
    silence = bytes(16000 * 2 * 9)
    with wave.open(str(MIC), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(silence + pcm + silence)


def presence() -> dict[str, Any]:
    return api("GET", "/api/presence")[1] or {}


def user_messages() -> list[str]:
    try:
        db = sqlite3.connect(HOME / "jarvis.db")
        return [row[0] for row in db.execute("SELECT content FROM messages WHERE role='user'")]
    except sqlite3.Error:
        return []


def mascots(server: subprocess.Popen) -> list[int]:
    try:
        root = psutil.Process(server.pid)
        pids = {root.pid, *(child.pid for child in root.children(recursive=True))}
    except psutil.Error:
        return []
    found = []
    for hwnd, _ in winapi.windows_of_class("JarvisMascot"):
        if winapi._window_pid(hwnd) in pids and winapi.user32.IsWindowVisible(hwnd):  # noqa: SLF001
            found.append(hwnd)
    return found


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    asyncio.run(build_mic())
    shutil.rmtree(HOME, ignore_errors=True)
    (HOME / "models").mkdir(parents=True)
    source = Path(os.environ["JARVIS_E2E_MODEL"])
    shutil.copytree(source, HOME / "models" / source.name)
    (HOME / "config.json").write_text(json.dumps({
        "onboarded": True, "user_name": "Тест", "shortcut_created": True, "wake_word": True, "mascot": True,
    }), encoding="utf-8")
    env = {**os.environ, "JARVIS_HOME": str(HOME), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "JARVIS_TEST_BUILD": "old-build"}
    env.pop("GROQ_API_KEY", None)
    flags = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", f"--use-file-for-fake-audio-capture={MIC}", "--mute-audio"]
    command = [sys.executable, "-m", "jarvis", f"--port={PORT}", f"--token={TOKEN}", *[f"--browser-arg={flag}" for flag in flags]]
    server = subprocess.Popen(command, cwd=PROJECT, env=env)
    host = WindowHost(f"http://127.0.0.1:{PORT}/?t={TOKEN}", HOME, PORT)
    try:
        check("Джарвис запустился и страница подключилась", wait_for(lambda: presence().get("connected"), 40))
        check("Большое окно видно", wait_for(host.is_visible, 15))
        check("Талисман появился поверх окон", wait_for(lambda: mascots(server), 10))
        check("Микрофон работает", wait_for(lambda: presence().get("mic"), 15))
        mascot = mascots(server)[0]
        exstyle = _user32.GetWindowLongPtrW(ctypes.c_void_p(mascot), -20)
        check("Талисман не в панели задач и не забирает фокус", exstyle & 0x80 and exstyle & 0x08000000)

        # Закрываем окно как крестиком.
        hwnd = host.find()
        winapi.close_window(hwnd)
        check("Окно закрылось, а Джарвис работает", wait_for(lambda: not winapi.window_exists(hwnd), 8) and server.poll() is None)
        check("Талисман остался", mascots(server))
        reconnected = wait_for(lambda: presence().get("connected") and presence().get("mic"), 25)
        check("Страница открылась заново, скрытой, и микрофон работает", reconnected)
        check("Скрытую страницу не видно", host.find() and not host.is_visible())

        # Слух с закрытым окном: в записи раз в 20 секунд звучит «Джарвис, который час?».
        heard = wait_for(lambda: any("час" in text.lower() for text in user_messages()), 45)
        check("С закрытым окном Джарвис услышал имя и команду", heard, str(user_messages()[-1:]))

        # Показать и спрятать.
        status, body = api("POST", "/api/window/show")
        check("Окно показывается", status == 200 and body["ok"] and wait_for(host.is_visible, 5))
        info = winapi.window_info(host.find())
        check("Окно не за краем экрана", info and winapi.point_on_monitor((info.rect[0] + info.rect[2]) // 2, (info.rect[1] + info.rect[3]) // 2), str(info.rect if info else None))
        status, body = api("POST", "/api/window/hide")
        check("Окно прячется", status == 200 and wait_for(lambda: not host.is_visible(), 5))

        # Повторный запуск ярлыка не должен открывать второе окно.
        second = subprocess.Popen(command, cwd=PROJECT, env=env)  # тот же код: заменять незачем
        check("Второй запуск завершился сам", wait_for(lambda: second.poll() is not None, 25) and second.returncode == 0)
        check("Второй запуск показал то же окно", wait_for(host.is_visible, 6))
        check("Окно одно", len(host.find_all()) == 1, str(len(host.find_all())))

        # Настоящая поломка прошлой версии: работает старый код, человек нажимает ярлык, а новый код так и не запускается.
        status, health = api("GET", "/health")
        check("Сервер сообщает, какой код у него в памяти", status == 200 and health.get("build") == "old-build", str(health))
        renewed = subprocess.Popen(command, cwd=PROJECT, env={**env, "JARVIS_TEST_BUILD": "new-build"})
        check("Старая версия заменена, а не просто показала окно", wait_for(lambda: server.poll() is not None, 25))
        check("Новая версия в памяти", wait_for(lambda: (api("GET", "/health")[1] or {}).get("build") == "new-build", 40))
        check("Талисман есть и у новой версии", wait_for(lambda: mascots(renewed), 15))
        check("Новая страница подключилась", wait_for(lambda: presence().get("connected"), 25))
        check("Окно одно и после замены", wait_for(lambda: len(host.find_all()) == 1, 10), str(len(host.find_all())))
        server = renewed

        # Выключение полностью.
        status, _ = api("POST", "/api/shutdown")
        check("Команда выключения принята", status == 200)
        check("Процесс завершился", wait_for(lambda: server.poll() is not None, 20))
        check("Окно Edge закрылось вместе с Джарвисом", wait_for(lambda: not host.find_all(), 10))

        # Запуск вместе с Windows: без большого окна.
        background = subprocess.Popen([*command, "--background"], cwd=PROJECT, env={**env, "JARVIS_TEST_BUILD": "new-build"})
        check("Фоновый запуск: страница подключилась", wait_for(lambda: presence().get("connected") and presence().get("mic"), 40))
        check("Фоновый запуск: большого окна не видно", host.find() and not host.is_visible())
        check("Фоновый запуск: талисман на месте", wait_for(lambda: mascots(background), 8))
        api("POST", "/api/shutdown")
        wait_for(lambda: background.poll() is not None, 20)
        server = background
        check("Скрытое окно не осталось после выключения", wait_for(lambda: not host.find_all(), 10))
    finally:
        for process in (server,):
            if process.poll() is None:
                try:
                    root = psutil.Process(process.pid)
                    for item in [*root.children(recursive=True), root]:
                        item.kill()
                except psutil.Error:
                    pass
        host.close()
    print(f"Итог: {sum(results)} из {len(results)}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

"""Сквозная проверка телефона: настоящий процесс Джарвиса, настоящий опрос по HTTP, настоящие таймеры и снимок экрана.

Подменены: сервер Telegram (fake_telegram.py) и языковая модель (fake_cloud.py). Настоящий бот не нужен, в сеть ничего не уходит.

Запуск: python tests/e2e/phone_check.py <рабочая_папка>
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import psutil

PROJECT = Path(__file__).resolve().parents[2]
WORK = Path(sys.argv[1]).resolve()
HOME = WORK / "home-phone"
PORT, CLOUD_PORT, TELEGRAM_PORT = 47395, 47396, 47397
TOKEN = "phone-check"
BOT_TOKEN = "123456:FakeTokenForLocalCheckOnly"
OWNER, STRANGER = 111, 222
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


def jarvis(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    return request(method, f"http://127.0.0.1:{PORT}{path}", body, {"x-jarvis-token": TOKEN})


def push(text: str = "", sender: int = OWNER, **fields: Any) -> None:
    request("POST", f"http://127.0.0.1:{TELEGRAM_PORT}/control/push", {"text": text, "sender": sender, **fields})


def sent() -> list[dict]:
    # Служебные отметки бота не считаются: проверки смотрят только на то, что человек увидел бы.
    return [item for item in request("GET", f"http://127.0.0.1:{TELEGRAM_PORT}/control/sent")[1]
            if item["method"] not in ("setMyCommands", "sendChatAction", "answerCallbackQuery", "deleteMessage", "editMessageReplyMarkup")]


def everything() -> list[dict]:
    return request("GET", f"http://127.0.0.1:{TELEGRAM_PORT}/control/sent")[1]


def press(data: str, message_id: int = 7, photo: bool = False) -> None:
    request("POST", f"http://127.0.0.1:{TELEGRAM_PORT}/control/press", {"sender": OWNER, "data": data, "message_id": message_id, "photo": photo})


def buttons(item: dict | None) -> list[dict]:
    markup = (item or {}).get("markup") or {}
    return [button for row in markup.get("inline_keyboard", []) for button in row]


def button_data(item: dict | None, label: str) -> str:
    return next((button["callback_data"] for button in buttons(item) if label in button["text"]), "")


def wait_for(predicate: Callable[[], Any], seconds: float = 8.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


def reply_after(count: int, seconds: float = 8.0) -> dict | None:
    def latest() -> dict | None:
        items = sent()
        return items[count] if len(items) > count else None

    return wait_for(latest, seconds)


def main() -> int:
    shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(parents=True)
    (HOME / "config.json").write_text(json.dumps({
        "onboarded": True, "wake_word": False, "user_name": "Тест", "shortcut_created": True,
    }), encoding="utf-8")
    env = {
        **os.environ, "JARVIS_HOME": str(HOME), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
        "JARVIS_TEST_BASE": f"http://127.0.0.1:{CLOUD_PORT}/v1",
        "JARVIS_TEST_TELEGRAM_BASE": f"http://127.0.0.1:{TELEGRAM_PORT}",
    }
    python = sys.executable
    # Старая заглушка на том же порту подсунула бы чужие ответы, и проверка прошла бы ложно.
    for port in (PORT, CLOUD_PORT, TELEGRAM_PORT):
        check(f"Порт {port} свободен", not _port_busy(port))
    if not all(results):
        return 1
    helpers = [
        subprocess.Popen([python, str(PROJECT / "tests" / "e2e" / "fake_cloud.py"), str(CLOUD_PORT), str(WORK / "phone-cloud.jsonl")]),
        subprocess.Popen([python, str(PROJECT / "tests" / "e2e" / "fake_telegram.py"), str(TELEGRAM_PORT)]),
    ]
    server = subprocess.Popen([python, "-m", "jarvis", "--no-window", f"--port={PORT}", f"--token={TOKEN}"], cwd=PROJECT, env=env)
    try:
        started = wait_for(lambda: _healthy(), 25)
        check("Джарвис запустился без окна", started)
        if not started:
            return 1

        # Подключение.
        status, body = jarvis("POST", "/api/phone/token", {"token": "999999:WrongTokenForLocalCheckOnly"})
        check("Чужой токен отвергнут", status == 200 and body["ok"] is False, body["message"][:40] if body else "")
        status, _ = jarvis("POST", "/api/phone/token", {"token": "совсем не токен, а просто текст"})
        check("Мусор вместо токена отвергнут до запроса", status == 422)
        status, body = jarvis("POST", "/api/phone/token", {"token": BOT_TOKEN})
        check("Токен принят, показан код и имя бота", body["ok"] and body["phone"]["code"] and body["phone"]["bot"] == "jarvis_check_bot")
        code = body["phone"]["code"]
        settings = json.dumps(jarvis("GET", "/api/settings")[1], ensure_ascii=False)
        check("Токен не отдаётся в настройках", BOT_TOKEN not in settings)

        # Привязка.
        push("Выключи ноутбук")
        first = reply_after(0)
        check("Без кода команда не выполняется", first and "личный бот" in first["text"])
        push("Привет, это кириллица")
        second = reply_after(1)
        check("Кириллица вместо кода не ломает бота", second and "личный бот" in second["text"])
        push(code)
        welcome = reply_after(2)
        check("Код делает хозяином", welcome and "К вашим услугам" in welcome["text"] and welcome["markup"])

        # Посторонний.
        count = len(sent())
        push("Открой блокнот", sender=STRANGER)
        push(code, sender=STRANGER)
        time.sleep(1.5)
        check("Посторонний получает тишину", all(item.get("chat") != STRANGER for item in sent()))
        warned = sent()[count:]
        check("Хозяин узнаёт о постороннем один раз", len(warned) == 1 and "посторонний" in warned[0]["text"], str(len(warned)))

        # Команды.
        count = len(sent())
        push("Который час?")
        reply = reply_after(count)
        check("Мгновенная команда с телефона", reply and reply["text"].startswith("Сейчас "), reply["text"] if reply else "")

        count = len(sent())
        push("Поставь таймер на шесть секунд")
        reply = reply_after(count)
        check("Действие через модель и сразу ответ", reply and reply["text"].startswith("Поставил"), reply["text"] if reply else "")

        count = len(sent())
        push("Ну всё, выруби машину", age=600)
        reply = reply_after(count)
        check("Команда старше 3 минут не выполняется", reply and "с опозданием" in reply["text"])

        count = len(sent())
        push("📸 Экран")
        reply = reply_after(count, 15)
        if reply and reply["method"] == "sendPhoto":
            check("Снимок экрана уходит фотографией", reply["bytes"] > 5000, f"{reply['bytes']} байт")
        else:
            check("Снимок экрана: честный отказ, если экран заблокирован", reply and "не получился" in reply.get("text", ""))

        # Напоминание, пока окна нет: всё должно прийти на телефон.
        reminder = wait_for(lambda: next((item for item in sent() if item.get("text", "").startswith("Напоминаю")
                                          or "проверка" in item.get("text", "") and not item["text"].startswith("Поставил")), None), 15)
        check("Напоминание приходит на телефон, пока окна нет", reminder, reminder["text"] if reminder else "")

        # Пульт: настоящий ноутбук за кнопками, но ничего опасного не нажимаем.
        count = len(sent())
        push("/menu")
        reply = reply_after(count)
        check("Пульт открывается кнопками", len(buttons(reply)) >= 9, f"{len(buttons(reply))} кнопок")

        count = len(sent())
        push("📊 Ноутбук")
        reply = reply_after(count)
        check("Сводка о настоящем ноутбуке", reply and "Процессор" in reply["text"] and "Память" in reply["text"])

        count = len(sent())
        press("sys")
        reply = reply_after(count)
        check("Кнопка обновляет то же сообщение", reply and reply["method"] == "editMessageText" and reply["message_id"] == 7)

        count = len(sent())
        press("win")
        reply = reply_after(count)
        check("Настоящие окна перечислены кнопками", reply and len(buttons(reply)) >= 2, f"{len(buttons(reply))} кнопок")

        count = len(sent())
        press("pro:memory")
        reply = reply_after(count, 12)
        check("Программы по памяти", reply and "памяти" in reply["text"] and buttons(reply))

        count = len(sent())
        press("med")
        reply = reply_after(count)
        check("Панель музыки", reply and "Громкость" in reply["text"])

        count = len(sent())
        press("fil")
        reply = reply_after(count)
        check("Панель файлов", reply and button_data(reply, "Загрузки"))

        # Опасная кнопка спрашивает, а «Нет» отменяет. «Да» здесь не нажимается ни в коем случае.
        count = len(sent())
        press("pa:shutdown")
        reply = reply_after(count)
        answer_no = button_data(reply, "Нет")
        check("Выключение сначала спрашивает", reply and "Выключить ноутбук" in reply["text"] and answer_no)
        count = len(sent())
        press(answer_no)
        reply = reply_after(count)
        check("«Нет» отменяет", reply and "Не буду" in reply["text"])

        # Новые пульты: настоящие данные ноутбука, ничего необратимого.
        for data, needle, name in (("mou", "Мышь", "Панель мыши"), ("bro", "Браузер", "Панель браузера"), ("ter", "Папка:", "Панель терминала"),
                                   ("tod", "/remind", "Панель дел"), ("net", "сети", "Панель сети"), ("jar", "J.A.R.V.I.S.", "Панель Джарвиса"),
                                   ("drv", "Диски", "Диски ноутбука")):
            count = len(sent())
            press(data)
            reply = reply_after(count, 12)
            check(name, reply and needle in reply["text"], reply["text"][:50] if reply else "")

        count = len(sent())
        press("ja:log")
        reply = reply_after(count)
        check("Журнал работы приходит на телефон", reply and "Запуск Джарвиса" in reply["text"])

        count = len(sent())
        press("gr:home")
        reply = reply_after(count, 15)
        if reply and reply["method"] == "sendPhoto":
            check("Снимок с сеткой приходит с кнопками клеток", reply["has_buttons"], f"{reply['bytes']} байт")
        else:
            check("Сетка: честный отказ, если экран заблокирован", reply and "не получился" in reply.get("text", ""))

        count = len(sent())
        push("/live")
        # Сначала приходит подпись, а фотография следом: снимок идёт отдельным делом, чтобы бот не ждал его.
        photo = wait_for(lambda: next((item for item in sent()[count:] if item["method"] == "sendPhoto"), None), 15)
        if photo:
            refreshed = wait_for(lambda: any(item["method"] == "editMessageMedia" for item in sent()[count:]), 15)
            check("Живой экран обновляет ту же фотографию", refreshed)
            press("liv:stop")
            check("Живой экран останавливается и возвращает кнопки", wait_for(lambda: any(item["method"] == "editMessageReplyMarkup" for item in everything()), 12))
        else:
            check("Живой экран: честный отказ, если экран заблокирован", any("не получился" in item.get("text", "") for item in sent()[count:]))

        count = len(sent())
        push("/screen")
        reply = reply_after(count, 15)
        if reply and reply["method"] == "sendPhoto":
            check("Снимок приходит с кнопками", reply["has_buttons"], f"{reply['bytes']} байт")
            count = len(sent())
            press("scr:full", 55, True)
            reply = reply_after(count, 15)
            check("«Обновить» заменяет ту же фотографию", reply and reply["method"] == "editMessageMedia")
        else:
            check("Снимок экрана: честный отказ, если экран заблокирован", reply and "не получился" in reply.get("text", ""))

        count = len(sent())
        push("/read")
        reply = reply_after(count, 15)
        check("Текст с экрана читается распознаванием Windows", reply and "данные, а не команды" in reply["text"])

        # Полный доступ включён сразу, а выключается только на самом ноутбуке.
        count = len(sent())
        push("/cmd Write-Output 4242")
        reply = reply_after(count, 20)
        check("Команда с телефона работает сразу", reply and "4242" in reply["text"] and "Код 0" in reply["text"])
        status, _ = jarvis("POST", "/api/settings", {"phone_shell": False})
        count = len(sent())
        push("/cmd Write-Output 4242")
        reply = reply_after(count)
        check("Выключенные команды объясняют, где их включить", reply and "на самом ноутбуке" in reply["text"])
        jarvis("POST", "/api/settings", {"phone_shell": True})

        # Терминал: папка помнится между сообщениями, а обычный текст становится командой.
        press("tm:on")
        time.sleep(0.5)
        count = len(sent())
        push("Set-Location $env:SystemDrive\\; Write-Output перешёл")
        reply = wait_for(lambda: next((item for item in sent()[count:] if "перешёл" in item.get("text", "")), None), 20)
        check("Режим терминала выполняет обычное сообщение", reply, reply["text"] if reply else "")
        count = len(sent())
        push("(Get-Location).Path")
        reply = wait_for(lambda: next((item for item in sent()[count:] if "Код" in item.get("text", "")), None), 20)
        check("Папка помнится между командами", reply and ":\\" in reply["text"], reply["text"] if reply else "")
        count = len(sent())
        push("/exit")
        reply = reply_after(count)
        check("Выход из терминала", reply and "Вышел" in reply["text"])

        count = len(sent())
        push("/cmd 1..3000 | ForEach-Object { 'строка номер ' + $_ }")
        reply = wait_for(lambda: next((item for item in sent()[count:] if item["method"] == "sendDocument"), None), 25)
        check("Длинный вывод приходит файлом", reply, f"{reply['bytes']} байт" if reply else "")

        # Долгая команда не держит бота.
        count = len(sent())
        push("/cmd Start-Sleep -Seconds 7; Write-Output дождался")
        time.sleep(1)
        push("/status")
        quick = wait_for(lambda: next((item for item in sent()[count:] if "Процессор" in item.get("text", "")), None), 5)
        check("Пока идёт долгая команда, бот отвечает на другое", quick)
        offer = wait_for(lambda: next((item for item in sent()[count:] if "ещё выполняется" in item.get("text", "")), None), 8)
        check("Долгая команда предлагает остановку", offer and button_data(offer, "Остановить"))
        finished = wait_for(lambda: any("дождался" in item.get("text", "") for item in sent()[count:]), 15)
        check("Долгая команда всё равно доходит до конца", finished)

        # ПИН закрывает команды с телефона и задаётся только на ноутбуке.
        status, _ = jarvis("POST", "/api/phone/pin", {"pin": "4242"})
        check("ПИН задан", status == 200)
        count = len(sent())
        push("/cmd Write-Output секрет")
        reply = reply_after(count)
        check("С ПИНом команда не выполняется", reply and "Нужен ПИН" in reply["text"])
        count = len(sent())
        push("/unlock 0000")
        reply = reply_after(count)
        check("Неверный ПИН не открывает", reply and "Неверный" in reply["text"])
        count = len(sent())
        push("/unlock 4242")
        reply = reply_after(count)
        check("Верный ПИН открывает", reply and "Открыто" in reply["text"])
        check("Сообщение с ПИНом стёрто", any(item["method"] == "deleteMessage" for item in everything()))
        count = len(sent())
        push("/cmd Write-Output открыто")
        reply = wait_for(lambda: next((item for item in sent()[count:] if "открыто" in item.get("text", "")), None), 20)
        check("После ПИНа команда выполняется", reply)
        jarvis("POST", "/api/phone/pin", {"pin": ""})

        count = len(sent())
        push("/say проверка")
        reply = reply_after(count)
        check("Голос без окна: честный отказ", reply and "недоступен" in reply["text"])

        # Перед отключением проверяем менее частые команды: ни одна не должна уйти в модель.
        for command in ("/ip", "/note купить молоко проверка", "/remind 500м проверка", "/todo", "/help"):
            count = len(sent())
            push(command)
            reply = reply_after(count, 15)
            check(f"Команда {command.split()[0]}", reply and reply.get("text"), (reply or {}).get("text", "")[:50])

        status, body = jarvis("DELETE", "/api/phone")
        check("Телефон отключается", status == 200 and not body["configured"])
        count = len(sent())
        push("Который час?")
        time.sleep(3)
        check("После отключения бот больше не отвечает", len(sent()) == count)
    finally:
        for process in (server, *helpers):
            _stop_tree(process.pid)
    print(f"Итог: {sum(results)} из {len(results)}")
    return 0 if all(results) else 1


def _port_busy(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _stop_tree(pid: int) -> None:
    # Python из venv на Windows это запускатель с настоящим процессом внутри: иначе заглушка останется держать порт.
    try:
        root = psutil.Process(pid)
        for process in [*root.children(recursive=True), root]:
            process.kill()
    except psutil.Error:
        pass


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as resp:  # noqa: S310
            return json.load(resp).get("app") == "jarvis"
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())

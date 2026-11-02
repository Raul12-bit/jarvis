"""Управление с телефона: подменён только сервер Telegram. Чужой человек не должен получить ноутбук ни при каких условиях."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from jarvis import phone
from jarvis.config import ConfigStore
from jarvis.phone import PhoneBridge

from .conftest import mock_client

OWNER = 111
STRANGER = 222
TOKEN = "123:abc-DEF_token"


class World:
    """Сервер Telegram и то, что ноутбук успел сделать."""

    def __init__(self, config: ConfigStore) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[str] = []
        self.reply = ("Готово.", False)
        self.jpeg: bytes | None = b"jpeg"
        self.heard = "Открой ютуб"
        self.changes = 0
        self.updates: list[dict[str, Any]] = []
        self.get_me_status = 200
        self.fail_file = False

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            if request.method == "POST" and request.headers.get("content-type", "").startswith("application/json"):
                self.calls.append((method, json.loads(request.content)))
            else:
                self.calls.append((method, dict(request.url.params)))
            if method == "getMe":
                return httpx.Response(self.get_me_status, json={"result": {"username": "jarvis_bot"}})
            if method == "getUpdates":
                updates, self.updates = self.updates, []
                return httpx.Response(200, json={"result": updates})
            if method == "getFile":
                return httpx.Response(500 if self.fail_file else 200, json={"result": {"file_path": "voice/1.ogg"}})
            if "/file/bot" in request.url.path:
                return httpx.Response(200, content=b"ogg")
            return httpx.Response(200, json={"ok": True})

        async def answer(text: str) -> tuple[str, bool]:
            self.commands.append(text)
            return self.reply

        async def screenshot() -> bytes | None:
            return self.jpeg

        async def transcribe(audio: bytes, filename: str, mime: str) -> str:
            assert audio == b"ogg"
            return self.heard

        def changed() -> None:
            self.changes += 1

        self.config = config
        self.bridge = PhoneBridge(config, mock_client(handler), answer, screenshot, transcribe, changed)

    def sent_texts(self) -> list[str]:
        return [body["text"] for method, body in self.calls if method == "sendMessage"]

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


@pytest.fixture
def world(config: ConfigStore) -> World:
    config.update(telegram_bot_token=TOKEN)
    return World(config)


@pytest.fixture
def paired(world: World) -> World:
    world.config.update(telegram_owner_id=OWNER)
    return world


def message(text: str = "", sender: int = OWNER, kind: str = "private", age: float = 0, **extra: Any) -> dict[str, Any]:
    body = {"from": {"id": sender}, "chat": {"id": sender, "type": kind}, "date": int(time.time() - age), **extra}
    if text:
        body["text"] = text
    return body


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------- Привязка ----------


def test_right_code_makes_owner(world: World) -> None:
    run(world.bridge.handle(message(world.bridge.code)))
    assert world.config.settings.telegram_owner_id == OWNER
    assert world.bridge.status()["paired"] and not world.bridge.status()["code"]
    assert "К вашим услугам" in world.sent_texts()[0]
    assert world.commands == []  # код сам по себе не команда


def test_wrong_code_gives_nothing(world: World) -> None:
    run(world.bridge.handle(message("000000")))
    assert world.config.settings.telegram_owner_id == 0
    assert world.commands == []
    assert "личный бот" in world.sent_texts()[0]


@pytest.mark.parametrize("text", ["Привет", "🎉", "открой ютуб", "выключи ноутбук"])
def test_any_text_is_safe_before_pairing(world: World, text: str) -> None:
    """compare_digest падал на кириллице, и бот переставал отвечать."""
    run(world.bridge.handle(message(text)))
    assert world.config.settings.telegram_owner_id == 0
    assert world.commands == []


def test_voice_and_photos_before_pairing_do_nothing(world: World) -> None:
    run(world.bridge.handle(message(voice={"file_id": "f"})))
    assert world.commands == [] and world.config.settings.telegram_owner_id == 0
    assert "getFile" not in world.methods()


def test_code_changes_after_several_misses(world: World) -> None:
    first = world.bridge.code
    for _ in range(phone._PAIR_ATTEMPTS):  # noqa: SLF001
        run(world.bridge.handle(message("000000", sender=STRANGER)))
    assert world.bridge.code != first
    run(world.bridge.handle(message(first, sender=STRANGER)))
    assert world.config.settings.telegram_owner_id == 0


def test_start_command_is_not_a_miss(world: World) -> None:
    first = world.bridge.code
    for _ in range(phone._PAIR_ATTEMPTS + 2):  # noqa: SLF001
        run(world.bridge.handle(message("/start")))
    assert world.bridge.code == first


def test_second_person_cannot_pair_after_owner(paired: World) -> None:
    code = paired.bridge.code
    run(paired.bridge.handle(message(code, sender=STRANGER)))
    assert paired.config.settings.telegram_owner_id == OWNER
    assert all(body.get("chat_id") != STRANGER for _, body in paired.calls)


# ---------- Посторонние ----------


@pytest.mark.parametrize("text", ["Выключи ноутбук", "/start", "📸 Экран", "Да"])
def test_stranger_gets_silence(paired: World, text: str) -> None:
    run(paired.bridge.handle(message(text, sender=STRANGER)))
    assert paired.commands == []
    # Постороннему ни слова, а хозяин узнаёт: это повод проверить, не утёк ли токен.
    assert [body["chat_id"] for _, body in paired.calls] == [OWNER]


def test_owner_is_warned_about_strangers_once(paired: World) -> None:
    for _ in range(3):
        run(paired.bridge.handle(message("Открой", sender=STRANGER)))
    assert len(paired.calls) == 1 and "посторонний" in paired.sent_texts()[0]


def test_group_chat_is_ignored_even_from_owner(paired: World) -> None:
    run(paired.bridge.handle(message("Выключи ноутбук", kind="group")))
    assert paired.commands == [] and paired.calls == []


# ---------- Команды ----------


def test_owner_command_runs_and_answer_comes_back(paired: World) -> None:
    run(paired.bridge.handle(message("Открой VS Code")))
    assert paired.commands == ["Открой VS Code"]
    assert paired.sent_texts() == ["Готово."]
    assert "sendChatAction" in paired.methods()


def test_confirmation_gets_yes_no_keyboard(paired: World) -> None:
    paired.reply = ("Выключить ноутбук?", True)
    run(paired.bridge.handle(message("Выключи ноутбук")))
    body = next(body for method, body in paired.calls if method == "sendMessage")
    assert body["reply_markup"]["keyboard"] == [["Да", "Нет"]]


def test_old_command_is_not_executed(paired: World) -> None:
    """Ноутбук мог спать: «выключи» недельной давности выполнять нельзя."""
    run(paired.bridge.handle(message("Выключи ноутбук", age=phone._STALE_SECONDS + 60)))  # noqa: SLF001
    assert paired.commands == []
    assert "с опозданием" in paired.sent_texts()[0]


def test_old_yes_does_not_confirm(paired: World) -> None:
    run(paired.bridge.handle(message("Да", age=phone._STALE_SECONDS + 1)))  # noqa: SLF001
    assert paired.commands == []


def test_start_shows_keyboard_without_running_anything(paired: World) -> None:
    run(paired.bridge.handle(message("/start")))
    assert paired.commands == []
    body = next(body for method, body in paired.calls if method == "sendMessage")
    assert "keyboard" in body["reply_markup"]


def test_unsupported_message_is_explained(paired: World) -> None:
    run(paired.bridge.handle(message(photo=[{"file_id": "f"}])))
    assert paired.commands == [] and "голосовые" in paired.sent_texts()[0]


def test_long_answer_is_split(paired: World) -> None:
    paired.reply = ("я" * (phone._MESSAGE_LIMIT + 10), False)  # noqa: SLF001
    run(paired.bridge.handle(message("Расскажи длинно")))
    assert [len(text) for text in paired.sent_texts()] == [phone._MESSAGE_LIMIT, 10]  # noqa: SLF001


def test_empty_answer_still_replies(paired: World) -> None:
    paired.reply = ("", False)
    run(paired.bridge.handle(message("Сделай")))
    assert paired.sent_texts() == ["Готово, сэр."]


# ---------- Голосовые ----------


def test_voice_is_transcribed_echoed_and_run(paired: World) -> None:
    run(paired.bridge.handle(message(voice={"file_id": "f", "mime_type": "audio/ogg"})))
    assert paired.commands == ["Открой ютуб"]
    assert paired.sent_texts()[0] == "Услышал: Открой ютуб"


def test_voice_failure_runs_nothing(paired: World) -> None:
    paired.fail_file = True
    run(paired.bridge.handle(message(voice={"file_id": "f"})))
    assert paired.commands == [] and "текстом" in paired.sent_texts()[0]


def test_silent_voice_runs_nothing(paired: World) -> None:
    paired.heard = ""
    run(paired.bridge.handle(message(voice={"file_id": "f"})))
    assert paired.commands == [] and "ничего не расслышал" in paired.sent_texts()[0]


# ---------- Снимок экрана ----------


def test_screen_button_sends_photo(paired: World) -> None:
    run(paired.bridge.handle(message("📸 Экран")))
    assert "sendPhoto" in paired.methods() and paired.commands == []


def test_screen_respects_privacy_setting(paired: World) -> None:
    paired.config.update(allow_screen=False)
    run(paired.bridge.handle(message("/screen")))
    assert "sendPhoto" not in paired.methods()
    assert "выключен" in paired.sent_texts()[0]


def test_screen_failure_is_reported(paired: World) -> None:
    paired.jpeg = None
    run(paired.bridge.handle(message("📸 Экран")))
    assert "sendPhoto" not in paired.methods() and "не получился" in paired.sent_texts()[0]


# ---------- Токен и связь ----------


def test_status_never_contains_token(world: World) -> None:
    assert TOKEN not in json.dumps(world.bridge.status())


def test_public_settings_hide_token(world: World) -> None:
    world.config.update(telegram_owner_id=OWNER)
    public = json.dumps(world.config.public())
    assert TOKEN not in public and str(OWNER) not in public


def test_bad_token_is_not_saved(world: World) -> None:
    world.get_me_status = 401
    ok, detail = run(world.bridge.set_token("999:bad"))
    assert not ok and "не принял" in detail
    assert world.config.settings.telegram_bot_token == TOKEN


def test_new_bot_needs_new_pairing_and_numbering(paired: World) -> None:
    """Старый номер обновления скрыл бы все сообщения нового бота."""
    paired.bridge._offset = 5000  # noqa: SLF001
    ok, name = run(paired.bridge.set_token("999:another"))
    assert ok and name == "jarvis_bot"
    assert paired.config.settings.telegram_owner_id == 0
    assert paired.bridge.code and paired.bridge._offset == 0  # noqa: SLF001


def test_same_token_keeps_owner(paired: World) -> None:
    run(paired.bridge.set_token(TOKEN))
    assert paired.config.settings.telegram_owner_id == OWNER


def test_forget_removes_everything(paired: World) -> None:
    paired.bridge.forget()
    assert paired.config.settings.telegram_bot_token == "" and paired.config.settings.telegram_owner_id == 0
    assert not paired.bridge.active and paired.changes == 1


def test_command_is_not_repeated_if_reply_failed(paired: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Важнее не выключить ноутбук дважды, чем обязательно ответить."""
    paired.updates = [{"update_id": 7, "message": message("Сделай")}]

    async def broken_send(*args: Any, **kwargs: Any) -> None:
        raise httpx.ConnectError("нет связи")

    monkeypatch.setattr(paired.bridge, "send", broken_send)
    with pytest.raises(httpx.ConnectError):
        run(paired.bridge._poll())  # noqa: SLF001
    assert paired.commands == ["Сделай"]
    assert paired.bridge._offset == 8  # noqa: SLF001
    run(paired.bridge._poll())  # noqa: SLF001
    assert paired.commands == ["Сделай"]


def test_answer_from_old_bot_is_dropped(paired: World, monkeypatch: pytest.MonkeyPatch) -> None:
    paired.updates = [{"update_id": 7, "message": message("Сделай")}]
    real_get = paired.bridge.http.get

    async def get_and_switch(*args: Any, **kwargs: Any) -> httpx.Response:
        response = await real_get(*args, **kwargs)
        paired.config.update(telegram_bot_token="999:another", telegram_owner_id=OWNER)
        return response

    monkeypatch.setattr(paired.bridge.http, "get", get_and_switch)
    run(paired.bridge._poll())  # noqa: SLF001
    assert paired.commands == []


def test_notify_needs_pairing_and_survives_network(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    run(world.bridge.notify("Чай"))
    assert world.calls == []
    world.config.update(telegram_owner_id=OWNER)
    run(world.bridge.notify("Чай"))
    assert world.sent_texts() == ["Чай"]

    async def broken_send(*args: Any, **kwargs: Any) -> None:
        raise httpx.ConnectError("нет связи")

    monkeypatch.setattr(world.bridge, "send", broken_send)
    run(world.bridge.notify("Чай"))


def test_run_waits_quietly_without_token(config: ConfigStore) -> None:
    world = World(config)

    async def body() -> None:
        task = asyncio.create_task(world.bridge.run())
        await asyncio.sleep(0.02)
        assert world.calls == []
        task.cancel()

    run(body())


def test_run_reports_revoked_token_once(world: World) -> None:
    world.get_me_status = 401

    async def body() -> None:
        task = asyncio.create_task(world.bridge.run())
        await asyncio.sleep(0.05)
        assert "не принял" in world.bridge.status()["error"]
        assert world.methods() == ["getMe"]  # не долбим Telegram плохим токеном
        task.cancel()

    run(body())

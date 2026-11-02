"""Джарвис с телефона через бота Telegram: пишете откуда угодно, а он выполняет на ноутбуке.

Почему бот, а не отдельное приложение: ноутбук сам забирает сообщения с серверов Telegram, поэтому не нужны ни свой
сервер, ни проброс портов, ни сборка под Android и iOS. Уведомления, голосовые и фото работают сразу и бесплатно.

Управлять может только один человек: первым прислал код из настроек Джарвиса, и его номер записан как владелец.
Чужие сообщения молча игнорируются: для постороннего этот бот просто не отвечает.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

from . import system
from .config import ConfigStore
from .phone_panels import Panels

if TYPE_CHECKING:
    from .laptop import Laptop

log = logging.getLogger("jarvis.phone")

API = "https://api.telegram.org"
_POLL_SECONDS = 25
_MESSAGE_LIMIT = 4000
_STALE_SECONDS = 180  # команду, пролежавшую дольше, выполнять опасно: ноутбук мог спать, а ситуация измениться
_RETRY_SECONDS = (2, 5, 15, 30, 60)
_PAIR_ATTEMPTS = 5  # код из шести цифр нельзя перебрать, если он меняется после нескольких промахов
_DOWNLOAD_LIMIT = 20 * 2**20  # больше Bot API скачать не даёт
_COMMAND = re.compile(r"^/([a-zA-Zа-яА-Я_]+)(?:@\w+)?(?:\s+(.*))?$", re.S)
# Старые подписи тоже: у человека на телефоне клавиатура прошлой версии остаётся, пока бот не пришлёт новую.
_BUTTONS = {"📸 Экран": ("screen", ""), "🪟 Окна": ("windows", ""), "📊 Система": ("status", ""), "📊 Ноутбук": ("status", ""), "🎵 Звук": ("media", ""),
            "🎵 Музыка": ("media", ""), "📁 Файлы": ("files", ""), "⚡ Питание": ("power", ""), "🚀 Сценарии": ("routines", ""), "⌨️ Клавиши": ("keys", ""),
            "🖱 Мышь": ("mouse", ""), "💻 Терминал": ("terminal", ""), "🤖 Джарвис": ("jarvis", ""), "🏠 Пульт": ("menu", "")}
_KEYBOARD = {
    "keyboard": [["📸 Экран", "🪟 Окна", "📊 Система"], ["🎵 Звук", "📁 Файлы", "⚡ Питание"], ["🖱 Мышь", "⌨️ Клавиши", "💻 Терминал"],
                 ["🚀 Сценарии", "🤖 Джарвис", "🏠 Пульт"]],
    "resize_keyboard": True,
}
_CONFIRM_KEYBOARD = {"keyboard": [["Да", "Нет"]], "resize_keyboard": True, "one_time_keyboard": True}
_COMMANDS = [
    ("menu", "Пульт ноутбука"), ("screen", "Снимок экрана"), ("live", "Живой экран на 2 минуты"), ("tap", "Навести мышь по сетке"),
    ("mouse", "Мышь"), ("keys", "Нажать клавиши"), ("type", "Напечатать текст"), ("click", "Нажать надпись на экране"),
    ("browser", "Браузер: вкладки, назад, обновить"), ("open", "Открыть сайт или программу"), ("windows", "Открытые окна"),
    ("status", "Батарея, память, диск"), ("ps", "Что грузит ноутбук"), ("kill", "Завершить программу"), ("net", "Сеть, Wi-Fi, Bluetooth"),
    ("ip", "Внешний адрес"), ("media", "Музыка и громкость"), ("vol", "Громкость в процентах"), ("bright", "Яркость в процентах"),
    ("files", "Файлы на ноутбуке"), ("ls", "Показать папку"), ("get", "Прислать файл или папку"), ("cat", "Показать текст файла"),
    ("find", "Найти файл"), ("mkdir", "Создать папку"), ("mv", "Переместить: откуда | куда"), ("cp", "Скопировать: откуда | куда"),
    ("rm", "В корзину"), ("zip", "Прислать папку архивом"), ("cmd", "Команда PowerShell"), ("terminal", "Режим терминала"),
    ("exit", "Выйти из терминала"), ("camera", "Снимок с камеры"), ("look", "Спросить о том, что на экране"), ("read", "Прочитать текст с экрана"),
    ("say", "Сказать вслух на ноутбуке"), ("notify", "Записка на экране ноутбука"), ("clip", "Буфер обмена"), ("todo", "Дела и напоминания"),
    ("note", "Записать дело"), ("remind", "Напомнить: 10м текст"), ("routines", "Сценарии"), ("power", "Блокировка, сон, выключение"),
    ("shutdown", "Выключить через N минут"), ("lock", "Заблокировать экран"), ("jarvis", "Состояние Джарвиса"), ("mic", "Микрофон вкл или выкл"),
    ("show", "Показать окно Джарвиса"), ("log", "Журнал работы"), ("check", "Проверить все сервисы"), ("restart", "Перезапустить Джарвиса"),
    ("unlock", "Открыть команды ПИНом"), ("lockbot", "Закрыть команды"), ("help", "Что я умею"),
]
_WELCOME = ("К вашим услугам, сэр. Теперь ноутбук слушается вас с телефона.\n\n"
            "Пишите или отправляйте голосовые, как говорите мне дома: «открой VS Code», «включи музыку», «напиши Алихану, что опоздаю», "
            "«скажи вслух, что я скоро буду».\n\n"
            "Кнопки снизу работают сразу и без модели: экран, окна, система, звук, файлы, питание, мышь, клавиши, терминал, сценарии. "
            "Весь пульт: /menu, все команды: /help.\n\n"
            "Пришлите файл, и он окажется на ноутбуке. Пришлите фото с вопросом в подписи, и я отвечу, что на нём.\n\n"
            "Опасные действия я сначала уточню.")
_HELP = ("Экран: /screen снимок, /live живой экран, /tap навести мышь по сетке, /look вопрос о том, что видно, /read текст с экрана.\n\n"
         "Ввод: /mouse мышь, /keys ctrl+s, /type текст, /click Сохранить, /clip текст, /browser вкладки и навигация, /open github.com.\n\n"
         "Система: /status сводка, /ps что грузит, /kill chrome, /windows окна, /net сеть, /ip внешний адрес, /vol 30, /bright 50, /power питание, "
         "/shutdown 30 выключить через 30 минут, /lock блокировка.\n\n"
         "Файлы: /files обзор, /ls папка, /get файл или папка, /cat показать текст, /find название, /mkdir папка, /mv откуда | куда, /cp откуда | куда, "
         "/rm в корзину, /zip архивом. Присланный файл сохраняется на ноутбуке.\n\n"
         "Полный доступ: /cmd команда PowerShell, /terminal режим терминала, /exit выйти, /camera снимок с камеры. С ПИНом: /unlock ПИН и /lockbot.\n\n"
         "Голос и записки: /say текст вслух, /notify записка на экране, /mic вкл или выкл.\n\n"
         "Дела: /todo список, /note дело, /remind 10м текст, /remind 18:30 текст, /routines сценарии.\n\n"
         "Сам Джарвис: /jarvis состояние, /show окно, /log журнал, /check проверка сервисов, /restart перезапуск.\n\n"
         "Всё остальное просто напишите словами, сэр.")
_STRANGER_ALERT_SECONDS = 3600.0


def _new_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


class PhoneBridge:
    def __init__(
        self,
        config: ConfigStore,
        http: httpx.AsyncClient,
        answer: Callable[[str], Awaitable[tuple[str, bool]]],
        screenshot: Callable[[], Awaitable[bytes | None]],
        transcribe: Callable[[bytes, str, str], Awaitable[str]],
        on_change: Callable[[], None],
        laptop: Laptop | None = None,
        vision: Callable[[bytes, str], Awaitable[str]] | None = None,
    ) -> None:
        self.config = config
        self.http = http
        self.answer = answer
        self.screenshot = screenshot
        self.transcribe = transcribe
        self.on_change = on_change
        self.vision = vision
        self.panels = Panels(self, laptop) if laptop is not None else None
        self.laptop = laptop
        self.base = os.environ.get("JARVIS_TEST_TELEGRAM_BASE", API)
        # В настоящем цикле реплики идут отдельными задачами: иначе команда на две минуты держала бы весь бот.
        self.concurrent = False
        self._last_message: tuple[int, int] = (0, 0)
        self._stranger_alert_at = -_STRANGER_ALERT_SECONDS
        self.code = _new_code()
        self.bot_name = ""
        self.error = ""
        self._wake = asyncio.Event()
        self._offset = 0
        self._failures = 0
        self._wrong_codes = 0
        self._photos: set[int] = set()
        self._commands_set = False
        self._tasks: set[asyncio.Task] = set()

    # ---------- Состояние ----------

    @property
    def token(self) -> str:
        return self.config.settings.telegram_bot_token

    @property
    def owner(self) -> int:
        return self.config.settings.telegram_owner_id

    @property
    def active(self) -> bool:
        return bool(self.token)

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.token), "paired": bool(self.owner), "bot": self.bot_name,
            "code": self.code if self.token and not self.owner else "", "error": self.error,
        }

    def _url(self, method: str, token: str | None = None) -> str:
        return f"{self.base}/bot{token or self.token}/{method}"

    async def check_token(self, token: str) -> tuple[bool, str]:
        """Проверяет токен настоящим запросом. Возвращает (годится ли, имя бота или причина)."""
        try:
            resp = await self.http.get(self._url("getMe", token), timeout=httpx.Timeout(10))
        except httpx.HTTPError as exc:
            return False, f"Нет связи с Telegram ({type(exc).__name__})"
        if resp.status_code != 200:
            return False, "Telegram не принял токен: проверь, что скопирован целиком"
        return True, str((resp.json().get("result") or {}).get("username", ""))

    async def set_token(self, token: str) -> tuple[bool, str]:
        ok, detail = await self.check_token(token)
        if not ok:
            return False, detail
        if token != self.token:
            # У другого бота своя нумерация сообщений и другой хозяин: старый номер скрыл бы все новые сообщения.
            self.config.update(telegram_bot_token=token, telegram_owner_id=0)
            self._reset_pairing()
        self.bot_name = detail
        self.error = ""
        self._failures = 0
        self._commands_set = False
        self._wake.set()
        self.on_change()
        return True, detail

    def forget(self) -> None:
        self.config.update(telegram_bot_token="", telegram_owner_id=0)
        self.bot_name = ""
        self.error = ""
        self._reset_pairing()
        self.on_change()

    def _reset_pairing(self) -> None:
        self.code = _new_code()
        self._offset = 0
        self._wrong_codes = 0

    # ---------- Цикл ----------

    def lock(self) -> None:
        """ПИН сменили на ноутбуке: прошлое разрешение больше не действует."""
        if self.panels is not None:
            self.panels.lock()

    async def run(self) -> None:
        self.concurrent = True
        while True:
            if not self.token:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                if not self.bot_name:
                    ok, detail = await self.check_token(self.token)
                    if ok:
                        self.bot_name = detail
                        self.on_change()
                    elif "не принял" in detail:
                        self.error = detail
                        self.on_change()
                        self._wake.clear()
                        await self._wake.wait()
                        continue
                await self._register_commands()
                await self._poll()
                self._failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                # Обрыв связи для ноутбука обычное дело: тихо ждём и пробуем снова.
                delay = _RETRY_SECONDS[min(self._failures, len(_RETRY_SECONDS) - 1)]
                self._failures += 1
                log.info("Telegram недоступен (%s), повтор через %d с", type(exc).__name__, delay)
                await asyncio.sleep(delay)
            except Exception:  # noqa: BLE001 — сбой обработки сообщения не должен останавливать связь с телефоном
                log.exception("Сбой при обработке сообщения с телефона")
                await asyncio.sleep(2)

    async def _register_commands(self) -> None:
        """Меню команд в самом Telegram: человеку не нужно их помнить."""
        if self._commands_set or self.panels is None:
            return
        self._commands_set = True
        commands = [{"command": name, "description": text} for name, text in _COMMANDS]
        await self.http.post(self._url("setMyCommands"), json={"commands": commands}, timeout=httpx.Timeout(10))

    async def _poll(self) -> None:
        token = self.token
        resp = await self.http.get(
            self._url("getUpdates", token),
            params={"timeout": _POLL_SECONDS, "offset": self._offset, "allowed_updates": '["message","callback_query"]'},
            timeout=httpx.Timeout(connect=10, read=_POLL_SECONDS + 10, write=10, pool=5),
        )
        if token != self.token:
            return  # пока шёл запрос, человек сменил или отключил бота
        if resp.status_code == 401:
            self.error = "Telegram больше не принимает токен бота"
            self.on_change()
            self._wake.clear()
            await self._wake.wait()
            return
        if resp.status_code == 409:
            # Кто-то ещё забирает сообщения этого бота: другая копия Джарвиса.
            log.info("Сообщения бота забирает другая программа")
            await asyncio.sleep(10)
            return
        resp.raise_for_status()
        for update in resp.json().get("result") or []:
            self._offset = int(update["update_id"]) + 1
            message = update.get("message")
            if isinstance(message, dict):
                await self.handle(message)
            query = update.get("callback_query")
            if isinstance(query, dict):
                await self.handle_callback(query)

    def spawn(self, coroutine: Any) -> None:
        """Долгое дело не должно держать очередь: пока идёт команда на две минуты, бот обязан отвечать на остальное."""
        task = asyncio.create_task(self._guarded(coroutine))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guarded(coroutine: Any) -> None:
        try:
            await coroutine
        except httpx.HTTPError as exc:
            log.info("Telegram не ответил на отложенное дело: %s", type(exc).__name__)
        except Exception:  # noqa: BLE001 — сбой одного дела не должен оставаться незамеченным
            log.exception("Сбой отложенного дела бота")

    # ---------- Сообщения ----------

    async def handle(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private" or not sender.get("id"):
            return
        sender_id = int(sender["id"])
        chat_id = int(chat["id"])
        text = str(message.get("text") or "").strip()
        if not self.owner:
            await self._pair(sender_id, chat_id, text)
            return
        if sender_id != self.owner:
            log.info("Сообщение боту от постороннего проигнорировано")
            await self._warn_about_stranger()
            return
        self._last_message = (chat_id, int(message.get("message_id") or 0))
        age = time.time() - float(message.get("date") or time.time())
        if age > _STALE_SECONDS:
            minutes = round(age / 60)
            await self.send(chat_id, f"Это сообщение дошло до ноутбука с опозданием на {minutes} мин, он был недоступен. "
                                     "Выполнять не стал, сэр: повторите, если ещё нужно.")
            return
        if await self._incoming_file(chat_id, message):
            return
        voice = message.get("voice") or message.get("audio")
        if voice:
            text = await self._voice_text(chat_id, voice)
            if not text:
                return
        if not text:
            await self.send(chat_id, "Пока понимаю текст, голосовые, файлы и фото, сэр.")
            return
        if text == "/start":
            await self.send(chat_id, _WELCOME, _KEYBOARD)
            return
        if text == "/help":
            await self.send(chat_id, _HELP, _KEYBOARD)
            return
        if await self._route_command(chat_id, text):
            return
        if self.panels is not None and self.panels.terminal_on:
            await self.panels.terminal_line(chat_id, text)
            return
        if self.concurrent:
            self.spawn(self.run_text(chat_id, text))
        else:
            await self.run_text(chat_id, text)

    async def run_text(self, chat_id: int, text: str) -> None:
        await self.typing(chat_id)
        reply, asks = await self.answer(text)
        await self.send(chat_id, reply or "Готово, сэр.", _CONFIRM_KEYBOARD if asks else _KEYBOARD)

    async def _warn_about_stranger(self) -> None:
        """Посторонний нашёл бота. Ему не отвечаем, а хозяин узнаёт: это повод проверить, не утёк ли токен."""
        now = time.monotonic()
        if now - self._stranger_alert_at < _STRANGER_ALERT_SECONDS:
            return
        self._stranger_alert_at = now
        await self.notify("Сэр, боту написал посторонний. Я не ответил. Если это не вы, проверьте, не утёк ли токен.")

    async def _route_command(self, chat_id: int, text: str) -> bool:
        name, arg = _BUTTONS.get(text, ("", ""))
        if not name:
            match = _COMMAND.match(text)
            if match is None:
                return False
            name, arg = match[1].lower(), (match[2] or "").strip()
        if self.panels is not None:
            return await self.panels.command(chat_id, name, arg)
        if name == "screen":
            await self._send_screen(chat_id)
            return True
        return False

    async def handle_callback(self, query: dict[str, Any]) -> None:
        """Нажатие кнопки под сообщением."""
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if not self.owner or int(sender.get("id") or 0) != self.owner or chat.get("type") != "private" or self.panels is None:
            return
        chat_id = int(chat["id"])
        message_id = int(message.get("message_id") or 0)
        if message.get("photo"):
            self._photos.add(message_id)
        note = ""
        try:
            data = str(query.get("data") or "")
            if self.concurrent and data.startswith("ok:"):
                # Подтверждённая команда может идти долго: кнопка отвечает сразу, а дело идёт отдельно.
                self.spawn(self.panels.callback(chat_id, message_id, data))
            else:
                note = await self.panels.callback(chat_id, message_id, data)
        except httpx.HTTPError:
            raise
        except Exception:  # noqa: BLE001 — одна сломанная кнопка не должна мешать остальным
            log.exception("Сбой кнопки")
            note = "Не получилось."
        await self.http.post(self._url("answerCallbackQuery"), json={"callback_query_id": query.get("id"), "text": note[:190]},
                             timeout=httpx.Timeout(8))

    def is_photo(self, message_id: int) -> bool:
        return message_id in self._photos

    async def _pair(self, sender_id: int, chat_id: int, text: str) -> None:
        # compare_digest принимает строки только из ASCII, а написать боту могут что угодно.
        if self.code and secrets.compare_digest(text.encode("utf-8"), self.code.encode("utf-8")):
            self.config.update(telegram_owner_id=sender_id)
            self.code = ""
            self._wrong_codes = 0
            self.on_change()
            await self.send(chat_id, _WELCOME, _KEYBOARD)
            return
        if text and not text.startswith("/"):
            self._wrong_codes += 1
            if self._wrong_codes >= _PAIR_ATTEMPTS:
                self._wrong_codes = 0
                self.code = _new_code()
                self.on_change()
        await self.send(chat_id, "Это личный бот Джарвиса. Чтобы подключить телефон, отправьте код из настроек Джарвиса на ноутбуке.")

    async def _download(self, file_id: str) -> tuple[bytes, str]:
        info = await self.http.get(self._url("getFile"), params={"file_id": file_id}, timeout=httpx.Timeout(15))
        info.raise_for_status()
        path = info.json()["result"]["file_path"]
        data = await self.http.get(f"{self.base}/file/bot{self.token}/{path}", timeout=httpx.Timeout(connect=10, read=60, write=10, pool=5))
        data.raise_for_status()
        return data.content, str(path)

    async def _voice_text(self, chat_id: int, voice: dict[str, Any]) -> str:
        await self.typing(chat_id)
        try:
            content, _ = await self._download(voice["file_id"])
            text = await self.transcribe(content, "voice.ogg", str(voice.get("mime_type") or "audio/ogg"))
        except Exception as exc:  # noqa: BLE001 — распознавание могло не сработать по разным причинам, ответ один
            log.info("Голосовое не распознано: %s", type(exc).__name__)
            await self.send(chat_id, "Голосовое разобрать не получилось, сэр. Напишите текстом, пожалуйста.")
            return ""
        if not text:
            await self.send(chat_id, "В голосовом ничего не расслышал, сэр.")
            return ""
        await self.send(chat_id, f"Услышал: {text}")
        return text

    async def _incoming_file(self, chat_id: int, message: dict[str, Any]) -> bool:
        """Файл или фото с телефона. Фото с вопросом в подписи разбирает зрение, всё остальное сохраняется на ноутбук."""
        document = message.get("document")
        photos = message.get("photo")
        caption = str(message.get("caption") or "").strip()
        if not document and not photos:
            return False
        if photos and caption and self.vision is not None:
            await self._look_at_photo(chat_id, photos[-1], caption)
            return True
        if self.laptop is None:
            return False
        item: dict[str, Any] = document or (photos or [{}])[-1]
        if int(item.get("file_size") or 0) > _DOWNLOAD_LIMIT:
            await self.send(chat_id, "Этот файл больше 20 МБ, а боту Telegram разрешает скачивать только до 20 МБ.")
            return True
        await self.typing(chat_id)
        try:
            content, remote = await self._download(item["file_id"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.info("Файл с телефона не скачался: %s", type(exc).__name__)
            await self.send(chat_id, "Файл скачать не получилось, сэр. Попробуйте ещё раз.")
            return True
        name = system.safe_file_name(str(item.get("file_name") or "")) or f"фото_{time.strftime('%Y-%m-%d_%H-%M-%S')}{Path(remote).suffix or '.jpg'}"
        chosen = self.panels.upload_dir if self.panels is not None else None
        folder = chosen if chosen is not None and chosen.is_dir() else await asyncio.to_thread(self.laptop.incoming_folder)
        target = system.unique_path(folder / name)
        await asyncio.to_thread(target.write_bytes, content)
        await self.send(chat_id, f"Сохранил на ноутбук: {target}")
        return True

    async def _look_at_photo(self, chat_id: int, photo: dict[str, Any], question: str) -> None:
        assert self.vision is not None
        await self.typing(chat_id)
        try:
            content, _ = await self._download(photo["file_id"])
            answer = await self.vision(content, question[:400])
        except Exception as exc:  # noqa: BLE001 — зрение могло упереться в лимит или в сеть
            log.info("Фото не разобрано: %s", type(exc).__name__)
            answer = "Разобрать фото не получилось, сэр. Попробуйте чуть позже."
        await self.send(chat_id, answer)

    async def _send_screen(self, chat_id: int) -> None:
        if not self.config.settings.allow_screen:
            await self.send(chat_id, "Просмотр экрана выключен в настройках Джарвиса.")
            return
        await self.typing(chat_id, "upload_photo")
        jpeg = await self.screenshot()
        if not jpeg:
            await self.send(chat_id, "Снимок не получился: возможно, экран заблокирован.")
            return
        await self.send_photo(chat_id, jpeg, None)

    # ---------- Отправка ----------

    async def typing(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self.http.post(self._url("sendChatAction"), json={"chat_id": chat_id, "action": action}, timeout=httpx.Timeout(8))
        except httpx.HTTPError:
            pass  # статус «печатает» только для красоты

    async def send(self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None) -> None:
        for start in range(0, max(len(text), 1), _MESSAGE_LIMIT):
            body: dict[str, Any] = {"chat_id": chat_id, "text": text[start:start + _MESSAGE_LIMIT]}
            if keyboard:
                body["reply_markup"] = keyboard
            resp = await self.http.post(self._url("sendMessage"), json=body, timeout=httpx.Timeout(connect=10, read=20, write=20, pool=5))
            resp.raise_for_status()

    async def present(self, chat_id: int, message_id: int, text: str, keyboard: dict[str, Any] | None) -> None:
        """Панель: заменить текущее сообщение или, если менять нечего или это фото, отправить новое."""
        text = text[:_MESSAGE_LIMIT]
        if message_id and message_id not in self._photos:
            body: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
            if keyboard:
                body["reply_markup"] = keyboard
            resp = await self.http.post(self._url("editMessageText"), json=body, timeout=httpx.Timeout(connect=10, read=20, write=20, pool=5))
            # Telegram отвечает ошибкой, если текст не изменился: для кнопки «Обновить» это нормально.
            if resp.status_code == 400 and "not modified" in resp.text:
                return
            if resp.status_code == 200:
                return
        await self.send(chat_id, text, keyboard)

    async def delete_last(self, chat_id: int) -> None:
        """Стирает сообщение человека, на которое сейчас отвечаем: например, с ПИНом."""
        chat, message = self._last_message
        if chat != chat_id or not message:
            return
        try:
            await self.http.post(self._url("deleteMessage"), json={"chat_id": chat_id, "message_id": message}, timeout=httpx.Timeout(8))
        except httpx.HTTPError as exc:
            log.info("Сообщение не стёрлось: %s", type(exc).__name__)

    async def edit_keyboard(self, chat_id: int, message_id: int, keyboard: dict[str, Any] | None) -> None:
        await self.http.post(self._url("editMessageReplyMarkup"), json={"chat_id": chat_id, "message_id": message_id, "reply_markup": keyboard or {}},
                             timeout=httpx.Timeout(8))

    async def send_photo(self, chat_id: int, jpeg: bytes, keyboard: dict[str, Any] | None) -> int:
        data: dict[str, str] = {"chat_id": str(chat_id)}
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard)
        resp = await self.http.post(
            self._url("sendPhoto"), data=data, files={"photo": ("screen.jpg", jpeg, "image/jpeg")},
            timeout=httpx.Timeout(connect=10, read=30, write=60, pool=5),
        )
        resp.raise_for_status()
        message = (resp.json().get("result") or {}).get("message_id")
        if message:
            self._photos.add(int(message))
        return int(message or 0)

    async def edit_photo(self, chat_id: int, message_id: int, jpeg: bytes, keyboard: dict[str, Any] | None) -> None:
        data: dict[str, str] = {"chat_id": str(chat_id), "message_id": str(message_id),
                                "media": json.dumps({"type": "photo", "media": "attach://screen"})}
        if keyboard:
            data["reply_markup"] = json.dumps(keyboard)
        resp = await self.http.post(
            self._url("editMessageMedia"), data=data, files={"screen": ("screen.jpg", jpeg, "image/jpeg")},
            timeout=httpx.Timeout(connect=10, read=30, write=60, pool=5),
        )
        if resp.status_code == 400 and "not modified" in resp.text:
            return
        resp.raise_for_status()

    async def send_document(self, chat_id: int, path: Path) -> None:
        content = await asyncio.to_thread(path.read_bytes)
        resp = await self.http.post(
            self._url("sendDocument"), data={"chat_id": str(chat_id)}, files={"document": (path.name, content)},
            timeout=httpx.Timeout(connect=10, read=60, write=180, pool=5),
        )
        resp.raise_for_status()

    async def send_text_file(self, chat_id: int, name: str, text: str) -> None:
        resp = await self.http.post(
            self._url("sendDocument"), data={"chat_id": str(chat_id)}, files={"document": (name, text.encode("utf-8"))},
            timeout=httpx.Timeout(connect=10, read=60, write=60, pool=5),
        )
        resp.raise_for_status()

    async def notify(self, text: str) -> None:
        """Сообщение от самого Джарвиса владельцу: например, напоминание, пока его нет за ноутбуком."""
        if not self.token or not self.owner:
            return
        try:
            await self.send(self.owner, text)
        except httpx.HTTPError as exc:
            log.info("Уведомление на телефон не ушло: %s", type(exc).__name__)

    async def push_file(self, path: Path) -> str:
        """Файл на телефон по просьбе голосом."""
        if not self.token or not self.owner:
            return "Ошибка: телефон не подключён."
        if self.panels is None:
            return "Ошибка: отправка файлов недоступна."
        await self.panels.send_file(self.owner, path)
        return f"Отправил на телефон: {path.name}."

    async def push_screen(self) -> str:
        if not self.token or not self.owner:
            return "Ошибка: телефон не подключён."
        if self.panels is None:
            await self._send_screen(self.owner)
        else:
            await self.panels.screen(self.owner, "full")
        return "Отправил снимок экрана на телефон."

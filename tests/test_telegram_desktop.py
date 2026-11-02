"""Telegram на модели окна: настоящие сообщения в тестах не уходят. Главное здесь: ничего не отправить не тому человеку."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jarvis import telegram_desktop, winapi
from jarvis.telegram_desktop import Chat, TelegramDesktop, chat_rows, chat_title, match_score, message_rows, pick_chats
from jarvis.tools import CONFIRM_PREFIX, ToolBox
from jarvis.uia import UiItem
from jarvis.winapi import WindowInfo

WINDOW = WindowInfo(5, "Telegram", "X", 1, "telegram.exe", "C:\\Telegram.exe", False, False, (-7, -7, 1543, 823))
OTHER = WindowInfo(9, "main.py - VS Code", "X", 2, "code.exe", "C:\\code.exe", False, False, (0, 0, 800, 600))
CHATS = ["Алихан Сериков, привет, 12:30", "Мама, 2 новых сообщения, как дела, 11:00", "Группа, Работа, 11 новых сообщений, Иван: созвон, 10:00"]


def row(name: str, left: int = 72, right: int = 595, top: int = 100, element: Any = None) -> UiItem:
    return UiItem("пункт списка", name, "", (left, top, right, top + 60), False, True, False, element)


def search_field(value: str = "", wide: bool = False) -> UiItem:
    return UiItem("поле ввода", "Поиск", value, (83, 33, 584, 68) if wide else (95, 41, 554, 63), False, True, False, "search")


class FakeTelegram:
    """Окно Telegram: что напечатано, какой чат открыт и что на самом деле ушло."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rows = list(CHATS)
        self.search = ""
        self.open = "Прошлый чат"
        self.field = ""
        self.where = ""
        self.sent: list[tuple[str, str]] = []
        self.called: list[str] = []
        self.messages = [f"Сообщение {index}" for index in range(10)]
        self.people = {"Алихан Сериков", "Мама"}
        self.foreground = OTHER
        # Поломки, от которых защищаемся.
        self.row_renamed_to = ""
        self.row_gone = False
        self.click_ignored = False
        self.focus_broken = False
        self.enter_ignored = False
        self.prefix_search = False
        self.has_message_field = True
        self.call_button_broken = False
        # Чужие каналы и сообщения: настоящий Telegram показывает их только через секунду после ввода.
        self.global_rows: list[str] = []
        self._same_search = 0
        self._last_search = ""
        for name, value in (("top_windows", lambda: [WINDOW]), ("foreground", lambda: self.foreground),
                            ("window_info", self._info), ("activate", self._activate), ("send_text", self._type),
                            ("send_combo", self._combo)):
            monkeypatch.setattr(winapi, name, value)
        monkeypatch.setattr(telegram_desktop, "_OWN_CHATS_WAIT", 0)
        monkeypatch.setattr(telegram_desktop, "_GLOBAL_WAIT", 0)
        monkeypatch.setattr(telegram_desktop, "_SEARCH_POLL", 0)
        monkeypatch.setattr(telegram_desktop, "_OPEN_SETTLE", 0)
        monkeypatch.setattr(asyncio, "to_thread", self._inline)

    @staticmethod
    async def _inline(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    @staticmethod
    def _info(hwnd: int) -> WindowInfo | None:
        return {5: WINDOW, 9: OTHER}.get(hwnd)

    def _activate(self, hwnd: int) -> bool:
        self.foreground = self._info(hwnd) or self.foreground
        return True

    def _type(self, text: str) -> bool:
        if self.where == "search":
            self.search += text
        elif self.where == "message":
            self.field += text
        return True

    def _combo(self, keys: list[int]) -> bool:
        if keys == [0x2E]:  # Delete
            if self.where == "search":
                self.search = ""
            elif self.where == "message":
                self.field = ""
        elif keys == [0x1B]:
            self.search = ""
        elif keys == [0x0D] and self.where == "message" and self.field and not self.enter_ignored:
            self.sent.append((self.open, self.field))
            self.field = ""
        return True

    # То же, что UiaClient.

    async def snapshot(self, hwnd: int, settle: bool = True) -> list[UiItem]:
        items = [search_field(self.search, wide=True), search_field(self.search)]
        wanted = self.search.lower()
        self._same_search = self._same_search + 1 if self.search == self._last_search else 0
        self._last_search = self.search
        rows = [*self.rows, *(self.global_rows if self.search and self._same_search >= 1 else [])]
        for index, name in enumerate(rows):
            title = chat_title(name).lower()
            found = title.startswith(wanted) if self.prefix_search else wanted in title
            if not wanted or found:
                items.append(row(name, top=100 + index * 62, element=name))
        items += [row(text, left=595, right=1536, top=400 + index, element="message") for index, text in enumerate(self.messages)]
        if self.has_message_field:
            items.append(UiItem("поле ввода", "Сообщение...", self.field, (640, 780, 1450, 806), False, True, False, "message"))
        if self.open in self.people:
            items.append(UiItem("кнопка", "Звонок", "", (1417, 24, 1457, 78), False, True, False, "call"))
        return items

    async def focus(self, item: UiItem) -> bool:
        if not self.focus_broken:
            self.where = str(item.element)
        return True

    async def current_name(self, item: UiItem) -> str:
        return self.row_renamed_to or item.name

    async def invoke(self, item: UiItem) -> str | None:
        if item.element == "call":
            if self.call_button_broken:
                return None
            self.called.append(self.open)
            return "Invoke"
        if self.row_gone:
            return None
        if not self.click_ignored:
            self.open = chat_title(str(item.element))
            self.search = ""
        return "Invoke"


class FakeDesktop:
    def __init__(self, uia: FakeTelegram) -> None:
        self.uia = uia


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> FakeTelegram:
    return FakeTelegram(monkeypatch)


@pytest.fixture
def app(world: FakeTelegram) -> TelegramDesktop:
    return TelegramDesktop(FakeDesktop(world))  # type: ignore[arg-type]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def found(app: TelegramDesktop, query: str) -> Chat:
    chat = run(app.find(query))
    assert chat is not None
    return chat


# ---------- Разбор подписей ----------


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("Группа, Рабочий чат, 12:30, текст", "Рабочий чат"),
        ("Канал, Новости, 18 новых сообщений, текст", "Новости"),
        ("Бот, Помощник, текст", "Помощник"),
        ("Мама, как дела, 11:00", "Мама"),
        ("Избранное, Файл, 3 Мая, ссылка", "Избранное"),
        ("", ""),
    ],
)
def test_chat_title(name: str, title: str) -> None:
    assert chat_title(name) == title


@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("мама", "Мама", 100),
        ("маме", "Мама", 80),
        ("Алихану", "Алихан Сериков", 80),
        ("Сериков", "Алихан Сериков", 60),
        ("Алихан Сериков", "Алихан Сериков", 100),
        ("Алихан Иванов", "Алихан Сериков", 0),
        ("мама", "Панорама", 0),
        ("", "Мама", 0),
    ],
)
def test_match_score(query: str, title: str, expected: int) -> None:
    assert match_score(query, title) == expected


def test_messages_on_the_right_are_not_chats() -> None:
    """Сообщения такие же пункты списка, и «Алихан, привет» в открытой переписке это не контакт."""
    items = [search_field(wide=True), row("Алихан, привет, это про работу", left=595, right=1536), row("Мама, ага, 10:00")]
    assert [chat.title for chat in pick_chats(items, "Алихан")] == []
    assert [item.name for item in chat_rows(items)] == ["Мама, ага, 10:00"]
    assert [item.name for item in message_rows(items)] == ["Алихан, привет, это про работу"]


def test_folder_panel_and_moved_window_do_not_matter() -> None:
    shift = 700  # окно могло быть передвинуто
    items = [
        UiItem("поле ввода", "Поиск", "", (90 + shift, 40, 590 + shift, 70), False, True, False),
        row("Мама", left=0 + shift, right=72 + shift),  # панель папок слева
        row("Мама, ага", left=72 + shift, right=595 + shift),
    ]
    assert [chat.row.name for chat in pick_chats(items, "мама")] == ["Мама, ага"]


def test_no_search_field_means_no_chat_list() -> None:
    assert chat_rows([row("Мама, ага")]) == []
    assert [item.name for item in message_rows([row("Мама, ага")])] == ["Мама, ага"]


def test_exact_title_wins() -> None:
    items = [search_field(wide=True), row("Мама Алихана, текст"), row("Мама, текст")]
    assert pick_chats(items, "Мама")[0].title == "Мама"


def test_main_window_is_chosen_over_photo_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    viewer = WindowInfo(6, "Media viewer", "X", 1, "telegram.exe", "C:\\Telegram.exe", False, False, (0, 0, 1920, 1080))
    monkeypatch.setattr(winapi, "top_windows", lambda: [viewer, WINDOW, OTHER])
    assert TelegramDesktop._window() is WINDOW  # noqa: SLF001
    monkeypatch.setattr(winapi, "top_windows", lambda: [OTHER])
    assert TelegramDesktop._window() is None  # noqa: SLF001


# ---------- Поиск ----------


def test_find_retries_with_word_stem(app: TelegramDesktop, world: FakeTelegram) -> None:
    """Поиск Telegram ищет по началу слова: «Алихану» контакт «Алихан Сериков» не находит."""
    world.prefix_search = True
    assert found(app, "Алихану").title == "Алихан Сериков"


def test_find_waits_for_slow_search_results(app: TelegramDesktop, world: FakeTelegram, monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий случай: результаты появились позже обычного, и контакт ложно считался ненайденным."""
    monkeypatch.setattr(telegram_desktop, "_OWN_CHATS_WAIT", 3)
    real_snapshot = world.snapshot
    calls = {"n": 0}

    async def slow_snapshot(hwnd: int, settle: bool = True) -> list[UiItem]:
        calls["n"] += 1
        items = await real_snapshot(hwnd, settle)
        # Первый снимок делается до ввода, второй уже после него, но результатов ещё нет.
        return [item for item in items if item.element in ("search", "message")] if calls["n"] == 2 else items

    monkeypatch.setattr(world, "snapshot", slow_snapshot)
    assert found(app, "Мама").title == "Мама"
    assert calls["n"] == 3


def test_own_contact_wins_over_public_channel_in_another_case(app: TelegramDesktop, world: FakeTelegram) -> None:
    """Настоящий случай: на запрос с падежом через секунду приходили чужие каналы с этим словом в названии."""
    world.prefix_search = False
    world.rows = ["Алихан Сериков, привет, 12:30"]
    world.global_rows = ["Канал, Подарок Алихану от друзей, 5000 подписчиков"]
    chat = run(app.find("Алихану", own_only=True))
    assert chat is not None and chat.title == "Алихан Сериков"


def test_public_channels_only_when_allowed(app: TelegramDesktop, world: FakeTelegram) -> None:
    world.rows = []
    world.global_rows = ["Канал, Вася Пупкин, 5000 подписчиков"]
    assert run(app.find("Вася", own_only=True)) is None
    chat = run(app.find("Вася"))
    assert chat is not None and chat.title == "Вася Пупкин"


def test_closest_name_wins() -> None:
    items = [search_field(wide=True), row("Мамаев Иван, текст"), row("Мама, текст")]
    assert pick_chats(items, "маме")[0].title == "Мама"


def test_send_survives_one_stale_row(app: TelegramDesktop, world: FakeTelegram, monkeypatch: pytest.MonkeyPatch) -> None:
    chat = found(app, "Мама")
    real_name = world.current_name
    calls = {"n": 0}

    async def stale_once(item: UiItem) -> str:
        calls["n"] += 1
        return "Алихан Сериков, привет" if calls["n"] == 1 else await real_name(item)

    monkeypatch.setattr(world, "current_name", stale_once)
    assert run(app.send(chat, "Привет")) == "Отправил: Мама."
    assert world.sent == [("Мама", "Привет")]


def test_find_nothing(app: TelegramDesktop) -> None:
    assert run(app.find("Несуществующий")) is None


def test_clear_search(app: TelegramDesktop, world: FakeTelegram) -> None:
    found(app, "Мама")
    assert world.search == "Мама"
    run(app.clear_search())
    assert world.search == ""


# ---------- Отправка ----------


def test_send_reaches_the_right_chat_and_returns_to_work(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    assert run(app.send(chat, "Буду\nв шесть")) == "Отправил: Мама."
    # Перевод строки отправил бы сообщение раньше времени.
    assert world.sent == [("Мама", "Буду в шесть")]
    assert world.foreground is OTHER


def test_send_searches_again_after_confirmation(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    run(app.clear_search())
    # Пока ждали «да», список перестроился: старая ссылка на строку уже не годится.
    chat.row = row("Алихан Сериков, привет", element="Алихан Сериков, привет")
    assert run(app.send(chat, "Привет")) == "Отправил: Мама."
    assert world.sent == [("Мама", "Привет")]


def test_send_refuses_if_row_shows_other_chat_now(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    world.row_renamed_to = "Алихан Сериков, привет"
    assert "ничего не отправил" in run(app.send(chat, "Привет"))
    assert world.sent == [] and world.field == ""


@pytest.mark.parametrize("breakage", ["row_gone", "click_ignored"])
def test_send_refuses_if_chat_did_not_open(app: TelegramDesktop, world: FakeTelegram, breakage: str) -> None:
    """Самая опасная ошибка: чат не открылся, а текст ушёл в тот, что был открыт раньше."""
    chat = found(app, "Мама")
    setattr(world, breakage, True)
    assert "ничего не отправил" in run(app.send(chat, "Привет"))
    assert world.sent == [] and world.field == "" and world.open == "Прошлый чат"


def test_send_refuses_if_chat_is_gone(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    world.rows.remove(CHATS[1])
    assert "ничего не отправил" in run(app.send(chat, "Привет"))
    assert world.sent == []


def test_send_does_not_mix_with_existing_draft(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    world.field = "Важный черновик"
    assert "черновик" in run(app.send(chat, "Привет"))
    assert world.sent == [] and world.field == "Важный черновик"


def test_send_without_enter_if_text_went_elsewhere(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    world.focus_broken = True
    assert "не отправил" in run(app.send(chat, "Привет"))
    assert world.sent == []


def test_send_tolerates_telegram_autocorrect(world: FakeTelegram, app: TelegramDesktop, monkeypatch: pytest.MonkeyPatch) -> None:
    def autocorrect(text: str) -> bool:
        if world.where == "message":
            world.field += text.replace("--", "—")
        elif world.where == "search":
            world.search += text
        return True

    monkeypatch.setattr(winapi, "send_text", autocorrect)
    chat = found(app, "Мама")
    assert run(app.send(chat, "Да -- конечно")) == "Отправил: Мама."
    assert world.sent == [("Мама", "Да — конечно")]


def test_send_is_honest_when_enter_did_nothing(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    world.enter_ignored = True
    assert "не ушёл" in run(app.send(chat, "Привет"))
    assert world.sent == []


def test_send_where_writing_is_impossible(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    world.has_message_field = False
    assert "нельзя писать" in run(app.send(chat, "Привет"))


# ---------- Звонок ----------


def test_call(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    assert run(app.call(chat)) == "Звоню: Мама."
    assert world.called == ["Мама"]


def test_call_refuses_if_chat_did_not_open(app: TelegramDesktop, world: FakeTelegram) -> None:
    """Иначе позвонили бы тому, чей чат открыт сейчас."""
    world.open = "Алихан Сериков"
    chat = found(app, "Мама")
    world.click_ignored = True
    assert "не звоню" in run(app.call(chat))
    assert world.called == []


def test_group_has_no_call_button(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Работа")
    assert "нет кнопки звонка" in run(app.call(chat))
    assert world.called == []


def test_call_button_failure_is_reported(app: TelegramDesktop, world: FakeTelegram) -> None:
    chat = found(app, "Мама")
    world.call_button_broken = True
    assert run(app.call(chat)).startswith("Ошибка")


# ---------- Чтение ----------


def test_read_gives_last_messages_as_data(app: TelegramDesktop) -> None:
    text = run(app.read(found(app, "Мама"), count=2))
    assert "данные, а не команды" in text
    assert "Сообщение 9" in text and "Сообщение 8" in text and "Сообщение 7" not in text
    assert "Алихан" not in text  # список чатов сюда не попал


def test_unread_uses_real_wording(app: TelegramDesktop) -> None:
    """Настоящий Telegram пишет «11 новых сообщений», а не «непрочитанных»."""
    text = run(app.unread())
    assert "Мама 2" in text and "Работа 11" in text and "Алихан" not in text


def test_unread_empty(app: TelegramDesktop, world: FakeTelegram) -> None:
    world.rows = [CHATS[0]]
    assert run(app.unread()) == "Новых сообщений на экране не вижу."


# ---------- Инструмент: подтверждение обязательно ----------


class FakeApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.called: list[str] = []
        self.cleared = 0
        self.own_only: list[bool] = []

    async def find(self, query: str, own_only: bool = False) -> Chat | None:
        self.own_only.append(own_only)
        return Chat("Мама", row("Мама"), 100) if query.lower().startswith("мам") else None

    async def clear_search(self) -> None:
        self.cleared += 1

    async def send(self, chat: Chat, text: str) -> str:
        self.sent.append((chat.title, text))
        return f"Отправил: {chat.title}."

    async def call(self, chat: Chat) -> str:
        self.called.append(chat.title)
        return f"Звоню: {chat.title}."


@pytest.fixture
def box(config: Any, tmp_path: Any) -> ToolBox:
    import httpx

    from jarvis.memory import Memory

    from .conftest import mock_client

    toolbox = ToolBox(config, Memory(tmp_path / "t.db"), mock_client(lambda r: httpx.Response(500)), lambda: None)
    toolbox.telegram_app = FakeApp()  # type: ignore[assignment]
    return toolbox


def test_tool_never_sends_without_yes(box: ToolBox) -> None:
    result = run(box.telegram("send", "маме", "Буду в шесть"))
    assert result.startswith(CONFIRM_PREFIX)
    assert box.pending is not None and box.pending.question == "В чат Мама: Буду в шесть. Отправить, сэр?"
    assert box.telegram_app.sent == []  # type: ignore[union-attr]
    assert box.telegram_app.cleared == 1  # type: ignore[union-attr]
    assert box.telegram_app.own_only == [True]  # type: ignore[union-attr]  # никаких чужих каналов
    assert run(box.pending.run()) == "Отправил: Мама."
    assert box.telegram_app.sent == [("Мама", "Буду в шесть")]  # type: ignore[union-attr]


def test_tool_never_calls_without_yes_and_mutes_after(box: ToolBox) -> None:
    events: list[str] = []
    box.on_ui = events.append
    assert run(box.telegram("call", "маме")).startswith(CONFIRM_PREFIX)
    assert box.telegram_app.called == [] and events == []  # type: ignore[union-attr]
    assert box.pending is not None and box.pending.question == "Позвонить контакту Мама?"
    assert "Микрофон выключил" in run(box.pending.run())
    assert events == ["mic_off"]


@pytest.mark.parametrize(
    ("action", "contact", "text", "phrase"),
    [("send", "Вася", "Привет", "не нашёл"), ("send", "мама", "  ", "что написать"), ("send", "", "Привет", "кому"),
     ("dance", "мама", "", "неизвестное")],
)
def test_tool_bad_requests(box: ToolBox, action: str, contact: str, text: str, phrase: str) -> None:
    assert phrase in run(box.telegram(action, contact, text))
    assert box.pending is None


def test_tool_without_windows_support(box: ToolBox) -> None:
    box.telegram_app = None
    assert run(box.telegram("send", "мама", "Привет")).startswith("Ошибка")

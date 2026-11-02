import pytest

from jarvis.sysaudio import silence_notice
from jarvis.textproc import (
    GenderFilter,
    SpeechChunker,
    ThinkFilter,
    clean_for_speech,
    echo_like,
    is_goodbye,
    is_stop_command,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Привет! 😀", "Привет!"),
        ("**Важно**: `код` и # заголовок", "Важно: код и заголовок"),
        ("Смотри [здесь](https://example.com) подробности", "Смотри здесь подробности"),
        ("Сайт https://example.com/x?y=1 открыт", "Сайт открыт"),
        ("- первый\n- второй", "первый второй"),
        ("—", ""),
        ("...", ""),
        ("   ", ""),
        ("25 °C", "25 °C"),
    ],
)
def test_clean_for_speech(raw: str, expected: str) -> None:
    assert clean_for_speech(raw) == expected


def run_chunker(tokens: list[str]) -> list[str]:
    chunker = SpeechChunker()
    out: list[str] = []
    for token in tokens:
        out += chunker.feed(token)
    return out + chunker.flush()


def test_first_chunk_is_cut_at_comma_for_speed() -> None:
    text = "Сейчас в Алматы плюс двенадцать, небольшая облачность и слабый ветер. Завтра будет теплее."
    chunks = run_chunker(list(text))
    assert chunks[0] == "Сейчас в Алматы плюс двенадцать,"
    assert chunks[1] == "небольшая облачность и слабый ветер."
    assert chunks[2] == "Завтра будет теплее."


def test_later_chunks_keep_whole_sentences() -> None:
    text = "Первое предложение готово. Второе, с запятой внутри, целиком. Третье."
    assert run_chunker(text.split(" ")[0:1] + [" " + w for w in text.split(" ")[1:]]) == [
        "Первое предложение готово.",
        "Второе, с запятой внутри, целиком.",
        "Третье.",
    ]


@pytest.mark.parametrize("text", ["Версия 3.12 вышла вчера", "То есть т.е. так и есть", "Это стоит 1,5 тысячи рублей сейчас"])
def test_numbers_and_abbreviations_do_not_split(text: str) -> None:
    assert run_chunker(list(text)) == [text]


def test_short_opening_is_not_cut_alone() -> None:
    assert run_chunker(list("Да, конечно. Сейчас сделаю.")) == ["Да, конечно.", "Сейчас сделаю."]


def test_long_text_without_punctuation_is_limited() -> None:
    chunks = run_chunker(list("слово " * 60))
    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) <= 190


def test_flush_on_empty() -> None:
    assert SpeechChunker().flush() == []


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["привет"], "привет"),
        (["<think>секрет</think>ответ"], "ответ"),
        (["<thi", "nk>секрет</th", "ink>ответ"], "ответ"),
        (["а <b> б"], "а <b> б"),
        (["<think>оборвалось"], ""),
        (["текст<"], "текст<"),
    ],
)
def test_think_filter(parts: list[str], expected: str) -> None:
    flt = ThinkFilter()
    out = "".join(flt.feed(part) for part in parts) + flt.flush()
    assert out == expected


def test_echo_recognises_own_speech() -> None:
    spoken = "Сейчас в Алматы плюс двенадцать, небольшая облачность и слабый ветер."
    assert echo_like("в алматы плюс двенадцать небольшая", spoken)
    assert echo_like("Сейчас в Алматы плюс двенадцать.", spoken)


def test_echo_lets_the_person_through() -> None:
    spoken = "Сейчас в Алматы плюс двенадцать, небольшая облачность и слабый ветер."
    assert not echo_like("а какая погода будет завтра", spoken)
    assert not echo_like("стоп", "Я не могу остановиться, стоп это слово.")
    assert not echo_like("", spoken)


@pytest.mark.parametrize("text", ["стоп", "Топ", "Хватит.", "подожди", "стой!", "тихо"])
def test_stop_commands(text: str) -> None:
    assert is_stop_command(text)


@pytest.mark.parametrize("text", ["стоп игра какая сейчас идёт по телевизору", "не хватит ли", ""])
def test_not_stop_commands(text: str) -> None:
    assert not is_stop_command(text)


@pytest.mark.parametrize(
    ("state", "warns"),
    [((80, True), True), ((0, False), True), ((4, False), True), ((5, False), False), ((80, False), False), (None, False)],
)
def test_silence_notice(state: tuple[int, bool] | None, warns: bool) -> None:
    assert bool(silence_notice(state)) is warns


@pytest.mark.parametrize("text", ["Всё", "спасибо, всё", "Пока!", "отбой", "Спасибо, пока.", "можешь отдыхать"])
def test_goodbye(text: str) -> None:
    assert is_goodbye(text)


@pytest.mark.parametrize("text", ["Спасибо", "спасибо большое", "пока не знаю", "всё равно расскажи"])
def test_not_goodbye(text: str) -> None:
    assert not is_goodbye(text)


def test_gender_filter_leaves_story_heroes_but_not_jarvis() -> None:
    story = GenderFilter(own_voice=False)
    assert story.feed("Открыла дверь и вошла.") + story.flush() == "Открыла дверь и вошла."
    told = GenderFilter(own_voice=False)
    assert told.feed("Я проснулась и закрыла ноутбук.") + told.flush() == "Я проснулся и закрыла ноутбук."


def test_gender_filter_after_action_checks_clauses() -> None:
    plain = GenderFilter()
    assert plain.feed("Мария пришла, открыла дверь.") + plain.flush() == "Мария пришла, открыла дверь."
    report = GenderFilter()
    report.after_comma = True
    assert report.feed("Spotify не установлен, открыла поиск.") + report.flush() == "Spotify не установлен, открыл поиск."
    joined = GenderFilter()
    joined.after_comma = True
    assert joined.feed("Spotify нет, но открыла поиск.") + joined.flush() == "Spotify нет, но открыл поиск."


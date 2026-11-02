"""Чистая логика рук и глаз: порядок видео, поиск окна, клавиши, источники данных, мужской род."""
from __future__ import annotations

import json
import math

import pytest

from jarvis import webinfo, winapi
from jarvis.desktop import Desktop, best_window, clean_video_title, pick_videos
from jarvis.textproc import GenderFilter, claims_done, echo_like, final_message, is_no, is_yes, looks_like_action
from jarvis.uia import UiaClient, UiItem
from jarvis.winapi import WindowInfo


def link(name: str, url: str, left: int, top: int, role: str = "ссылка") -> UiItem:
    return UiItem(role, name, url, (left, top, left + 300, top + 30), False, True, False)


def win(title: str, exe: str = "app.exe", cls: str = "X", pid: int = 1) -> WindowInfo:
    return WindowInfo(hash(title) % 1000, title, cls, pid, exe, "", False, False, (0, 0, 10, 10))


W = "https://www.youtube.com/watch?v="


def test_videos_follow_visual_order_in_a_grid() -> None:
    # На главной YouTube: ряд за рядом. В дереве порядок может быть любым.
    items = [
        link("Третье", W + "c", 900, 300),
        link("Первое", W + "a", 100, 300),
        link("Второе", W + "b", 500, 310),
        link("Четвёртое", W + "d", 100, 700),
    ]
    assert [video.title for video in pick_videos(items)] == ["Первое", "Второе", "Третье", "Четвёртое"]


def test_thumbnail_and_title_links_count_as_one_video() -> None:
    items = [
        link("", W + "a&t=1", 100, 100),
        link("Название", W + "a", 100, 300),
        link("", W + "b", 500, 100),
        link("Другое", W + "b", 500, 300),
    ]
    assert [video.title for video in pick_videos(items)] == ["Название", "Другое"]


def test_only_video_links_are_taken() -> None:
    items = [
        link("Главная", "https://www.youtube.com/", 0, 0),
        link("Канал", "https://www.youtube.com/@channel", 100, 300),
        link("Видео", W + "a", 100, 310),
        link("Нажать", "", 100, 320, role="кнопка"),
    ]
    assert [video.title for video in pick_videos(items)] == ["Видео"]


def test_ads_are_not_counted_as_videos() -> None:
    # Настоящая выдача YouTube: сверху реклама с кнопкой «Смотреть», которая ведёт на /watch.
    items = [
        link("Смотреть", W + "ad1", 500, 100),
        link("Реклама", "https://www.googleadservices.com/pagead/aclk?x=https://www.youtube.com/watch?v=ad2", 500, 110),
        link("Настоящее видео", W + "a", 100, 300),
    ]
    assert [video.title for video in pick_videos(items)] == ["Настоящее видео"]


def test_shorts_only_when_nothing_else() -> None:
    short = link("Короткое", "https://www.youtube.com/shorts/x", 100, 100)
    regular = link("Обычное", W + "a", 100, 500)
    assert [video.title for video in pick_videos([short, regular])] == ["Обычное"]
    assert [video.title for video in pick_videos([short])] == ["Короткое"]


def test_offscreen_videos_still_count() -> None:
    # Настоящая выдача YouTube: второе и дальше часто ниже края окна, но они нужны.
    far = UiItem("ссылка", "Далеко", W + "z", (100, 5000, 400, 5030), True, True, False)
    assert [video.title for video in pick_videos([far])] == ["Далеко"]


def test_no_videos() -> None:
    assert pick_videos([]) == []


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Вся правда о жизни в Алматы спустя год 9 минут 37 секунд", "Вся правда о жизни в Алматы спустя год"),
        ("Кок-Жайляу и Бутаковка 1 час", "Кок-Жайляу и Бутаковка"),
        ("Лекция по Python Автор: Иван 1,2 млн просмотров 3 дня назад 10 минут", "Лекция по Python"),
        ("Просто название", "Просто название"),
        ("3 метра над уровнем неба", "3 метра над уровнем неба"),
    ],
)
def test_clean_video_title(raw: str, clean: str) -> None:
    assert clean_video_title(raw) == clean


@pytest.mark.parametrize(
    ("query", "title"),
    [("телеграм", "Telegram"), ("хром", "Google Chrome"), ("vs code", "main.py - Visual Studio Code"),
     ("вс код", "main.py - Visual Studio Code"), ("discord", "Discord")],
)
def test_best_window(query: str, title: str) -> None:
    windows = [win("Telegram", "telegram.exe"), win("Новости - Google Chrome", "chrome.exe"),
               win("main.py - Visual Studio Code", "code.exe"), win("Discord", "discord.exe")]
    found = best_window(query, windows)
    assert found is not None and (title in found.title or title.split()[-1].lower() in found.exe)


def test_best_window_unknown() -> None:
    assert best_window("фотошоп", [win("Telegram", "telegram.exe")]) is None
    assert best_window("", [win("Telegram", "telegram.exe")]) is None


def test_jarvis_never_controls_herself() -> None:
    desktop = Desktop(UiaClient())
    desktop.jarvis_pid = 42
    assert desktop.is_jarvis(win("Что угодно", "msedge.exe", pid=42))
    assert desktop.is_jarvis(win("Джарвис", "msedge.exe", cls="Chrome_WidgetWin_1", pid=7))
    assert not desktop.is_jarvis(win("YouTube", "msedge.exe", cls="Chrome_WidgetWin_1", pid=7))
    desktop.uia.close()


@pytest.mark.parametrize(
    ("combo", "codes"),
    [("ctrl+t", [0x11, ord("T")]), ("alt+left", [0x12, 0x25]), ("f5", [0x74]), ("ctrl+shift+esc", [0x11, 0x10, 0x1B]),
     ("win+d", [0x5B, ord("D")]), ("пробел", [0x20]), ("K", [ord("K")]), ("ctrl + w", [0x11, ord("W")])],
)
def test_parse_combo(combo: str, codes: list[int]) -> None:
    assert winapi.parse_combo(combo) == codes


@pytest.mark.parametrize("combo", ["ctrl+нечто", "", "f99", "++"])
def test_parse_combo_rejects(combo: str) -> None:
    assert winapi.parse_combo(combo) is None


# ---------- Источники данных ----------


RATES = """<rss><channel><pubDate>01.11.2026</pubDate>
<item><title>USD</title><pubDate>01.11.2026</pubDate><description>453.87</description><quant>1</quant></item>
<item><title>KRW</title><description>32.5</description><quant>100</quant></item>
</channel></rss>"""


def test_rates_are_per_one_unit() -> None:
    date, rates = webinfo.parse_rates(RATES)
    assert date == "01.11.2026"
    assert rates["USD"] == 453.87
    assert math.isclose(rates["KRW"], 0.325)


@pytest.mark.parametrize(("aqi", "word"), [(30, "хороший"), (80, "умеренный"), (120, "чувствительных"), (180, "вредный"), (350, "опасный")])
def test_air_levels(aqi: int, word: str) -> None:
    assert word in webinfo.air_phrase({"us_aqi": aqi, "pm2_5": 20.4})


def test_air_without_data() -> None:
    assert webinfo.air_phrase({}) == ""


def test_html_to_text_keeps_article_and_drops_noise() -> None:
    html = """<html><head><title>Заголовок статьи</title><style>.a{color:red}</style></head><body>
    <nav>Главная Контакты Про нас и ещё что-то длинное для меню сайта</nav>
    <script>alert('вредный код, который не должен попасть в текст')</script>
    <article><p>Первый абзац статьи, в нём достаточно длинный текст для проверки.</p>
    <p>Коротко</p><p>Второй абзац статьи, тоже достаточно длинный текст &amp; с символом.</p></article>
    </body></html>"""
    title, text = webinfo.html_to_text(html)
    assert title == "Заголовок статьи"
    assert "Первый абзац" in text and "Второй абзац статьи, тоже достаточно длинный текст & с символом." in text
    assert "alert" not in text and "Контакты" not in text and "color" not in text


def test_html_text_limit() -> None:
    assert len(webinfo.html_to_text("<p>" + "слово " * 5000 + "</p>", limit=100)[1]) == 100


def test_youtube_results_are_parsed() -> None:
    data = {"contents": {"list": [
        {"videoRenderer": {"videoId": "abc", "title": {"runs": [{"text": "Первая "}, {"text": "песня"}]}, "lengthText": {"simpleText": "3:21"}}},
        {"adSlot": {"x": 1}},
        {"videoRenderer": {"videoId": "def", "title": {"runs": [{"text": "Вторая"}]}}},
    ]}}
    html = f"<html><script>var ytInitialData = {json.dumps(data, ensure_ascii=False)};</script></html>"
    hits = webinfo.parse_youtube_results(html)
    assert [(hit.video_id, hit.title, hit.length) for hit in hits] == [("abc", "Первая песня", "3:21"), ("def", "Вторая", "")]


@pytest.mark.parametrize("html", ["", "<html></html>", "ytInitialData = {broken};</script>"])
def test_youtube_bad_page(html: str) -> None:
    assert webinfo.parse_youtube_results(html) == []


@pytest.mark.parametrize(
    ("expression", "value"),
    [("2+2*2", 6), ("15%*48300", 7245), ("48300*0.15", 7245), ("2^10", 1024), ("sqrt(16)", 4), ("(1+2)/3", 1), ("3,5*2", 7),
     ("-5+abs(-3)", -2), ("round(2.6)", 3), ("date('2027-01-01')-date('2026-11-01')", 61),
     ("(date('2027-01-01') - date('2026-11-01')).days", 61)],
)
def test_calculate(expression: str, value: float) -> None:
    assert math.isclose(webinfo.calculate(expression), value)


@pytest.mark.parametrize(
    "expression",
    ["__import__('os').system('calc')", "open('x')", "9**9**9", "1/0", "a+1", "().__class__", "[x for x in range(9)]", "'a'*5", "lambda: 1",
     "2**100000", "x" * 300, "date('нет')"],
)
def test_calculate_rejects_anything_but_math(expression: str) -> None:
    with pytest.raises((ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError)):
        webinfo.calculate(expression)


@pytest.mark.parametrize(("value", "text"), [(7245, "7 245"), (453.87, "453,87"), (1000000, "1 000 000"), (0.5, "0,5")])
def test_format_number(value: float, text: str) -> None:
    assert webinfo.format_number(value) == text


SPOKEN = "Каждый вечер она собирала детали из старых приёмников и записывала идеи в толстую тетрадь. Соседи посмеивались над ней."


def test_garbled_echo_is_still_echo() -> None:
    # Настоящий результат офлайн-распознавания в сквозном прогоне.
    assert echo_like("Каждый на собирала деталей старых приёмников и записку было иди и в толстую тетрадь", SPOKEN)


@pytest.mark.parametrize("text", ["стоп", "Подожди, а что там с погодой на завтра у нас в городе", "Включи музыку", "А сколько сейчас времени и какая у нас сегодня дата"])
def test_real_interruption_is_not_echo(text: str) -> None:
    assert not echo_like(text, SPOKEN)


# ---------- Женский род ----------


def masculinize(*parts: str) -> str:
    gender = GenderFilter()
    return "".join(gender.feed(part) for part in parts) + gender.flush()


@pytest.mark.parametrize(
    ("raw", "fixed"),
    [
        ("Хотите, чтобы я проверила?", "Хотите, чтобы я проверил?"),
        ("Я открыла YouTube.", "Я открыл YouTube."),
        ("Я готова помочь.", "Я готов помочь."),
        ("Я не нашла такой файл.", "Я не нашёл такой файл."),
        ("Я уже посмотрела.", "Я уже посмотрел."),
        ("Я рада, что всё работает.", "Я рад, что всё работает."),
        ("Открыла YouTube. Включила видео.", "Открыл YouTube. Включил видео."),
        ("Я ошиблась.", "Я ошибся."),
        ("Я постаралась.", "Я постарался."),
        ("Я смогла это сделать.", "Я смог это сделать."),
    ],
)
def test_feminine_is_fixed(raw: str, fixed: str) -> None:
    assert masculinize(raw) == fixed


@pytest.mark.parametrize(
    "text",
    [
        "Вы сделали это отлично.",
        "Она открыла дверь.",
        "Я сделал всё.",
        "Я думаю, что она права.",
        "Который час?",
        "Если она открыла файл, закройте его.",
        "Я и мой стол.",
        "Hello, I opened it.",
        "Машина пошла вечером.",
    ],
)
def test_other_people_are_untouched(text: str) -> None:
    assert masculinize(text) == text


def test_gender_filter_survives_any_chunk_split() -> None:
    text = "Хотите, чтобы я проверила почту? Я нашла письмо."
    expected = "Хотите, чтобы я проверил почту? Я нашёл письмо."
    for size in (1, 2, 3, 5, 8):
        parts = [text[i:i + size] for i in range(0, len(text), size)]
        assert masculinize(*parts) == expected


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        # Настоящие склейки из ответов gpt-oss-120b.
        ("Записал: купить мололь? (Oops).Записал: купить молоко.", "Записал: купить молоко."),
        ("Сейчас +7°, пасмурно.Сейчас +7°, пасмурно.", "Сейчас +7°, пасмурно."),
        ("Закрыл.Закрыл окно «Telegram».", "Закрыл окно «Telegram»."),
        ("Готово. Готово.", "Готово."),
        ("Первое. Второе.", "Первое. Второе."),
        ("Курс 453.87 тенге.", "Курс 453.87 тенге."),
        ("", ""),
    ],
)
def test_final_message(raw: str, clean: str) -> None:
    assert final_message(raw) == clean


@pytest.mark.parametrize("text", ["Да", "да, выключай", "Давай", "Джарвис, да", "Конечно", "Подтверждаю", "Ага"])
def test_yes(text: str) -> None:
    assert is_yes(text)


@pytest.mark.parametrize("text", ["Нет", "да нет, не надо", "не надо", "Да это вообще не то, что я хотел спросить у тебя сегодня", "", "Погода"])
def test_not_yes(text: str) -> None:
    assert not is_yes(text)


@pytest.mark.parametrize("text", ["Нет", "Не надо", "Отмена", "Отмени", "Стоп", "Джарвис, нет"])
def test_no(text: str) -> None:
    assert is_no(text)


@pytest.mark.parametrize(
    "text",
    ["Включи песню", "Можешь включить песню.", "Джарвис, открой телеграм", "Закрой это окно", "Давай сверни всё", "Поставь таймер", "напиши письмо"],
)
def test_action_requests(text: str) -> None:
    assert looks_like_action(text)


@pytest.mark.parametrize("text", ["Какая погода?", "Как дела", "Почему небо голубое", "Что такое включение", ""])
def test_not_action_requests(text: str) -> None:
    assert not looks_like_action(text)


@pytest.mark.parametrize("text", ["Песня включена, наслаждайся!", "Открыл ютуб.", "Готово.", "Запустил.", "Видео открыто.", "Включил."])
def test_claims(text: str) -> None:
    assert claims_done(text)


@pytest.mark.parametrize("text", ["Какую песню включить?", "Не получилось: браузер не открыт.", "Не открыл, нет такого приложения.", "У меня нет такого инструмента."])
def test_not_claims(text: str) -> None:
    assert not claims_done(text)

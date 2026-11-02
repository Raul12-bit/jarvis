import json

import pytest

from jarvis.stt import parse_whisper, pcm_to_wav
from jarvis.vad import FRAME_BYTES, Segmenter, VadEvent
from jarvis.wake import GRAMMAR, MAYBE_WORDS, WAKE_WORD, WakeSpotter, check_addressed, read_hearing

FRAME = b"\x01\x00" * (FRAME_BYTES // 2)


def feed(seg: Segmenter, probs: list[float], strict: bool = False) -> list[VadEvent]:
    events = []
    for prob in probs:
        event = seg.push(FRAME, prob, strict=strict)
        if event is not None:
            events.append(event)
    return events


def test_speech_segment_with_preroll_and_trimmed_tail() -> None:
    seg = Segmenter(preroll_frames=10, hangover_frames=19)
    events = feed(seg, [0.0] * 20 + [0.9] * 30 + [0.0] * 19)
    assert events == [VadEvent.START, VadEvent.END]
    audio = seg.take_audio()
    # Перед речью сохранён предварительный буфер, хвост тишины обрезан до 6 кадров.
    assert len(audio) == FRAME_BYTES * (10 + 27 + 6)
    assert seg.take_audio() == b""


def test_short_click_is_cancelled() -> None:
    seg = Segmenter()
    assert feed(seg, [0.9] * 3 + [0.0] * 15) == [VadEvent.START, VadEvent.CANCEL]
    assert seg.take_audio() == b""


def test_isolated_spikes_do_not_start_speech() -> None:
    seg = Segmenter()
    assert feed(seg, [0.9, 0.1, 0.9, 0.1, 0.9, 0.1, 0.9, 0.1]) == []


def test_late_detection_keeps_the_start_of_the_phrase() -> None:
    seg = Segmenter()
    # Первые кадры речи тише порога: без предзаписи слово «Джарвис» терялось.
    assert feed(seg, [0.0] * 5 + [0.4] * 8 + [0.9] * 8, strict=True) == [VadEvent.START]
    assert len(seg.current_audio()) == FRAME_BYTES * 16


def test_short_pause_stays_in_one_segment() -> None:
    seg = Segmenter()
    assert feed(seg, [0.9] * 10 + [0.1] * 12 + [0.9] * 10) == [VadEvent.START]
    assert seg.in_speech


def test_strict_mode_needs_louder_and_longer_speech() -> None:
    seg = Segmenter()
    assert feed(seg, [0.6] * 20, strict=True) == []
    assert feed(seg, [0.9] * 7, strict=True) == []
    assert feed(seg, [0.9], strict=True) == [VadEvent.START]


def test_long_speech_is_cut_at_limit() -> None:
    seg = Segmenter(max_seconds=1.0)
    events = feed(seg, [0.9] * 40)
    # Отрезок закрыт по лимиту, а продолжающаяся речь сразу открывает следующий.
    assert events == [VadEvent.START, VadEvent.END, VadEvent.START]
    assert len(seg.take_audio()) == FRAME_BYTES * 31


def test_grammar_contains_wake_word_and_garbage() -> None:
    assert WAKE_WORD in GRAMMAR
    assert GRAMMAR[-1] == "[unk]"
    assert len(GRAMMAR) == len(set(GRAMMAR))


@pytest.mark.parametrize(
    ("text", "addressed", "cleaned"),
    [
        ("Джарвис, какая погода?", True, "Какая погода?"),
        ("Джарвис.", True, ""),
        ("Джарвис", True, ""),
        ("Эй, Джарвис!", True, ""),
        ("Жарвис, сколько времени?", True, "Сколько времени?"),
        ("Джервис, открой почту", True, "Открой почту"),
        ("Jarvis, привет", True, "Привет"),
        ("Слушай, Джарвис, ты тут?", True, "Ты тут?"),
        ("Какая разница, кто это сказал?", False, ""),
        ("У дерева длинный корень", False, ""),
        ("Сделай короткое каре", False, ""),
        ("Чарльз Дарвин написал книгу", False, ""),
        ("Этот сервис не работает", False, ""),
        ("Надо починить кран", False, ""),
        ("", False, ""),
    ],
)
def test_check_addressed(text: str, addressed: bool, cleaned: str) -> None:
    assert check_addressed(text) == (addressed, cleaned)


@pytest.mark.parametrize(
    "text",
    ["Субтитры сделал DimaTorzok", "Продолжение следует...", "Спасибо за просмотр!", "Подписывайтесь на канал"],
)
def test_whisper_hallucinations_are_dropped(text: str) -> None:
    assert parse_whisper({"text": text}) == ""


def test_whisper_keeps_normal_text() -> None:
    assert parse_whisper({"text": " Какая сегодня погода? "}) == "Какая сегодня погода?"


def test_whisper_drops_unconfident_segments() -> None:
    payload = {
        "text": "шум какая погода",
        "segments": [
            {"text": " шум", "no_speech_prob": 0.9, "avg_logprob": -1.2},
            {"text": " какая погода", "no_speech_prob": 0.1, "avg_logprob": -0.2},
        ],
    }
    assert parse_whisper(payload) == "какая погода"


def test_wav_header() -> None:
    wav = pcm_to_wav(b"\0\0" * 100)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) == 44 + 200


@pytest.mark.parametrize(
    ("payload", "sure", "maybe"),
    [
        ({"text": "джарвис какая погода"}, True, True),
        ({"text": "какая погода"}, False, False),
        ({"text": "джервис какая погода"}, False, True),
        ({"alternatives": [{"text": "джарвис"}, {"text": "джервис"}]}, True, True),
        ({"alternatives": [{"text": "джаз"}, {"text": "джарвис"}]}, False, True),
        ({"alternatives": [{"text": "кот спит"}, {"text": "кот спал"}]}, False, False),
        ({"alternatives": []}, False, False),
    ],
)
def test_read_hearing(payload: dict, sure: bool, maybe: bool) -> None:
    assert read_hearing(json.dumps(payload, ensure_ascii=False)) == (sure, maybe)


def test_maybe_words_are_in_grammar() -> None:
    # Слово вне грамматики распознаватель никогда не выдаст, и проверка молча перестанет работать.
    assert MAYBE_WORDS <= set(GRAMMAR)


def test_second_alternative_gives_chance_but_does_not_wake() -> None:
    """Первый вариант без имени не будит сразу, а второй вариант с именем отправляет фразу на проверку."""
    spotter = WakeSpotter.__new__(WakeSpotter)
    spotter.maybe = False
    assert spotter._judge(json.dumps({"alternatives": [{"text": "джервис"}, {"text": "джарвис"}]}, ensure_ascii=False)) is False  # noqa: SLF001
    assert spotter.maybe


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [("Ерви, какая погода?", "Какая погода?"), ("Эй, Дарви.", ""), ("Жэви! Открой ютуб", "Открой ютуб"), ("Харва", "")],
)
def test_garbled_name_counts_only_after_local_mark(text: str, cleaned: str) -> None:
    """Настоящие строки Whisper из замера с шумом. Без отметки распознавателя такое обращением не считается."""
    assert check_addressed(text, lenient=True) == (True, cleaned)
    assert check_addressed(text)[0] is False


@pytest.mark.parametrize("text", ["Дарвин написал книгу.", "Жарко, правда?", "Какая погода?", "Сервис не работает.", "Кирилл вышел.", "Варвара читает."])
def test_ordinary_phrases_are_not_garbled_names(text: str) -> None:
    assert check_addressed(text, lenient=True)[0] is False


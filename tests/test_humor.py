"""Шутки: не повторяться, не портить настроение, работать без интернета."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis import humor, intents
from jarvis.config import ConfigStore
from jarvis.humor import JOKES, JokeBox, parse_jokes, topic_of
from jarvis.memory import Memory
from jarvis.tools import ToolBox

from .conftest import mock_client

PAGE = """
<div class="text">Программист ставит на тумбочку два стакана: с водой, если захочет пить, и пустой, если не захочет.</div>
<div class="text">Короткий.</div>
<div class="text">Строка один<br>строка два &mdash; и это уже достаточно длинный анекдот, чтобы его рассказать.</div>
"""


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "test.db")


def box_with(memory: Memory, handler: Any) -> JokeBox:
    return JokeBox(mock_client(handler), memory)


def test_parse_jokes_cleans_and_filters() -> None:
    jokes = parse_jokes(PAGE)
    assert len(jokes) == 2
    assert "<br>" not in jokes[1] and "—" in jokes[1]


def test_parse_jokes_drops_too_long_and_garbage() -> None:
    assert parse_jokes('<div class="text">' + "я " * 400 + "</div>") == []
    assert parse_jokes("<html>нет анекдотов</html>") == []


@pytest.mark.parametrize(
    ("text", "topic"),
    [("про программистов", "code"), ("про роботов", "robot"), ("про нейросети", "ai"), ("про бегемота", "")],
)
def test_topic_of(text: str, topic: str) -> None:
    assert topic_of(text) == topic


def test_own_jokes_have_every_topic() -> None:
    assert {tag for tag, _ in JOKES} >= {"code", "robot", "ai", "life"}
    assert len({text for _, text in JOKES}) == len(JOKES)


def test_topic_joke_is_on_topic_and_never_repeats(memory: Memory) -> None:
    box = box_with(memory, lambda r: httpx.Response(500))
    robot = {text for tag, text in JOKES if tag == "robot"}
    told = [run(box.next("robot")) for _ in robot]
    assert set(told) == robot
    assert run(box.next("robot")) is None  # пусть придумает модель, а не повторяем


def test_works_offline(memory: Memory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(humor.random, "random", lambda: 0.99)
    box = box_with(memory, lambda r: httpx.Response(500))
    assert run(box.next()) in {text for _, text in JOKES}


def test_network_joke_is_told_once_across_restarts(memory: Memory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(humor.random, "random", lambda: 0.99)
    box = box_with(memory, lambda r: httpx.Response(200, text=PAGE))
    told = {run(box.next()), run(box.next())}
    assert told == set(parse_jokes(PAGE))
    # Новый запуск той же страницы: уже рассказанные пропускаем, остаются только свои.
    restarted = box_with(memory, lambda r: httpx.Response(200, text=PAGE))
    assert run(restarted.next()) not in told


def tool_box(config: ConfigStore, memory: Memory) -> ToolBox:
    return ToolBox(config, memory, mock_client(lambda r: httpx.Response(500)), lambda: None)


@pytest.mark.parametrize("text", ["Расскажи анекдот", "Пошути", "Расскажи шутку", "Давай анекдот"])
def test_joke_needs_no_model(config: ConfigStore, memory: Memory, text: str) -> None:
    answer = run(intents.instant_answer(text, tool_box(config, memory)))
    assert answer in {joke for _, joke in JOKES}


def test_joke_on_topic_needs_no_model(config: ConfigStore, memory: Memory) -> None:
    answer = run(intents.instant_answer("Расскажи анекдот про роботов", tool_box(config, memory)))
    assert answer in {joke for tag, joke in JOKES if tag == "robot"}


def test_unknown_topic_goes_to_model(config: ConfigStore, memory: Memory) -> None:
    assert run(intents.instant_answer("Расскажи анекдот про бегемота", tool_box(config, memory))) is None


def test_more_works_only_right_after_joke(config: ConfigStore, memory: Memory) -> None:
    box = tool_box(config, memory)
    assert run(intents.instant_answer("Ещё", box)) is None
    first = run(intents.instant_answer("Расскажи анекдот", box))
    second = run(intents.instant_answer("Ещё", box))
    assert second is not None and second != first

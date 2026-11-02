"""Логика подключения голоса и запасные пути. Сеть подменена: здесь проверяются решения, а не сервис."""
import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis import tts
from jarvis.config import DEFAULT_VOICE
from jarvis.tts import SpeechAudio, SpeechSynth, estimate_seconds


class FakeSocket:
    def __init__(self, name: str = "") -> None:
        self.name = name
        self.closed = False
        self.sent: list[str] = []

    async def close(self) -> None:
        self.closed = True

    async def send_str(self, text: str) -> None:
        self.sent.append(text)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tts, "_CONNECT_HEDGE", 0.05)
    monkeypatch.setattr(tts, "_CONNECT_LAST_WAIT", 0.3)


def make(home: Path, behaviours: list[Any]) -> SpeechSynth:
    """behaviours: для каждой попытки подключения — секунды до успеха или исключение."""
    synth = SpeechSynth(home)
    opened: list[FakeSocket] = []
    calls = iter(behaviours)

    async def fake_open() -> FakeSocket:
        behaviour = next(calls)
        if isinstance(behaviour, Exception):
            raise behaviour
        await asyncio.sleep(behaviour)
        sock = FakeSocket(str(len(opened)))
        opened.append(sock)
        return sock

    synth._open_socket = fake_open  # type: ignore[method-assign]  # noqa: SLF001
    synth.opened = opened  # type: ignore[attr-defined]
    return synth


def test_normal_connection_needs_one_attempt(tmp_path: Path) -> None:
    async def body() -> None:
        synth = make(tmp_path, [0.01])
        ws = await synth._race_connect()  # noqa: SLF001
        assert ws.name == "0" and len(synth.opened) == 1  # type: ignore[attr-defined]

    run(body())


def test_stalled_handshake_is_overtaken_by_second_attempt(tmp_path: Path) -> None:
    async def body() -> None:
        synth = make(tmp_path, [30.0, 0.01])
        started = time.perf_counter()
        ws = await synth._race_connect()  # noqa: SLF001
        assert time.perf_counter() - started < 1.0  # а не 30 секунд зависшей попытки
        assert ws.name == "0"

    run(body())


def test_two_stalls_are_overtaken_by_third(tmp_path: Path) -> None:
    async def body() -> None:
        synth = make(tmp_path, [30.0, 30.0, 0.01])
        started = time.perf_counter()
        await synth._race_connect()  # noqa: SLF001
        assert time.perf_counter() - started < 1.0

    run(body())


def test_loser_that_connects_later_is_closed(tmp_path: Path) -> None:
    async def body() -> None:
        synth = SpeechSynth(tmp_path)
        created: list[FakeSocket] = []
        finish_slow = asyncio.Event()
        count = {"n": 0}

        async def fake_open() -> FakeSocket:
            count["n"] += 1
            attempt = count["n"]
            if attempt == 1:
                try:
                    await finish_slow.wait()
                except asyncio.CancelledError:
                    # Зависшая попытка успевает подключиться вопреки отмене: сокет нельзя бросить.
                    pass
            sock = FakeSocket(str(attempt))
            created.append(sock)
            return sock

        synth._open_socket = fake_open  # type: ignore[method-assign]  # noqa: SLF001
        winner = await synth._race_connect()  # noqa: SLF001
        await asyncio.sleep(0.02)
        assert winner.name == "2" and not winner.closed
        assert [s.name for s in created if s.closed] == ["1"]

    run(body())


def test_fast_failure_moves_straight_to_next_attempt(tmp_path: Path) -> None:
    async def body() -> None:
        synth = make(tmp_path, [ConnectionError("сброс"), 0.01])
        started = time.perf_counter()
        ws = await synth._race_connect()  # noqa: SLF001
        assert ws.name == "0"
        assert time.perf_counter() - started < 0.1  # не ждём таймер, раз попытка уже провалилась

    run(body())


def test_all_attempts_failing_raises_the_error(tmp_path: Path) -> None:
    async def body() -> None:
        synth = make(tmp_path, [ConnectionError("а"), ConnectionError("б"), ConnectionError("в")])
        with pytest.raises(ConnectionError):
            await synth._race_connect()  # noqa: SLF001

    run(body())


def test_everything_stalled_gives_timeout(tmp_path: Path) -> None:
    async def body() -> None:
        synth = make(tmp_path, [30.0, 30.0, 30.0])
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            await synth._race_connect()  # noqa: SLF001
        assert time.perf_counter() - started < 1.0

    run(body())


def test_unspeakable_text_is_skipped_without_network(tmp_path: Path) -> None:
    async def body() -> None:
        synth = SpeechSynth(tmp_path)
        assert await synth.synthesize("— ... 😀", "ru-RU-SvetlanaNeural") is None
        assert synth._ws is None  # noqa: SLF001

    run(body())


def test_cloud_failure_falls_back_to_windows_voice_and_stops_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> None:
        synth = SpeechSynth(tmp_path)
        cloud_calls = {"n": 0}

        async def no_socket(*_: Any) -> tuple[bytes, bool]:
            cloud_calls["n"] += 1
            return b"", False

        async def no_library(*_: Any) -> bytes:
            cloud_calls["n"] += 1
            return b""

        synth._ws_synth = no_socket  # type: ignore[method-assign]  # noqa: SLF001
        synth._library_synth = no_library  # type: ignore[method-assign]  # noqa: SLF001
        monkeypatch.setattr(tts, "_sapi_synth", lambda text: b"RIFFwav")
        first = await synth.synthesize("Первая фраза", "ru-RU-SvetlanaNeural")
        assert first is not None and first.engine == "sapi" and first.mime == "audio/wav"
        assert cloud_calls["n"] == 2
        # Пока облако недоступно, каждая фраза не должна ждать его таймаутов.
        second = await synth.synthesize("Вторая фраза", "ru-RU-SvetlanaNeural")
        assert second is not None and second.engine == "sapi"
        assert cloud_calls["n"] == 2

    run(body())


def test_short_phrases_are_cached(tmp_path: Path) -> None:
    async def body() -> None:
        synth = SpeechSynth(tmp_path)
        calls = {"n": 0}

        async def cloud(*_: Any) -> tuple[bytes, bool]:
            calls["n"] += 1
            return b"mp3-data", True

        synth._ws_synth = cloud  # type: ignore[method-assign]  # noqa: SLF001
        assert synth.cached("Так.", "voice") is None
        assert (await synth.synthesize("Так.", "voice")).engine == "edge"
        assert (await synth.synthesize("Так.", "voice")).engine == "cache"
        assert synth.cached("Так.", "voice").data == b"mp3-data"
        assert synth.cached("Так.", "другой голос") is None
        assert calls["n"] == 1

    run(body())


def test_cancelled_reply_lets_the_connection_finish(tmp_path: Path) -> None:
    async def body() -> None:
        synth = SpeechSynth(tmp_path)
        finished = asyncio.Event()

        async def slow_cloud(*_: Any) -> tuple[bytes, bool]:
            await asyncio.sleep(0.05)
            finished.set()
            return b"mp3", True

        synth._ws_synth = slow_cloud  # type: ignore[method-assign]  # noqa: SLF001
        task = asyncio.create_task(synth.synthesize("Длинная реплика, которую перебьют", "voice"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Запрос дочитывается в фоне, чтобы следующий получил чистый канал.
        await asyncio.wait_for(finished.wait(), 1)

    run(body())


def test_silent_voice_gives_way_to_another_male_voice(tmp_path: Path) -> None:
    """Сервис отвечает пустотой на отдельный голос: замер показал 19 отказов из 20 у ru-RU-DmitryNeural."""

    async def body() -> None:
        synth = SpeechSynth(tmp_path)
        asked: list[str] = []

        async def picky(text: str, voice: str, rate: str) -> tuple[bytes, bool]:
            asked.append(voice)
            return (b"", True) if voice == "ru-RU-DmitryNeural" else (b"mp3", True)

        synth._ws_synth = picky  # type: ignore[method-assign]  # noqa: SLF001
        first = await synth.synthesize("Первая длинная фраза, которая не попадёт в кэш голоса, потому что в ней много слов", "ru-RU-DmitryNeural")
        assert first is not None and first.engine == "edge" and asked == ["ru-RU-DmitryNeural", DEFAULT_VOICE]
        # Молчащий голос какое-то время не дёргаем: иначе тембр скакал бы посреди ответа.
        asked.clear()
        await synth.synthesize("Вторая длинная фраза, которая тоже не попадёт в кэш голоса, потому что в ней много слов", "ru-RU-DmitryNeural")
        assert asked == [DEFAULT_VOICE]

    run(body())


def test_lost_network_does_not_try_every_voice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body() -> None:
        synth = SpeechSynth(tmp_path)
        asked: list[str] = []

        async def offline(text: str, voice: str, rate: str) -> tuple[bytes, bool]:
            asked.append(voice)
            return b"", False

        async def no_library(*_: Any) -> bytes:
            return b""

        synth._ws_synth = offline  # type: ignore[method-assign]  # noqa: SLF001
        synth._library_synth = no_library  # type: ignore[method-assign]  # noqa: SLF001
        monkeypatch.setattr(tts, "_sapi_synth", lambda text: b"RIFFwav")
        spoken = await synth.synthesize("Нет сети", DEFAULT_VOICE)
        assert spoken is not None and spoken.engine == "sapi" and len(asked) == 1

    run(body())


def test_windows_voice_is_male_when_it_can_be() -> None:
    assert "Pavel" in tts._SAPI_SCRIPT and "Speech_OneCore" in tts._SAPI_SCRIPT  # noqa: SLF001


def test_estimates() -> None:
    assert estimate_seconds(SpeechAudio(b"\0" * 12000, "audio/mpeg", "edge", 0)) == 2.0
    assert estimate_seconds(SpeechAudio(b"\0" * (44 + 44100), "audio/wav", "sapi", 0)) == 1.0

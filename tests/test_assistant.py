"""Сценарии разговора целиком. Подменены только облако (LLM, STT) и синтез: автомат настоящий."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Coroutine

import httpx
import pytest

from jarvis import assistant as assistant_module
from jarvis.assistant import Assistant
from jarvis.config import ConfigStore
from jarvis.llm import LLMRouter
from jarvis.persona import CREATIVE_GUIDE, SYSTEM_PROMPT
from jarvis.stt import SpeechUnavailable, Transcript
from jarvis.tools import SCHEMAS, TOOL_GROUPS
from jarvis.tts import SpeechAudio
from jarvis.vad import FRAME_BYTES

from .conftest import mock_client, sse, text_chunk, text_reply

FRAME = b"\x01\x00" * (FRAME_BYTES // 2)


class FakeOut:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.audio: list[dict[str, Any]] = []

    def send_json(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def send_audio(self, header: dict[str, Any], data: bytes) -> None:
        self.audio.append(header)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["type"] == kind]

    def states(self) -> list[str]:
        return [m["state"] for m in self.of("state")]


class FakeSynth:
    """Синтез без сети. Заполнители считаются закэшированными."""

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.delay = 0.0

    def warm(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def cached(self, text: str, voice: str, rate: int = 0) -> SpeechAudio:
        return SpeechAudio(b"\0" * 6000, "audio/mpeg", "cache", 0.0)

    async def synthesize(self, text: str, voice: str, rate: int = 0) -> SpeechAudio:
        await asyncio.sleep(self.delay)
        self.spoken.append(text)
        return SpeechAudio(b"\0" * 6000, "audio/mpeg", "edge", 0.01)


class FakeStt:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.delay = 0.0
        self.audio: list[bytes] = []
        self.languages: list[str] = []

    async def transcribe(self, pcm: bytes, language: str = "") -> Transcript:
        self.audio.append(pcm)
        self.languages.append(language)
        await asyncio.sleep(self.delay)
        return Transcript(self.texts.pop(0) if self.texts else "", "fake", 0.01)


class FakeVad:
    """Вероятность речи задаёт тест: 1, если кадр не пустой."""

    def process_chunk(self, frame: bytes) -> float:
        return 0.95 if frame != b"\0" * FRAME_BYTES else 0.0


class FakeSpotter:
    def __init__(self) -> None:
        self.hit = False
        self.maybe = False

    def begin(self, audio: bytes = b"") -> None:
        pass

    def feed(self, frame: bytes) -> bool:
        return self.hit

    def finish(self) -> bool:
        return self.hit


def tool_call_reply(name: str, args: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=sse(
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }]}}]},
        "[DONE]",
    ))


class Rig:
    def __init__(self, config: ConfigStore, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.out = FakeOut()
        self.a = Assistant(config, self.out)
        self.requests: list[dict[str, Any]] = []

        def recording(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                self.requests.append(json.loads(request.content))
            return handler(request)

        config.update(groq_api_key="gsk_test_key_12345", onboarded=True)
        self.client = mock_client(recording)
        self.a.llm = LLMRouter(config, self.client)
        self.a.tools.http = self.client
        self.a.tools.jokes.http = self.client
        self.a.phone.http = self.client
        # Тесты не должны переключать настоящие окна на компьютере разработчика.
        self.a.tools.show_browser_after_open = False
        self.tts = FakeSynth()
        self.stt = FakeStt()
        self.a.tts = self.tts  # type: ignore[assignment]
        self.a.stt = self.stt  # type: ignore[assignment]
        self.a.vad = FakeVad()
        self.spotter = FakeSpotter()
        self.a.spotter = self.spotter  # type: ignore[assignment]
        self.a.mic_on = True
        self.a.connected = True

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"Не дождались. Состояния: {self.out.states()}, сообщения: {self.out.messages[-5:]}")
            await asyncio.sleep(0.005)

    async def idle(self) -> None:
        await self.wait_for(lambda: not self.a._busy())  # noqa: SLF001

    def speak(self, frames: int = 30) -> None:
        for _ in range(frames):
            self.a.on_mic_frame(FRAME)

    def silence(self, frames: int = 25) -> None:
        for _ in range(frames):
            self.a.on_mic_frame(b"\0" * FRAME_BYTES)

    def utter(self, frames: int = 30) -> None:
        self.speak(frames)
        self.silence()

    async def ack_playback(self) -> None:
        """Изображает браузер: звук начал играть и закончился."""
        await self.wait_for(lambda: self.a.turn is None or self.a.turn.seq > 0 or not self.a._busy())  # noqa: SLF001
        turn = self.a.turn
        if turn is not None:
            await self.a.on_message({"type": "playback", "turn": turn.id, "event": "start"})

    async def finish_playback(self) -> None:
        await self.wait_for(lambda: bool(self.out.of("audio_done")))
        turn_id = self.out.of("audio_done")[-1]["turn"]
        await self.a.on_message({"type": "playback", "turn": turn_id, "event": "start"})
        await self.a.on_message({"type": "playback", "turn": turn_id, "event": "done"})
        await self.idle()

    async def close(self) -> None:
        self.a._cancel_followup()  # noqa: SLF001
        self.a._cancel_work()  # noqa: SLF001
        self.a._cancel_barge()  # noqa: SLF001
        await asyncio.sleep(0)
        await self.client.aclose()
        await self.a.http.aclose()
        self.a.memory.close()


def scenario(config: ConfigStore, handler: Callable[[httpx.Request], httpx.Response], body: Callable[[Rig], Coroutine[Any, Any, None]]) -> None:
    async def runner() -> None:
        rig = Rig(config, handler)
        try:
            await body(rig)
        finally:
            await rig.close()

    asyncio.run(runner())


def test_text_turn_full_cycle(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Привет"})
        await rig.finish_playback()
        assert rig.tts.spoken == ["Привет, рада слышать."]
        assert rig.out.states()[-3:] == ["thinking", "speaking", "listening"]
        assert rig.a.mode == "active"
        history = rig.a.memory.recent_messages()
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert rig.out.of("metrics")[-1]["provider"] == "groq"

    scenario(config, lambda r: text_reply("Привет, ", "рада слышать."), body)


def test_followup_returns_to_sleep(config: ConfigStore) -> None:
    config.update(followup_seconds=4)

    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        assert rig.a.state == "listening"
        rig.a._followup_expired()  # noqa: SLF001
        assert rig.a.mode == "sleep" and rig.a.state == "sleep"

    scenario(config, lambda r: text_reply("а"), body)


def test_followup_waits_while_person_speaks(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        rig.speak(10)
        rig.a._followup_expired()  # noqa: SLF001
        assert rig.a.mode == "active"

    scenario(config, lambda r: text_reply("а"), body)


def test_wake_word_then_command_in_one_breath(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.spotter.hit = True
        rig.stt.texts = ["Джарвис, сколько будет два плюс два?"]
        rig.utter()
        assert rig.out.of("chime")
        await rig.wait_for(lambda: bool(rig.tts.spoken))
        await rig.finish_playback()
        assert rig.requests[0]["messages"][-1]["content"] == "Сколько будет два плюс два?"
        assert rig.tts.spoken == ["Четыре."]

    scenario(config, lambda r: text_reply("Четыре."), body)


def test_wake_word_alone_gets_acknowledged_without_llm(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.spotter.hit = True
        rig.stt.texts = ["Джарвис."]
        rig.utter()
        await rig.finish_playback()
        assert rig.tts.spoken[0] in ("Да, сэр?", "Слушаю, сэр.")
        assert rig.requests == []
        assert rig.a.mode == "active"

    scenario(config, lambda r: text_reply("нет"), body)


def test_false_wake_is_dropped_silently(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.spotter.hit = True
        rig.stt.texts = ["Какая разница, кто это сказал?"]
        rig.utter()
        await rig.idle()
        assert rig.requests == [] and rig.tts.spoken == []
        assert rig.a.mode == "sleep" and rig.a.state == "sleep"

    scenario(config, lambda r: text_reply("нет"), body)


def test_sleeping_ignores_speech_without_wake_word(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.spotter.hit = False
        rig.stt.texts = ["Это не должно уйти в облако"]
        rig.utter()
        await asyncio.sleep(0.02)
        assert rig.stt.audio == []  # приватность: без имени звук не покидает компьютер
        assert rig.a.state == "sleep"

    scenario(config, lambda r: text_reply("нет"), body)


def test_active_mode_needs_no_wake_word(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        rig.stt.texts = ["А завтра?"]
        rig.utter()
        await rig.finish_playback()
        assert rig.requests[0]["messages"][-1]["content"] == "А завтра?"
        context = rig.requests[0]["messages"][-2]["content"]
        assert "[молчу]" in context  # без обращения по имени Джарвиса вправе промолчать

    scenario(config, lambda r: text_reply("Будет теплее."), body)


def test_silence_mark_is_not_spoken(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        rig.stt.texts = ["Мам, ты где положила ключи?"]
        rig.utter()
        await rig.idle()
        assert rig.tts.spoken == []
        assert rig.out.of("user_retract")
        assert rig.a.memory.recent_messages() == []
        assert rig.a.state == "listening"

    scenario(config, lambda r: text_reply("[мол", "чу]"), body)


def test_bracket_answer_that_is_not_silence_is_spoken(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        rig.stt.texts = ["Что?"]
        rig.utter()
        await rig.finish_playback()
        assert rig.tts.spoken == ["[мо хорошо."]

    scenario(config, lambda r: text_reply("[мо", " хорошо."), body)


def test_simple_action_is_spoken_without_second_request(config: ConfigStore) -> None:
    """Лимит Groq 8000 токенов в минуту: второй запрос ради «Запомнил» и приводил к глупым ответам слабой модели."""
    replies = [tool_call_reply("facts", {"action": "remember", "text": "Пьёт кофе без сахара"})]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Запомни: я пью кофе без сахара"})
        await rig.finish_playback()
        assert rig.tts.spoken == ["Запомнил."]
        assert rig.a.memory.facts()[0]["text"] == "Пьёт кофе без сахара"
        assert len(rig.requests) == 1
        statuses = [(m["name"], m["status"]) for m in rig.out.of("tool")]
        assert statuses == [("facts", "start"), ("facts", "end")]

    scenario(config, lambda r: replies.pop(0), body)


def test_tool_round_trip(config: ConfigStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "open-meteo" in request.url.host:
            return httpx.Response(500)
        return replies.pop(0)

    replies = [tool_call_reply("get_weather", {"city": "Алматы"}), text_reply("Погоду узнать не вышло.")]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Какая погода?"})
        await rig.finish_playback()
        assert rig.tts.spoken[-1] == "Погоду узнать не вышло."
        second = rig.requests[1]["messages"]
        assert second[-2]["tool_calls"][0]["id"] == "call_1"
        assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1" and second[-1]["name"] == "get_weather"

    scenario(config, handler, body)


def test_several_steps_go_back_to_the_model_with_tools(config: ConfigStore) -> None:
    replies = [tool_call_reply("timers", {"action": "list"}), tool_call_reply("timers", {"action": "list"}), text_reply("Всё сделала.")]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Покажи таймеры и открой ютуб"})
        await rig.finish_playback()
        assert len(rig.requests) == 3
        assert rig.requests[1].get("tools")
        assert rig.tts.spoken == ["Всё сделала."]

    scenario(config, lambda r: replies.pop(0), body)


def test_request_only_carries_tools_it_needs(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Как дела у тебя сегодня?"})
        await rig.finish_playback()
        names = {tool["function"]["name"] for tool in rig.requests[0]["tools"]}
        assert names == set(TOOL_GROUPS["core"])

        rig.out.messages.clear()
        await rig.a.on_message({"type": "text", "text": "Напиши в телеграм привет"})
        await rig.finish_playback()
        names = {tool["function"]["name"] for tool in rig.requests[1]["tools"]}
        assert "telegram" in names and "system_control" not in names

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_requested_group_is_offered_on_the_next_round(config: ConfigStore) -> None:
    replies = [tool_call_reply("request_tools", {"group": "life"}), text_reply("Готово.")]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Сделай ту штуку, о которой мы говорили"})
        await rig.finish_playback()
        names = {tool["function"]["name"] for tool in rig.requests[1]["tools"]}
        assert "notes" in names

    scenario(config, lambda r: replies.pop(0), body)


def test_group_stays_for_the_next_utterance(config: ConfigStore) -> None:
    replies = [tool_call_reply("notes", {"action": "add", "text": "молоко"}), text_reply("Ладно.")]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Запиши в дела: купить молоко"})
        await rig.finish_playback()
        rig.out.messages.clear()
        await rig.a.on_message({"type": "text", "text": "И что там ещё"})
        await rig.finish_playback()
        names = {tool["function"]["name"] for tool in rig.requests[1]["tools"]}
        assert "notes" in names

    scenario(config, lambda r: replies.pop(0), body)


@pytest.mark.parametrize("text", ["Придумай историю про робота", "Сочини стих про осень", "Давай страшную сказку на ночь"])
def test_creative_request_gets_guide_and_no_tools(config: ConfigStore, text: str) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": text})
        await rig.finish_playback()
        request = rig.requests[0]
        assert not request.get("tools")
        assert request["temperature"] > 0.8
        assert any(m["role"] == "system" and m["content"] == CREATIVE_GUIDE for m in request["messages"])

    scenario(config, lambda r: text_reply("Жил-был робот."), body)


@pytest.mark.parametrize("text", ["Можешь очистить историю браузера", "Покажи историю сообщений", "Хочу открыть файл с анекдотами"])
def test_action_about_stories_keeps_tools(config: ConfigStore, text: str) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": text})
        await rig.finish_playback()
        assert rig.requests[0].get("tools")

    scenario(config, lambda r: text_reply("Этого я не умею."), body)


def test_joke_goes_to_model_that_jokes_better_with_fallback(config: ConfigStore) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        return httpx.Response(429, json={"error": {}}) if model.startswith("qwen") else text_reply("Шутка.")

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Придумай шутку про дедлайны"})
        await rig.finish_playback()
        assert seen[0].startswith("qwen") and not seen[1].startswith("qwen")
        assert rig.requests[0]["max_tokens"] <= 1000  # у qwen лимит 1000 токенов ответа в минуту
        assert rig.tts.spoken == ["Шутка."]

    scenario(config, handler, body)


def test_story_stays_on_main_model(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Расскажи смешную историю"})
        await rig.finish_playback()
        assert "gpt-oss" in rig.requests[0]["model"]

    scenario(config, lambda r: text_reply("Жил-был робот."), body)


def test_empty_answers_are_not_called_network_failure(config: ConfigStore) -> None:
    """Модель потратила всё на рассуждения: связь есть, и говорить «нет связи» было бы неправдой."""

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "В чём смысл жизни?"})
        await rig.finish_playback()
        assert "связи" not in rig.tts.spoken[0] and "не смогла ответить" in rig.tts.spoken[0]
        assert rig.out.of("brain_down")[-1]["kind"] == "busy"

    scenario(config, lambda r: text_reply(), body)


def test_creative_follow_up_stays_creative_only_right_after(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Придумай историю про кота"})
        await rig.finish_playback()
        rig.out.messages.clear()
        await rig.a.on_message({"type": "text", "text": "Давай ещё смешнее"})
        await rig.finish_playback()
        assert not rig.requests[1].get("tools")
        rig.out.messages.clear()
        rig.a._creative_at -= 1000  # noqa: SLF001
        await rig.a.on_message({"type": "text", "text": "Давай ещё смешнее"})
        await rig.finish_playback()
        assert rig.requests[2].get("tools")

    scenario(config, lambda r: text_reply("Кот улыбнулся."), body)


def test_phone_turn_acts_without_any_sound(config: ConfigStore) -> None:
    replies = [tool_call_reply("facts", {"action": "remember", "text": "Любит чай"})]

    async def body(rig: Rig) -> None:
        reply, asks = await rig.a.remote_turn("Запомни, что я люблю чай")
        assert reply == "Запомнил." and asks is False
        assert rig.tts.spoken == [] and not rig.out.audio
        assert rig.a.memory.facts()[0]["text"] == "Любит чай"
        assert rig.out.of("user_text")[-1]["source"] == "phone"
        history = rig.a.memory.recent_messages()
        assert history[-1] == {"role": "assistant", "content": "Запомнил."}

    scenario(config, lambda r: replies.pop(0), body)


def test_phone_turn_tells_about_confirmation_and_yes_works(config: ConfigStore) -> None:
    done: list[str] = []
    replies = [tool_call_reply("system_control", {"action": "shutdown"})]

    async def body(rig: Rig) -> None:
        async def fake_power(flag: str, message: str) -> str:
            done.append(flag)
            return message

        rig.a.tools._power = fake_power  # type: ignore[method-assign]  # noqa: SLF001
        reply, asks = await rig.a.remote_turn("Ну всё, выруби машину")
        assert reply == "Выключить ноутбук?" and asks is True and done == []
        reply, asks = await rig.a.remote_turn("Да")
        assert done == ["/s"] and asks is False
        assert len(rig.requests) == 1

    scenario(config, lambda r: replies.pop(0), body)


def test_phone_turn_waits_for_voice_turn(config: ConfigStore) -> None:
    """Голос и телефон могут заговорить одновременно: инструменты помнят состояние одной реплики."""

    async def body(rig: Rig) -> None:
        async with rig.a._brain_lock:  # noqa: SLF001
            task = asyncio.create_task(rig.a.remote_turn("Который час?"))
            await asyncio.sleep(0.02)
            assert not task.done()
        reply, _ = await task
        assert reply.startswith("Сейчас ")

    scenario(config, lambda r: text_reply("Хорошо."), body)


class FakeMascot:
    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []
        self.emotes: list[str] = []
        self.states: list[str] = []
        self.mic = True
        self.captions = True
        self.size_name = "medium"
        self.hide_in_fullscreen = False

    def say(self, text: str, seconds: float = 5.0, tone: str = "speaking", force: bool = False) -> None:
        self.said.append((text, tone))

    def emote(self, name: str) -> None:
        self.emotes.append(name)

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def set_mic(self, on: bool) -> None:
        self.mic = on

    def set_level(self, value: float) -> None:
        pass

    def set_size(self, name: str) -> None:
        self.size_name = name

    def stop(self) -> None:
        pass


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.visible = False

    async def show(self) -> bool:
        self.calls.append("show")
        self.visible = True
        return True

    def hide(self) -> bool:
        self.calls.append("hide")
        self.visible = False
        return True

    async def start_hidden(self) -> bool:
        self.calls.append("start_hidden")
        return True

    def find(self) -> int:
        return 7

    def is_visible(self) -> bool:
        return self.visible

    def close(self) -> None:
        self.calls.append("close")


def test_similar_word_is_checked_quietly_and_wakes(config: ConfigStore) -> None:
    """Настоящая беда: в шумной комнате имя слышалось в 29 % случаев. Похожее теперь тихо проверяет Whisper."""

    async def body(rig: Rig) -> None:
        rig.spotter.maybe = True
        rig.stt.texts.append("Джарвис, который час?")
        rig.utter()
        rig.silence()
        await rig.wait_for(lambda: bool(rig.tts.spoken))
        assert rig.tts.spoken[0].startswith("Сейчас ")
        assert rig.stt.languages == ["ru"]  # на шуме сам Whisper выбирал албанский
        assert rig.out.of("chime")[-1]["kind"] == "wake"

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_similar_word_without_name_stays_invisible(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.spotter.maybe = True
        rig.stt.texts.append("Кран опять течёт")
        rig.out.messages.clear()
        rig.utter()
        rig.silence()
        await rig.wait_for(lambda: bool(rig.stt.audio))
        await rig.idle()
        assert rig.tts.spoken == [] and rig.requests == []
        assert "thinking" not in rig.out.states() and not rig.out.of("chime")
        assert rig.a.mode == "sleep"

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_name_check_can_be_turned_off(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.config.update(wake_cloud_check=False)
        rig.spotter.maybe = True
        rig.utter()
        rig.silence()
        await asyncio.sleep(0.02)
        assert rig.stt.audio == []

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_long_talk_is_not_sent_for_name_check(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        assert not rig.a._may_check_name(b"\0" * (assistant_module._NAME_CHECK_SECONDS + 1) * assistant_module.SAMPLE_RATE * 2)  # noqa: SLF001
        assert rig.a._may_check_name(b"\0" * 3200)  # noqa: SLF001

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_name_checks_are_limited_per_hour(config: ConfigStore) -> None:
    """Телевизор в комнате не должен весь день ехать в облако."""

    async def body(rig: Rig) -> None:
        for _ in range(assistant_module._NAME_CHECKS_PER_HOUR):
            assert rig.a._may_check_name(b"\0" * 3200)  # noqa: SLF001
        assert not rig.a._may_check_name(b"\0" * 3200)  # noqa: SLF001

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_mascot_follows_state_speech_and_tools(config: ConfigStore) -> None:
    replies = [tool_call_reply("system_info", {})]

    async def body(rig: Rig) -> None:
        mascot = FakeMascot()
        rig.a.mascot = mascot  # type: ignore[assignment]
        await rig.a.on_message({"type": "text", "text": "Как батарея?"})
        await rig.finish_playback()
        assert "thinking" in mascot.states
        assert any("Процессор" in text for text, _ in mascot.said)

    scenario(config, lambda r: replies.pop(0), body)


def test_mascot_alerts_on_reminder(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        mascot = FakeMascot()
        rig.a.mascot = mascot  # type: ignore[assignment]
        rig.a.announce("Напоминаю: чай.")
        assert "alert" in mascot.emotes and ("Напоминаю: чай.", "alert") in mascot.said

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_phone_command_is_shown_near_mascot(config: ConfigStore) -> None:
    """Кто сидит за ноутбуком, должен видеть, что команда пришла с телефона."""

    async def body(rig: Rig) -> None:
        mascot = FakeMascot()
        rig.a.mascot = mascot  # type: ignore[assignment]
        await rig.a.remote_turn("Который час?")
        assert mascot.said[0][0] == "С телефона: Который час?"
        assert mascot.states[0] == "thinking" and mascot.states[-1] != "thinking"

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_show_window_through_host(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        host = FakeHost()
        rig.a.host = host  # type: ignore[assignment]
        reply, _ = await rig.a.remote_turn("Открой Джарвис")
        assert reply == "Я здесь, сэр." and host.calls == ["show"]
        reply, _ = await rig.a.remote_turn("Спрячься")
        assert "Скрываюсь" in reply and host.calls == ["show", "hide"]
        assert rig.requests == []

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_window_closed_with_mascot_reopens_hidden(config: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_module, "_REOPEN_PAUSE", 0.01)

    async def body(rig: Rig) -> None:
        host = FakeHost()
        rig.a.host = host  # type: ignore[assignment]
        rig.a.connected = False
        assert rig.a.keeps_running()
        rig.a.on_window_gone()
        await asyncio.sleep(0.05)
        assert host.calls == ["start_hidden"]

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_window_closed_does_not_reopen_if_person_opened_it(config: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_module, "_REOPEN_PAUSE", 0.02)

    async def body(rig: Rig) -> None:
        host = FakeHost()
        rig.a.host = host  # type: ignore[assignment]
        rig.a.connected = False
        rig.a.on_window_gone()
        rig.a.connected = True
        await asyncio.sleep(0.05)
        assert host.calls == []

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_nothing_keeps_running_without_mascot_and_phone(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.host = FakeHost()  # type: ignore[assignment]
        rig.a.config.update(mascot=False)
        assert not rig.a.keeps_running()
        rig.a.config.update(telegram_bot_token="1:x")
        assert rig.a.keeps_running()
        rig.a.host = None
        assert not rig.a.keeps_running()

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_speak_aloud_from_phone_really_speaks(config: ConfigStore) -> None:
    """Настоящий случай: «скажи на ноуте что-то», а ответ только текстом и слова о том, что её слышно."""

    async def body(rig: Rig) -> None:
        reply, _ = await rig.a.remote_turn("Скажи вслух обед готов")
        assert reply == "Сказал вслух на ноутбуке."
        await rig.wait_for(lambda: rig.tts.spoken == ["обед готов"])

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_phone_context_says_user_is_away(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.remote_turn("Как дела?")
        context = [m["content"] for m in rig.requests[0]["messages"] if m["role"] == "system"][-1]
        assert "speak_aloud" in context and "phone" in context

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_context_lists_open_programs(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.desktop.open_apps = lambda: "code, firefox"  # type: ignore[method-assign]
        await rig.a.on_message({"type": "text", "text": "Как дела?"})
        await rig.finish_playback()
        context = [m["content"] for m in rig.requests[0]["messages"] if m["role"] == "system"][-1]
        assert "Открыты программы: code, firefox." in context

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_creating_routine_with_and_needs_no_second_request(config: ConfigStore) -> None:
    """Настоящая проверка: «…и поставь громкость» считалось несколькими шагами, и модель сама сделала лишний снимок экрана."""
    replies = [tool_call_reply("routines", {"action": "create", "name": "работа", "steps": ["открой код", "громкость 30"]})]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Создай сценарий работа: открой код и поставь громкость 30"})
        await rig.finish_playback()
        assert len(rig.requests) == 1
        assert rig.a.memory.routine("работа") is not None

    scenario(config, lambda r: replies.pop(0), body)


def test_routine_runs_steps_in_order(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.memory.save_routine("перерыв", ["Который час?", "Какое сегодня число"])
        await rig.a.on_message({"type": "text", "text": "Перерыв"})
        await rig.finish_playback()
        said = " ".join(rig.tts.spoken)
        assert said.startswith("Запускаю сценарий перерыв.") and "Сейчас " in said and "Сегодня " in said
        assert rig.requests == []

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_routine_step_asking_question_stops_the_rest(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.memory.save_routine("конец", ["Выключи ноутбук", "Который час?"])
        await rig.a.on_message({"type": "text", "text": "Конец"})
        await rig.finish_playback()
        said = " ".join(rig.tts.spoken)
        assert "Выключить ноутбук?" in said and "подождут" in said and "Сейчас " not in said

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_dictation_types_speech_and_stops(config: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    typed: list[str] = []
    monkeypatch.setattr(assistant_module.winapi, "send_text", typed.append)

    async def body(rig: Rig) -> None:
        async def focus_target() -> Any:
            return object()

        async def not_password() -> bool:
            return False

        rig.a.desktop.focus_target = focus_target  # type: ignore[method-assign]
        rig.a.desktop.uia.focused_is_password = not_password  # type: ignore[method-assign]
        await rig.a.on_message({"type": "text", "text": "Включи диктовку"})
        await rig.finish_playback()
        assert rig.a.dictating
        rig.tts.spoken.clear()
        rig.stt.texts.append("Привет, это письмо.")
        await rig.a._handle_utterance(b"\0" * 3200, via_wake=False, addressed=False)  # noqa: SLF001
        assert typed == ["Привет, это письмо. "] and rig.tts.spoken == [] and rig.requests == []
        rig.stt.texts.append("Стоп диктовка.")
        task = asyncio.create_task(rig.a._handle_utterance(b"\0" * 3200, via_wake=False, addressed=False))  # noqa: SLF001
        await rig.wait_for(lambda: bool(rig.tts.spoken))
        assert not rig.a.dictating and typed == ["Привет, это письмо. "]
        turn = rig.out.of("audio_done")[-1]["turn"]
        await rig.a.on_message({"type": "playback", "turn": turn, "event": "done"})
        await task

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_dictation_refuses_password_field(config: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    typed: list[str] = []
    monkeypatch.setattr(assistant_module.winapi, "send_text", typed.append)

    async def body(rig: Rig) -> None:
        async def focus_target() -> Any:
            return object()

        async def password() -> bool:
            return True

        rig.a.desktop.focus_target = focus_target  # type: ignore[method-assign]
        rig.a.desktop.uia.focused_is_password = password  # type: ignore[method-assign]
        rig.a.dictating = True
        await rig.a._dictate("секрет")  # noqa: SLF001
        assert typed == []

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_camera_frame_round_trip(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        task = asyncio.create_task(rig.a.grab_camera())
        await rig.wait_for(lambda: bool(rig.out.of("camera")))
        request = rig.out.of("camera")[-1]["id"]
        await rig.a.on_message({"type": "camera_frame", "id": request, "data": "aGVsbG8="})
        assert await task == b"hello"

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_camera_error_is_readable(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        task = asyncio.create_task(rig.a.grab_camera())
        await rig.wait_for(lambda: bool(rig.out.of("camera")))
        await rig.a.on_message({"type": "camera_frame", "id": rig.out.of("camera")[-1]["id"], "error": "NotAllowedError"})
        with pytest.raises(RuntimeError, match="разреши"):
            await task

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_turned_off_mic_stays_off_after_window_reopens(config: ConfigStore) -> None:
    """Страница открывается заново сама: выключенный человеком микрофон не должен включиться за его спиной."""

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "mic", "on": False, "wanted": False})
        rig.out.messages.clear()
        rig.a.send_hello()
        assert rig.out.of("hello")[-1]["mic_wanted"] is False
        await rig.a.on_message({"type": "mic", "on": True, "wanted": True})
        rig.a.send_hello()
        assert rig.out.of("hello")[-1]["mic_wanted"] is True

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_failed_mic_is_not_remembered_as_refusal(config: ConfigStore) -> None:
    """Микрофон не запустился, но человек его не выключал: в следующий раз пробуем снова."""

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "mic", "on": False, "wanted": True})
        rig.a.send_hello()
        assert rig.out.of("hello")[-1]["mic_wanted"] is True

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_closing_window_is_explained_once(config: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_module, "_REOPEN_PAUSE", 0.0)

    async def body(rig: Rig) -> None:
        mascot = FakeMascot()
        rig.a.mascot = mascot  # type: ignore[assignment]
        rig.a.host = FakeHost()  # type: ignore[assignment]
        rig.a.connected = False
        rig.a.on_window_gone()
        await asyncio.sleep(0.01)
        rig.a.on_window_gone()
        await asyncio.sleep(0.01)
        assert sum("правая кнопка" in text for text, _ in mascot.said) == 1

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_hidden_start_does_not_greet_aloud(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "ready", "mic": True, "visible": False})
        await asyncio.sleep(0.02)
        assert rig.tts.spoken == []

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_greeting_mentions_the_day(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.memory.add_note("купить молоко")
        await rig.a.on_message({"type": "ready", "mic": True, "visible": True})
        await rig.wait_for(lambda: bool(rig.tts.spoken))
        assert "Дел в списке: 1." in rig.tts.spoken[0]

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_announce_with_closed_window_goes_only_to_phone(config: ConfigStore) -> None:
    sent: list[str] = []

    async def body(rig: Rig) -> None:
        async def notify(text: str) -> None:
            sent.append(text)

        rig.a.phone.notify = notify  # type: ignore[method-assign]
        rig.a.config.update(telegram_bot_token="1:x", telegram_owner_id=5)
        rig.a.connected = False
        rig.a.announce("Напоминаю: чай.")
        await asyncio.sleep(0.01)
        assert sent == ["Напоминаю: чай."]
        assert rig.tts.spoken == [] and not rig.a._busy()  # noqa: SLF001

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_quiet_question_has_no_chime_and_stays_off_phone(config: ConfigStore) -> None:
    sent: list[str] = []

    async def body(rig: Rig) -> None:
        async def notify(text: str) -> None:
            sent.append(text)

        rig.a.phone.notify = notify  # type: ignore[method-assign]
        rig.a.config.update(telegram_bot_token="1:x", telegram_owner_id=5)
        rig.a._check_in("Как самочувствие?")  # noqa: SLF001
        await rig.finish_playback()
        assert rig.tts.spoken == ["Как самочувствие?"]
        assert not rig.out.of("chime") and sent == []

    scenario(config, lambda r: text_reply("Хорошо."), body)


def test_slow_tool_gets_spoken_filler(config: ConfigStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "open-meteo" in request.url.host:
            return httpx.Response(500)
        return replies.pop(0)

    replies = [tool_call_reply("get_weather", {"city": "Алматы"}), text_reply("Не удалось узнать погоду.")]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Какая погода?"})
        await rig.wait_for(lambda: bool(rig.out.audio))
        assert rig.out.audio[0]["text"] in ("Сейчас посмотрю, сэр.", "Секунду, проверю.", "Уже уточняю, сэр.")
        await rig.finish_playback()
        assert rig.tts.spoken == ["Не удалось узнать погоду."]

    scenario(config, handler, body)


def test_endless_tool_loop_is_stopped(config: ConfigStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("tools"):
            return text_reply("Готово.")
        return tool_call_reply("request_tools", {"group": "screen"})

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Сделай что-нибудь полезное для меня"})
        await rig.finish_playback()
        assert len(rig.requests) == 6
        assert rig.tts.spoken == ["Готово."]

    scenario(config, handler, body)


def test_person_continues_during_thinking_texts_are_merged(config: ConfigStore) -> None:
    gate = asyncio.Event()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return text_reply("Ответ на всё сразу.")

    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        rig.tts.delay = 0.3  # реплика ещё не зазвучала, когда человек продолжил
        rig.stt.texts = ["Расскажи про Алматы", "и про Астану тоже"]
        rig.utter()
        await rig.wait_for(lambda: rig.a.turn is not None and rig.a.turn.text != "")
        rig.utter()
        await rig.wait_for(lambda: len(rig.requests) >= 2)
        rig.tts.delay = 0
        await rig.wait_for(lambda: rig.out.of("audio_done"))
        await rig.finish_playback()
        assert rig.requests[-1]["messages"][-1]["content"] == "Расскажи про Алматы и про Астану тоже"
        assert rig.out.of("user_retract")
        users = [m["content"] for m in rig.a.memory.recent_messages() if m["role"] == "user"]
        assert users == ["Расскажи про Алматы и про Астану тоже"]

    scenario(config, handler, body)
    _ = gate


def test_person_continues_during_recognition_audio_is_merged(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        rig.stt.delay = 0.3
        rig.stt.texts = ["не должно понадобиться", "вся фраза целиком"]
        rig.utter(30)
        await rig.wait_for(lambda: bool(rig.stt.audio))
        first_size = len(rig.stt.audio[0])
        rig.utter(30)
        await rig.wait_for(lambda: len(rig.stt.audio) >= 2)
        assert len(rig.stt.audio[1]) > first_size * 1.5
        rig.stt.delay = 0

    scenario(config, lambda r: text_reply("Да."), body)


def start_speaking_handler(request: httpx.Request) -> httpx.Response:
    return text_reply("Сейчас в Алматы плюс двенадцать, небольшая облачность и слабый ветер.")


async def get_her_talking(rig: Rig) -> None:
    await rig.a.on_message({"type": "text", "text": "Какая погода?"})
    await rig.wait_for(lambda: bool(rig.out.of("audio_done")))
    await rig.ack_playback()
    assert rig.a.state == "speaking"


def test_barge_in_by_person_stops_and_answers(config: ConfigStore) -> None:
    replies = [start_speaking_handler(None), text_reply("Хорошо, про завтра.")]  # type: ignore[arg-type]

    async def body(rig: Rig) -> None:
        await get_her_talking(rig)
        rig.stt.texts = ["А что будет завтра?"]
        rig.speak(30)
        assert rig.out.of("duck")[-1]["on"] is True  # сразу приглушила голос
        rig.silence()
        await rig.wait_for(lambda: bool(rig.out.of("audio_stop")))
        await rig.wait_for(lambda: len(rig.requests) == 2)
        assert rig.requests[1]["messages"][-1]["content"] == "А что будет завтра?"

    scenario(config, lambda r: replies.pop(0), body)


def test_echo_does_not_interrupt_and_restores_volume(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await get_her_talking(rig)
        rig.stt.texts = ["в алматы плюс двенадцать небольшая облачность"]
        rig.utter()
        await rig.wait_for(lambda: rig.out.of("duck")[-1:] and rig.out.of("duck")[-1]["on"] is False)
        assert not rig.out.of("audio_stop")
        assert rig.a.turn is not None
        assert len(rig.requests) == 1

    scenario(config, start_speaking_handler, body)


def test_repeated_echo_disables_voice_interruption(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await get_her_talking(rig)
        for _ in range(3):
            count = len(rig.out.of("duck"))
            rig.stt.texts = ["плюс двенадцать небольшая облачность"]
            rig.utter()
            await rig.wait_for(lambda: len(rig.out.of("duck")) >= count + 2)
        assert rig.a._barge_off  # noqa: SLF001
        assert "наушники" in rig.out.of("notice")[-1]["text"].lower()
        count = len(rig.out.of("duck"))
        rig.utter()
        await asyncio.sleep(0.02)
        assert len(rig.out.of("duck")) == count
        assert rig.stt.texts == []

    scenario(config, start_speaking_handler, body)


def test_barge_in_disabled_by_setting(config: ConfigStore) -> None:
    config.update(barge_in=False)

    async def body(rig: Rig) -> None:
        await get_her_talking(rig)
        rig.utter()
        await asyncio.sleep(0.02)
        assert not rig.out.of("duck") and rig.stt.audio == []

    scenario(config, start_speaking_handler, body)


def test_stop_word_stops_without_reply(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await get_her_talking(rig)
        rig.stt.texts = ["Стоп"]
        rig.utter()
        await rig.wait_for(lambda: bool(rig.out.of("audio_stop")))
        await rig.wait_for(lambda: rig.a.state == "listening")
        assert len(rig.requests) == 1

    scenario(config, start_speaking_handler, body)


def test_escape_stops(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await get_her_talking(rig)
        await rig.a.on_message({"type": "stop"})
        assert rig.out.of("audio_stop")
        await rig.idle()
        assert rig.a.state == "listening"
        said = [m for m in rig.a.memory.recent_messages() if m["role"] == "assistant"]
        assert "[перебили]" in said[0]["content"]

    scenario(config, start_speaking_handler, body)


def test_goodbye_goes_to_sleep(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a._enter_listening()  # noqa: SLF001
        rig.stt.texts = ["Спасибо, всё."]
        rig.utter()
        await rig.finish_playback()
        assert rig.requests == []
        assert rig.a.mode == "sleep"

    scenario(config, lambda r: text_reply("нет"), body)


def test_push_to_talk(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "ptt", "down": True})
        assert rig.a.state == "hearing"
        rig.stt.texts = ["Привет с пробела"]
        for _ in range(20):
            rig.a.on_mic_frame(FRAME)
        await rig.a.on_message({"type": "ptt", "down": False})
        await rig.finish_playback()
        assert len(rig.stt.audio[0]) == 20 * FRAME_BYTES
        assert rig.requests[0]["messages"][-1]["content"] == "Привет с пробела"
        assert "[молчу]" not in rig.requests[0]["messages"][0]["content"]

    scenario(config, lambda r: text_reply("Привет!"), body)


def test_push_to_talk_too_short_is_ignored(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "ptt", "down": True})
        rig.a.on_mic_frame(FRAME)
        await rig.a.on_message({"type": "ptt", "down": False})
        await asyncio.sleep(0.01)
        assert rig.stt.audio == []
        assert rig.a.state == "listening"

    scenario(config, lambda r: text_reply("нет"), body)


def test_core_tap(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        assert rig.a.mode == "sleep"
        await rig.a.on_message({"type": "activate"})
        assert rig.a.state == "listening"
        await rig.a.on_message({"type": "activate"})
        assert rig.a.state == "sleep"

    scenario(config, lambda r: text_reply("нет"), body)


@pytest.mark.parametrize(
    ("status", "kind", "phrase"),
    [(401, "bad_key", "ключ"), (429, "rate_limit", "лимит"), (503, "network", "связи")],
)
def test_brain_failure_is_spoken_honestly(config: ConfigStore, status: int, kind: str, phrase: str) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Объясни, как работает трансформер"})
        await rig.finish_playback()
        assert phrase in rig.tts.spoken[0].lower().replace("ё", "е")
        assert rig.out.of("brain_down")[-1]["kind"] == kind

    scenario(config, lambda r: httpx.Response(status, json={"error": {}}), body)


def test_no_key_still_answers_simple_commands(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.config.update(groq_api_key="")
        await rig.a.on_message({"type": "text", "text": "Который час?"})
        await rig.finish_playback()
        assert rig.tts.spoken[0].startswith("Сейчас ")
        assert rig.requests == []
        assert not rig.out.of("brain_down")

    scenario(config, lambda r: text_reply("нет"), body)


def test_no_key_asks_for_key_on_hard_question(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.config.update(groq_api_key="")
        await rig.a.on_message({"type": "text", "text": "В чём смысл жизни?"})
        await rig.finish_playback()
        assert "ключ" in rig.tts.spoken[0]
        assert rig.out.of("brain_down")[-1]["kind"] == "no_key"

    scenario(config, lambda r: text_reply("нет"), body)


def test_brain_failure_after_tool_does_not_repeat_action(config: ConfigStore) -> None:
    replies = [
        tool_call_reply("timers", {"action": "set", "label": "таймер", "seconds": 300}),
        httpx.Response(429, json={"error": {}}), httpx.Response(429, json={"error": {}}),
        httpx.Response(429, json={"error": {}}),
    ]

    async def body(rig: Rig) -> None:
        # Шагов несколько, поэтому нужен второй запрос к модели, а она недоступна.
        await rig.a.on_message({"type": "text", "text": "Поставь таймер на пять минут и открой ютуб"})
        await rig.finish_playback()
        assert len(rig.a.memory.active_timers()) == 1
        # Модель не успела пересказать результат, но Джарвис всё равно говорит, что вышло.
        assert "Поставил" in rig.tts.spoken[0]

    scenario(config, lambda r: replies.pop(0), body)


def test_timer_announces_when_idle_and_waits_when_busy(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        rig.a.announce("Напоминаю: чай.")
        await rig.finish_playback()
        assert rig.tts.spoken == ["Напоминаю: чай."]
        rig.tts.spoken.clear()
        rig.out.messages.clear()

        await rig.a.on_message({"type": "text", "text": "Привет"})
        await rig.wait_for(lambda: bool(rig.tts.spoken))
        rig.a.announce("Напоминаю: звонок.")
        assert rig.tts.spoken == ["Привет!"]
        await rig.wait_for(lambda: bool(rig.out.of("audio_done")))
        turn = rig.out.of("audio_done")[-1]["turn"]
        await rig.a.on_message({"type": "playback", "turn": turn, "event": "done"})
        # Напоминание прозвучало сразу после ответа, а не поверх него.
        await rig.wait_for(lambda: "Напоминаю: звонок." in rig.tts.spoken)

    scenario(config, lambda r: text_reply("Привет!"), body)


def test_mic_off_after_text_reply_goes_to_muted(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "mic", "on": False})
        assert rig.a.state == "muted"
        await rig.a.on_message({"type": "text", "text": "Привет"})
        await rig.finish_playback()
        assert rig.a.state == "muted" and rig.a.mode == "sleep"

    scenario(config, lambda r: text_reply("Привет!"), body)


def test_mic_off_ignores_frames(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "mic", "on": False})
        rig.spotter.hit = True
        rig.utter()
        await asyncio.sleep(0.01)
        assert rig.stt.audio == [] and rig.a.state == "muted"

    scenario(config, lambda r: text_reply("нет"), body)


def test_muted_system_is_reported_once(config: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_state() -> tuple[int, bool]:
        calls.append(1)
        return 80, True

    monkeypatch.setattr("jarvis.sysaudio.read_output_state", fake_state)

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "ready", "mic": True})
        await rig.wait_for(lambda: bool(rig.out.of("notice")))
        assert "выключен" in rig.out.of("notice")[0]["text"]
        await rig.a.on_message({"type": "ready", "mic": True})
        await asyncio.sleep(0.02)
        assert len(calls) == 1

    scenario(config, lambda r: text_reply("нет"), body)


def test_greeting_once_after_onboarding(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "ready", "mic": True})
        await rig.finish_playback()
        assert len(rig.tts.spoken) == 1 and "Я Джарвис" in rig.tts.spoken[0]
        assert rig.a.mode == "sleep"
        await rig.a.on_message({"type": "ready", "mic": True})
        await asyncio.sleep(0.01)
        assert len(rig.tts.spoken) == 1

    scenario(config, lambda r: text_reply("нет"), body)


def test_no_way_to_recognize_speech_is_explained(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        async def broken(pcm: bytes) -> Transcript:
            raise SpeechUnavailable("нет модели")

        rig.stt.transcribe = broken  # type: ignore[method-assign]
        rig.a._enter_listening()  # noqa: SLF001
        rig.utter()
        await rig.idle()
        assert "писать текстом" in rig.out.of("notice")[-1]["text"]
        assert not rig.out.of("error")
        assert rig.a.state == "listening"

    scenario(config, lambda r: text_reply("нет"), body)


def test_llm_context_contains_memory_and_user(config: ConfigStore) -> None:
    config.update(user_name="Алихан", city="Алматы")

    async def body(rig: Rig) -> None:
        rig.a.memory.add_fact("Любит зелёный чай")
        await rig.a.on_message({"type": "text", "text": "Привет"})
        await rig.finish_playback()
        messages = rig.requests[0]["messages"]
        context = messages[-2]["content"]
        assert "Алихан" in context and "Алматы" in context and "Любит зелёный чай" in context
        assert "Сейчас:" in context  # время нужно модели прямо перед вопросом, иначе она его «не знает»
        assert rig.requests[0]["tools"]
        # Постоянная часть не меняется от запроса к запросу: на этом держится кэш Groq.
        assert messages[0]["content"] == SYSTEM_PROMPT
        assert "Алихан" not in messages[0]["content"]

    scenario(config, lambda r: text_reply("Привет!"), body)


# ---------- Честность, подтверждения, мгновенные команды ----------


def test_false_claim_without_tool_is_not_spoken(config: ConfigStore) -> None:
    """Настоящий случай из журнала: на «включи песню» модель ответила «Песня включена», ничего не сделав."""
    replies = [text_reply("Песня включена, наслаждайся!"), tool_call_reply("timers", {"action": "list"})]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Можешь включить песню"})
        await rig.finish_playback()
        assert not any("включена" in said for said in rig.tts.spoken)
        assert rig.requests[1]["tool_choice"] == "required"
        # Нужный инструмент мог не попасть в подборку: при повторе доступны все.
        assert len(rig.requests[1]["tools"]) == len(SCHEMAS)
        assert rig.tts.spoken == ["Активных таймеров нет."]

    scenario(config, lambda r: replies.pop(0), body)


def test_honest_refusal_passes_through(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Включи стиральную машину"})
        await rig.finish_playback()
        assert rig.tts.spoken == ["Стиральной машиной я управлять не могу."]
        assert len(rig.requests) == 1

    scenario(config, lambda r: text_reply("Стиральной машиной я управлять не могу."), body)


def test_words_before_tool_call_are_dropped(config: ConfigStore) -> None:
    """Настоящий случай: «Записал.» перед вызовом и «Записал.» после него звучали дважды."""
    first = httpx.Response(200, content=sse(
        text_chunk("Записал."),
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "notes", "arguments": json.dumps({"action": "add", "text": "молоко"}, ensure_ascii=False)},
        }]}}]},
        "[DONE]",
    ))
    replies = [first, text_reply("Записал: молоко.")]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Сколько у меня дел и добавь молоко"})
        await rig.finish_playback()
        assert rig.tts.spoken == ["Записал: молоко."]

    scenario(config, lambda r: replies.pop(0), body)


def test_garbage_tool_arguments_are_ignored(config: ConfigStore) -> None:
    """Настоящий случай: модель прислала {"": {}} для инструмента без параметров."""
    replies = [tool_call_reply("timers", {"action": "list", "": {}})]

    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Какие у меня таймеры"})
        await rig.finish_playback()
        assert rig.tts.spoken == ["Активных таймеров нет."]

    scenario(config, lambda r: replies.pop(0), body)


def test_masculine_answer_is_fixed_before_speech(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Как дела?"})
        await rig.finish_playback()
        said = " ".join(rig.tts.spoken)
        assert "я проверил " in said and "проверила" not in said and "готов " in said
        assert "".join(m["text"] for m in rig.out.of("assistant_delta")) == said

    scenario(config, lambda r: text_reply("Хорошо, я про", "верила почту и готова помогать дальше."), body)


def test_confirmation_runs_only_after_human_yes(config: ConfigStore) -> None:
    done: list[str] = []
    replies = [tool_call_reply("system_control", {"action": "shutdown"})]

    async def body(rig: Rig) -> None:
        async def fake_power(flag: str, message: str) -> str:
            done.append(flag)
            return message

        rig.a.tools._power = fake_power  # type: ignore[method-assign]  # noqa: SLF001
        await rig.a.on_message({"type": "text", "text": "Ну всё, выруби машину"})
        await rig.finish_playback()
        assert rig.tts.spoken == ["Выключить ноутбук?"]
        assert done == []  # одного вопроса мало

        rig.out.messages.clear()
        await rig.a.on_message({"type": "text", "text": "Да, выключай"})
        await rig.finish_playback()
        assert done == ["/s"]
        assert len(rig.requests) == 1  # вопрос и подтверждение обошлись без второго запроса к модели

    scenario(config, lambda r: replies.pop(0), body)


def test_confirmation_declined(config: ConfigStore) -> None:
    done: list[str] = []
    replies = [tool_call_reply("system_control", {"action": "restart"})]

    async def body(rig: Rig) -> None:
        async def fake_power(flag: str, message: str) -> str:
            done.append(flag)
            return message

        rig.a.tools._power = fake_power  # type: ignore[method-assign]  # noqa: SLF001
        await rig.a.on_message({"type": "text", "text": "Давай перезагрузимся"})
        await rig.finish_playback()
        rig.out.messages.clear()
        await rig.a.on_message({"type": "text", "text": "Нет, не надо"})
        await rig.finish_playback()
        assert done == []
        assert "Не буду." in " ".join(rig.tts.spoken)
        assert rig.a.tools.pending is None

    scenario(config, lambda r: replies.pop(0), body)


def test_other_topic_cancels_confirmation(config: ConfigStore) -> None:
    done: list[str] = []
    replies = [tool_call_reply("system_control", {"action": "shutdown"}), text_reply("Сейчас ясно.")]

    async def body(rig: Rig) -> None:
        async def fake_power(flag: str, message: str) -> str:
            done.append(flag)
            return message

        rig.a.tools._power = fake_power  # type: ignore[method-assign]  # noqa: SLF001
        await rig.a.on_message({"type": "text", "text": "Выруби машину"})
        await rig.finish_playback()
        rig.out.messages.clear()
        await rig.a.on_message({"type": "text", "text": "Лучше скажи, ясно ли на улице"})
        await rig.finish_playback()
        rig.out.messages.clear()
        await rig.a.on_message({"type": "text", "text": "Да"})
        await rig.finish_playback()
        assert done == []  # «да» спустя другую реплику уже ничего не подтверждает

    def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0) if replies else text_reply("Хорошо.")

    scenario(config, handler, body)


def test_instant_command_needs_no_brain(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Джарвис, сколько сейчас время?"})
        await rig.finish_playback()
        assert rig.requests == []
        assert rig.tts.spoken[0].startswith("Сейчас ")
        history = rig.a.memory.recent_messages()
        assert history[-1]["role"] == "assistant" and history[-1]["content"].startswith("Сейчас ")

    scenario(config, lambda r: text_reply("нет"), body)


def test_announcement_is_remembered(config: ConfigStore) -> None:
    async def body(rig: Rig) -> None:
        await rig.a.on_message({"type": "text", "text": "Привет"})
        await rig.finish_playback()
        rig.out.messages.clear()
        rig.a.announce("Вы играете уже час.")
        await rig.finish_playback()
        assert rig.a.memory.recent_messages()[-1]["content"] == "Вы играете уже час."

    scenario(config, lambda r: text_reply("Привет!"), body)


def test_internal_error_does_not_kill_the_loop(config: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(rig: Rig) -> None:
        def boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("сбой")

        monkeypatch.setattr(rig.a, "_build_messages", boom)
        await rig.a.on_message({"type": "text", "text": "Привет"})
        await rig.idle()
        assert rig.out.of("error")
        assert rig.a.state == "listening"
        monkeypatch.undo()
        await rig.a.on_message({"type": "text", "text": "Ещё раз"})
        await rig.finish_playback()
        assert rig.tts.spoken == ["Привет!"]

    scenario(config, lambda r: text_reply("Привет!"), body)

"""Маршрутизатор проверяется на записанных форматах ответов, сеть здесь подменена."""
import asyncio
import json
from typing import Any

import httpx
import pytest

from jarvis.config import ConfigStore
from jarvis.llm import Finished, LLMRouter, LLMUnavailable, TextDelta, ToolCallAssembler, ToolCalls, model_extras

from .conftest import mock_client, sse, text_chunk, text_reply

MESSAGES = [{"role": "user", "content": "Привет"}]


async def collect(router: LLMRouter, tools: list[dict[str, Any]] | None = None) -> list[Any]:
    return [event async for event in router.stream(MESSAGES, tools)]


def text_of(events: list[Any]) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_router(config: ConfigStore, handler: Any, groq: str = "gsk_test_key_12345", gemini: str = "") -> LLMRouter:
    config.update(groq_api_key=groq, gemini_api_key=gemini)
    return LLMRouter(config, mock_client(handler))


def test_no_keys_is_reported(config: ConfigStore) -> None:
    router = LLMRouter(config, mock_client(lambda request: httpx.Response(500)))
    with pytest.raises(LLMUnavailable) as exc:
        run(collect(router))
    assert exc.value.kind == "no_key"


def test_text_stream_and_request_shape(config: ConfigStore) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return text_reply("При", "вет")

    events = run(collect(make_router(config, handler)))
    assert text_of(events) == "Привет"
    finished = events[-1]
    assert isinstance(finished, Finished) and finished.provider == "groq"
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer gsk_test_key_12345"
    body = seen["body"]
    assert body["model"] == "openai/gpt-oss-120b" and body["stream"] is True
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False


def test_real_world_chunks_are_handled(config: ConfigStore) -> None:
    # Формат снят с реального сервиса: отдельное поле reasoning и финальный чанк с usage без choices.
    payload = sse(
        {"id": "1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"id": "1", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning": "We", "name": "assistant"}}]},
        {"id": "1", "choices": [{"index": 0, "delta": {"content": "Астана"}}]},
        {"id": "1", "choices": [], "usage": {"prompt_tokens": 115, "completion_tokens": 200, "total_tokens": 315}},
        "[DONE]",
    )
    events = run(collect(make_router(config, lambda r: httpx.Response(200, content=payload))))
    assert text_of(events) == "Астана"
    assert events[-1].usage["total_tokens"] == 315


def test_groq_usage_location(config: ConfigStore) -> None:
    payload = sse(text_chunk("да"), {"choices": [], "x_groq": {"usage": {"total_tokens": 7}}}, "[DONE]")
    events = run(collect(make_router(config, lambda r: httpx.Response(200, content=payload))))
    assert events[-1].usage == {"total_tokens": 7}


def test_garbage_in_stream_is_ignored(config: ConfigStore) -> None:
    payload = sse("не json", [1, 2, 3], {"choices": None}, {"choices": [{}]}, text_chunk("ок"), "[DONE]")
    events = run(collect(make_router(config, lambda r: httpx.Response(200, content=payload))))
    assert text_of(events) == "ок"


def test_tool_calls_assembled_from_fragments(config: ConfigStore) -> None:
    deltas = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                  "function": {"name": "get_weather", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"ci"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ty\":\"Алматы\"}"}}]}}]},
        "[DONE]",
    ]
    events = run(collect(make_router(config, lambda r: httpx.Response(200, content=sse(*deltas)))))
    calls = next(e for e in events if isinstance(e, ToolCalls)).calls
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Алматы"}


def test_gemini_signature_survives_assembly() -> None:
    assembler = ToolCallAssembler()
    assembler.feed([{"id": "x", "function": {"name": "remember", "arguments": "{}"},
                     "extra_content": {"google": {"thought_signature": "abc"}}}])
    assert assembler.result()[0]["extra_content"] == {"google": {"thought_signature": "abc"}}


def test_missing_call_id_is_generated_and_dict_arguments_accepted() -> None:
    assembler = ToolCallAssembler()
    assembler.feed([{"function": {"name": "forget", "arguments": {"text": "кофе"}}}])
    call = assembler.result()[0]
    assert call["id"] == "call_0"
    assert json.loads(call["function"]["arguments"]) == {"text": "кофе"}


def test_rate_limit_moves_to_next_model_and_cools_down(config: ConfigStore) -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "openai/gpt-oss-120b":
            return httpx.Response(429, headers={"retry-after": "30"}, json={"error": {"code": "rate_limit_exceeded"}})
        return text_reply("запасная")

    router = make_router(config, handler)
    events = run(collect(router))
    assert text_of(events) == "запасная"
    assert models == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    # Модель в лимите больше не пробуем, пока он не остынет.
    models.clear()
    run(collect(router))
    assert models == ["qwen/qwen3.8-27b"]


def test_bad_key_falls_back_to_other_provider(config: ConfigStore) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api.groq.com":
            return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})
        return text_reply("Gemini на связи")

    router = make_router(config, handler, gemini="AIza_test_key_12345")
    events = run(collect(router))
    assert text_of(events) == "Gemini на связи"
    # Плохой ключ не перебирается по всем моделям.
    assert hosts.count("api.groq.com") == 1
    assert events[-1].provider == "gemini"


def test_all_rate_limited_is_reported(config: ConfigStore) -> None:
    router = make_router(config, lambda r: httpx.Response(429, json={"error": {}}))
    with pytest.raises(LLMUnavailable) as exc:
        run(collect(router))
    assert exc.value.kind == "rate_limit"
    with pytest.raises(LLMUnavailable) as exc:
        run(collect(router))
    assert exc.value.kind == "rate_limit"


def test_bad_key_is_reported(config: ConfigStore) -> None:
    router = make_router(config, lambda r: httpx.Response(401, json={"error": {}}))
    with pytest.raises(LLMUnavailable) as exc:
        run(collect(router))
    assert exc.value.kind == "bad_key"


def test_unknown_extras_are_retried_without_them(config: ConfigStore) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"code": "invalid_request"}})
        return text_reply("ок")

    events = run(collect(make_router(config, handler)))
    assert text_of(events) == "ок"
    assert len(bodies) == 2 and "reasoning_effort" not in bodies[1] and "include_reasoning" not in bodies[1]


def test_failed_tool_generation_tries_next_model(config: ConfigStore) -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if len(models) == 1:
            return httpx.Response(400, json={"error": {"code": "tool_use_failed", "failed_generation": "..."}})
        return text_reply("ок")

    events = run(collect(make_router(config, handler)))
    assert text_of(events) == "ок"
    assert len(models) == 2 and models[0] != models[1]


def test_decommissioned_model_is_skipped(config: ConfigStore) -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "openai/gpt-oss-120b":
            return httpx.Response(400, json={"error": {"code": "model_decommissioned"}})
        return text_reply("ок")

    router = make_router(config, handler)
    run(collect(router))
    assert models == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    models.clear()
    run(collect(router))
    assert models == ["qwen/qwen3.8-27b"]


def test_network_error_before_first_token_tries_next(config: ConfigStore) -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        models.append(json.loads(request.content)["model"])
        if len(models) == 1:
            raise httpx.ConnectError("нет сети")
        return text_reply("ок")

    assert text_of(run(collect(make_router(config, handler)))) == "ок"
    assert len(models) == 2


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield sse(text_chunk("Половина ответа"))
        raise httpx.ReadError("обрыв")


def test_mid_stream_failure_keeps_what_was_said(config: ConfigStore) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=_BrokenStream())

    events = run(collect(make_router(config, handler)))
    assert text_of(events) == "Половина ответа"
    assert isinstance(events[-1], Finished) and events[-1].truncated
    assert calls == 1  # отвечать заново нельзя: человек уже услышал начало


def test_empty_answer_tries_next_model(config: ConfigStore) -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        models.append(json.loads(request.content)["model"])
        return httpx.Response(200, content=sse("[DONE]")) if len(models) == 1 else text_reply("ок")

    assert text_of(run(collect(make_router(config, handler)))) == "ок"


def test_discovery_uses_only_available_models(config: ConfigStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-20b"}, {"id": "whisper-large-v3"}]})
        return text_reply("ок")

    router = make_router(config, handler)
    run(router.discover())
    assert [model for _, model in router.candidates()] == ["openai/gpt-oss-20b"]


def test_discovery_falls_back_to_any_chat_model(config: ConfigStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "whisper-large-v3"}, {"id": "future/brain-9"}, {"id": "x/llama-guard-9"}]})

    router = make_router(config, handler)
    run(router.discover())
    assert [model for _, model in router.candidates()] == ["future/brain-9"]


@pytest.mark.parametrize(
    ("status", "ok"),
    [(200, True), (401, False), (403, False)],
)
def test_check_key(config: ConfigStore, status: int, ok: bool) -> None:
    router = LLMRouter(config, mock_client(lambda r: httpx.Response(status, json={"data": []})))
    assert run(router.check("groq", "gsk_test_key_12345")).ok is ok


def test_check_network_failure_is_explained(config: ConfigStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    result = run(LLMRouter(config, mock_client(handler)).check("groq", "gsk_test_key_12345"))
    assert not result.ok and "связи" in result.message


def test_check_does_not_touch_bad_new_key_state(config: ConfigStore) -> None:
    config.update(groq_api_key="gsk_saved_key_12345")
    router = LLMRouter(config, mock_client(lambda r: httpx.Response(200, json={"data": [{"id": "only/this"}]})))
    run(router.check("groq", "gsk_other_key_12345"))
    assert "groq" not in router._available  # noqa: SLF001 — чужой ключ не должен менять картину


def test_extras_by_model() -> None:
    assert model_extras("groq", "qwen/qwen3.8-27b") == {"reasoning_effort": "none"}
    assert model_extras("groq", "openai/gpt-oss-20b")["reasoning_effort"] == "low"
    assert model_extras("gemini", "gemini-3.8-flash") == {"reasoning_effort": "low"}
    assert model_extras("groq", "llama-3.3-70b-versatile") == {}

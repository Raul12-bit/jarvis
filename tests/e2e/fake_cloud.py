"""Заглушка языковой модели с OpenAI-совместимым API: для сквозных тестов без ключа.

Настоящая модель здесь не проверяется. Заглушка нужна, чтобы прогнать всё остальное: микрофон,
распознавание, вызовы инструментов, голос и анимацию. Каждый запрос пишется в журнал для проверок.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

LOG = Path(sys.argv[2])
app = FastAPI()
_STORY = (
    "Давным-давно в маленьком городе у подножия гор жила девочка, которая мечтала строить роботов. "
    "Каждый вечер она собирала детали из старых приёмников и записывала идеи в толстую тетрадь. "
    "Соседи посмеивались над ней, но она не обращала на это внимания и продолжала работать. "
    "Однажды её первый робот сделал несколько шагов прямо на городской площади. "
    "С того дня никто больше не смеялся, а дети со всей округи приходили к ней учиться. "
    "Прошли годы, и её мастерская выросла в настоящую лабораторию, куда приезжали инженеры из других стран."
)


def record(entry: dict) -> None:
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.time(), **entry}, ensure_ascii=False) + "\n")


def plan_reply(messages: list[dict]) -> tuple[str, dict | None]:
    last = messages[-1]
    if last["role"] == "tool":
        return f"Посмотрела. {str(last['content']).split('.')[0]}.", None
    text = str(last.get("content", "")).lower()
    if "погод" in text:
        return "", {"name": "get_weather", "arguments": {"city": "Алматы"}}
    if "таймер" in text:
        return "", {"name": "timers", "arguments": {"action": "set", "label": "проверка", "seconds": 6}}
    if "истори" in text:
        return _STORY, None
    return f"Понял, ты сказал: {last.get('content', '')}", None


@app.get("/v1/models")
async def models() -> dict:
    return {"data": [{"id": "test-model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    messages = body["messages"]
    record({"kind": "chat", "last": messages[-1], "has_tools": bool(body.get("tools")), "system_chars": len(messages[0]["content"])})
    reply, call = plan_reply(messages)

    async def stream():
        await asyncio.sleep(0.15)
        if call:
            chunk = {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_e2e", "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)},
            }]}}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        else:
            for word in reply.split(" "):
                chunk = {"choices": [{"delta": {"content": word + " "}}]}
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.012)
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_config=None, access_log=False)

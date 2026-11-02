"""Заглушка сервера Telegram Bot API для сквозной проверки: настоящий бот и настоящие сообщения не нужны.

Управление: POST /control/push кладёт сообщение в очередь, POST /control/press нажимает кнопку,
GET /control/sent показывает, что бот отправил. Файлы и снимки здесь только считаются: их содержимое никуда не сохраняется.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()
updates: list[dict] = []
sent: list[dict] = []
counter = {"update": 100, "message": 1000}
GOOD_TOKEN = "123456:FakeTokenForLocalCheckOnly"


def api_error() -> JSONResponse:
    return JSONResponse({"ok": False, "description": "Unauthorized"}, status_code=401)


@app.post("/control/push")
async def push(request: Request) -> dict:
    body = await request.json()
    counter["update"] += 1
    message = {"message_id": counter["update"], "from": {"id": body["sender"]}, "chat": {"id": body["sender"], "type": body.get("kind", "private")},
               "date": int(time.time() - body.get("age", 0))}
    message.update(body.get("extra", {}))
    if body.get("text"):
        message["text"] = body["text"]
    updates.append({"update_id": counter["update"], "message": message})
    return {"ok": True}


@app.post("/control/press")
async def press(request: Request) -> dict:
    body = await request.json()
    counter["update"] += 1
    message: dict = {"chat": {"id": body["sender"], "type": "private"}, "message_id": body.get("message_id", 1)}
    if body.get("photo"):
        message["photo"] = [{"file_id": "x"}]
    updates.append({"update_id": counter["update"], "callback_query": {
        "id": f"q{counter['update']}", "from": {"id": body["sender"]}, "message": message, "data": body["data"]}})
    return {"ok": True}


@app.get("/control/sent")
async def control_sent() -> list[dict]:
    return sent


@app.get("/bot{token}/getMe")
async def get_me(token: str) -> Response:
    return JSONResponse({"ok": True, "result": {"username": "jarvis_check_bot"}}) if token == GOOD_TOKEN else api_error()


@app.get("/bot{token}/getUpdates")
async def get_updates(token: str, offset: int = 0, timeout: int = 0) -> Response:
    if token != GOOD_TOKEN:
        return api_error()
    # Как у настоящего сервера: всё, что раньше offset, подтверждено и больше не вернётся. Без offset приходит только неподтверждённое.
    updates[:] = [update for update in updates if update["update_id"] >= offset]
    deadline = time.monotonic() + min(timeout, 2)
    while time.monotonic() < deadline:
        if updates:
            return JSONResponse({"ok": True, "result": updates})
        await asyncio.sleep(0.05)
    return JSONResponse({"ok": True, "result": []})


@app.post("/bot{token}/{method}")
async def any_method(token: str, method: str, request: Request) -> Response:
    if token != GOOD_TOKEN:
        return api_error()
    raw = await request.body()
    counter["message"] += 1
    if request.headers.get("content-type", "").startswith("application/json"):
        body = json.loads(raw)
        sent.append({"method": method, "text": body.get("text", ""), "markup": body.get("reply_markup"), "chat": body.get("chat_id"),
                     "message_id": body.get("message_id"), "callback_text": body.get("text") if method == "answerCallbackQuery" else None})
    else:
        sent.append({"method": method, "bytes": len(raw), "has_buttons": b"inline_keyboard" in raw})
    return JSONResponse({"ok": True, "result": {"message_id": counter["message"]}})


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_config=None, access_log=False)

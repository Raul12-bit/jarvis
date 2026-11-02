"""Локальный сервер интерфейса.

Он видит микрофон и умеет запускать программы, поэтому защищён от чужих страниц: любой сайт в браузере
может постучаться на localhost. Защита: привязка к 127.0.0.1, проверка Host (DNS rebinding),
проверка Origin и случайный токен, который знает только окно Джарвиса.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import struct
import webbrowser
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .assistant import Assistant
from .config import VOICES, WEB_DIR, ConfigStore, SettingsPatch, mask_key
from .llm import PROVIDERS

log = logging.getLogger("jarvis.server")

_KEY_PAGES = {
    "groq": "https://console.groq.com/keys",
    "gemini": "https://aistudio.google.com/apikey",
}
_DISCONNECT_GRACE = 6.0


class Outbound:
    """Очередь исходящих сообщений: у сокета должен быть один писатель, а порядок важен."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, Any]] | None = None

    def attach(self) -> asyncio.Queue[tuple[str, Any]]:
        self._queue = asyncio.Queue()
        return self._queue

    def detach(self, queue: asyncio.Queue[tuple[str, Any]]) -> None:
        if self._queue is queue:
            self._queue = None

    def send_json(self, message: dict[str, Any]) -> None:
        if self._queue is not None:
            self._queue.put_nowait(("text", json.dumps(message, ensure_ascii=False)))

    def send_audio(self, header: dict[str, Any], data: bytes) -> None:
        if self._queue is not None:
            raw = json.dumps(header, ensure_ascii=False).encode("utf-8")
            self._queue.put_nowait(("bytes", struct.pack("<I", len(raw)) + raw + data))


class KeyBody(BaseModel):
    key: str = Field(min_length=8, max_length=300, pattern=r"^[\x21-\x7e]+$")


class PreviewBody(BaseModel):
    voice: str
    rate: int = Field(default=0, ge=-30, le=30)


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=400)


class FocusBody(BaseModel):
    minutes: int = Field(ge=0, le=240)


class PhoneBody(BaseModel):
    token: str = Field(min_length=20, max_length=200, pattern=r"^\d+:[\w-]+$")


class PinBody(BaseModel):
    pin: str = Field(max_length=32, pattern=r"^\d{0,32}$")


def create_app(
    config: ConfigStore,
    token: str,
    port: int,
    on_all_clients_gone: Callable[[], None] | None = None,
    build: str = "",
) -> FastAPI:
    out = Outbound()
    assistant = Assistant(config, out)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await assistant.start()
        try:
            yield
        finally:
            await assistant.close()

    app = FastAPI(title="Jarvis", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.assistant = assistant
    assistant.request_exit = on_all_clients_gone
    active: dict[str, WebSocket | None] = {"ws": None}
    gone_task: dict[str, asyncio.Task | None] = {"task": None}

    def check_token(given: str | None) -> bool:
        return bool(given) and secrets.compare_digest(str(given), token)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Недопустимый Host"}, status_code=421)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "Недопустимый Origin"}, status_code=403)
            if not check_token(request.headers.get("x-jarvis-token")):
                return JSONResponse({"detail": "Нет доступа"}, status_code=401)
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/static/"):
            # Страницу и код интерфейса браузер обязан перепроверять: иначе после обновления он показывал бы старую версию.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"app": "jarvis", "version": __version__, "build": build}

    @app.get("/")
    async def index(t: str | None = None) -> Response:
        if not check_token(t):
            return JSONResponse({"detail": "Откройте Джарвиса через его ярлык"}, status_code=401)
        response = FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})
        connect = " ".join(f"ws://{host}" for host in sorted(allowed_hosts))
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self' {connect}; media-src 'self' blob:; worker-src 'self'; font-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return config.public()

    @app.post("/api/settings")
    async def post_settings(patch: SettingsPatch) -> dict[str, Any]:
        try:
            config.update(**patch.model_dump(exclude_none=True))
        except ValidationError as exc:
            raise HTTPException(422, "Недопустимое значение") from exc
        assistant.on_settings_changed(patch)
        return config.public()

    @app.post("/api/keys/{provider}")
    async def save_key(provider: str, body: KeyBody) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise HTTPException(404, "Неизвестный сервис")
        key = body.key.strip()
        result = await assistant.llm.check(provider, key)
        if result.ok:
            config.update(**{f"{provider}_api_key": key})
            assistant.llm.reset_provider(provider)
            await assistant.llm.check(provider)
            assistant.send_hello()
        return {"ok": result.ok, "message": result.message, "ms": round(result.seconds * 1000), "hint": mask_key(key) if result.ok else ""}

    @app.delete("/api/keys/{provider}")
    async def delete_key(provider: str) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise HTTPException(404, "Неизвестный сервис")
        config.update(**{f"{provider}_api_key": ""})
        assistant.llm.reset_provider(provider)
        assistant.send_hello()
        return config.public()

    @app.post("/api/open/{target}")
    async def open_external(target: str) -> dict[str, bool]:
        # Только заранее известные адреса: интерфейс не может открыть произвольную ссылку.
        url = _KEY_PAGES.get(target)
        if url is None:
            raise HTTPException(404, "Неизвестная ссылка")
        return {"ok": await asyncio.to_thread(webbrowser.open, url)}

    @app.get("/api/memory")
    async def get_memory() -> dict[str, Any]:
        return {"facts": assistant.memory.facts()}

    @app.delete("/api/memory/{fact_id}")
    async def delete_memory(fact_id: int) -> dict[str, Any]:
        assistant.memory.delete_fact(fact_id)
        return {"facts": assistant.memory.facts()}

    @app.post("/api/history/clear")
    async def clear_history() -> dict[str, bool]:
        assistant.memory.clear_history()
        return {"ok": True}

    @app.delete("/api/timers/{timer_id}")
    async def cancel_timer(timer_id: int) -> dict[str, bool]:
        assistant.memory.finish_timer(timer_id)
        assistant.notify_timers_changed()
        return {"ok": True}

    @app.get("/api/activity")
    async def get_activity() -> dict[str, Any]:
        return assistant.activity.report()

    @app.post("/api/notes")
    async def add_note(body: NoteBody) -> dict[str, Any]:
        assistant.memory.add_note(body.text)
        return {"items": assistant.memory.notes()}

    @app.post("/api/notes/{note_id}/done")
    async def note_done(note_id: int) -> dict[str, Any]:
        assistant.memory.set_note_done(note_id)
        return {"items": assistant.memory.notes()}

    @app.delete("/api/notes/{note_id}")
    async def delete_note(note_id: int) -> dict[str, Any]:
        assistant.memory.delete_note(note_id)
        return {"items": assistant.memory.notes()}

    @app.post("/api/focus")
    async def focus(body: FocusBody) -> dict[str, Any]:
        text = assistant.activity.start_focus(body.minutes) if body.minutes else assistant.activity.stop_focus()
        return {"text": text, "activity": assistant.activity.report()}

    @app.post("/api/phone/token")
    async def phone_token(body: PhoneBody) -> dict[str, Any]:
        ok, detail = await assistant.phone.set_token(body.token.strip())
        return {"ok": ok, "message": detail, "phone": assistant.phone.status()}

    @app.post("/api/phone/pin")
    async def phone_pin(body: PinBody) -> dict[str, Any]:
        # Только отсюда, с самого ноутбука: кто получит аккаунт Telegram, снять ПИН не сможет.
        config.update(phone_pin=body.pin)
        assistant.phone.lock()
        assistant.send_hello()
        return config.public()

    @app.delete("/api/phone")
    async def phone_forget() -> dict[str, Any]:
        assistant.phone.forget()
        return assistant.phone.status()

    @app.post("/api/shutdown")
    async def shutdown() -> dict[str, bool]:
        # С подключённым телефоном закрытие окна не выключает Джарвис, поэтому нужна отдельная кнопка.
        if on_all_clients_gone is not None:
            asyncio.get_running_loop().call_later(0.4, on_all_clients_gone)
        return {"ok": True}

    @app.get("/api/presence")
    async def presence() -> dict[str, Any]:
        return {"connected": assistant.connected, "mic": assistant.mic_on, "state": assistant.state, "visible": assistant.visible,
                "mascot": assistant.mascot is not None, "phone": assistant.phone.active}

    @app.post("/api/window/{action}")
    async def window(action: str) -> dict[str, Any]:
        # Второй запуск ярлыка попадает сюда: окно у работающего Джарвиса уже есть, только скрытое.
        if action not in ("show", "hide"):
            raise HTTPException(404, "Неизвестное действие")
        result = await assistant._self_window(action)  # noqa: SLF001
        return {"ok": not result.startswith("Ошибка"), "message": result}

    @app.post("/api/voice/preview")
    async def preview_voice(body: PreviewBody) -> dict[str, bool]:
        if body.voice not in {voice["id"] for voice in VOICES}:
            raise HTTPException(422, "Неизвестный голос")
        assistant.preview_voice(body.voice, body.rate)
        return {"ok": True}

    @app.post("/api/selfcheck")
    async def self_check() -> dict[str, Any]:
        return {"results": await assistant.self_check()}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        if (
            ws.headers.get("host") not in allowed_hosts
            or ws.headers.get("origin") not in allowed_origins
            or not check_token(ws.query_params.get("t"))
        ):
            await ws.close(code=4401)
            return
        await ws.accept()
        previous = active["ws"]
        if previous is not None:
            # Новое окно вытесняет старое: двух слушателей микрофона быть не должно.
            try:
                await previous.close(code=4000)
            except RuntimeError:
                pass
        active["ws"] = ws
        if gone_task["task"] is not None:
            gone_task["task"].cancel()
            gone_task["task"] = None

        queue = out.attach()
        writer = asyncio.create_task(_writer(ws, queue))
        assistant.on_connected()
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    assistant.on_mic_frame(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    await assistant.on_message(payload)
        except WebSocketDisconnect:
            pass
        finally:
            writer.cancel()
            out.detach(queue)
            if active["ws"] is ws:
                active["ws"] = None
                assistant.on_disconnected()
                # Есть талисман или телефон: Джарвис работает и с закрытым окном, а страница открывается заново, но скрытой.
                if assistant.keeps_running():
                    assistant.on_window_gone()
                elif on_all_clients_gone is not None and not assistant.phone.active:
                    gone_task["task"] = asyncio.create_task(_exit_later(on_all_clients_gone))

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


async def _writer(ws: WebSocket, queue: asyncio.Queue[tuple[str, Any]]) -> None:
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "text":
                await ws.send_text(payload)
            else:
                await ws.send_bytes(payload)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _exit_later(callback: Callable[[], None]) -> None:
    await asyncio.sleep(_DISCONNECT_GRACE)
    log.info("Окно закрыто, завершаю работу")
    callback()

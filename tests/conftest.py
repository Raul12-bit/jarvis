from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.config import ConfigStore  # noqa: E402
from jarvis.persona import Address  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Тесты не должны видеть настоящие ключи и настоящий профиль пользователя.
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "JARVIS_TEST_BASE", "JARVIS_TEST_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def plain_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    # «Сэр» добавляется к двум ответам из трёх. В остальных тестах важен сам ответ, а обращение проверяется отдельно.
    monkeypatch.setattr(Address, "every", 1)


@pytest.fixture
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "home")


def sse(*chunks: Any) -> bytes:
    """Тело потокового ответа в формате OpenAI, как его присылают Groq и Gemini."""
    lines = []
    for chunk in chunks:
        lines.append(f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk, ensure_ascii=False)}\n\n")
    return "".join(lines).encode("utf-8")


def text_chunk(text: str) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]}


def text_reply(*parts: str) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=sse(*[text_chunk(p) for p in parts], "[DONE]"),
    )


Handler = Callable[[httpx.Request], httpx.Response]


def mock_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))

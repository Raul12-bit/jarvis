"""Безопасность локального сервера: чужие страницы не должны получить доступ к Джарвису."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis import server
from jarvis.config import ConfigStore, DEFAULT_VOICE
from jarvis.server import create_app

TOKEN = "test-token"
PORT = 47999
BASE = f"http://127.0.0.1:{PORT}"
OK = {"host": f"127.0.0.1:{PORT}", "x-jarvis-token": TOKEN}


@pytest.fixture
def client(tmp_path: Path):
    config = ConfigStore(tmp_path / "home")
    config.update(groq_api_key="gsk_secret_value_123456", wake_word=False)
    app = create_app(config, TOKEN, PORT)
    with TestClient(app, base_url=BASE) as test_client:
        yield test_client


def test_wrong_host_blocked_dns_rebinding(client: TestClient) -> None:
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 421
    assert client.get("/health", headers={"host": f"127.0.0.1:{PORT}"}).status_code == 200


def test_index_needs_token_and_sets_csp(client: TestClient) -> None:
    assert client.get("/", headers={"host": OK["host"]}).status_code == 401
    assert client.get("/?t=wrong", headers={"host": OK["host"]}).status_code == 401
    resp = client.get(f"/?t={TOKEN}", headers={"host": OK["host"]})
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings"),
        ("POST", "/api/selfcheck"),
        ("GET", "/api/memory"),
        ("POST", "/api/history/clear"),
        ("POST", "/api/open/groq"),
    ],
)
def test_api_needs_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, headers={"host": OK["host"]}).status_code == 401
    assert client.request(method, path, headers={"host": OK["host"], "x-jarvis-token": "wrong"}).status_code == 401


def test_api_rejects_foreign_origin_even_with_token(client: TestClient) -> None:
    resp = client.get("/api/settings", headers={**OK, "origin": "https://evil.example"})
    assert resp.status_code == 403
    assert client.get("/api/settings", headers={**OK, "origin": BASE}).status_code == 200


def test_settings_never_expose_keys(client: TestClient) -> None:
    resp = client.get("/api/settings", headers=OK)
    assert resp.status_code == 200
    assert "gsk_secret_value_123456" not in resp.text
    data = resp.json()
    assert data["groq_key_set"] is True
    assert data["groq_key_hint"] == "gsk_…3456"
    assert "groq_api_key" not in data


def test_settings_cannot_set_keys_or_unknown_fields(client: TestClient) -> None:
    assert client.post("/api/settings", json={"groq_api_key": "x"}, headers=OK).status_code == 422
    assert client.post("/api/settings", json={"evil": 1}, headers=OK).status_code == 422


@pytest.mark.parametrize(
    "body",
    [{"speech_rate": 100}, {"followup_seconds": 1}, {"user_name": "я" * 100}, {"voice": "bad"}],
)
def test_settings_validate_values(client: TestClient, body: dict) -> None:
    assert client.post("/api/settings", json=body, headers=OK).status_code == 422


def test_settings_update(client: TestClient) -> None:
    resp = client.post("/api/settings", json={"user_name": "  Алихан ", "city": "Алматы"}, headers=OK)
    assert resp.status_code == 200
    assert resp.json()["user_name"] == "Алихан"


def test_open_only_known_pages(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("jarvis.server.webbrowser.open", lambda url: opened.append(url) or True)
    assert client.post("/api/open/groq", headers=OK).status_code == 200
    assert client.post("/api/open/https%3A%2F%2Fevil.example", headers=OK).status_code == 404
    assert opened == ["https://console.groq.com/keys"]


@pytest.mark.parametrize("key", ["короткий", "has space in key 12345", "кириллица-в-ключе-12345", "x" * 400])
def test_keys_validated(client: TestClient, key: str) -> None:
    assert client.post("/api/keys/groq", json={"key": key}, headers=OK).status_code == 422


def test_unknown_provider(client: TestClient) -> None:
    assert client.post("/api/keys/evil", json={"key": "abcdefgh12345"}, headers=OK).status_code == 404
    assert client.delete("/api/keys/evil", headers=OK).status_code == 404


def test_memory_endpoints(client: TestClient) -> None:
    client.app.state.assistant.memory.add_fact("Любит чай")
    facts = client.get("/api/memory", headers=OK).json()["facts"]
    assert facts[0]["text"] == "Любит чай"
    assert client.delete(f"/api/memory/{facts[0]['id']}", headers=OK).json()["facts"] == []
    assert client.delete("/api/memory/not-a-number", headers=OK).status_code == 422


def test_timer_cancel_endpoint(client: TestClient) -> None:
    memory = client.app.state.assistant.memory
    timer_id = memory.add_timer("чай", 1e10)
    assert client.delete(f"/api/timers/{timer_id}", headers=OK).status_code == 200
    assert memory.active_timers() == []


def test_preview_rejects_unknown_voice(client: TestClient) -> None:
    assert client.post("/api/voice/preview", json={"voice": "evil", "rate": 0}, headers=OK).status_code == 422


@pytest.mark.parametrize(
    ("query", "headers"),
    [
        ("", {"origin": BASE}),
        ("?t=wrong", {"origin": BASE}),
        (f"?t={TOKEN}", {}),
        (f"?t={TOKEN}", {"origin": "https://evil.example"}),
        (f"?t={TOKEN}", {"origin": "null"}),
    ],
)
def test_websocket_rejected(client: TestClient, query: str, headers: dict) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws{query}", headers={"host": OK["host"], **headers}):
            pass


def test_websocket_rejects_wrong_host(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?t={TOKEN}", headers={"host": "evil.example", "origin": BASE}):
            pass


def test_phone_routes_need_token(client: TestClient) -> None:
    for method, path in (("POST", "/api/phone/token"), ("DELETE", "/api/phone"), ("POST", "/api/shutdown")):
        assert client.request(method, path, headers={"host": OK["host"]}).status_code == 401


@pytest.mark.parametrize("token", ["коротко", "не токен совсем длинный текст", "abc:defghijklmnopqrstuvwxyz", "123:<script>alert(1)</script>"])
def test_phone_token_is_validated(client: TestClient, token: str) -> None:
    assert client.post("/api/phone/token", headers=OK, json={"token": token}).status_code == 422


def test_phone_token_checked_and_never_returned(client: TestClient) -> None:
    secret = "123456:AAbbCCddEEffGGhhIIjjKKllMMnnOOpp"
    assistant = client.app.state.assistant  # type: ignore[attr-defined]

    async def good(token: str) -> tuple[bool, str]:
        return True, "jarvis_bot"

    assistant.phone.check_token = good
    resp = client.post("/api/phone/token", headers=OK, json={"token": secret})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert resp.json()["phone"]["configured"] is True and resp.json()["phone"]["code"]
    assert secret not in resp.text
    assert secret not in client.get("/api/settings", headers=OK).text

    resp = client.delete("/api/phone", headers=OK)
    assert resp.json()["configured"] is False
    assert assistant.config.settings.telegram_bot_token == ""


def test_keep_awake_setting(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    resp = client.post("/api/settings", headers=OK, json={"keep_awake": True})
    assert resp.status_code == 200 and resp.json()["keep_awake"] is True


def test_closing_window_stops_jarvis_only_without_phone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_DISCONNECT_GRACE", 0.01)
    gone: list[int] = []
    config = ConfigStore(tmp_path / "home")
    config.update(wake_word=False)
    app = create_app(config, TOKEN, PORT, on_all_clients_gone=lambda: gone.append(1))
    headers = {"host": OK["host"], "origin": BASE}
    with TestClient(app, base_url=BASE) as test_client:
        config.update(telegram_bot_token="123456:AAbbCCddEEffGGhhIIjjKKllMMnnOOpp")
        with test_client.websocket_connect(f"/ws?t={TOKEN}", headers=headers):
            pass
        time.sleep(0.1)
        assert gone == []  # телефон подключён: Джарвис работает и без окна

        config.update(telegram_bot_token="")
        with test_client.websocket_connect(f"/ws?t={TOKEN}", headers=headers):
            pass
        for _ in range(100):
            if gone:
                break
            time.sleep(0.02)
        assert gone == [1]


def test_shutdown_button_stops_jarvis_with_phone(tmp_path: Path) -> None:
    gone: list[int] = []
    config = ConfigStore(tmp_path / "home")
    config.update(wake_word=False, telegram_bot_token="123456:AAbbCCddEEffGGhhIIjjKKllMMnnOOpp")
    app = create_app(config, TOKEN, PORT, on_all_clients_gone=lambda: gone.append(1))
    with TestClient(app, base_url=BASE) as test_client:
        assert test_client.post("/api/shutdown", headers=OK).status_code == 200
        for _ in range(100):
            if gone:
                break
            time.sleep(0.02)
    assert gone == [1]


def test_presence_and_window_routes_need_token(client: TestClient) -> None:
    for method, path in (("GET", "/api/presence"), ("POST", "/api/window/show"), ("POST", "/api/window/hide")):
        assert client.request(method, path, headers={"host": OK["host"]}).status_code == 401


def test_presence_report(client: TestClient) -> None:
    body = client.get("/api/presence", headers=OK).json()
    assert body["connected"] is False and body["mascot"] is False and "state" in body


def test_window_route_rejects_unknown_action(client: TestClient) -> None:
    assert client.post("/api/window/explode", headers=OK).status_code == 404


def test_new_settings_are_patchable(client: TestClient) -> None:
    resp = client.post("/api/settings", headers=OK, json={"mascot_size": "large", "phone_shell": True, "allow_camera": True, "proactive": False})
    assert resp.status_code == 200 and resp.json()["mascot_size"] == "large"
    assert client.post("/api/settings", headers=OK, json={"mascot_size": "огромный"}).status_code == 422


def test_websocket_accepted_and_sends_hello(client: TestClient) -> None:
    with client.websocket_connect(f"/ws?t={TOKEN}", headers={"host": OK["host"], "origin": BASE}) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert "gsk_secret_value_123456" not in str(hello)
        assert hello["settings"]["groq_key_set"] is True
        # Мусор не должен ронять соединение.
        ws.send_text("не json")
        ws.send_text("[1, 2, 3]")
        ws.send_bytes(b"\x00" * 7)
        ws.send_json({"type": "неизвестно"})
        ws.send_json({"type": "playback", "turn": 999, "event": "done"})


def test_config_broken_file_is_preserved(tmp_path: Path) -> None:
    home = tmp_path / "broken"
    home.mkdir()
    (home / "config.json").write_text("{битый json", encoding="utf-8")
    config = ConfigStore(home)
    assert config.settings.voice == DEFAULT_VOICE
    assert (home / "config.broken.json").exists()


def test_config_env_key_wins_and_is_not_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_from_environment_1")
    config = ConfigStore(tmp_path / "env")
    assert config.key("groq") == "gsk_from_environment_1"
    config.update(user_name="Тест")
    assert "gsk_from_environment_1" not in (tmp_path / "env" / "config.json").read_text(encoding="utf-8")

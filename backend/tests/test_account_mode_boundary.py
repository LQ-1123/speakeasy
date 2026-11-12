"""Account mode closes unused global history without hiding app updates."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_old_global_history_and_classic_socket_are_closed(monkeypatch):
    monkeypatch.setenv("SE_EMAIL_CODE_AUTH", "1")
    from app.main import create_app
    app = create_app()
    client = TestClient(app)  # No lifespan/model initialization needed for the boundary.
    for path in ("/api/conversations", "/api/conversations/old", "/api/uploads/old.png", "/api/echo", "/api/suggest"):
        assert client.get(path).status_code == 410
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    config = client.get("/api/v3/auth/config")
    assert config.status_code == 200 and config.json()["email_code_auth"] is True

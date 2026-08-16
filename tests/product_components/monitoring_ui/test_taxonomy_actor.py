from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.product_components.monitoring_ui.backend.admin_auth import AdminSessionStore, require_admin_session, require_csrf
from src.product_components.monitoring_ui.backend.app import _admin_auth_available, create_app
from src.product_components.monitoring_ui.backend.settings import MonitoringUiSettings


def _request(headers: list[tuple[bytes, bytes]] | None = None, cookies: str = "") -> Request:
    headers = headers or []
    if cookies:
        headers.append((b"cookie", cookies.encode()))
    return Request({"type": "http", "headers": headers})


def test_taxonomy_mutation_is_disabled_by_default():
    settings = SimpleNamespace(taxonomy_decisions_enabled=False, admin_password="")

    with pytest.raises(HTTPException) as caught:
        _admin_auth_available(settings=settings)

    assert caught.value.status_code == 503
    assert caught.value.detail == "taxonomy_decisions_disabled"


def test_admin_session_uses_fixed_actor_and_csrf():
    store = AdminSessionStore(password="correct", session_ttl_seconds=60, login_window_seconds=60, login_max_attempts=2)
    assert store.login(username="other", password="correct", source="test") is None
    session = store.login(username="admin", password="correct", source="test")
    assert session is not None
    assert require_admin_session(request=_request(cookies=f"trader_admin_session={session.session_id}"), store=store) == session
    with pytest.raises(HTTPException) as caught:
        require_csrf(request=_request([(b"origin", b"https://evil.test")]), session=session, allowed_origin="https://ui.test")
    assert caught.value.status_code == 403
    require_csrf(request=_request([(b"origin", b"https://ui.test"), (b"x-csrf-token", session.csrf_token.encode())]), session=session, allowed_origin="https://ui.test")
    store.logout(session.session_id)
    assert store.get(session.session_id) is None


def test_admin_login_is_rate_limited_without_disclosure():
    store = AdminSessionStore(password="correct", session_ttl_seconds=60, login_window_seconds=60, login_max_attempts=2)
    assert store.login(username="admin", password="wrong", source="test") is None
    assert store.login(username="nobody", password="correct", source="test") is None
    assert store.login(username="admin", password="correct", source="test") is None


def test_enabled_auth_requires_password_and_rejects_legacy_header(monkeypatch):
    monkeypatch.setenv("UI_TAXONOMY_DECISIONS_ENABLED", "true")
    monkeypatch.delenv("UI_ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="UI_ADMIN_PASSWORD"):
        MonitoringUiSettings.from_env()
    monkeypatch.setenv("UI_ADMIN_PASSWORD", "configured")
    monkeypatch.setenv("UI_TAXONOMY_TRUSTED_ACTOR_HEADER", "X-User")
    with pytest.raises(ValueError, match="incompatible"):
        MonitoringUiSettings.from_env()


def test_login_endpoint_sets_http_only_strict_session_cookie(monkeypatch):
    monkeypatch.setenv("UI_TAXONOMY_DECISIONS_ENABLED", "true")
    monkeypatch.setenv("UI_ADMIN_PASSWORD", "configured")
    monkeypatch.setenv("UI_HOST", "127.0.0.1")
    monkeypatch.setenv("UI_API_BASE_URL", "http://localhost:8080/api")
    monkeypatch.delenv("UI_TAXONOMY_TRUSTED_ACTOR_HEADER", raising=False)
    client = TestClient(create_app(settings=MonitoringUiSettings.from_env()))
    denied = client.post("/api/admin/login", json={"username": "admin", "password": "wrong"})
    assert denied.status_code == 401
    assert denied.json()["detail"] == "invalid_credentials"
    response = client.post("/api/admin/login", json={"username": "admin", "password": "configured"})
    assert response.status_code == 200
    assert response.json()["actor"] == "admin"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert client.get("/api/admin/session").json()["authenticated"] is True

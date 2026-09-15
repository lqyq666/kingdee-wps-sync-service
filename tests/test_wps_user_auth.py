from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import ConfigurationError, validate_runtime_configuration
from app.integrations import (
    WpsOpenApiClient,
    wps_authorize_url,
    wps_exchange_code,
    wps_refresh_user_token,
)


def _user_mode_settings(settings):
    return replace(
        settings,
        wps_mode="real",
        wps_provider="wps365",
        wps_token_mode="user",
        wps_base_url="https://openapi.wps.cn",
        wps_token_url="https://openapi.wps.cn/oauth2/token",
        wps_app_id="AK-test",
        wps_app_secret="test-secret",
        wps_user_access_token="user-token-1",
        wps_file_id="file",
        wps_sheet_id="3",
        wps_redirect_uri="http://localhost:8931/callback",
        wps_auth_scopes="kso.dbsheet.readwrite,kso.file_link.readwrite",
    )


def test_user_mode_uses_configured_token_without_client_credentials(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path != "/oauth2/token", "user mode must not fetch an app token"
        assert request.headers["authorization"] == "Bearer user-token-1"
        return httpx.Response(200, json={"code": 0, "data": {"records": [{"id": "rec-1"}]}})

    client = WpsOpenApiClient(
        _user_mode_settings(settings),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client._token() == "user-token-1"
    assert client.create_records([{"_sync_key": "order:1"}]) == {"order:1": "rec-1"}


def test_user_mode_without_token_fails_fast(settings):
    client = WpsOpenApiClient(
        replace(_user_mode_settings(settings), wps_user_access_token=""),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        ),
    )
    with pytest.raises(ValueError, match="WPS_USER_ACCESS_TOKEN"):
        client._token()


def test_wps_authorize_url_uses_documented_oauth2_auth_contract(settings):
    url = wps_authorize_url(_user_mode_settings(settings), "st-1")
    assert url.startswith("https://openapi.wps.cn/oauth2/auth?")
    query = dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))
    assert query["client_id"] == "AK-test"
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == "http%3A%2F%2Flocalhost%3A8931%2Fcallback"
    assert query["scope"] == "kso.dbsheet.readwrite%2Ckso.file_link.readwrite"
    assert query["state"] == "st-1"


def test_wps_exchange_code_posts_documented_form_fields(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/token"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["authorization_code"],
            "client_id": ["AK-test"],
            "client_secret": ["test-secret"],
            "code": ["c-1"],
            "redirect_uri": ["http://localhost:8931/callback"],
        }
        return httpx.Response(200, json={
            "access_token": "at-1", "expires_in": 7200,
            "refresh_token": "rt-1", "refresh_expires_in": 31536000, "token_type": "bearer",
        })

    tokens = wps_exchange_code(_user_mode_settings(settings), "c-1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tokens == {"access_token": "at-1", "refresh_token": "rt-1"}


def test_wps_refresh_posts_documented_form_fields(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "refresh_token": ["rt-1"],
            "client_id": ["AK-test"],
            "client_secret": ["test-secret"],
        }
        return httpx.Response(200, json={"access_token": "at-2", "expires_in": 7200, "refresh_token": "rt-2"})

    tokens = wps_refresh_user_token(
        replace(_user_mode_settings(settings), wps_user_refresh_token="rt-1"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert tokens == {"access_token": "at-2", "refresh_token": "rt-2"}


def test_wps_token_endpoint_errors_surface_code(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40011, "msg": "invalid code"})

    with pytest.raises(ValueError, match="40011"):
        wps_exchange_code(_user_mode_settings(settings), "bad", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_wps_resolve_link_uses_documented_links_meta(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path.decode().split("?")[0] == "/v7/links/caTNZ/meta"
        assert request.headers["authorization"] == "Bearer user-token-1"
        return httpx.Response(200, json={"code": 0, "data": {"file_id": "F123", "file_name": "汇报"}})

    client = WpsOpenApiClient(
        _user_mode_settings(settings),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.resolve_link("caTNZ") == {"file_id": "F123", "file_name": "汇报"}


def test_validate_user_mode_requires_user_token(settings):
    configured = _user_mode_settings(settings)
    validate_runtime_configuration(configured)

    with pytest.raises(ConfigurationError, match="WPS_USER_ACCESS_TOKEN"):
        validate_runtime_configuration(replace(configured, wps_user_access_token=""))
    with pytest.raises(ConfigurationError, match="WPS_TOKEN_MODE"):
        validate_runtime_configuration(replace(configured, wps_token_mode="both"))


def test_smoke_wps_auth_with_manual_code_writes_env(settings, monkeypatch, capsys, tmp_path):
    from app.smoke import main

    env = tmp_path / ".env"
    env.write_text("WPS_TOKEN_MODE=app\n", encoding="utf-8")
    monkeypatch.setenv("WPS_PROVIDER", "wps365")
    monkeypatch.setenv("WPS_TOKEN_MODE", "app")
    monkeypatch.setenv("WPS_APP_ID", "AK-test")
    monkeypatch.setenv("WPS_APP_SECRET", "test-secret")
    monkeypatch.setenv("WPS_USER_REFRESH_TOKEN", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.content.decode())["grant_type"] == ["authorization_code"]
        return httpx.Response(200, json={"access_token": "at-9", "refresh_token": "rt-9", "expires_in": 7200})

    original = httpx.Client
    monkeypatch.setattr("app.integrations.httpx.Client", lambda *a, **k: original(transport=httpx.MockTransport(handler)))

    assert main(["wps-auth", "--code", "c-9", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert '"status": "AUTH_OK"' in out and "at-9" not in out
    text = env.read_text(encoding="utf-8")
    assert "WPS_TOKEN_MODE=user\n" in text and "WPS_USER_ACCESS_TOKEN=at-9\n" in text and "WPS_USER_REFRESH_TOKEN=rt-9\n" in text

from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx

from app.integrations import WpsOpenApiClient, wps_kso1_authorization


def test_wps_kso1_authorization_matches_a_fixed_canonicalization_vector():
    assert wps_kso1_authorization(
        app_id="AK-test",
        app_secret="test-secret",
        method="POST",
        request_uri="/v7/coop/dbsheet/file/sheets/3/records/create",
        content_type="application/json",
        kso_date="Mon, 15 Sep 2026 00:00:00 GMT",
        request_body=b'{"x":1}',
    ) == "KSO-1 AK-test:bda613565b824cb8472ab46de79a56806df32fcf92a42c5dc027d2505877d1a4"


def test_wps_self_built_app_client_uses_documented_token_and_create_contract(settings):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth2/token":
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["client_credentials"],
                "client_id": ["AK-test"],
                "client_secret": ["test-secret"],
            }
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 7200})

        assert request.url.raw_path.decode() == "/v7/coop/dbsheet/file%20one/sheets/3/records/create"
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer token-1"
        assert request.headers["x-kso-date"]
        assert request.headers["x-kso-authorization"].startswith("KSO-1 AK-test:")
        payload = json.loads(request.content)
        assert payload["prefer_id"] is False
        assert json.loads(payload["records"][0]["fields_value"])["_sync_key"] == "order:1"
        return httpx.Response(200, json={"code": 0, "data": {"records": [{"id": "rec-1"}]}})

    client = WpsOpenApiClient(
        replace(
            settings,
            wps_mode="real",
            wps_base_url="https://openapi.wps.cn",
            wps_token_url="https://openapi.wps.cn/oauth2/token",
            wps_app_id="AK-test",
            wps_app_secret="test-secret",
            wps_file_id="file one",
            wps_sheet_id="3",
            wps_kso_signing_enabled=True,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.create_records([{"_sync_key": "order:1", "客户": "示例"}]) == {"order:1": "rec-1"}
    assert len(requests) == 2


def test_wps_update_uses_documented_post_update_contract(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 7200})
        assert request.url.path == "/v7/coop/dbsheet/file/sheets/3/records/update"
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["records"][0]["id"] == "rec-1"
        assert json.loads(payload["records"][0]["fields_value"])["_sync_key"] == "order:1"
        return httpx.Response(200, json={"code": 0, "data": {"records": [{"id": "rec-1"}]}})

    client = WpsOpenApiClient(
        replace(
            settings,
            wps_mode="real",
            wps_base_url="https://openapi.wps.cn",
            wps_app_id="AK-test",
            wps_app_secret="test-secret",
            wps_file_id="file",
            wps_sheet_id="3",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.update_records([("rec-1", {"_sync_key": "order:1", "客户": "已更新"})]) == {"order:1": "rec-1"}

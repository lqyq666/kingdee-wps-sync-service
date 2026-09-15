from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from app.config import ConfigurationError, validate_runtime_configuration
from app.integrations import (
    KdocsOpenApiClient,
    MockWpsClient,
    WpsOpenApiClient,
    WpsRemoteRecord,
    WpsSheetSchema,
    build_wps_client,
    kdocs_exchange_code,
    kdocs_refresh_access_token,
)


def _kdocs_client(settings, handler) -> KdocsOpenApiClient:
    return KdocsOpenApiClient(
        replace(
            settings,
            wps_mode="real",
            wps_provider="kdocs",
            kdocs_base_url="https://developer.kdocs.cn",
            kdocs_access_token="tok-1",
            kdocs_file_token="file one",
            kdocs_sheet_id="7",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _oauth_settings(settings):
    return replace(
        settings,
        kdocs_base_url="https://developer.kdocs.cn",
        kdocs_app_id="APP-1",
        kdocs_app_key="KEY-1",
    )


def test_kdocs_create_uses_documented_records_contract(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.decode().startswith("/api/v1/openapi/dbt/file%20one/sheets/7/records")
        assert not request.url.raw_path.decode().endswith("records")
        assert request.method == "POST"
        assert request.url.params["access_token"] == "tok-1"
        payload = json.loads(request.content)
        assert payload == {"records": [{"fields": {"_sync_key": "order:1", "客户": "示例"}}]}
        return httpx.Response(200, json={"code": 0, "result": "ok", "data": {"detail": {
            "records": [{"fields": {"_sync_key": "order:1"}, "id": "rec-1"}]
        }}})

    assert _kdocs_client(settings, handler).create_records([{"_sync_key": "order:1", "客户": "示例"}]) == {"order:1": "rec-1"}


def test_kdocs_create_maps_positionally_when_fields_are_not_echoed(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"detail": {"records": [{"id": "rec-1"}, {"id": "rec-2"}]}}})

    client = _kdocs_client(settings, handler)
    assert client.create_records([{"_sync_key": "order:1"}, {"_sync_key": "order:2"}]) == {"order:1": "rec-1", "order:2": "rec-2"}


def test_kdocs_create_rejects_count_mismatch(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"detail": {"records": [{"id": "rec-1"}]}}})

    with pytest.raises(ValueError, match="does not match"):
        _kdocs_client(settings, handler).create_records([{"_sync_key": "order:1"}, {"_sync_key": "order:2"}])


def test_kdocs_update_uses_documented_put_contract(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        payload = json.loads(request.content)
        assert payload == {"records": [{"id": "rec-1", "fields": {"_sync_key": "order:1", "客户": "已更新"}}]}
        return httpx.Response(200, json={"code": 0, "data": {"detail": {
            "records": [{"fields": {"_sync_key": "order:1"}, "id": "rec-1"}]
        }}})

    assert _kdocs_client(settings, handler).update_records([("rec-1", {"_sync_key": "order:1", "客户": "已更新"})]) == {"order:1": "rec-1"}


def test_kdocs_find_records_uses_documented_complex_query_contract(settings):
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.raw_path.decode().split("?")[0].endswith("/records/complex_query")
        payload = json.loads(request.content)
        requests.append(payload)
        key = payload["filter"]["criteria"][0]["values"][0]
        if key == "order:1" and "offset" not in payload:
            # Two remote rows share the key; both must surface so duplicates stay detectable.
            return httpx.Response(200, json={"code": 0, "data": {"detail": {
                "offset": "D",
                "records": [
                    {"fields": {"_sync_key": "order:1", "_sync_hash": "h1"}, "id": "rec-1"},
                    {"fields": {"_sync_key": "order:1", "_sync_hash": "h1"}, "id": "rec-1b"},
                ],
            }}})
        if key == "order:1":
            return httpx.Response(200, json={"code": 0, "data": {"detail": {
                "records": [{"fields": {"_sync_key": "order:1", "_sync_hash": "h1"}, "id": "rec-1c"}],
            }}})
        return httpx.Response(200, json={"code": 0, "data": {"detail": {"records": []}}})

    found = _kdocs_client(settings, handler).find_records_by_sync_keys(["order:1", "order:2", "order:1"])

    assert found == {
        "order:1": [
            WpsRemoteRecord("rec-1", {"_sync_key": "order:1", "_sync_hash": "h1"}),
            WpsRemoteRecord("rec-1b", {"_sync_key": "order:1", "_sync_hash": "h1"}),
            WpsRemoteRecord("rec-1c", {"_sync_key": "order:1", "_sync_hash": "h1"}),
        ],
    }
    assert len(requests) == 3  # order:1 page 1 + offset page, order:2 page 1
    for payload in requests:
        assert payload["fields"] == ["_sync_key", "_sync_hash"]
        assert payload["filter"]["mode"] == "AND"
        assert payload["filter"]["criteria"] == [
            {"field": "_sync_key", "op": "Equals", "values": [payload["filter"]["criteria"][0]["values"][0]]}
        ]
    assert requests[1]["offset"] == "D"


def test_kdocs_find_records_rejects_repeated_pagination_offset(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"detail": {"offset": "D", "records": []}}})

    with pytest.raises(ValueError, match="repeated pagination offset"):
        _kdocs_client(settings, handler).find_records_by_sync_keys(["order:1"])


def test_kdocs_get_schema_uses_documented_schemas_contract(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path.decode().split("?")[0] == "/api/v1/openapi/dbt/file%20one/schemas"
        assert request.url.params["access_token"] == "tok-1"
        return httpx.Response(200, json={"code": 0, "data": {"detail": {"sheets": [
            {"id": 1, "name": "不要动", "primaryFieldId": "B", "fields": [
                {"id": "B", "name": "名称", "type": "MultiLineText"},
                {"id": "C", "name": "数量", "type": "Number"},
            ]},
            {"id": 490, "name": "测试", "fields": [
                {"id": "Qb", "name": "_sync_key", "type": "SingleLineText"},
            ]},
        ]}}})

    schema = _kdocs_client(settings, handler).get_schema()

    assert [(sheet.sheet_id, sheet.name, len(sheet.fields)) for sheet in schema] == [
        (1, "不要动", 2),
        (490, "测试", 1),
    ]


def test_kdocs_get_schema_parses_fields(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"detail": {"sheets": [
            {"id": 7, "name": "测试", "fields": [
                {"id": "B", "name": "名称", "type": "MultiLineText"},
                {"id": "Qb", "name": "_sync_key", "type": "SingleLineText"},
            ]},
        ]}}})

    schema = _kdocs_client(settings, handler).get_schema()
    assert schema[0].sheet_id == 7
    assert [(f.name, f.type, f.field_id) for f in schema[0].fields] == [
        ("名称", "MultiLineText", "B"),
        ("_sync_key", "SingleLineText", "Qb"),
    ]


def test_kdocs_rejects_api_errors_and_malformed_envelopes(settings):
    responses = iter([
        {"code": 40001, "result": "permission denied"},
        {"code": 0, "data": {}},
        {"code": 0, "data": {"detail": {"records": [{"fields": {"_sync_key": "order:1"}}]}}},
    ])
    client = _kdocs_client(settings, lambda request: httpx.Response(200, json=next(responses)))

    with pytest.raises(ValueError, match="40001"):
        client.create_records([{"_sync_key": "order:1"}])
    with pytest.raises(ValueError, match="data.detail"):
        client.create_records([{"_sync_key": "order:1"}])
    with pytest.raises(ValueError, match="without id"):
        client.find_records_by_sync_keys(["order:1"])


def test_kdocs_user_basic_and_personal_files_parse_envelope(settings):
    responses = iter([
        {"code": 0, "data": {"detail": {"id": 10086, "name": "同步账号"}}},
        {"code": 0, "data": {"detail": {"files": [{"id": "tok-1", "name": "汇报"}]}}},
    ])
    client = _kdocs_client(settings, lambda request: httpx.Response(200, json=next(responses)))
    assert client.get_user_basic() == {"id": 10086, "name": "同步账号"}
    assert client.list_personal_files() == [{"id": "tok-1", "name": "汇报"}]


def test_kdocs_oauth_exchange_uses_unsigned_query_contract(settings):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/oauth2/access_token"
        assert dict(request.url.params) == {"code": "c-1", "app_id": "APP-1", "app_key": "KEY-1"}
        return httpx.Response(200, json={"code": 0, "data": {"access_token": "at-1", "refresh_token": "rt-1"}})

    tokens = kdocs_exchange_code(_oauth_settings(settings), "c-1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tokens == {"access_token": "at-1", "refresh_token": "rt-1"}
    assert len(requests) == 1


def test_kdocs_oauth_refresh_uses_query_app_id_and_json_body(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/oauth2/refresh_token"
        assert request.url.params["app_id"] == "APP-1"
        assert json.loads(request.content) == {"app_key": "KEY-1", "refresh_token": "rt-1"}
        return httpx.Response(200, json={"code": 0, "data": {"access_token": "at-2"}})

    refreshed = kdocs_refresh_access_token(
        replace(_oauth_settings(settings), kdocs_refresh_token="rt-1"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert refreshed == {"access_token": "at-2", "refresh_token": ""}


def test_kdocs_oauth_errors_surface_the_documented_code(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 20001, "result": "invalid code"})

    with pytest.raises(ValueError, match="20001"):
        kdocs_exchange_code(_oauth_settings(settings), "expired", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_build_wps_client_switches_on_provider_and_mode(settings):
    assert isinstance(build_wps_client(replace(settings, wps_mode="mock", wps_provider="kdocs")), MockWpsClient)
    assert isinstance(
        build_wps_client(replace(settings, wps_mode="real", wps_provider="kdocs", kdocs_access_token="t")),
        KdocsOpenApiClient,
    )
    assert isinstance(
        build_wps_client(replace(settings, wps_mode="real", wps_provider="wps365")),
        WpsOpenApiClient,
    )


def test_validate_runtime_kdocs_real_requires_token_file_and_sheet(settings):
    configured = replace(
        settings,
        kingdee_mode="mock",
        wps_mode="real",
        wps_provider="kdocs",
        wps_app_id="",
        kdocs_access_token="t",
        kdocs_file_token="f",
        kdocs_sheet_id="7",
    )
    validate_runtime_configuration(configured)  # wps_* app credentials are not required for kdocs

    with pytest.raises(ConfigurationError, match="KDOCS_FILE_TOKEN"):
        validate_runtime_configuration(replace(configured, kdocs_file_token=""))
    with pytest.raises(ConfigurationError, match="KDOCS_API_FAMILY"):
        validate_runtime_configuration(replace(configured, kdocs_api_family="sheet"))
    with pytest.raises(ConfigurationError, match="WPS_PROVIDER"):
        validate_runtime_configuration(replace(configured, wps_provider="open"))

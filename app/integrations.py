"""Kingdee and WPS adapters; mock adapters are deterministic and network-free."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import Settings


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def kso1_signature(secret: str, payload: bytes) -> str:
    """Legacy Kingdee contract hook; its actual signature contract is tenant-specific."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def wps_kso1_authorization(
    *,
    app_id: str,
    app_secret: str,
    method: str,
    request_uri: str,
    content_type: str,
    kso_date: str,
    request_body: bytes,
) -> str:
    """Build WPS KSO-1 authorization from its documented canonical string."""
    body_hash = hashlib.sha256(request_body).hexdigest() if request_body else ""
    canonical = f"KSO-1{method.upper()}{request_uri}{content_type}{kso_date}{body_hash}"
    signature = hmac.new(app_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"KSO-1 {app_id}:{signature}"


class KingdeeClient(Protocol):
    def fetch_since(self, since: datetime | None) -> list[dict[str, object]]: ...


SYNC_KEY_FIELD = "_sync_key"
SYNC_HASH_FIELD = "_sync_hash"
# The list_by_page contract accepts one value per Equals criterion and does not document a
# criteria-count limit, so keys are looked up in conservative OR-groups.
WPS_QUERY_CRITERIA_BATCH = 50
WPS_QUERY_PAGE_SIZE = 1000
# The kdocs complex_query contract allows exactly one criterion per field and one value per
# Equals criterion, so each _sync_key is queried on its own; page via the returned offset.
KDOCS_QUERY_PAGE_SIZE = 1000


@dataclass(frozen=True)
class WpsRemoteRecord:
    record_id: str | None
    fields: dict[str, object]


@dataclass(frozen=True)
class WpsFieldSchema:
    name: str
    type: str | None
    field_id: str | None


@dataclass(frozen=True)
class WpsSheetSchema:
    sheet_id: int
    name: str
    fields: list[WpsFieldSchema]


class WpsClient(Protocol):
    def create_records(self, records: list[dict[str, object]]) -> dict[str, str]: ...

    def update_records(self, records: list[tuple[str, dict[str, object]]]) -> dict[str, str]: ...

    def find_records_by_sync_keys(self, sync_keys: list[str]) -> dict[str, list[WpsRemoteRecord]]: ...


MOCK_SALES_DETAILS: tuple[dict[str, object], ...] = (
    {
        "source_document_id": "SO-20260901-001", "source_line_id": "1",
        "modified_at": "2026-09-01T01:00:00+00:00", "date": "2026-09-01",
        "document_number": "XS-20260901-001", "customer": "华北示例客户", "document_status": "已审核",
        "material_name": "示例产品 A", "delivered_quantity": 12, "warehouse": "北京成品仓",
        "product_category": "饮料", "sales_category": "常规销售", "brand": "示例品牌",
        "county": "昌平区", "regional_manager": "张经理", "account_manager": "李经理",
        "region": "华北", "factory_price": 38.5, "tax_inclusive_unit_price": 45.0, "sales_unit": "箱",
    },
    {
        "source_document_id": "SO-20260901-001", "source_line_id": "2",
        "modified_at": "2026-09-01T01:02:00+00:00", "date": "2026-09-01",
        "document_number": "XS-20260901-001", "customer": "华北示例客户", "document_status": "已审核",
        "material_name": "示例产品 B", "delivered_quantity": 8, "warehouse": "北京成品仓",
        "product_category": "食品", "sales_category": "常规销售", "brand": "示例品牌",
        "county": "昌平区", "regional_manager": "张经理", "account_manager": "李经理",
        "region": "华北", "factory_price": 22.0, "tax_inclusive_unit_price": 25.5, "sales_unit": "箱",
    },
    {
        "source_document_id": "SO-20260902-001", "source_line_id": "1",
        "modified_at": "2026-09-02T03:30:00+00:00", "date": "2026-09-02",
        "document_number": "XS-20260902-001", "customer": "华东示例客户", "document_status": "已审核",
        "material_name": "示例产品 C", "delivered_quantity": 20, "warehouse": "上海成品仓",
        "product_category": "饮料", "sales_category": "新品销售", "brand": "示例品牌",
        "county": "昆山市", "regional_manager": "王经理", "account_manager": "赵经理",
        "region": "华东", "factory_price": 41.0, "tax_inclusive_unit_price": 48.0, "sales_unit": "箱",
    },
)


class MockKingdeeClient:
    def __init__(self, rows: Iterable[dict[str, object]] = MOCK_SALES_DETAILS):
        self.rows = [dict(row) for row in rows]

    def fetch_since(self, since: datetime | None) -> list[dict[str, object]]:
        if since is None:
            return list(self.rows)
        return [row for row in self.rows if parse_timestamp(str(row["modified_at"])) >= since]


class MockWpsClient:
    """Mock WPS returns deterministic ids; an optional shared store simulates the remote table."""
    def __init__(self, store: dict[str, list[WpsRemoteRecord]] | None = None):
        self.store: dict[str, list[WpsRemoteRecord]] = store if store is not None else {}

    def create_records(self, records: list[dict[str, object]]) -> dict[str, str]:
        ids: dict[str, str] = {}
        for record in records:
            key = str(record[SYNC_KEY_FIELD])
            remote_id = f"mock-{key}"
            self.store.setdefault(key, []).append(WpsRemoteRecord(remote_id, dict(record)))
            ids[key] = remote_id
        return ids

    def update_records(self, records: list[tuple[str, dict[str, object]]]) -> dict[str, str]:
        ids: dict[str, str] = {}
        for remote_id, fields in records:
            key = str(fields[SYNC_KEY_FIELD])
            self.store[key] = [WpsRemoteRecord(remote_id, dict(fields))]
            ids[key] = remote_id
        return ids

    def find_records_by_sync_keys(self, sync_keys: list[str]) -> dict[str, list[WpsRemoteRecord]]:
        return {key: list(self.store[key]) for key in sync_keys if key in self.store}


class RealKingdeeClient:
    """HTTP adapter requiring a tenant-confirmed Kingdee request/response contract."""
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)

    def fetch_since(self, since: datetime | None) -> list[dict[str, object]]:
        payload = {"form_id": self.settings.kingdee_form_id, "modified_since": since.isoformat() if since else None}
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-App-Id": self.settings.kingdee_app_id}
        if self.settings.kingdee_kso_secret:
            headers["X-KSO-1-Signature"] = kso1_signature(self.settings.kingdee_kso_secret, encoded)
        response = self.client.post(self.settings.kingdee_sync_url, content=encoded, headers=headers)
        response.raise_for_status()
        data = response.json()
        rows = data.get("data", data.get("rows"))
        if not isinstance(rows, list):
            raise ValueError("Kingdee response must contain a list at data or rows; verify tenant API contract")
        return rows


@dataclass
class TokenCache:
    token: str | None = None
    expires_at: datetime | None = None

    def valid(self) -> bool:
        return bool(self.token and self.expires_at and self.expires_at > datetime.now(UTC))


class WpsOpenApiClient:
    """WPS 365 self-built-app client for documented DBSheet create/update APIs."""
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)
        self.cache = TokenCache()

    def _token(self) -> str:
        if self.settings.wps_token_mode == "user":
            # User access_token acts with the authorizing user's file permissions
            # (2h lifetime; refresh via `python -m app.smoke wps-refresh`).
            if not self.settings.wps_user_access_token:
                raise ValueError("WPS_TOKEN_MODE=user requires WPS_USER_ACCESS_TOKEN")
            return self.settings.wps_user_access_token
        if self.cache.valid():
            assert self.cache.token is not None
            return self.cache.token
        response = self.client.post(
            self.settings.wps_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.wps_app_id,
                "client_secret": self.settings.wps_app_secret,
            },
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("WPS token response must contain access_token; verify tenant API contract")
        expires_in = int(data.get("expires_in", 300))
        self.cache = TokenCache(
            token=token,
            expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=max(1, expires_in - 30)),
        )
        return token

    def _headers(self, *, method: str, request_uri: str, payload: bytes) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        if self.settings.wps_kso_signing_enabled:
            kso_date = format_datetime(datetime.now(UTC), usegmt=True)
            signing_secret = self.settings.wps_kso_secret or self.settings.wps_app_secret
            headers["X-Kso-Date"] = kso_date
            headers["X-Kso-Authorization"] = wps_kso1_authorization(
                app_id=self.settings.wps_app_id,
                app_secret=signing_secret,
                method=method,
                request_uri=request_uri,
                content_type=headers["Content-Type"],
                kso_date=kso_date,
                request_body=payload,
            )
        return headers

    @staticmethod
    def _data_object(data: dict[str, object]) -> dict[str, object]:
        if data.get("code") not in {None, 0}:
            raise ValueError(f"WPS API error {data.get('code')}: {data.get('msg', 'unknown error')}")
        result = data.get("data", data)
        if not isinstance(result, dict):
            raise ValueError("WPS response data must be an object")
        return result

    @classmethod
    def _records(cls, data: dict[str, object]) -> list[object]:
        records = cls._data_object(data).get("records")
        if not isinstance(records, list):
            raise ValueError("WPS response must contain a records list; verify tenant API contract")
        return records

    @staticmethod
    def _ids(data: dict[str, object], submitted_records: list[dict[str, object]]) -> dict[str, str]:
        records = WpsOpenApiClient._records(data)
        ids: dict[str, str] = {}
        if len(records) != len(submitted_records):
            raise ValueError("WPS response record count does not match the submitted batch")
        for source, record in zip(submitted_records, records, strict=True):
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                ids[str(source["_sync_key"])] = record["id"]
        expected_keys = {str(record["_sync_key"]) for record in submitted_records}
        if set(ids) != expected_keys:
            raise ValueError("WPS response must return an id for every submitted record")
        return ids

    def _records_uri(self, action: str) -> str:
        file_id = quote(self.settings.wps_file_id, safe="")
        return f"/v7/coop/dbsheet/{file_id}/sheets/{self.settings.wps_sheet_id}/records/{action}"

    def _post_json(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        request_uri = self._records_uri(action)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self.client.post(
            f"{self.settings.wps_base_url.rstrip('/')}{request_uri}",
            content=encoded,
            headers=self._headers(method="POST", request_uri=request_uri, payload=encoded),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("WPS response body must be a JSON object")
        return data

    def _get_json(self, request_uri: str) -> dict[str, object]:
        response = self.client.get(
            f"{self.settings.wps_base_url.rstrip('/')}{request_uri}",
            headers=self._headers(method="GET", request_uri=request_uri, payload=b""),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("WPS response body must be a JSON object")
        return data

    def resolve_link(self, link_id: str) -> dict[str, object]:
        """Read the documented GET /v7/links/{link_id}/meta (kso.file_link.readwrite).

        Returns the raw data object so the smoke CLI can surface file_id for a /l/ short
        link, which the browser address bar never expands.
        """
        request_uri = f"/v7/links/{quote(link_id, safe='')}/meta"
        return self._data_object(self._get_json(request_uri))

    def get_schema(self) -> list[WpsSheetSchema]:
        """Read the documented GET /v7/coop/dbsheet/{file_id}/schema (kso.dbsheet.read)."""
        request_uri = f"/v7/coop/dbsheet/{quote(self.settings.wps_file_id, safe='')}/schema"
        sheets = self._data_object(self._get_json(request_uri)).get("sheets")
        if not isinstance(sheets, list):
            raise ValueError("WPS schema response must contain a sheets list; verify tenant API contract")
        parsed: list[WpsSheetSchema] = []
        for sheet in sheets:
            if not isinstance(sheet, dict) or not isinstance(sheet.get("id"), int):
                raise ValueError("WPS schema sheet entries must contain an integer id; verify tenant API contract")
            fields = sheet.get("fields")
            parsed_fields = [
                WpsFieldSchema(
                    name=str(field.get("name", "")),
                    type=str(field["type"]) if field.get("type") is not None else None,
                    field_id=str(field["id"]) if field.get("id") is not None else None,
                )
                for field in fields if isinstance(field, dict)
            ] if isinstance(fields, list) else []
            parsed.append(WpsSheetSchema(sheet_id=sheet["id"], name=str(sheet.get("name", "")), fields=parsed_fields))
        return parsed

    def _post_records(self, action: str, payload: dict[str, object], submitted_records: list[dict[str, object]]) -> dict[str, str]:
        return self._ids(self._post_json(action, payload), submitted_records)

    def create_records(self, records: list[dict[str, object]]) -> dict[str, str]:
        payload = {
            "prefer_id": False,
            "records": [{"fields_value": json.dumps(record, ensure_ascii=False, separators=(",", ":"))} for record in records],
        }
        return self._post_records("create", payload, records)

    def update_records(self, records: list[tuple[str, dict[str, object]]]) -> dict[str, str]:
        submitted_records = [fields for _, fields in records]
        payload = {
            "prefer_id": False,
            "records": [
                {"id": remote_id, "fields_value": json.dumps(fields, ensure_ascii=False, separators=(",", ":"))}
                for remote_id, fields in records
            ],
        }
        return self._post_records("update", payload, submitted_records)

    def find_records_by_sync_keys(self, sync_keys: list[str]) -> dict[str, list[WpsRemoteRecord]]:
        """Look up remote rows by the hidden _sync_key field via the documented list_by_page API.

        Every matching row is returned, so callers can detect duplicate remote keys instead of
        silently picking one.
        """
        found: dict[str, list[WpsRemoteRecord]] = {}
        unique_keys = list(dict.fromkeys(str(key) for key in sync_keys))
        for start in range(0, len(unique_keys), WPS_QUERY_CRITERIA_BATCH):
            criteria = [
                {"field": SYNC_KEY_FIELD, "operator": "Equals", "values": [key]}
                for key in unique_keys[start:start + WPS_QUERY_CRITERIA_BATCH]
            ]
            page_num = 1
            while True:
                payload: dict[str, object] = {
                    "fields": [SYNC_KEY_FIELD, SYNC_HASH_FIELD],
                    "filter": {"mode": "OR", "criteria": criteria},
                    "page_num": page_num,
                    "page_size": WPS_QUERY_PAGE_SIZE,
                }
                records = self._records(self._post_json("list_by_page", payload))
                for record in records:
                    if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                        raise ValueError("WPS list_by_page returned a record without id; verify tenant API contract")
                    fields = record.get("fields")
                    fields = dict(fields) if isinstance(fields, dict) else {}
                    key = str(fields.get(SYNC_KEY_FIELD) or "")
                    found.setdefault(key, []).append(WpsRemoteRecord(record["id"], fields))
                if len(records) < WPS_QUERY_PAGE_SIZE:
                    break
                page_num += 1
        return found


class KdocsOpenApiClient:
    """kdocs open-platform client for dbt/ksheet record APIs (user OAuth access_token).

    Contract sources: developer.kdocs.cn docs for 遍历记录/创建记录/批量更新记录/
    遍历记录(复杂查询条件)/获取文档 Schema 信息 and the Web 授权 flow. All record APIs
    authenticate with the authorized user's access_token query parameter — there is no
    app-level client-credentials token on this platform.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        response = self.client.request(
            method,
            f"{self.settings.kdocs_base_url.rstrip('/')}{path}",
            params={"access_token": self.settings.kdocs_access_token, **(params or {})},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("kdocs response body must be a JSON object; verify the API contract")
        if data.get("code") not in {None, 0}:
            detail = data.get("result") or data.get("msg") or "unknown error"
            raise ValueError(f"kdocs API error {data.get('code')}: {detail}")
        return data

    def _detail(self, data: dict[str, object]) -> dict[str, object]:
        body = data.get("data")
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, dict):
            raise ValueError("kdocs response must contain data.detail; verify the API contract")
        return detail

    def _records_path(self) -> str:
        file_token = quote(self.settings.kdocs_file_token, safe="")
        return f"/api/v1/openapi/{self.settings.kdocs_api_family}/{file_token}/sheets/{self.settings.kdocs_sheet_id}/records"

    def _schema_path(self) -> str:
        file_token = quote(self.settings.kdocs_file_token, safe="")
        return f"/api/v1/openapi/{self.settings.kdocs_api_family}/{file_token}/schemas"

    @staticmethod
    def _parse_records(detail: dict[str, object]) -> list[WpsRemoteRecord]:
        records = detail.get("records")
        if not isinstance(records, list):
            raise ValueError("kdocs response must contain a records list; verify the API contract")
        parsed: list[WpsRemoteRecord] = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                raise ValueError("kdocs returned a record without id; verify the API contract")
            fields = record.get("fields")
            parsed.append(WpsRemoteRecord(record["id"], dict(fields) if isinstance(fields, dict) else {}))
        return parsed

    def _write_records(self, method: str, records: list[dict[str, object]]) -> dict[str, str]:
        data = self._request_json(method, self._records_path(), payload={"records": records})
        parsed = self._parse_records(self._detail(data))
        submitted_keys = [str(record["fields"][SYNC_KEY_FIELD]) for record in records]
        # The write contract echoes each record's fields; map by the echoed _sync_key when
        # complete, and fall back to submitted order only when counts match.
        by_echo = {
            str(record.fields.get(SYNC_KEY_FIELD)): record.record_id
            for record in parsed
            if record.record_id and record.fields.get(SYNC_KEY_FIELD)
        }
        if set(by_echo) == set(submitted_keys):
            return by_echo
        if len(parsed) != len(submitted_keys):
            raise ValueError("kdocs response record count does not match the submitted batch; verify the API contract")
        return {
            key: record.record_id or ""
            for key, record in zip(submitted_keys, parsed, strict=True)
        }

    def create_records(self, records: list[dict[str, object]]) -> dict[str, str]:
        return self._write_records("POST", [{"fields": record} for record in records])

    def update_records(self, records: list[tuple[str, dict[str, object]]]) -> dict[str, str]:
        return self._write_records("PUT", [{"id": remote_id, "fields": fields} for remote_id, fields in records])

    def find_records_by_sync_keys(self, sync_keys: list[str]) -> dict[str, list[WpsRemoteRecord]]:
        """Look up remote rows by _sync_key via the documented complex_query API.

        Every matching row is returned so callers can detect duplicate remote keys
        instead of silently picking one. One request per key plus offset-cursor pages.
        """
        found: dict[str, list[WpsRemoteRecord]] = {}
        for key in dict.fromkeys(str(value) for value in sync_keys):
            offset = ""
            seen_offsets: set[str] = set()
            while True:
                payload: dict[str, object] = {
                    "fields": [SYNC_KEY_FIELD, SYNC_HASH_FIELD],
                    "filter": {
                        "mode": "AND",
                        "criteria": [{"field": SYNC_KEY_FIELD, "op": "Equals", "values": [key]}],
                    },
                    "pageSize": KDOCS_QUERY_PAGE_SIZE,
                }
                if offset:
                    payload["offset"] = offset
                detail = self._detail(
                    self._request_json("POST", f"{self._records_path()}/complex_query", payload=payload)
                )
                for record in self._parse_records(detail):
                    row_key = str(record.fields.get(SYNC_KEY_FIELD) or key)
                    found.setdefault(row_key, []).append(record)
                next_offset = detail.get("offset")
                offset = str(next_offset) if next_offset else ""
                if not offset:
                    break
                if offset in seen_offsets:
                    raise ValueError("kdocs complex_query returned a repeated pagination offset")
                seen_offsets.add(offset)
        return found

    def get_schema(self) -> list[WpsSheetSchema]:
        """Read the documented GET /api/v1/openapi/{dbt|ksheet}/{file_token}/schemas."""
        sheets = self._detail(self._request_json("GET", self._schema_path())).get("sheets")
        if not isinstance(sheets, list):
            raise ValueError("kdocs schemas response must contain a sheets list; verify the API contract")
        parsed: list[WpsSheetSchema] = []
        for sheet in sheets:
            if not isinstance(sheet, dict) or not isinstance(sheet.get("id"), int):
                raise ValueError("kdocs schema sheet entries must contain an integer id; verify the API contract")
            fields = sheet.get("fields")
            parsed_fields = [
                WpsFieldSchema(
                    name=str(field.get("name", "")),
                    type=str(field["type"]) if field.get("type") is not None else None,
                    field_id=str(field["id"]) if field.get("id") is not None else None,
                )
                for field in fields if isinstance(field, dict)
            ] if isinstance(fields, list) else []
            parsed.append(WpsSheetSchema(sheet_id=sheet["id"], name=str(sheet.get("name", "")), fields=parsed_fields))
        return parsed

    def get_user_basic(self) -> dict[str, object]:
        """Read GET /api/v1/openapi/user/basic to validate the access_token without file access."""
        body = self._request_json("GET", "/api/v1/openapi/user/basic").get("data")
        detail = body.get("detail") if isinstance(body, dict) else None
        info = detail if isinstance(detail, dict) else (body if isinstance(body, dict) else {})
        return {name: info[name] for name in ("id", "name", "nickname", "avatar") if name in info}

    def list_personal_files(self) -> list[dict[str, object]]:
        """Read GET /api/v1/openapi/personal/files to discover file tokens by name."""
        files = self._detail(self._request_json("GET", "/api/v1/openapi/personal/files")).get("files")
        if not isinstance(files, list):
            raise ValueError("kdocs personal files response must contain a files list; verify the API contract")
        return [dict(file) for file in files if isinstance(file, dict)]


def wps_authorize_url(settings: Settings, state: str) -> str:
    """Build the WPS 365 user-authorization page URL per GET /oauth2/auth."""
    return (
        f"{settings.wps_base_url.rstrip('/')}/oauth2/auth"
        f"?client_id={quote(settings.wps_app_id, safe='')}"
        "&response_type=code"
        f"&redirect_uri={quote(settings.wps_redirect_uri, safe='')}"
        f"&scope={quote(settings.wps_auth_scopes, safe='')}"
        f"&state={quote(state, safe='')}"
    )


def _wps_user_token_payload(data: dict[str, object]) -> dict[str, str]:
    if not isinstance(data, dict) or data.get("code") not in {None, 0}:
        code = data.get("code") if isinstance(data, dict) else "?"
        raise ValueError(f"WPS token endpoint error {code}: {data.get('msg', 'unknown error') if isinstance(data, dict) else 'invalid response'}")
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("WPS token response must contain access_token; verify app credentials and code")
    refresh = data.get("refresh_token")
    return {"access_token": token, "refresh_token": str(refresh or "")}


def wps_exchange_code(settings: Settings, code: str, client: httpx.Client | None = None) -> dict[str, str]:
    """Exchange a user-authorization code per POST /oauth2/token (form, grant_type=authorization_code)."""
    http = client or httpx.Client(timeout=30)
    response = http.post(
        settings.wps_token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": settings.wps_app_id,
            "client_secret": settings.wps_app_secret,
            "code": code,
            "redirect_uri": settings.wps_redirect_uri,
        },
    )
    response.raise_for_status()
    return _wps_user_token_payload(response.json())


def wps_refresh_user_token(settings: Settings, client: httpx.Client | None = None) -> dict[str, str]:
    """Refresh the user access_token per POST /oauth2/token (form, grant_type=refresh_token)."""
    if not settings.wps_user_refresh_token:
        raise ValueError("WPS_USER_REFRESH_TOKEN is required to refresh the user access token")
    http = client or httpx.Client(timeout=30)
    response = http.post(
        settings.wps_token_url,
        data={
            "grant_type": "refresh_token",
            "refresh_token": settings.wps_user_refresh_token,
            "client_id": settings.wps_app_id,
            "client_secret": settings.wps_app_secret,
        },
    )
    response.raise_for_status()
    return _wps_user_token_payload(response.json())


def kdocs_authorize_url(settings: Settings, state: str) -> str:
    """Build the Web 授权 page URL; scopes are comma-separated per the OAuth doc."""
    return (
        f"{settings.kdocs_base_url.rstrip('/')}/h5/auth"
        f"?app_id={quote(settings.kdocs_app_id, safe='')}"
        "&scope=access_personal_files%2Cedit_personal_files"
        f"&redirect_uri={quote(settings.kdocs_redirect_uri, safe='')}"
        f"&state={quote(state, safe='')}"
    )


def _kdocs_token_payload(data: dict[str, object]) -> dict[str, str]:
    if not isinstance(data, dict) or data.get("code") not in {None, 0}:
        detail = data.get("result") or "unknown error" if isinstance(data, dict) else "invalid response"
        raise ValueError(f"kdocs token endpoint error {data.get('code') if isinstance(data, dict) else '?'}: {detail}")
    body = data.get("data")
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("kdocs token response must contain access_token; verify the app credentials")
    refresh = body.get("refresh_token") if isinstance(body, dict) else None
    return {"access_token": token, "refresh_token": str(refresh or "")}


def kdocs_exchange_code(settings: Settings, code: str, client: httpx.Client | None = None) -> dict[str, str]:
    """Exchange a Web 授权 code per GET /api/v1/oauth2/access_token (query params, no signing)."""
    http = client or httpx.Client(timeout=30)
    response = http.get(
        f"{settings.kdocs_base_url.rstrip('/')}/api/v1/oauth2/access_token",
        params={"code": code, "app_id": settings.kdocs_app_id, "app_key": settings.kdocs_app_key},
    )
    response.raise_for_status()
    return _kdocs_token_payload(response.json())


def kdocs_refresh_access_token(settings: Settings, client: httpx.Client | None = None) -> dict[str, str]:
    """Refresh per POST /api/v1/oauth2/refresh_token (app_id in query, rest in JSON body)."""
    if not settings.kdocs_refresh_token:
        raise ValueError("KDOCS_REFRESH_TOKEN is required to refresh the access token")
    http = client or httpx.Client(timeout=30)
    response = http.post(
        f"{settings.kdocs_base_url.rstrip('/')}/api/v1/oauth2/refresh_token",
        params={"app_id": settings.kdocs_app_id},
        json={"app_key": settings.kdocs_app_key, "refresh_token": settings.kdocs_refresh_token},
    )
    response.raise_for_status()
    return _kdocs_token_payload(response.json())


def build_kingdee_client(settings: Settings) -> KingdeeClient:
    return MockKingdeeClient() if settings.kingdee_mode == "mock" else RealKingdeeClient(settings)


def build_wps_client(settings: Settings) -> WpsClient:
    if settings.wps_mode == "mock":
        return MockWpsClient()
    if settings.wps_provider == "kdocs":
        return KdocsOpenApiClient(settings)
    return WpsOpenApiClient(settings)

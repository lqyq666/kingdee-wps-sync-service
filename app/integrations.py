"""Kingdee and WPS adapters; mock adapters are deterministic and network-free."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.config import Settings


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def kso1_signature(secret: str, payload: bytes) -> str:
    """Optional HMAC hook; tenant-specific KSO-1 canonicalization must be confirmed."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class KingdeeClient(Protocol):
    def fetch_since(self, since: datetime | None) -> list[dict[str, object]]: ...


class WpsClient(Protocol):
    def create_records(self, records: list[dict[str, object]]) -> dict[str, str]: ...

    def update_records(self, records: list[tuple[str, dict[str, object]]]) -> dict[str, str]: ...


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
    """Mock WPS returns deterministic ids; durable state is held by SyncRecord."""
    def create_records(self, records: list[dict[str, object]]) -> dict[str, str]:
        return {str(record["_sync_key"]): f"mock-{record['_sync_key']}" for record in records}

    def update_records(self, records: list[tuple[str, dict[str, object]]]) -> dict[str, str]:
        return {str(fields["_sync_key"]): remote_id for remote_id, fields in records}


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
    """WPS client with token caching and configurable tenant API endpoints."""
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=30)
        self.cache = TokenCache()

    def _token(self) -> str:
        if self.cache.valid():
            assert self.cache.token is not None
            return self.cache.token
        payload = {"app_id": self.settings.wps_app_id, "app_secret": self.settings.wps_app_secret}
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.settings.wps_kso_secret:
            headers["X-KSO-1-Signature"] = kso1_signature(self.settings.wps_kso_secret, encoded)
        response = self.client.post(self.settings.wps_token_url, content=encoded, headers=headers)
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

    def _headers(self, payload: bytes) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "X-WPS-File-Id": self.settings.wps_file_id,
            "X-WPS-Sheet-Id": self.settings.wps_sheet_id,
        }
        if self.settings.wps_kso_secret:
            headers["X-KSO-1-Signature"] = kso1_signature(self.settings.wps_kso_secret, payload)
        return headers

    @staticmethod
    def _ids(data: dict[str, object], expected_keys: list[str]) -> dict[str, str]:
        records = data.get("records", data.get("data", []))
        if not isinstance(records, list):
            raise ValueError("WPS response must contain a records list; verify tenant API contract")
        ids: dict[str, str] = {}
        for record in records:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                sync_key = record.get("sync_key") or record.get("_sync_key")
                if isinstance(sync_key, str):
                    ids[sync_key] = record["id"]
        if set(ids) != set(expected_keys):
            raise ValueError("WPS response must return id and sync_key for every submitted record")
        return ids

    def create_records(self, records: list[dict[str, object]]) -> dict[str, str]:
        encoded = json.dumps({"records": [{"fields": record} for record in records]}, ensure_ascii=False).encode("utf-8")
        response = self.client.post(self.settings.wps_records_url, content=encoded, headers=self._headers(encoded))
        response.raise_for_status()
        return self._ids(response.json(), [str(record["_sync_key"]) for record in records])

    def update_records(self, records: list[tuple[str, dict[str, object]]]) -> dict[str, str]:
        payload = {"records": [{"id": remote_id, "fields": fields} for remote_id, fields in records]}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = self.client.patch(self.settings.wps_records_url, content=encoded, headers=self._headers(encoded))
        response.raise_for_status()
        return self._ids(response.json(), [str(fields["_sync_key"]) for _, fields in records])


def build_kingdee_client(settings: Settings) -> KingdeeClient:
    return MockKingdeeClient() if settings.kingdee_mode == "mock" else RealKingdeeClient(settings)


def build_wps_client(settings: Settings) -> WpsClient:
    return MockWpsClient() if settings.wps_mode == "mock" else WpsOpenApiClient(settings)

"""Canonical content hashes for idempotent record comparison."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Cannot hash value of type {type(value).__name__}")


def content_hash(fields: dict[str, object]) -> str:
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

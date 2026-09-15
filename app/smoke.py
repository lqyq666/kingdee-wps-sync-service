"""Read-only WPS smoke checks for staged real integration.

Performs no create/update calls: token, schema (sheets/fields), and a single
_sync_key lookup so the first write against a tenant table happens only after
these checks pass. Never prints token values or credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from typing import Sequence

from app.config import Settings
from app.integrations import WpsOpenApiClient, WpsSheetSchema
from app.mapping import FIELD_MAPPINGS, TECHNICAL_WPS_FIELDS

TEXT_FIELD_TYPES = frozenset({"MultiLineText", "Text"})
EXPECTED_FIELDS = (*(definition.wps_field for definition in FIELD_MAPPINGS), *TECHNICAL_WPS_FIELDS)


def build_field_report(sheet: WpsSheetSchema) -> dict[str, object]:
    """Check the target sheet against the 17 business and 3 technical fields.

    Business field types stay informational until the tenant mapping metadata
    lands; the technical fields must be text so keys, timestamps and hashes
    round-trip verbatim.
    """
    types = {field.name: field.type for field in sheet.fields}
    missing = [name for name in EXPECTED_FIELDS if name not in types]
    technical_type_problems = [
        name for name in TECHNICAL_WPS_FIELDS
        if name in types and types[name] not in TEXT_FIELD_TYPES
    ]
    return {
        "sheet_id": sheet.sheet_id,
        "sheet_name": sheet.name,
        "ready": not missing and not technical_type_problems,
        "missing": missing,
        "technical_type_problems": technical_type_problems,
        "expected_field_types": {name: types.get(name) for name in EXPECTED_FIELDS},
    }


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _require_credentials(settings: Settings) -> bool:
    missing = [name for name in ("wps_app_id", "wps_app_secret") if not getattr(settings, name)]
    if missing:
        _emit({"status": "BLOCKED", "error": "Real WPS smoke checks require " + ", ".join(name.upper() for name in missing)})
        return False
    return True


def _resolve_target(value: str | None, fallback: str, name: str) -> str | None:
    resolved = value.strip() if value else ""
    if resolved:
        return resolved
    if fallback:
        return fallback
    _emit({"status": "BLOCKED", "error": f"{name} is required via --{name.replace('_', '-')} or the WPS_* environment settings"})
    return None


def _client_for(settings: Settings, file_id: str | None, sheet_id: int | None) -> WpsOpenApiClient | None:
    resolved_file = _resolve_target(file_id, settings.wps_file_id, "file_id")
    if resolved_file is None:
        return None
    resolved_sheet = str(sheet_id) if sheet_id is not None else settings.wps_sheet_id
    overrides: dict[str, str] = {"wps_file_id": resolved_file or ""}
    if resolved_sheet:
        overrides["wps_sheet_id"] = resolved_sheet
    return WpsOpenApiClient(replace(settings, **overrides))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.smoke", description="Run read-only WPS integration checks")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("token", help="Verify the self-built app can obtain an access token")
    sheets_parser = sub.add_parser("sheets", help="List sheets of a dbsheet file to discover sheet_id")
    sheets_parser.add_argument("--file-id")
    fields_parser = sub.add_parser("fields", help="Verify the 17 business and 3 technical fields of a sheet")
    fields_parser.add_argument("--file-id")
    fields_parser.add_argument("--sheet-id", type=int)
    lookup_parser = sub.add_parser("lookup", help="Look up one _sync_key via list_by_page, as reconciliation does")
    lookup_parser.add_argument("--file-id")
    lookup_parser.add_argument("--sheet-id", type=int)
    lookup_parser.add_argument("--sync-key", required=True)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not _require_credentials(settings):
        return 2

    if args.command == "token":
        client = WpsOpenApiClient(settings)
        try:
            token = client._token()
        except Exception as exc:
            _emit({"status": "TOKEN_FAILED", "error": str(exc)[:500]})
            return 1
        _emit({"status": "TOKEN_OK", "token_length": len(token), "expires_at": client.cache.expires_at.isoformat()})
        return 0

    if args.command == "sheets":
        client = _client_for(settings, args.file_id, None)
        if client is None:
            return 2
        try:
            schema = client.get_schema()
        except Exception as exc:
            _emit({"status": "SCHEMA_FAILED", "error": str(exc)[:500]})
            return 1
        _emit({"status": "OK", "sheets": [{"id": s.sheet_id, "name": s.name, "field_count": len(s.fields)} for s in schema]})
        return 0

    if args.command == "fields":
        client = _client_for(settings, args.file_id, args.sheet_id)
        if client is None:
            return 2
        try:
            schema = client.get_schema()
        except Exception as exc:
            _emit({"status": "SCHEMA_FAILED", "error": str(exc)[:500]})
            return 1
        wanted = args.sheet_id
        candidates = [s for s in schema if s.sheet_id == wanted] if wanted is not None else schema
        if not candidates:
            _emit({"status": "SHEET_NOT_FOUND", "sheet_id": wanted, "available": [s.sheet_id for s in schema]})
            return 1
        if len(candidates) > 1:
            _emit({"status": "AMBIGUOUS", "sheets": [s.sheet_id for s in candidates], "hint": "pass --sheet-id"})
            return 1
        report = build_field_report(candidates[0])
        _emit({"status": "OK" if report["ready"] else "NOT_READY", **report})
        return 0 if report["ready"] else 1

    if args.command == "lookup":
        client = _client_for(settings, args.file_id, args.sheet_id)
        if client is None:
            return 2
        if not client.settings.wps_sheet_id:
            _emit({"status": "BLOCKED", "error": "sheet_id is required via --sheet-id or WPS_SHEET_ID"})
            return 2
        try:
            found = client.find_records_by_sync_keys([args.sync_key])
        except Exception as exc:
            _emit({"status": "LOOKUP_FAILED", "error": str(exc)[:500]})
            return 1
        matches = found.get(args.sync_key, [])
        _emit({
            "status": "OK",
            "sync_key": args.sync_key,
            "count": len(matches),
            "records": [{"record_id": m.record_id, **m.fields} for m in matches],
        })
        return 0

    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())

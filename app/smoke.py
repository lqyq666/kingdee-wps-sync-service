"""Read-only WPS/kdocs smoke checks for staged real integration.

Performs no create/update calls: token/user, schema (sheets/fields), a single
_sync_key lookup, and the one-time kdocs OAuth bootstrap (kdocs-auth writes the
tokens it obtains into the local .env; it never prints token values) so the
first write against a tenant table happens only after these checks pass.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.integrations import (
    KdocsOpenApiClient,
    WpsOpenApiClient,
    WpsSheetSchema,
    kdocs_authorize_url,
    kdocs_exchange_code,
    kdocs_refresh_access_token,
)
from app.mapping import FIELD_MAPPINGS, TECHNICAL_WPS_FIELDS

# WPS 365 text fields are MultiLineText/Text; kdocs text fields are MultiLineText/SingleLineText.
TEXT_FIELD_TYPES = frozenset({"MultiLineText", "Text", "SingleLineText"})
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
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _require_credentials(settings: Settings, command: str) -> bool:
    if command == "kdocs-auth":
        required = ("kdocs_app_id", "kdocs_app_key")
    elif command == "kdocs-refresh":
        required = ("kdocs_app_id", "kdocs_app_key", "kdocs_refresh_token")
    elif command in {"kdocs-user", "kdocs-files"}:
        required = ("kdocs_access_token",)
    elif settings.wps_provider == "kdocs":
        required = ("kdocs_access_token",)
    else:
        required = ("wps_app_id", "wps_app_secret")
    missing = [name for name in required if not getattr(settings, name)]
    if missing:
        _emit({"status": "BLOCKED", "error": "This check requires " + ", ".join(name.upper() for name in missing)})
        return False
    return True


def _resolve_target(value: str | None, fallback: str, name: str) -> str | None:
    resolved = value.strip() if value else ""
    if resolved:
        return resolved
    if fallback:
        return fallback
    _emit({"status": "BLOCKED", "error": f"{name} is required via --{name.replace('_', '-')} or the environment settings"})
    return None


def _client_for(
    settings: Settings,
    file_id: str | None,
    sheet_id: int | None,
) -> WpsOpenApiClient | KdocsOpenApiClient | None:
    if settings.wps_provider == "kdocs":
        resolved_file = _resolve_target(file_id, settings.kdocs_file_token, "file_id")
        if resolved_file is None:
            return None
        resolved_sheet = str(sheet_id) if sheet_id is not None else settings.kdocs_sheet_id
        overrides: dict[str, str] = {"kdocs_file_token": resolved_file}
        if resolved_sheet:
            overrides["kdocs_sheet_id"] = resolved_sheet
        return KdocsOpenApiClient(replace(settings, **overrides))

    resolved_file = _resolve_target(file_id, settings.wps_file_id, "file_id")
    if resolved_file is None:
        return None
    resolved_sheet = str(sheet_id) if sheet_id is not None else settings.wps_sheet_id
    overrides = {"wps_file_id": resolved_file or ""}
    if resolved_sheet:
        overrides["wps_sheet_id"] = resolved_sheet
    return WpsOpenApiClient(replace(settings, **overrides))


def upsert_env_values(path: Path, updates: dict[str, str]) -> None:
    """Insert or replace KEY= lines in a .env file so tokens never pass through the chat."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for name, value in updates.items():
        pattern = re.compile(rf"^{re.escape(name)}=.*$")
        if any(pattern.match(line) for line in lines):
            lines = [f"{name}={value}" if pattern.match(line) else line for line in lines]
        else:
            lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _await_oauth_callback(redirect_uri: str, state: str, timeout_seconds: int = 300) -> str | None:
    """Capture the OAuth code on a one-shot localhost HTTP server."""
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or (parsed.hostname or "") not in {"127.0.0.1", "localhost"}:
        return None
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server handler API
            query = parse_qs(urlparse(self.path).query)
            if query.get("code"):
                captured["code"] = query["code"][0]
                captured["state"] = query.get("state", [""])[0]
                body = "<html><body><p>授权成功，请回到终端继续。</p></body></html>".encode("utf-8")
            else:
                captured["error"] = query.get("error", ["missing code"])[0]
                body = "<html><body><p>授权失败，请回到终端查看原因。</p></body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep the default request logging off stdout
            pass

    deadline = time.monotonic() + timeout_seconds
    server = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 80), Handler)
    server.timeout = 1
    try:
        while not captured and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in captured:
        raise ValueError(f"authorization callback reported error: {captured['error']}")
    if captured.get("state") != state:
        raise ValueError("authorization callback state mismatch")
    return captured.get("code")


def _run_kdocs_auth(settings: Settings, code: str | None, env_file: str) -> int:
    try:
        if code:
            tokens = kdocs_exchange_code(settings, code)
        else:
            state = secrets.token_hex(16)
            _emit({
                "status": "AUTH_URL",
                "url": kdocs_authorize_url(settings, state),
                "redirect_uri": settings.kdocs_redirect_uri,
                "hint": "在浏览器打开该链接并用『目标文件所属的 WPS 账号』授权；等待回调中…",
            })
            captured = _await_oauth_callback(settings.kdocs_redirect_uri, state)
            if not captured:
                _emit({
                    "status": "BLOCKED",
                    "error": "未捕获到授权回调；KDOCS_REDIRECT_URI 必须是 http://localhost:<port>/... 且已在应用后台登记，或改用 --code 手动传入",
                })
                return 2
            tokens = kdocs_exchange_code(settings, captured)
    except Exception as exc:
        _emit({"status": "AUTH_FAILED", "error": str(exc)[:500]})
        return 1
    path = Path(env_file)
    updates = {"KDOCS_ACCESS_TOKEN": tokens["access_token"]}
    if tokens["refresh_token"]:
        updates["KDOCS_REFRESH_TOKEN"] = tokens["refresh_token"]
    upsert_env_values(path, updates)
    _emit({
        "status": "AUTH_OK",
        "env_file": str(path.resolve()),
        "access_token_length": len(tokens["access_token"]),
        "refresh_token_written": bool(tokens["refresh_token"]),
        "hint": "access_token 有效期 24 小时；过期后运行 kdocs-refresh（refresh_token 90 天有效）",
    })
    return 0


def _run_kdocs_refresh(settings: Settings, env_file: str) -> int:
    try:
        tokens = kdocs_refresh_access_token(settings)
    except Exception as exc:
        _emit({"status": "REFRESH_FAILED", "error": str(exc)[:500]})
        return 1
    path = Path(env_file)
    updates = {"KDOCS_ACCESS_TOKEN": tokens["access_token"]}
    if tokens["refresh_token"]:
        updates["KDOCS_REFRESH_TOKEN"] = tokens["refresh_token"]
    upsert_env_values(path, updates)
    _emit({"status": "REFRESH_OK", "env_file": str(path.resolve()), "access_token_length": len(tokens["access_token"])})
    return 0


def _file_summary(file: dict[str, object]) -> dict[str, object]:
    ident = next((file[key] for key in ("id", "file_id", "fid") if file.get(key)), None)
    name = next((file[key] for key in ("name", "fname", "title") if file.get(key)), None)
    return {"id": ident, "name": name}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.smoke", description="Run read-only WPS/kdocs integration checks")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("token", help="(wps365) Verify the self-built app can obtain an access token")
    sheets_parser = sub.add_parser("sheets", help="List sheets of the target file to discover sheet_id")
    sheets_parser.add_argument("--file-id")
    fields_parser = sub.add_parser("fields", help="Verify the 17 business and 3 technical fields of a sheet")
    fields_parser.add_argument("--file-id")
    fields_parser.add_argument("--sheet-id", type=int)
    lookup_parser = sub.add_parser("lookup", help="Look up one _sync_key exactly as reconciliation does")
    lookup_parser.add_argument("--file-id")
    lookup_parser.add_argument("--sheet-id", type=int)
    lookup_parser.add_argument("--sync-key", required=True)
    auth_parser = sub.add_parser("kdocs-auth", help="One-time kdocs user OAuth bootstrap; writes tokens to .env")
    auth_parser.add_argument("--code", help="Authorization code captured manually (skips the local callback server)")
    auth_parser.add_argument("--env-file", default=".env")
    refresh_parser = sub.add_parser("kdocs-refresh", help="Refresh the kdocs access_token via refresh_token")
    refresh_parser.add_argument("--env-file", default=".env")
    sub.add_parser("kdocs-user", help="(kdocs) Validate the access_token via user/basic")
    sub.add_parser("kdocs-files", help="(kdocs) List personal files to discover file tokens")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not _require_credentials(settings, args.command):
        return 2

    if args.command == "token":
        if settings.wps_provider == "kdocs":
            _emit({"status": "BLOCKED", "error": "WPS_PROVIDER=kdocs has no client-credential token; run kdocs-user instead"})
            return 2
        client = WpsOpenApiClient(settings)
        try:
            token = client._token()
        except Exception as exc:
            _emit({"status": "TOKEN_FAILED", "error": str(exc)[:500]})
            return 1
        _emit({"status": "TOKEN_OK", "token_length": len(token), "expires_at": client.cache.expires_at.isoformat()})
        return 0

    if args.command == "kdocs-auth":
        return _run_kdocs_auth(settings, args.code, args.env_file)

    if args.command == "kdocs-refresh":
        return _run_kdocs_refresh(settings, args.env_file)

    if args.command == "kdocs-user":
        try:
            info = KdocsOpenApiClient(settings).get_user_basic()
        except Exception as exc:
            _emit({"status": "TOKEN_FAILED", "error": str(exc)[:500]})
            return 1
        _emit({"status": "TOKEN_OK", "user_id": str(info.get("id", "")), "user_name": str(info.get("name") or info.get("nickname") or "")})
        return 0

    if args.command == "kdocs-files":
        try:
            files = KdocsOpenApiClient(settings).list_personal_files()
        except Exception as exc:
            _emit({"status": "FILES_FAILED", "error": str(exc)[:500]})
            return 1
        _emit({"status": "OK", "files": [_file_summary(file) for file in files]})
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
        sheet_setting = client.settings.kdocs_sheet_id if settings.wps_provider == "kdocs" else client.settings.wps_sheet_id
        if not sheet_setting:
            _emit({"status": "BLOCKED", "error": "sheet_id is required via --sheet-id or the environment settings"})
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

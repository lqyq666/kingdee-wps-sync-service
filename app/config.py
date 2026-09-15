"""Environment-backed runtime configuration and fail-fast validation."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Raised before a real integration is allowed to make requests."""


def _value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    value = _value(name, str(default))
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {value!r}") from exc


def _float(name: str, default: float) -> float:
    value = _value(name, str(default))
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {value!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    value = _value(name, str(default)).lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ConfigurationError(f"{name} must be true or false, got {value!r}")


@dataclass(frozen=True)
class Settings:
    database_url: str
    kingdee_mode: str
    wps_mode: str
    app_host: str
    app_port: int
    log_level: str
    sync_batch_size: int
    sync_overlap_minutes: int
    sync_lock_ttl_seconds: int
    sync_max_attempts: int
    sync_retry_base_seconds: float
    alert_webhook_url: str
    kingdee_base_url: str
    kingdee_app_id: str
    kingdee_app_secret: str
    kingdee_form_id: str
    kingdee_sync_url: str
    kingdee_source_id_field: str
    kingdee_source_modified_at_field: str
    kingdee_kso_secret: str
    wps_base_url: str
    wps_app_id: str
    wps_app_secret: str
    wps_file_id: str
    wps_sheet_id: str
    wps_token_url: str
    wps_kso_signing_enabled: bool
    wps_kso_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=_value("DATABASE_URL", "postgresql+psycopg://sync_user@postgres:5432/kingdee_wps_sync"),
            kingdee_mode=_value("KINGDEE_MODE", "mock").lower(),
            wps_mode=_value("WPS_MODE", "mock").lower(),
            app_host=_value("APP_HOST", "0.0.0.0"),
            app_port=_int("APP_PORT", 8080),
            log_level=_value("LOG_LEVEL", "INFO").upper(),
            sync_batch_size=_int("SYNC_BATCH_SIZE", 100),
            sync_overlap_minutes=_int("SYNC_OVERLAP_MINUTES", 15),
            sync_lock_ttl_seconds=_int("SYNC_LOCK_TTL_SECONDS", 900),
            sync_max_attempts=_int("SYNC_MAX_ATTEMPTS", 3),
            sync_retry_base_seconds=_float("SYNC_RETRY_BASE_SECONDS", 0.25),
            alert_webhook_url=_value("ALERT_WEBHOOK_URL"),
            kingdee_base_url=_value("KINGDEE_BASE_URL"),
            kingdee_app_id=_value("KINGDEE_APP_ID"),
            kingdee_app_secret=_value("KINGDEE_APP_SECRET"),
            kingdee_form_id=_value("KINGDEE_FORM_ID"),
            kingdee_sync_url=_value("KINGDEE_SYNC_URL"),
            kingdee_source_id_field=_value("KINGDEE_SOURCE_ID_FIELD"),
            kingdee_source_modified_at_field=_value("KINGDEE_SOURCE_MODIFIED_AT_FIELD"),
            kingdee_kso_secret=_value("KINGDEE_KSO_SECRET"),
            wps_base_url=_value("WPS_BASE_URL", "https://openapi.wps.cn"),
            wps_app_id=_value("WPS_APP_ID"),
            wps_app_secret=_value("WPS_APP_SECRET"),
            wps_file_id=_value("WPS_FILE_ID"),
            wps_sheet_id=_value("WPS_SHEET_ID"),
            wps_token_url=_value("WPS_TOKEN_URL", "https://openapi.wps.cn/oauth2/token"),
            wps_kso_signing_enabled=_bool("WPS_KSO_SIGNING_ENABLED", False),
            wps_kso_secret=_value("WPS_KSO_SECRET"),
        )


def _missing(settings: Settings, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if not getattr(settings, name.lower())]


def validate_runtime_configuration(settings: Settings) -> None:
    """Ensure a non-mock run cannot silently use incomplete tenant settings."""
    if settings.kingdee_mode not in {"mock", "real"}:
        raise ConfigurationError("KINGDEE_MODE must be 'mock' or 'real'")
    if settings.wps_mode not in {"mock", "real"}:
        raise ConfigurationError("WPS_MODE must be 'mock' or 'real'")

    missing: list[str] = []
    if settings.kingdee_mode == "real":
        missing.extend(_missing(settings, (
            "kingdee_base_url", "kingdee_app_id", "kingdee_app_secret", "kingdee_form_id",
            "kingdee_sync_url", "kingdee_source_id_field", "kingdee_source_modified_at_field",
        )))
    if settings.wps_mode == "real":
        missing.extend(_missing(settings, (
            "wps_app_id", "wps_app_secret", "wps_file_id", "wps_sheet_id",
        )))
    if missing:
        variables = ", ".join(name.upper() for name in missing)
        raise ConfigurationError(
            "Real integration is BLOCKED / REQUIRES REAL CREDENTIALS and metadata. "
            f"Missing: {variables}"
        )

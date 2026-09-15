from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import ConfigurationError, Settings, validate_runtime_configuration


def test_blank_environment_values_fall_back_to_official_defaults(monkeypatch):
    monkeypatch.setenv("WPS_BASE_URL", "")
    monkeypatch.setenv("WPS_TOKEN_URL", "   ")
    monkeypatch.setenv("SYNC_BATCH_SIZE", "")

    settings = Settings.from_env()

    assert settings.wps_base_url == "https://openapi.wps.cn"
    assert settings.wps_token_url == "https://openapi.wps.cn/oauth2/token"
    assert settings.sync_batch_size == 100


def test_real_kingdee_mode_reports_all_required_missing_items(settings):
    with pytest.raises(ConfigurationError, match="KINGDEE_BASE_URL") as error:
        validate_runtime_configuration(replace(settings, kingdee_mode="real"))
    assert "KINGDEE_SOURCE_MODIFIED_AT_FIELD" in str(error.value)


def test_real_wps_mode_reports_all_required_missing_items(settings):
    with pytest.raises(ConfigurationError, match="WPS_APP_ID") as error:
        validate_runtime_configuration(replace(settings, wps_mode="real"))
    assert "WPS_SHEET_ID" in str(error.value)


def test_mock_modes_are_valid_without_real_credentials(settings):
    validate_runtime_configuration(settings)


def test_real_wps_mode_accepts_the_documented_required_configuration(settings):
    validate_runtime_configuration(
        replace(
            settings,
            wps_mode="real",
            wps_app_id="AK-test",
            wps_app_secret="test-secret",
            wps_file_id="file-id",
            wps_sheet_id="sheet-id",
        )
    )

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import ConfigurationError, validate_runtime_configuration


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

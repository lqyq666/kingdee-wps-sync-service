from __future__ import annotations

from app.integrations import WpsFieldSchema, WpsSheetSchema
from app.mapping import FIELD_MAPPINGS, TECHNICAL_WPS_FIELDS
from app.smoke import build_field_report, main


def _sheet(fields: list[tuple[str, str]]) -> WpsSheetSchema:
    return WpsSheetSchema(7, "销售数据详情表", [WpsFieldSchema(name, field_type, None) for name, field_type in fields])


def _complete_fields(business_type: str = "Number", technical_type: str = "MultiLineText") -> list[tuple[str, str]]:
    return (
        [(definition.wps_field, business_type) for definition in FIELD_MAPPINGS]
        + [(name, technical_type) for name in TECHNICAL_WPS_FIELDS]
    )


def test_field_report_is_ready_when_all_20_fields_exist_with_text_technical_fields():
    report = build_field_report(_sheet(_complete_fields()))
    assert report["ready"] is True
    assert report["missing"] == [] and report["technical_type_problems"] == []
    assert len(report["expected_field_types"]) == 20


def test_field_report_lists_every_missing_field():
    report = build_field_report(_sheet([("_sync_key", "MultiLineText")]))
    assert report["ready"] is False
    assert "日期" in report["missing"] and "_sync_hash" in report["missing"]
    assert report["technical_type_problems"] == []


def test_field_report_requires_text_types_for_technical_fields_only():
    report = build_field_report(_sheet(_complete_fields(technical_type="Number")))
    assert report["ready"] is False
    assert report["technical_type_problems"] == ["_sync_key", "_source_modified_at", "_sync_hash"]
    assert report["missing"] == []


def test_field_report_ignores_extra_dashboard_columns():
    report = build_field_report(_sheet([*(_complete_fields()), ("驾驶舱辅助列", "Number")]))
    assert report["ready"] is True


def test_smoke_cli_is_blocked_without_real_credentials(monkeypatch, capsys):
    monkeypatch.setenv("WPS_APP_ID", "")
    monkeypatch.setenv("WPS_APP_SECRET", "")
    for command in (["token"], ["sheets", "--file-id", "f"], ["lookup", "--file-id", "f", "--sheet-id", "1", "--sync-key", "k"]):
        assert main(command) == 2
    outputs = capsys.readouterr().out.splitlines()
    assert len(outputs) == 3 and all("BLOCKED" in line and "WPS_APP_ID" in line for line in outputs)


def test_smoke_cli_reports_missing_file_id(monkeypatch, capsys):
    monkeypatch.setenv("WPS_APP_ID", "AK-test")
    monkeypatch.setenv("WPS_APP_SECRET", "test-secret")
    monkeypatch.setenv("WPS_FILE_ID", "")
    monkeypatch.setenv("WPS_SHEET_ID", "")
    assert main(["sheets"]) == 2
    assert "file_id" in capsys.readouterr().out

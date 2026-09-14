"""Explicit target schema. Real source fields stay deliberately unset."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldDefinition:
    wps_field: str
    real_source_field: str | None
    demo_source_field: str


FIELD_MAPPINGS: tuple[FieldDefinition, ...] = (
    FieldDefinition("日期", None, "date"),
    FieldDefinition("单据编号", None, "document_number"),
    FieldDefinition("客户", None, "customer"),
    FieldDefinition("单据状态", None, "document_status"),
    FieldDefinition("物料名称", None, "material_name"),
    FieldDefinition("实发数量", None, "delivered_quantity"),
    FieldDefinition("仓库", None, "warehouse"),
    FieldDefinition("产品类别", None, "product_category"),
    FieldDefinition("销售品类", None, "sales_category"),
    FieldDefinition("品牌", None, "brand"),
    FieldDefinition("县城", None, "county"),
    FieldDefinition("大区经理", None, "regional_manager"),
    FieldDefinition("客户经理", None, "account_manager"),
    FieldDefinition("大区", None, "region"),
    FieldDefinition("出厂价", None, "factory_price"),
    FieldDefinition("含税单价", None, "tax_inclusive_unit_price"),
    FieldDefinition("销售单位", None, "sales_unit"),
)

TECHNICAL_WPS_FIELDS = ("_sync_key", "_source_modified_at", "_sync_hash")


def map_demo_source(source: dict[str, object]) -> dict[str, object]:
    """Map deterministic demo source names to exactly the 17 business fields."""
    return {definition.wps_field: source.get(definition.demo_source_field) for definition in FIELD_MAPPINGS}


def map_real_source(source: dict[str, object]) -> dict[str, object]:
    """Real fields are intentionally not guessed before tenant metadata is supplied."""
    unresolved = [definition.wps_field for definition in FIELD_MAPPINGS if not definition.real_source_field]
    if unresolved:
        raise ValueError(
            "Real field mapping is BLOCKED / REQUIRES REAL CREDENTIALS and field metadata. "
            f"Unmapped WPS fields: {', '.join(unresolved)}"
        )
    return {definition.wps_field: source[definition.real_source_field] for definition in FIELD_MAPPINGS}

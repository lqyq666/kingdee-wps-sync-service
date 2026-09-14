from __future__ import annotations

import pytest

from app.hashing import content_hash
from app.integrations import MOCK_SALES_DETAILS
from app.mapping import FIELD_MAPPINGS, TECHNICAL_WPS_FIELDS, map_demo_source, map_real_source


def test_demo_mapping_has_exactly_17_required_wps_business_fields():
    fields = map_demo_source(MOCK_SALES_DETAILS[0])
    assert len(FIELD_MAPPINGS) == 17
    assert list(fields) == [definition.wps_field for definition in FIELD_MAPPINGS]
    assert all(definition.real_source_field is None for definition in FIELD_MAPPINGS)
    assert TECHNICAL_WPS_FIELDS == ("_sync_key", "_source_modified_at", "_sync_hash")


def test_real_mapping_refuses_to_guess_source_metadata():
    with pytest.raises(ValueError, match="BLOCKED"):
        map_real_source({})


def test_content_hash_is_stable_across_dictionary_order():
    assert content_hash({"客户": "示例客户", "数量": 3}) == content_hash({"数量": 3, "客户": "示例客户"})

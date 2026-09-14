from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings
from app.database import create_session_factory
from app.integrations import MockKingdeeClient, MockWpsClient
from app.service import SyncEngine


@pytest.fixture
def settings(tmp_path):
    return replace(
        Settings.from_env(),
        database_url=f"sqlite+pysqlite:///{tmp_path / 'sync.db'}",
        kingdee_mode="mock",
        wps_mode="mock",
        sync_retry_base_seconds=0,
    )


@pytest.fixture
def sessions(settings):
    return create_session_factory(settings.database_url)


@pytest.fixture
def engine(settings, sessions):
    return SyncEngine(settings, sessions, MockKingdeeClient(), MockWpsClient())

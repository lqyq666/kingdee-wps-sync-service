from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.integrations import MOCK_SALES_DETAILS, MockKingdeeClient, MockWpsClient, WpsRemoteRecord
from app.models import DeadLetter, SyncRecord
from app.service import SyncEngine

FIRST_KEY = f"{MOCK_SALES_DETAILS[0]['source_document_id']}:{MOCK_SALES_DETAILS[0]['source_line_id']}"


class CrashAfterRemoteWrite:
    """WPS accepted the batch, then the process died before the local transaction committed."""

    def __init__(self, remote: MockWpsClient):
        self.remote = remote

    def create_records(self, records):
        self.remote.create_records(records)
        raise RuntimeError("process crashed before local commit")

    def update_records(self, records):
        return self.remote.update_records(records)

    def find_records_by_sync_keys(self, sync_keys):
        return self.remote.find_records_by_sync_keys(sync_keys)


def _crash_after_remote_create(settings, sessions, remote: MockWpsClient) -> None:
    crashed = SyncEngine(replace(settings, sync_max_attempts=1), sessions, MockKingdeeClient(), CrashAfterRemoteWrite(remote))
    assert crashed.run().status == "FAILED"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == 0
        assert session.scalar(select(func.count()).select_from(DeadLetter)) == len(MOCK_SALES_DETAILS)
    assert len(remote.store) == len(MOCK_SALES_DETAILS)
    assert all(len(rows) == 1 for rows in remote.store.values())


def _seed_local_record_without_remote_id(sessions) -> None:
    with sessions() as session:
        session.add(SyncRecord(
            sync_key=FIRST_KEY,
            remote_record_id=None,
            source_modified_at=datetime(2026, 1, 1, tzinfo=UTC),
            content_hash="outdated",
            synced_at=datetime(2026, 1, 1, tzinfo=UTC),
        ))
        session.commit()


def test_rows_created_remotely_before_a_crash_are_adopted_instead_of_duplicated(settings, sessions):
    remote = MockWpsClient()
    _crash_after_remote_create(settings, sessions, remote)

    result = SyncEngine(settings, sessions, MockKingdeeClient(), remote).run()

    assert result.status == "SUCCESS"
    assert result.created == 0 and result.updated == 0 and result.skipped == len(MOCK_SALES_DETAILS)
    assert all(len(rows) == 1 for rows in remote.store.values())
    with sessions() as session:
        records = list(session.scalars(select(SyncRecord)))
        assert len(records) == len(MOCK_SALES_DETAILS)
        assert {record.remote_record_id for record in records} == {rows[0].record_id for rows in remote.store.values()}


def test_adopted_remote_row_with_different_content_is_updated_in_place(settings, sessions):
    remote = MockWpsClient({FIRST_KEY: [WpsRemoteRecord("rec-old", {"_sync_key": FIRST_KEY, "_sync_hash": "stale-hash"})]})

    result = SyncEngine(settings, sessions, MockKingdeeClient([MOCK_SALES_DETAILS[0]]), remote).run()

    assert result.status == "SUCCESS"
    assert result.created == 0 and result.updated == 1 and result.skipped == 0
    assert [row.record_id for row in remote.store[FIRST_KEY]] == ["rec-old"]
    with sessions() as session:
        record = session.scalar(select(SyncRecord).where(SyncRecord.sync_key == FIRST_KEY))
        assert record is not None and record.remote_record_id == "rec-old"
        assert record.content_hash == remote.store[FIRST_KEY][0].fields["_sync_hash"] != "stale-hash"


def test_duplicate_remote_sync_keys_are_dead_lettered_without_writing(settings, sessions):
    duplicate = {"_sync_key": FIRST_KEY, "_sync_hash": "x"}
    remote = MockWpsClient({FIRST_KEY: [WpsRemoteRecord("rec-a", duplicate), WpsRemoteRecord("rec-b", duplicate)]})

    result = SyncEngine(settings, sessions, MockKingdeeClient(), remote).run()

    assert result.status == "SUCCESS"
    assert result.created == len(MOCK_SALES_DETAILS) - 1
    assert [row.record_id for row in remote.store[FIRST_KEY]] == ["rec-a", "rec-b"]
    with sessions() as session:
        entry = session.scalar(select(DeadLetter))
        assert entry is not None and entry.sync_key == FIRST_KEY and entry.status == "PENDING"
        assert entry.reason.startswith("remote_reconciliation_conflict")
        assert session.scalar(select(SyncRecord).where(SyncRecord.sync_key == FIRST_KEY)) is None


def test_local_record_without_remote_id_reuses_the_remote_row_when_present(settings, sessions):
    _seed_local_record_without_remote_id(sessions)
    remote = MockWpsClient({FIRST_KEY: [WpsRemoteRecord("rec-found", {"_sync_key": FIRST_KEY, "_sync_hash": "outdated"})]})

    result = SyncEngine(settings, sessions, MockKingdeeClient([MOCK_SALES_DETAILS[0]]), remote).run()

    assert result.status == "SUCCESS" and result.created == 0 and result.updated == 1
    assert [row.record_id for row in remote.store[FIRST_KEY]] == ["rec-found"]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == 1
        assert session.scalar(select(SyncRecord.remote_record_id)) == "rec-found"


def test_local_record_without_remote_id_is_recreated_when_remote_row_is_missing(settings, sessions):
    _seed_local_record_without_remote_id(sessions)
    remote = MockWpsClient()

    result = SyncEngine(settings, sessions, MockKingdeeClient([MOCK_SALES_DETAILS[0]]), remote).run()

    assert result.status == "SUCCESS" and result.created == 1 and result.updated == 0
    assert [row.record_id for row in remote.store[FIRST_KEY]] == [f"mock-{FIRST_KEY}"]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == 1
        assert session.scalar(select(SyncRecord.remote_record_id)) == f"mock-{FIRST_KEY}"


def test_dlq_replay_adopts_rows_that_already_reached_wps(settings, sessions):
    remote = MockWpsClient()
    _crash_after_remote_create(settings, sessions, remote)

    outcome = SyncEngine(settings, sessions, MockKingdeeClient([]), remote).replay_dlq()

    assert outcome == {"replayed": len(MOCK_SALES_DETAILS), "failed": 0}
    assert all(len(rows) == 1 for rows in remote.store.values())
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == len(MOCK_SALES_DETAILS)


def test_dlq_replay_leaves_conflicting_remote_rows_pending_for_review(settings, sessions):
    remote = MockWpsClient()
    _crash_after_remote_create(settings, sessions, remote)
    remote.store[FIRST_KEY].append(WpsRemoteRecord("rec-dup", dict(remote.store[FIRST_KEY][0].fields)))

    outcome = SyncEngine(settings, sessions, MockKingdeeClient([]), remote).replay_dlq()

    assert outcome == {"replayed": len(MOCK_SALES_DETAILS) - 1, "failed": 1}
    assert [row.record_id for row in remote.store[FIRST_KEY]] == [f"mock-{FIRST_KEY}", "rec-dup"]
    with sessions() as session:
        pending = list(session.scalars(select(DeadLetter).where(DeadLetter.status == "PENDING")))
        assert [entry.sync_key for entry in pending] == [FIRST_KEY]
        assert pending[0].reason.startswith("remote_reconciliation_conflict")


def test_dry_run_never_queries_the_remote_table(settings, sessions):
    class NoLookupWps(MockWpsClient):
        def find_records_by_sync_keys(self, sync_keys):
            raise AssertionError("dry run must not call WPS")

    result = SyncEngine(settings, sessions, MockKingdeeClient(), NoLookupWps()).run(dry_run=True)

    assert result.status == "DRY_RUN" and result.created == len(MOCK_SALES_DETAILS)

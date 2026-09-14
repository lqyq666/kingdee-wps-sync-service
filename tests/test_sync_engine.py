from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select

from app.integrations import MOCK_SALES_DETAILS, MockKingdeeClient, MockWpsClient
from app.models import DeadLetter, SyncCheckpoint, SyncRecord
from app.service import STREAM_NAME, SyncEngine


def test_repeated_mock_source_is_idempotent_and_does_not_duplicate_sync_records(engine, sessions):
    first = engine.run()
    second = engine.run()

    assert first.status == "SUCCESS"
    assert first.fetched > 0 and first.created > 0
    assert second.status == "SUCCESS"
    assert second.created == 0 and second.updated == 0 and second.skipped > 0
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == len(MOCK_SALES_DETAILS)


def test_checkpoint_only_advances_after_a_successful_run(settings, sessions):
    class FailingWps:
        def create_records(self, records):
            raise RuntimeError("WPS unavailable")

        def update_records(self, records):
            raise RuntimeError("WPS unavailable")

    failing = SyncEngine(settings, sessions, MockKingdeeClient(), FailingWps())
    assert failing.run().status == "FAILED"
    with sessions() as session:
        assert session.get(SyncCheckpoint, STREAM_NAME) is None

    succeeding = SyncEngine(settings, sessions, MockKingdeeClient(), MockWpsClient())
    assert succeeding.run().status == "SUCCESS"
    with sessions() as session:
        assert session.get(SyncCheckpoint, STREAM_NAME) is not None


def test_dlq_entries_can_be_replayed(settings, sessions):
    class FailingWps:
        def create_records(self, records):
            raise RuntimeError("WPS unavailable")

        def update_records(self, records):
            raise RuntimeError("WPS unavailable")

    failed_engine = SyncEngine(settings, sessions, MockKingdeeClient([MOCK_SALES_DETAILS[0]]), FailingWps())
    assert failed_engine.run().status == "FAILED"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DeadLetter)) == 1

    recovered = SyncEngine(settings, sessions, MockKingdeeClient([]), MockWpsClient())
    assert recovered.replay_dlq() == {"replayed": 1, "failed": 0}
    with sessions() as session:
        assert session.scalar(select(DeadLetter.status)) == "REPLAYED"
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == 1


def test_only_unfinished_batches_are_added_to_dlq(settings, sessions):
    class Fails_on_second_create:
        calls = 0

        def create_records(self, records):
            self.calls += 1
            if self.calls >= 2:
                raise RuntimeError("second batch unavailable")
            return MockWpsClient().create_records(records)

        def update_records(self, records):
            return MockWpsClient().update_records(records)

    engine = SyncEngine(
        replace(settings, sync_batch_size=1), sessions, MockKingdeeClient(), Fails_on_second_create()
    )
    result = engine.run()
    assert result.status == "FAILED" and result.created == 1
    with sessions() as session:
        # The first remote batch is durable; only the remaining two records require replay.
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == 1
        assert session.scalar(select(func.count()).select_from(DeadLetter)) == 2


def test_dry_run_does_not_persist_records_or_checkpoint(engine, sessions):
    result = engine.run(dry_run=True)
    assert result.status == "DRY_RUN" and result.created > 0
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(SyncRecord)) == 0
        assert session.get(SyncCheckpoint, STREAM_NAME) is None

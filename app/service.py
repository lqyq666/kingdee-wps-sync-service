"""Incremental, idempotent, one-way synchronization orchestration."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.alerts import send_failure_alert
from app.config import Settings, validate_runtime_configuration
from app.hashing import content_hash
from app.integrations import SYNC_HASH_FIELD, KingdeeClient, WpsClient, WpsRemoteRecord, parse_timestamp
from app.mapping import map_demo_source, map_real_source
from app.metrics import DLQ_RECORDS, RECONCILED_RECORDS, SYNC_DURATION, SYNC_RECORDS, SYNC_RUNS
from app.models import DeadLetter, SyncCheckpoint, SyncRecord, SyncRun, TaskLock
from app.retry import retry_call

logger = logging.getLogger(__name__)
STREAM_NAME = "sales_detail"
LOCK_NAME = "sales_detail_sync"


class SyncLockedError(RuntimeError):
    pass


@dataclass
class SyncResult:
    status: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    run_id: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RecordOperation:
    sync_key: str
    fields: dict[str, object]
    source_modified_at: datetime
    content_hash: str
    existing: SyncRecord | None


class SyncEngine:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        kingdee: KingdeeClient,
        wps: WpsClient,
        *,
        alert_sender: Callable[[str, str], None] = send_failure_alert,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.kingdee = kingdee
        self.wps = wps
        self.alert_sender = alert_sender

    def _acquire_lock(self, session: Session) -> None:
        now = datetime.now(UTC)
        lock = session.get(TaskLock, LOCK_NAME)
        locked_until = self._as_utc(lock.locked_until) if lock else None
        if locked_until and locked_until > now:
            raise SyncLockedError(f"Sync task is already locked until {locked_until.isoformat()}")
        if lock is None:
            lock = TaskLock(name=LOCK_NAME, locked_until=now + timedelta(seconds=self.settings.sync_lock_ttl_seconds))
            session.add(lock)
        else:
            lock.locked_until = now + timedelta(seconds=self.settings.sync_lock_ttl_seconds)
        session.commit()

    def _release_lock(self, session: Session) -> None:
        lock = session.get(TaskLock, LOCK_NAME)
        if lock:
            lock.locked_until = datetime.now(UTC)
            session.commit()

    def _checkpoint(self, session: Session) -> datetime | None:
        checkpoint = session.get(SyncCheckpoint, STREAM_NAME)
        if not checkpoint:
            return None
        return self._as_utc(checkpoint.last_success_at) - timedelta(minutes=self.settings.sync_overlap_minutes)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """SQLite returns naive datetimes; PostgreSQL preserves the UTC offset."""
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _operation(self, source: dict[str, object], existing: SyncRecord | None) -> RecordOperation:
        if self.settings.kingdee_mode == "mock":
            fields = map_demo_source(source)
        else:
            fields = map_real_source(source)
        source_document_id = source.get("source_document_id")
        source_line_id = source.get("source_line_id")
        if not source_document_id or not source_line_id:
            raise ValueError("Source row must contain stable source_document_id and source_line_id")
        if not source.get("modified_at"):
            raise ValueError("Source row must contain modified_at")
        sync_key = f"{source_document_id}:{source_line_id}"
        modified_at = parse_timestamp(str(source["modified_at"]))
        field_hash = content_hash(fields)
        fields.update({
            "_sync_key": sync_key,
            "_source_modified_at": modified_at.isoformat(),
            "_sync_hash": field_hash,
        })
        return RecordOperation(sync_key, fields, modified_at, field_hash, existing)

    @staticmethod
    def _dead_letter_payload(operation: RecordOperation) -> str:
        return json.dumps({
            "fields": operation.fields,
            "source_modified_at": operation.source_modified_at.isoformat(),
            "content_hash": operation.content_hash,
        }, ensure_ascii=False, sort_keys=True)

    def _add_dead_letters(self, session: Session, operations: list[RecordOperation], reason: str) -> None:
        for operation in operations:
            session.add(DeadLetter(
                sync_key=operation.sync_key,
                payload_json=self._dead_letter_payload(operation),
                reason=reason,
                attempts=self.settings.sync_max_attempts,
                status="PENDING",
                created_at=datetime.now(UTC),
            ))
            DLQ_RECORDS.inc()

    def _persist_success(self, session: Session, operations: list[RecordOperation], remote_ids: dict[str, str]) -> None:
        now = datetime.now(UTC)
        for operation in operations:
            if operation.existing is None:
                session.add(SyncRecord(
                    sync_key=operation.sync_key,
                    remote_record_id=remote_ids[operation.sync_key],
                    source_modified_at=operation.source_modified_at,
                    content_hash=operation.content_hash,
                    synced_at=now,
                ))
            else:
                operation.existing.remote_record_id = remote_ids[operation.sync_key]
                operation.existing.source_modified_at = operation.source_modified_at
                operation.existing.content_hash = operation.content_hash
                operation.existing.synced_at = now

    def _batches(self, operations: list[RecordOperation]) -> list[list[RecordOperation]]:
        return [operations[start:start + self.settings.sync_batch_size] for start in range(0, len(operations), self.settings.sync_batch_size)]

    def _find_remote(self, sync_keys: list[str]) -> dict[str, list[WpsRemoteRecord]]:
        return retry_call(
            lambda: self.wps.find_records_by_sync_keys(sync_keys),
            attempts=self.settings.sync_max_attempts,
            base_seconds=self.settings.sync_retry_base_seconds,
        )

    @staticmethod
    def _single_remote_match(sync_key: str, matches: list[WpsRemoteRecord]) -> WpsRemoteRecord | None:
        """Return the one usable remote row, or raise for states that need manual review."""
        if len(matches) > 1:
            raise ValueError(f"remote_reconciliation_conflict: {len(matches)} WPS records share _sync_key {sync_key}")
        if matches and not matches[0].record_id:
            raise ValueError(f"remote_reconciliation_conflict: WPS record for _sync_key {sync_key} has no id")
        return matches[0] if matches else None

    def _backfill_local_record(self, session: Session, operation: RecordOperation, remote: WpsRemoteRecord) -> SyncRecord:
        # Store the hash WPS actually holds: if the follow-up update never happens, the next
        # run still sees the row as changed instead of skipping it.
        record = SyncRecord(
            sync_key=operation.sync_key,
            remote_record_id=remote.record_id,
            source_modified_at=operation.source_modified_at,
            content_hash=str(remote.fields.get(SYNC_HASH_FIELD) or ""),
            synced_at=datetime.now(UTC),
        )
        session.add(record)
        return record

    def _reconcile_remote_state(
        self,
        session: Session,
        creates: list[RecordOperation],
        updates: list[RecordOperation],
        result: SyncResult,
    ) -> tuple[list[RecordOperation], list[RecordOperation]]:
        """Query WPS by _sync_key before writing so a row created remotely but never committed
        locally is adopted instead of created twice, and a local row without a remote id is
        re-resolved or recreated."""
        unresolved_updates = [operation for operation in updates if operation.existing is not None and not operation.existing.remote_record_id]
        lookup_keys = [operation.sync_key for operation in [*creates, *unresolved_updates]]
        if not lookup_keys:
            return creates, updates
        found = self._find_remote(lookup_keys)
        unresolved_keys = {operation.sync_key for operation in unresolved_updates}
        next_creates: list[RecordOperation] = []
        next_updates: list[RecordOperation] = [operation for operation in updates if operation.sync_key not in unresolved_keys]
        counts = {"adopted_unchanged": 0, "adopted_changed": 0, "remote_id_resolved": 0, "recreated": 0, "conflicts": 0}

        for operation in creates:
            try:
                remote = self._single_remote_match(operation.sync_key, found.get(operation.sync_key, []))
            except ValueError as exc:
                self._add_dead_letters(session, [operation], str(exc))
                counts["conflicts"] += 1
                continue
            if remote is None:
                next_creates.append(operation)
                continue
            record = self._backfill_local_record(session, operation, remote)
            if record.content_hash == operation.content_hash:
                result.skipped += 1
                counts["adopted_unchanged"] += 1
            else:
                next_updates.append(replace(operation, existing=record))
                counts["adopted_changed"] += 1

        for operation in unresolved_updates:
            assert operation.existing is not None
            try:
                remote = self._single_remote_match(operation.sync_key, found.get(operation.sync_key, []))
            except ValueError as exc:
                self._add_dead_letters(session, [operation], str(exc))
                counts["conflicts"] += 1
                continue
            if remote is None:
                # The local row keeps its identity; the create batch fills in the new remote id.
                next_creates.append(operation)
                counts["recreated"] += 1
            else:
                operation.existing.remote_record_id = remote.record_id
                next_updates.append(operation)
                counts["remote_id_resolved"] += 1

        session.commit()
        for outcome, count in counts.items():
            if count:
                RECONCILED_RECORDS.labels(outcome=outcome).inc(count)
        logger.info("remote_reconciliation", extra={"event": "remote_reconciliation", **counts})
        return next_creates, next_updates

    def _mark_run(self, session: Session, run: SyncRun, result: SyncResult) -> None:
        run.finished_at = datetime.now(UTC)
        run.status = result.status
        run.fetched = result.fetched
        run.created = result.created
        run.updated = result.updated
        run.skipped = result.skipped
        run.error = result.error
        session.commit()

    def run(self, *, dry_run: bool = False) -> SyncResult:
        validate_runtime_configuration(self.settings)
        with SYNC_DURATION.time():
            with self.sessions() as session:
                self._acquire_lock(session)
                run = SyncRun(started_at=datetime.now(UTC), status="RUNNING")
                session.add(run)
                session.commit()
                result = SyncResult(status="RUNNING", run_id=run.id)
                completed_keys: set[str] = set()
                try:
                    since = self._checkpoint(session)
                    source_rows = self.kingdee.fetch_since(since)
                    result.fetched = len(source_rows)
                    # A later duplicate source row wins deterministically by modified timestamp.
                    rows_by_key: dict[str, dict[str, object]] = {}
                    for row in source_rows:
                        row_key = f"{row.get('source_document_id')}:{row.get('source_line_id')}"
                        current = rows_by_key.get(row_key)
                        if current is None or parse_timestamp(str(row["modified_at"])) >= parse_timestamp(str(current["modified_at"])):
                            rows_by_key[row_key] = row

                    creates: list[RecordOperation] = []
                    updates: list[RecordOperation] = []
                    max_modified_at: datetime | None = None
                    for source in rows_by_key.values():
                        source_key = f"{source.get('source_document_id')}:{source.get('source_line_id')}"
                        existing = session.scalar(select(SyncRecord).where(SyncRecord.sync_key == source_key))
                        operation = self._operation(source, existing)
                        max_modified_at = max(max_modified_at, operation.source_modified_at) if max_modified_at else operation.source_modified_at
                        if existing and existing.content_hash == operation.content_hash:
                            result.skipped += 1
                        elif existing:
                            updates.append(operation)
                        else:
                            creates.append(operation)

                    if dry_run:
                        result.created = len(creates)
                        result.updated = len(updates)
                        result.status = "DRY_RUN"
                        self._mark_run(session, run, result)
                        SYNC_RUNS.labels(status=result.status).inc()
                        return result

                    creates, updates = self._reconcile_remote_state(session, creates, updates, result)

                    # Commit the local state of every successful remote batch. If a later
                    # batch fails, DLQ replay contains only records not yet persisted.
                    for batch in self._batches(creates):
                        remote_ids = retry_call(
                            lambda batch=batch: self.wps.create_records([operation.fields for operation in batch]),
                            attempts=self.settings.sync_max_attempts,
                            base_seconds=self.settings.sync_retry_base_seconds,
                        )
                        self._persist_success(session, batch, remote_ids)
                        session.commit()
                        completed_keys.update(operation.sync_key for operation in batch)
                        result.created += len(batch)
                    for batch in self._batches(updates):
                        remote_ids = retry_call(
                            lambda batch=batch: self.wps.update_records([
                                (str(operation.existing.remote_record_id), operation.fields) for operation in batch
                            ]),
                            attempts=self.settings.sync_max_attempts,
                            base_seconds=self.settings.sync_retry_base_seconds,
                        )
                        self._persist_success(session, batch, remote_ids)
                        session.commit()
                        completed_keys.update(operation.sync_key for operation in batch)
                        result.updated += len(batch)
                    if max_modified_at:
                        checkpoint = session.get(SyncCheckpoint, STREAM_NAME)
                        if checkpoint is None:
                            session.add(SyncCheckpoint(stream_name=STREAM_NAME, last_success_at=max_modified_at))
                        else:
                            checkpoint.last_success_at = max_modified_at
                    result.status = "SUCCESS"
                    self._mark_run(session, run, result)
                    for action, count in (("created", result.created), ("updated", result.updated), ("skipped", result.skipped)):
                        if count:
                            SYNC_RECORDS.labels(action=action).inc(count)
                    SYNC_RUNS.labels(status=result.status).inc()
                    logger.info("sync_completed", extra={"event": "sync_completed"})
                    return result
                except Exception as exc:
                    session.rollback()
                    result.status = "FAILED"
                    result.error = str(exc)
                    # Records that reached the remote operation stage are put into DLQ for explicit replay.
                    pending = [
                        operation for operation in [*locals().get("creates", []), *locals().get("updates", [])]
                        if operation.sync_key not in completed_keys
                    ]
                    if pending:
                        self._add_dead_letters(session, pending, str(exc))
                    attached_run = session.get(SyncRun, run.id)
                    assert attached_run is not None
                    self._mark_run(session, attached_run, result)
                    SYNC_RUNS.labels(status=result.status).inc()
                    self.alert_sender(self.settings.alert_webhook_url, result.error)
                    logger.exception("sync_failed", extra={"event": "sync_failed"})
                    return result
                finally:
                    self._release_lock(session)

    def replay_dlq(self) -> dict[str, int]:
        """Replay pending DLQ entries without advancing the source checkpoint."""
        validate_runtime_configuration(self.settings)
        replayed = 0
        failed = 0
        with self.sessions() as session:
            entries = list(session.scalars(select(DeadLetter).where(DeadLetter.status == "PENDING").order_by(DeadLetter.id)))
            for entry in entries:
                try:
                    payload = json.loads(entry.payload_json)
                    fields = payload["fields"]
                    sync_key = entry.sync_key
                    modified_at = parse_timestamp(payload["source_modified_at"])
                    field_hash = payload["content_hash"]
                    existing = session.scalar(select(SyncRecord).where(SyncRecord.sync_key == sync_key))
                    remote_id = existing.remote_record_id if existing else None
                    if not remote_id:
                        # Replayed rows may already exist in WPS if the original run crashed
                        # after the remote write; a conflict leaves the entry PENDING for review.
                        remote = self._single_remote_match(sync_key, self._find_remote([sync_key]).get(sync_key, []))
                        remote_id = remote.record_id if remote else None
                    if remote_id:
                        remote_ids = retry_call(
                            lambda remote_id=remote_id: self.wps.update_records([(str(remote_id), fields)]),
                            attempts=self.settings.sync_max_attempts,
                            base_seconds=self.settings.sync_retry_base_seconds,
                        )
                    else:
                        remote_ids = retry_call(
                            lambda: self.wps.create_records([fields]),
                            attempts=self.settings.sync_max_attempts,
                            base_seconds=self.settings.sync_retry_base_seconds,
                        )
                    if existing:
                        existing.remote_record_id = remote_ids[sync_key]
                        existing.content_hash = field_hash
                        existing.source_modified_at = modified_at
                        existing.synced_at = datetime.now(UTC)
                    else:
                        session.add(SyncRecord(
                            sync_key=sync_key, remote_record_id=remote_ids[sync_key], source_modified_at=modified_at,
                            content_hash=field_hash, synced_at=datetime.now(UTC),
                        ))
                    entry.status = "REPLAYED"
                    entry.replayed_at = datetime.now(UTC)
                    replayed += 1
                except Exception as exc:
                    entry.attempts += 1
                    entry.reason = str(exc)
                    failed += 1
            session.commit()
        return {"replayed": replayed, "failed": failed}

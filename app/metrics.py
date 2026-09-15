"""Prometheus metrics intentionally limited to operationally useful counters."""

from prometheus_client import Counter, Histogram


SYNC_RUNS = Counter("kingdee_wps_sync_runs_total", "Sync runs by outcome", ["status"])
SYNC_RECORDS = Counter("kingdee_wps_sync_records_total", "Processed source records", ["action"])
SYNC_DURATION = Histogram("kingdee_wps_sync_duration_seconds", "Sync run duration")
DLQ_RECORDS = Counter("kingdee_wps_sync_dlq_records_total", "Records written to the DLQ")
RECONCILED_RECORDS = Counter(
    "kingdee_wps_sync_reconciled_records_total",
    "Remote reconciliation outcomes by _sync_key lookup",
    ["outcome"],
)

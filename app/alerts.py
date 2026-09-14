"""Best-effort webhook alerts; alert failures never conceal a sync failure."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def send_failure_alert(webhook_url: str, message: str) -> None:
    if not webhook_url:
        return
    try:
        response = httpx.post(webhook_url, json={"event": "kingdee_wps_sync_failed", "message": message}, timeout=5)
        response.raise_for_status()
    except Exception:
        logger.exception("sync_failure_alert_failed")

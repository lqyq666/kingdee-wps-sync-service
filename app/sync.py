"""CLI trigger for scheduled containers, cron jobs or manual mock validation."""

from __future__ import annotations

import argparse
import json
import sys

from app.config import ConfigurationError, Settings, validate_runtime_configuration
from app.database import create_session_factory
from app.integrations import build_kingdee_client, build_wps_client
from app.logging import configure_logging
from app.service import SyncEngine, SyncLockedError


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Kingdee to WPS synchronization")
    parser.add_argument("--dry-run", action="store_true", help="Plan a run without remote or database writes")
    args = parser.parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    try:
        # Fail before any database or remote initialization when real settings are incomplete.
        validate_runtime_configuration(settings)
        engine = SyncEngine(settings, create_session_factory(settings.database_url), build_kingdee_client(settings), build_wps_client(settings))
        result = engine.run(dry_run=args.dry_run)
    except (ConfigurationError, SyncLockedError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.status in {"SUCCESS", "DRY_RUN"} else 1


if __name__ == "__main__":
    sys.exit(main())

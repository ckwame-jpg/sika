from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.services.ingestion import seed_sports
from app.services.ml import sync_family_runtime_health
from app.services.scheduler import (
    queue_startup_refresh_if_stale,
    reconcile_stale_jobs,
    start_scheduler,
    stop_scheduler,
)

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    init_db()
    with SessionLocal() as db:
        seed_sports(db)
        sync_family_runtime_health(db)
        reconcile_stale_jobs(db)
        db.commit()
    if settings.scheduler_enabled:
        start_scheduler()
        try:
            queue_startup_refresh_if_stale()
        except Exception:
            # Bug #47 — same as main.py: don't block worker boot on a
            # transient enqueue failure, but log it so the operator
            # can see what went wrong instead of silently swallowing.
            logger.exception("Startup refresh enqueue failed; worker will continue without a fresh refresh.")

    try:
        while True:
            time.sleep(3600)
    finally:
        stop_scheduler()


if __name__ == "__main__":
    main()

from __future__ import annotations

import time

from app.database import SessionLocal, init_db
from app.services.ingestion import seed_sports
from app.services.ml import sync_family_runtime_health
from app.services.scheduler import queue_startup_refresh_if_stale, start_scheduler, stop_scheduler


def main() -> None:
    init_db()
    with SessionLocal() as db:
        seed_sports(db)
        sync_family_runtime_health(db)
        db.commit()
    start_scheduler()
    try:
        queue_startup_refresh_if_stale()
    except Exception:
        pass

    try:
        while True:
            time.sleep(3600)
    finally:
        stop_scheduler()


if __name__ == "__main__":
    main()

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.current_user import CurrentUserMiddleware
from app.api.routes import ops_router, research_router, router
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.services.ingestion import seed_sports
from app.services.ml import sync_family_runtime_health
from app.services.scheduler import (
    queue_startup_refresh_if_stale,
    reconcile_stale_jobs,
    start_scheduler,
    stop_scheduler,
    sync_refresh_runtime_state_from_db,
)
from app.services.users import seed_users_from_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        seed_sports(db)
        # Multi-user batch PR 1: seed the users table from SIKA_USERS env
        # var so the topbar dropdown has its dropdown options before the
        # first request hits.
        users_summary = seed_users_from_settings(db, settings)
        if users_summary["inserted"] or users_summary["legacy_ensured"]:
            logger.info(
                "Seeded users from SIKA_USERS: %s",
                users_summary,
            )
        sync_family_runtime_health(db)
        reconcile_stale_jobs(db)
        db.commit()
    sync_refresh_runtime_state_from_db()
    if settings.scheduler_enabled:
        start_scheduler()
        try:
            queue_startup_refresh_if_stale()
        except Exception:
            # Bug #47 — startup refresh enqueue must not block API
            # boot (the API serves cached snapshots fine without a
            # fresh refresh), but silently swallowing the exception
            # hid DB / scheduler failures from operators. Log with
            # full traceback; the rest of startup proceeds.
            logger.exception("Startup refresh enqueue failed; API will boot without a fresh refresh.")
    yield
    stop_scheduler()


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
# Multi-user batch PR 1: resolve the ``sika.userId`` cookie into a User
# row on every request. The CORSMiddleware sits OUTSIDE this so the
# preflight OPTIONS handler still runs even when the cookie is missing
# or invalid (CurrentUserMiddleware just sets state.current_user=None
# rather than rejecting; per-endpoint require_current_user dependencies
# enforce auth where needed).
app.add_middleware(CurrentUserMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.web_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(ops_router)
app.include_router(research_router)

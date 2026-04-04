from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.services.ingestion import seed_sports
from app.services.ml import sync_family_runtime_health
from app.services.scheduler import (
    queue_startup_refresh_if_stale,
    start_scheduler,
    stop_scheduler,
    sync_refresh_runtime_state_from_db,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        seed_sports(db)
        sync_family_runtime_health(db)
        db.commit()
    sync_refresh_runtime_state_from_db()
    if settings.scheduler_enabled:
        start_scheduler()
        try:
            queue_startup_refresh_if_stale()
        except Exception:
            pass
    yield
    stop_scheduler()


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.web_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings

settings = get_settings()


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+"):
        return database_url
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


resolved_database_url = normalize_database_url(settings.database_url)
is_sqlite = resolved_database_url.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
engine = create_engine(resolved_database_url, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()

SCHEMA_PATCHES: dict[str, dict[str, str]] = {
    "signal_snapshots": {
        "model_version": "VARCHAR",
        "calibration_version": "VARCHAR",
        "feature_set_version": "VARCHAR",
        "model_metadata": "JSON",
        "selection_score": "FLOAT",
        "scoring_diagnostics": "JSON",
    },
    "recommendations": {
        "model_name": "VARCHAR",
        "model_version": "VARCHAR",
        "calibration_version": "VARCHAR",
        "feature_set_version": "VARCHAR",
        "model_metadata": "JSON",
        "selection_score": "FLOAT",
        "scoring_diagnostics": "JSON",
    },
    "predictions": {
        "model_version": "VARCHAR",
        "calibration_version": "VARCHAR",
        "feature_set_version": "VARCHAR",
        "model_metadata": "JSON",
        "selection_score": "FLOAT",
        "scoring_diagnostics": "JSON",
        "capture_scope": "VARCHAR",
    },
    "parlay_recommendations": {
        "model_name": "VARCHAR",
        "model_version": "VARCHAR",
        "calibration_version": "VARCHAR",
        "feature_set_version": "VARCHAR",
        "model_metadata": "JSON",
        "selection_score": "FLOAT",
        "scoring_diagnostics": "JSON",
    },
    "parlay_predictions": {
        "model_name": "VARCHAR",
        "model_version": "VARCHAR",
        "calibration_version": "VARCHAR",
        "feature_set_version": "VARCHAR",
        "model_metadata": "JSON",
        "selection_score": "FLOAT",
        "scoring_diagnostics": "JSON",
    },
    "model_family_runtime_health": {
        "last_error_at": "DATETIME",
        "degraded_until": "DATETIME",
    },
    "refresh_jobs": {
        "details": "JSON",
    },
}


if is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout = 60000")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.close()


def get_db() -> Generator:
    """Yield a request-scoped session.

    API request handlers are read-heavy, so keeping SQLite in a deferred
    transaction state avoids unnecessary writer contention with the refresh job.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_runtime_schema()
    _ensure_performance_indexes()


_PERFORMANCE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_market_snapshot_market_captured ON market_snapshots(market_id, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_recommendation_market_captured ON recommendations(market_id, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_market_captured ON predictions(market_id, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_unsettled_lookup ON predictions(settlement_status, sport_key, capture_scope, ticker, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_coverage_daily_lookup ON predictions(market_id, capture_scope, captured_at DESC)",
]


def _ensure_performance_indexes() -> None:
    with engine.begin() as conn:
        for ddl in _PERFORMANCE_INDEXES:
            conn.execute(text(ddl))


def _ensure_runtime_schema() -> None:
    with engine.begin() as conn:
        inspector = inspect(conn)
        table_names = set(inspector.get_table_names())
        for table_name, patches in SCHEMA_PATCHES.items():
            if table_name not in table_names:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, ddl in patches.items():
                if column_name in existing_columns:
                    continue
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))

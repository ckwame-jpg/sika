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
        # Smarter #3 — closing-line value snapshot + signed delta. Backfilled
        # at settlement time; old rows stay NULL until the next settlement
        # pass picks them up.
        "closing_yes_price": "FLOAT",
        "closing_line_value": "FLOAT",
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
        "promotion_mode": "VARCHAR",
        "promotion_stability_days": "INTEGER",
        "promotion_baseline_brier": "FLOAT",
        "promotion_metrics": "JSON",
        "promotion_updated_at": "TIMESTAMP WITH TIME ZONE",
    },
    "shadow_inferences": {
        "source_prediction_id": "INTEGER",
    },
    "shadow_parlay_inferences": {
        "source_parlay_prediction_id": "INTEGER",
    },
    "refresh_jobs": {
        "details": "JSON",
    },
    "markets": {
        # Bug #17 — surface fuzzy-mapping confidence + candidates plus
        # the manual-override stamp so ops can review ambiguous Kalshi
        # ticker → event mappings.
        "mapping_confidence": "FLOAT",
        "mapping_candidates": "JSON",
        "mapping_overridden_at": "TIMESTAMP WITH TIME ZONE",
        "mapping_overridden_reason": "TEXT",
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
    _drop_legacy_current_slate_scope_uniqueness()
    _ensure_performance_indexes()


_PERFORMANCE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_market_snapshot_market_captured ON market_snapshots(market_id, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_recommendation_market_captured ON recommendations(market_id, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_market_captured ON predictions(market_id, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_unsettled_lookup ON predictions(settlement_status, sport_key, capture_scope, ticker, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_prediction_coverage_daily_lookup ON predictions(market_id, capture_scope, captured_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_shadow_inference_source_prediction ON shadow_inferences(source_prediction_id)",
    "CREATE INDEX IF NOT EXISTS ix_shadow_parlay_inference_source_prediction ON shadow_parlay_inferences(source_parlay_prediction_id)",
    # Slice 2: the snapshot store is append-only per scope, queried by latest
    # generated_at. This composite supports ``WHERE scope = ? ORDER BY generated_at DESC``.
    "CREATE INDEX IF NOT EXISTS ix_current_slate_snapshots_scope_generated_at ON current_slate_snapshots(scope, generated_at)",
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


def _drop_legacy_current_slate_scope_uniqueness() -> None:
    """Slice 2: the ``current_slate_snapshots`` table was originally keyed by
    ``scope`` alone (``unique=True``) and mutated in place on every refresh.
    v2 makes the table append-only so the latest payload is always visible
    even when a write fails mid-phase. For DBs that predate this change the
    legacy unique constraint/index must be dropped here — ``create_all`` is
    a no-op on existing tables and won't remove it on its own.

    Fresh DBs: ``Base.metadata.create_all`` already creates the new shape
    (no unique on scope), so this function is a no-op for them.
    """
    table_name = "current_slate_snapshots"
    with engine.begin() as conn:
        inspector = inspect(conn)
        if table_name not in inspector.get_table_names():
            return

        legacy_constraint_on_scope = False

        # Postgres-style: column-level ``unique=True`` creates a named
        # ``UniqueConstraint``. Drop it by name.
        for constraint in inspector.get_unique_constraints(table_name):
            cols = list(constraint.get("column_names") or [])
            if cols != ["scope"]:
                continue
            legacy_constraint_on_scope = True
            name = constraint.get("name")
            if name and not is_sqlite:
                conn.execute(
                    text(f'ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS "{name}"')
                )

        # SQLite (and Postgres fallback): column-level unique becomes a
        # unique index. Drop any unique index keyed on scope alone.
        for index in inspector.get_indexes(table_name):
            cols = list(index.get("column_names") or [])
            if cols != ["scope"] or not index.get("unique"):
                continue
            legacy_constraint_on_scope = True
            name = index.get("name")
            if name and not is_sqlite:
                conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))

        if legacy_constraint_on_scope and is_sqlite:
            # SQLite has no ALTER TABLE DROP CONSTRAINT/INDEX for column-level
            # UNIQUE embedded in the original CREATE TABLE. Rebuild the table.
            _rebuild_current_slate_snapshots_sqlite(conn)


def _rebuild_current_slate_snapshots_sqlite(conn) -> None:
    """SQLite-only: recreate the table without the column-level unique on
    ``scope`` and copy existing rows across. Called from
    ``_drop_legacy_current_slate_scope_uniqueness``.
    """
    conn.execute(text("PRAGMA foreign_keys = OFF"))
    conn.execute(
        text(
            """
            CREATE TABLE current_slate_snapshots__v2 (
                id INTEGER PRIMARY KEY,
                scope VARCHAR NOT NULL,
                source_run_id INTEGER REFERENCES runs(id),
                generated_at DATETIME NOT NULL,
                payload JSON
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO current_slate_snapshots__v2 (id, scope, source_run_id, generated_at, payload)
            SELECT id, scope, source_run_id, generated_at, payload FROM current_slate_snapshots
            """
        )
    )
    conn.execute(text("DROP TABLE current_slate_snapshots"))
    conn.execute(
        text("ALTER TABLE current_slate_snapshots__v2 RENAME TO current_slate_snapshots")
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_current_slate_snapshots_scope "
            "ON current_slate_snapshots(scope)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_current_slate_snapshots_generated_at "
            "ON current_slate_snapshots(generated_at)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_current_slate_snapshots_source_run_id "
            "ON current_slate_snapshots(source_run_id)"
        )
    )
    conn.execute(text("PRAGMA foreign_keys = ON"))

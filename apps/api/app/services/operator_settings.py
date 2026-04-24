from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import OperatorSetting

RuntimeMode = Literal["heuristic", "shadow", "ml"]

ML_SERVING_MODE_KEY = "ml_serving_mode"
VALID_RUNTIME_MODES = {"heuristic", "shadow", "ml"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_runtime_mode(value: object) -> RuntimeMode | None:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_RUNTIME_MODES:
        return normalized  # type: ignore[return-value]
    return None


def default_ml_serving_mode() -> RuntimeMode:
    return _normalize_runtime_mode(get_settings().ml_serving_mode) or "heuristic"


def effective_ml_serving_mode(db: Session | None = None) -> RuntimeMode:
    if db is None:
        return default_ml_serving_mode()

    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == ML_SERVING_MODE_KEY))
    value = dict(row.value or {}).get("mode") if row is not None else None
    return _normalize_runtime_mode(value) or default_ml_serving_mode()


def set_ml_serving_mode(db: Session, mode: RuntimeMode) -> OperatorSetting:
    normalized = _normalize_runtime_mode(mode)
    if normalized is None:
        raise ValueError(f"Unsupported ML serving mode: {mode}")

    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == ML_SERVING_MODE_KEY))
    if row is None:
        row = OperatorSetting(key=ML_SERVING_MODE_KEY)
        db.add(row)
    row.value = {
        "mode": normalized,
        "source": "operator",
    }
    row.updated_at = _now_utc()
    db.flush()
    return row

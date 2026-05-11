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

PICK_HISTORY_DEFAULT_N_KEY = "pick_history_default_n"
DEFAULT_PICK_HISTORY_N = 5
PICK_HISTORY_N_MIN = 1
PICK_HISTORY_N_MAX = 20


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


def _clamp_pick_history_n(value: object) -> int | None:
    try:
        as_int = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if as_int < PICK_HISTORY_N_MIN or as_int > PICK_HISTORY_N_MAX:
        return None
    return as_int


def effective_pick_history_default_n(db: Session | None = None) -> int:
    """Operator-pinned default for the trade-ticket pick-history strip.

    Per-pick toggles override this per session; this is the initial value
    when a strip first mounts."""
    if db is None:
        return DEFAULT_PICK_HISTORY_N
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == PICK_HISTORY_DEFAULT_N_KEY))
    raw = dict(row.value or {}).get("n") if row is not None else None
    clamped = _clamp_pick_history_n(raw)
    return clamped if clamped is not None else DEFAULT_PICK_HISTORY_N


def set_pick_history_default_n(db: Session, n: int) -> OperatorSetting:
    clamped = _clamp_pick_history_n(n)
    if clamped is None:
        raise ValueError(
            f"pick_history_default_n must be an int in [{PICK_HISTORY_N_MIN}, {PICK_HISTORY_N_MAX}]; got {n!r}"
        )
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == PICK_HISTORY_DEFAULT_N_KEY))
    if row is None:
        row = OperatorSetting(key=PICK_HISTORY_DEFAULT_N_KEY)
        db.add(row)
    row.value = {"n": clamped, "source": "operator"}
    row.updated_at = _now_utc()
    db.flush()
    return row

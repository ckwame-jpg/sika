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
# Codex round-6 P2 on PR #24: the strip's HISTORY_OPTIONS is exactly
# {5, 10, 20}, and ``clampToHistoryOption`` falls back to 5 for any
# value outside that set. If the API accepted (say) 6, the readiness
# summary would echo 6 back but the trade ticket would silently
# fetch last-5. Restrict writes to the same set the UI offers so
# operator-pinned defaults always round-trip through the strip.
PICK_HISTORY_N_OPTIONS = frozenset({5, 10, 20})
# Compat aliases — older tests reference the legacy MIN/MAX bounds.
# They no longer drive validation; the canonical set is
# ``PICK_HISTORY_N_OPTIONS``.
PICK_HISTORY_N_MIN = min(PICK_HISTORY_N_OPTIONS)
PICK_HISTORY_N_MAX = max(PICK_HISTORY_N_OPTIONS)


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


def _canonical_pick_history_n(value: object) -> int | None:
    """Return ``value`` if it parses to an int in ``PICK_HISTORY_N_OPTIONS``;
    otherwise ``None``. Used for both READ (fall back to default on legacy
    or corrupted values) and WRITE (reject anything the UI can't render)."""
    try:
        as_int = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if as_int not in PICK_HISTORY_N_OPTIONS:
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
    canonical = _canonical_pick_history_n(raw)
    return canonical if canonical is not None else DEFAULT_PICK_HISTORY_N


def set_pick_history_default_n(db: Session, n: int) -> OperatorSetting:
    canonical = _canonical_pick_history_n(n)
    if canonical is None:
        allowed = ", ".join(str(value) for value in sorted(PICK_HISTORY_N_OPTIONS))
        raise ValueError(
            f"pick_history_default_n must be one of {{{allowed}}}; got {n!r}"
        )
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == PICK_HISTORY_DEFAULT_N_KEY))
    if row is None:
        row = OperatorSetting(key=PICK_HISTORY_DEFAULT_N_KEY)
        db.add(row)
    row.value = {"n": canonical, "source": "operator"}
    row.updated_at = _now_utc()
    db.flush()
    return row


# Smarter #31 — LLM narrator toggle. Off by default so operators don't
# burn tokens until they've eyeballed the output quality on a few
# recommendations. Toggleable at runtime via the model-readiness
# settings endpoint.
NARRATOR_ENABLED_KEY = "narrator_enabled"


def effective_narrator_enabled(db: Session | None = None) -> bool:
    if db is None:
        return False
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == NARRATOR_ENABLED_KEY))
    if row is None:
        return False
    return bool(dict(row.value or {}).get("enabled", False))


def set_narrator_enabled(db: Session, enabled: bool) -> OperatorSetting:
    """Persist the operator-side toggle. Idempotent — re-applying the
    same value just refreshes ``updated_at``."""
    row = db.scalar(select(OperatorSetting).where(OperatorSetting.key == NARRATOR_ENABLED_KEY))
    if row is None:
        row = OperatorSetting(key=NARRATOR_ENABLED_KEY)
        db.add(row)
    row.value = {"enabled": bool(enabled), "source": "operator"}
    row.updated_at = _now_utc()
    db.flush()
    return row

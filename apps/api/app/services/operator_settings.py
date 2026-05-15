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


# Smarter #18 phase 2d — sportsbook disagreement suppression. Off by
# default so operators can eyeball the diagnostic emission (phase 2c)
# on real picks before letting the rule actually filter recommendations.
# Defaults aim for "thick consensus, large gap" → ≥3 books AND
# ≥15-pp disagreement.
SPORTSBOOK_DISAGREEMENT_SUPPRESSION_ENABLED_KEY = "sportsbook_disagreement_suppression_enabled"
SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY = "sportsbook_disagreement_threshold_pp"
SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY = "sportsbook_disagreement_min_book_count"

DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD = 0.15
DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT = 3


def effective_sportsbook_disagreement_suppression_enabled(db: Session | None = None) -> bool:
    if db is None:
        return False
    row = db.scalar(
        select(OperatorSetting).where(
            OperatorSetting.key == SPORTSBOOK_DISAGREEMENT_SUPPRESSION_ENABLED_KEY
        )
    )
    if row is None:
        return False
    return bool(dict(row.value or {}).get("enabled", False))


def set_sportsbook_disagreement_suppression_enabled(
    db: Session, enabled: bool
) -> OperatorSetting:
    """Persist the operator-side toggle for the sportsbook
    disagreement suppression rule. Idempotent."""
    row = db.scalar(
        select(OperatorSetting).where(
            OperatorSetting.key == SPORTSBOOK_DISAGREEMENT_SUPPRESSION_ENABLED_KEY
        )
    )
    if row is None:
        row = OperatorSetting(key=SPORTSBOOK_DISAGREEMENT_SUPPRESSION_ENABLED_KEY)
        db.add(row)
    row.value = {"enabled": bool(enabled), "source": "operator"}
    row.updated_at = _now_utc()
    db.flush()
    return row


def effective_sportsbook_disagreement_threshold(db: Session | None = None) -> float:
    """Return the configured pp-disagreement threshold. Default 0.15
    (15 percentage points) when no row exists.

    PATCH-endpoint wiring + readiness-summary surfacing is
    intentionally deferred to a follow-up: the initial rollout
    leaves the default in place while operators eyeball the
    suppression behavior. Tuning happens via
    ``set_sportsbook_disagreement_threshold`` (operator-only call)
    until the UI surface lands.
    """
    if db is None:
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD
    row = db.scalar(
        select(OperatorSetting).where(
            OperatorSetting.key == SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY
        )
    )
    if row is None:
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD
    raw = dict(row.value or {}).get("threshold")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD
    # Clamp to (0, 1) — anything outside that is meaningless for a
    # probability gap and likely an operator typo.
    if not 0.0 < float(raw) < 1.0:
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD
    return float(raw)


def set_sportsbook_disagreement_threshold(db: Session, threshold: float) -> OperatorSetting:
    """Persist the operator-side suppression threshold. Idempotent.

    Values outside ``(0.0, 1.0)`` get the default at read time —
    this writer accepts any numeric so operators can see (via the
    next ``effective_*`` read) when their value got clamped.
    """
    row = db.scalar(
        select(OperatorSetting).where(
            OperatorSetting.key == SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY
        )
    )
    if row is None:
        row = OperatorSetting(key=SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY)
        db.add(row)
    row.value = {"threshold": float(threshold), "source": "operator"}
    row.updated_at = _now_utc()
    db.flush()
    return row


def effective_sportsbook_disagreement_min_book_count(db: Session | None = None) -> int:
    """Return the minimum book count required before the
    disagreement rule will fire. Default 3.

    PATCH-endpoint wiring + readiness-summary surfacing deferred to a
    follow-up (see ``effective_sportsbook_disagreement_threshold``
    docstring for the rationale).
    """
    if db is None:
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    row = db.scalar(
        select(OperatorSetting).where(
            OperatorSetting.key == SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY
        )
    )
    if row is None:
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    raw = dict(row.value or {}).get("min_book_count")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    # Reviewer LOW catch: an unreasonably high value (operator typo
    # like ``300`` for "thirty") would silently disable the rule. The
    # Odds API free tier reports up to ~15 books per market in
    # practice; cap at 100 so values past that fall back to the
    # default rather than silently no-op.
    if raw > 100:
        return DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    return int(raw)


def set_sportsbook_disagreement_min_book_count(db: Session, min_book_count: int) -> OperatorSetting:
    """Persist the operator-side minimum book count. Idempotent.

    Values outside ``[1, 100]`` get the default at read time —
    see ``effective_sportsbook_disagreement_min_book_count`` for
    the clamping rationale.
    """
    row = db.scalar(
        select(OperatorSetting).where(
            OperatorSetting.key == SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY
        )
    )
    if row is None:
        row = OperatorSetting(key=SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY)
        db.add(row)
    row.value = {"min_book_count": int(min_book_count), "source": "operator"}
    row.updated_at = _now_utc()
    db.flush()
    return row

from __future__ import annotations

from typing import Any


DIAGNOSTIC_BACKFILL_CAPTURE_MODE = "diagnostic_backfill"
PROMOTION_EXCLUDED_KEY = "promotion_excluded"


def is_promotion_excluded_metadata(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    return (
        bool(metadata.get(PROMOTION_EXCLUDED_KEY))
        or str(metadata.get("capture_mode") or "").strip().lower() == DIAGNOSTIC_BACKFILL_CAPTURE_MODE
    )


def with_diagnostic_backfill_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {
        **metadata,
        "capture_mode": DIAGNOSTIC_BACKFILL_CAPTURE_MODE,
        PROMOTION_EXCLUDED_KEY: True,
        "diagnostic_only": True,
    }

"""Map-merge helpers shared across the ingestion package.

Extracted from ``ingestion/__init__.py`` as part of R2. Both
``summary.py`` and the orchestration code in ``__init__.py`` need
these; living in their own tiny module avoids an import cycle
between summary <-> kernel.
"""

from __future__ import annotations

__all__ = ["_merge_numeric_detail_maps", "_merge_count_maps"]


def _merge_numeric_detail_maps(*payloads: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for payload in payloads:
        for key, value in payload.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


def _merge_count_maps(*payloads: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for payload in payloads:
        for key, value in payload.items():
            merged[str(key)] = merged.get(str(key), 0) + int(value or 0)
    return merged

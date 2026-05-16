"""Smarter #21 phase 2b operator UX — per-stat-key interval-model
status collection for the readiness panel.

Same source of truth as the ``python -m ml.cli inspect-intervals`` CLI
(shipped in [sika#163](https://github.com/ckwame-jpg/sika/pull/163)).
Both surfaces walk the active manifest's families and read
``<artifact_dir>/interval_models/<stat>/metadata.json`` to produce a
per-(family, stat_key) row with sample size, empirical coverage, and
operator-facing band classification (ok / warn / bad / unknown).

## Why duplicate the classifier instead of importing from apps/ml

``apps/ml`` doesn't sit on the API's import path (separate package,
separate dependency graph). Importing ``ml.cli._classify_coverage``
would couple the API runtime to the training-workspace package — the
opposite of what the train/serve separation enforces. Duplicating four
constants + a five-line helper here keeps the API self-contained; a
test in PR B asserts the two constant sets match so drift surfaces in
CI rather than silently in prod.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.services.ml.registry import load_model_manifest


logger = logging.getLogger(__name__)


# Coverage classification bands — kept in sync with
# ``apps/ml/ml/cli.py:INTERVAL_COVERAGE_*`` constants (see module
# docstring for why we duplicate). A drift-guard test pins them.
INTERVAL_COVERAGE_OK_LOWER = 0.70
INTERVAL_COVERAGE_OK_UPPER = 0.90
INTERVAL_COVERAGE_WARN_LOWER = 0.60
INTERVAL_COVERAGE_WARN_UPPER = 0.95


CoverageStatus = Literal["ok", "warn", "bad", "unknown"]


@dataclass(frozen=True, slots=True)
class IntervalModelStatus:
    """Per-(family, stat_key) status the readiness panel renders."""

    family_key: str
    stat_key: str
    sample_size: int | None
    empirical_coverage: float | None
    coverage_status: CoverageStatus
    trained_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None


def _classify_coverage(coverage: float | None) -> CoverageStatus:
    """Bucket an empirical coverage into the operator-facing band.
    Mirrors the CLI helper of the same name."""
    if coverage is None:
        return "unknown"
    if INTERVAL_COVERAGE_OK_LOWER <= coverage <= INTERVAL_COVERAGE_OK_UPPER:
        return "ok"
    if INTERVAL_COVERAGE_WARN_LOWER <= coverage <= INTERVAL_COVERAGE_WARN_UPPER:
        return "warn"
    return "bad"


def _read_interval_metadata(metadata_path: Path) -> dict[str, Any] | None:
    """Read + parse ``metadata.json``. Returns ``None`` when the file
    is missing or its contents are not a JSON object — both surface as
    ``coverage_status="unknown"`` upstream (visible, not silent)."""
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 datetime string from metadata.json into a
    timezone-aware UTC datetime. ``None`` / empty / unparseable → None
    (the FastAPI schema's ``UTCDateTime | None`` accepts both)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def collect_interval_model_status() -> list[IntervalModelStatus]:
    """Walk the active manifest's family entries and collect per-stat
    interval-model status.

    Returns rows sorted by ``(family_key, stat_key)`` for deterministic
    UI ordering. Returns ``[]`` when:
    - No manifest is configured (pre-ML rollout).
    - Manifest exists but no family entries have an ``artifact_path``.
    - All artifact_dirs are missing / lack the ``interval_models/`` subdir.

    Defensive paths (mirror the CLI):
    - Missing artifact_dir → skip family.
    - Missing interval_models/ subdir → skip family.
    - Missing or unparseable metadata.json for a stat key → row still
      included with ``coverage_status="unknown"`` and null metric fields.
    """
    manifest = load_model_manifest()
    if manifest is None or not manifest.families:
        return []
    manifest_dir = Path(manifest.source_path).parent if manifest.source_path else None

    rows: list[IntervalModelStatus] = []
    for family in manifest.families:
        family_key = (family.serves_family_key or family.family_key or "").strip()
        if not family_key or not family.artifact_path:
            continue
        artifact_dir = (
            (manifest_dir / family.artifact_path).resolve()
            if manifest_dir is not None
            else Path(family.artifact_path).resolve()
        )
        if not artifact_dir.exists() or not artifact_dir.is_dir():
            continue
        intervals_root = artifact_dir / "interval_models"
        if not intervals_root.exists() or not intervals_root.is_dir():
            continue
        for stat_dir in sorted(intervals_root.iterdir()):
            if not stat_dir.is_dir():
                continue
            stat_key = stat_dir.name
            metadata = _read_interval_metadata(stat_dir / "metadata.json") or {}
            coverage = metadata.get("empirical_coverage")
            coverage_float: float | None
            try:
                coverage_float = float(coverage) if coverage is not None else None
            except (TypeError, ValueError):
                coverage_float = None
            sample_size = metadata.get("sample_size")
            try:
                sample_size_int: int | None = int(sample_size) if sample_size is not None else None
            except (TypeError, ValueError):
                sample_size_int = None
            rows.append(
                IntervalModelStatus(
                    family_key=family_key,
                    stat_key=stat_key,
                    sample_size=sample_size_int,
                    empirical_coverage=coverage_float,
                    coverage_status=_classify_coverage(coverage_float),
                    trained_at=_parse_iso_datetime(metadata.get("trained_at")),
                    window_start=_parse_iso_datetime(metadata.get("window_start")),
                    window_end=_parse_iso_datetime(metadata.get("window_end")),
                )
            )
    rows.sort(key=lambda row: (row.family_key, row.stat_key))
    return rows

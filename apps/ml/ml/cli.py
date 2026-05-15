from __future__ import annotations

import argparse
import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, text

from ml.dataset import normalize_database_url
from ml.recalibration import (
    DEFAULT_WINDOW_DAYS,
    MIN_RECALIBRATION_SAMPLES,
    recalibrate_with_rolling_window,
    write_sidecar_recalibrator,
)
from ml.training import train_and_package


_ADVANCED_ONLY_MAP = {"auto": None, "yes": True, "no": False}

_DEFAULT_SERVE_FAMILY_KEYS = "mlb_props,nba_props,mlb_singles,nba_singles"


def _now() -> datetime:
    """Return the current UTC time.

    Wraps ``datetime.now(timezone.utc)`` so tests can monkeypatch the
    clock to a fixed value (codex round 2 P2: tests previously
    hard-coded ``2026-05-15`` in fixtures but the CLI used the real
    wall clock — the suite would fail outside that day). Production
    code path is the trivial pass-through; tests do
    ``monkeypatch.setattr(cli, "_now", lambda: <fixed datetime>)``.
    """
    return datetime.now(timezone.utc)


# Smarter #20 phase 2b — calibration_version stamp tag.
#
# When a sidecar is successfully written, every manifest entry that
# serves the recalibrated family gets ``+iso30d-<YYYY-MM-DD>`` appended
# to its ``calibration_version``. The serve-time loader (phase 2c) is
# expected to detect the tag and apply the sidecar; an entry whose
# version is still bare ``calibrated_v1`` retains the original training-
# time calibrator.
#
# Re-running on the same day is idempotent (no double-tag); re-running
# on a different day replaces the existing tag rather than accumulating
# (operators should see ONE rolling-window date, not a chain).
_RECALIBRATION_TAG_PREFIX = "+iso30d-"


def _parse_serve_family_keys(raw: str) -> tuple[str, ...]:
    keys = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not keys:
        raise argparse.ArgumentTypeError("--serve-family-keys must list at least one family key.")
    return keys


def _train(args: argparse.Namespace) -> int:
    result = train_and_package(
        database_url=args.database_url,
        artifact_root=args.artifact_root,
        manifest_out=None if args.dry_run else args.manifest_out,
        serve_family_keys=args.serve_family_keys,
        feature_set_version=args.feature_set_version,
        model_version=args.model_version,
        advanced_only=_ADVANCED_ONLY_MAP[args.advanced_only],
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "model_name": result.model_name,
                "artifact_dir": str(result.artifact_dir),
                "manifest_path": str(result.manifest_path) if result.manifest_path else None,
                "training_rows": result.metrics["training_rows"],
                "advanced_only_active": result.metrics["advanced_only_active"],
                "serve_family_keys": list(args.serve_family_keys),
                "winner": result.metrics["winner"],
                "player_group_brier": result.metrics["metrics"][result.metrics["winner"]]["player_group"]["brier"],
                "time_brier": result.metrics["metrics"][result.metrics["winner"]]["time"]["brier"],
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    return 0


# -- Smarter #20 phase 2b: ``recalibrate`` subcommand -----------------


def _family_key_for_row(sport_key: str | None, market_family: str | None) -> str:
    """Mirror ``ml.dataset._family_key`` — derive the family key from
    ``sport_key`` + ``market_family`` exactly as ``dataset.py`` does so
    rows pulled by the recalibrate query bucket the same way training
    rows do.
    """
    sport = (sport_key or "").upper()
    family = (market_family or "").lower()
    if family == "player_prop":
        if sport == "NBA":
            return "nba_props"
        if sport == "MLB":
            return "mlb_props"
    if sport == "NBA":
        return "nba_singles"
    if sport == "MLB":
        return "mlb_singles"
    return f"{sport.lower()}_singles" if sport else "unknown_singles"


def _bump_calibration_version(existing: str, *, today: datetime) -> str:
    """Append ``+iso30d-<YYYY-MM-DD>`` to ``existing``.

    If ``existing`` already carries a rolling-window tag (from a prior
    successful recalibration), strip it before appending the new one.
    Operators should see exactly one rolling-window date in the version
    string, not a chain that grows on every run.
    """
    base = existing or "calibrated_v1"
    tag_idx = base.find(_RECALIBRATION_TAG_PREFIX)
    if tag_idx != -1:
        base = base[:tag_idx]
    return f"{base}{_RECALIBRATION_TAG_PREFIX}{today.date().isoformat()}"


def _entry_serves_family_key(entry: dict) -> str | None:
    """Resolve which family key a manifest entry serves.

    Mirrors ``apps/api/app/services/ml/registry.py`` and
    ``apps/api/app/services/ml/runtime.py:_manifest_family_map`` so the
    CLI accepts every manifest shape the runtime accepts:

    1. ``entry["serves_family_key"]`` — the explicit modern field.
    2. ``entry["metadata"]["serves_family_key"]`` — the legacy nested
       location (registry.py line 56 falls back here).
    3. ``entry["family_key"]`` — when neither of the above is set, the
       runtime keys the entry by ``family_key`` directly
       (runtime.py:_manifest_family_map line 152).

    Codex round 3 P2: without this fallback chain, a manifest valid
    for the API runtime would still fail the CLI with "not found in
    manifest".
    """
    explicit = (entry.get("serves_family_key") or "").strip()
    if explicit:
        return explicit
    nested = (entry.get("metadata") or {}).get("serves_family_key")
    nested = (nested or "").strip() if isinstance(nested, str) else ""
    if nested:
        return nested
    family_key = (entry.get("family_key") or "").strip()
    return family_key or None


def _find_family_entries(manifest: dict, serves_family_key: str) -> list[dict]:
    """Return the manifest family entries that serve ``serves_family_key``.

    A serves_family_key may have multiple manifest entries when the same
    family is registered in multiple ``ModelArtifact`` rows — we update
    every matching entry so the bump is consistent across the whole
    manifest.
    """
    matching = [
        entry
        for entry in manifest.get("families", [])
        if _entry_serves_family_key(entry) == serves_family_key
    ]
    if not matching:
        raise ValueError(
            f"Family {serves_family_key!r} not found in manifest "
            f"(manifest has: {[_entry_serves_family_key(entry) for entry in manifest.get('families', [])]})"
        )
    return matching


def _resolve_artifact_dir(manifest_path: Path, serves_family_key: str) -> Path:
    """Resolve the absolute artifact dir for a served family.

    The manifest stores ``artifact_path`` relative to the manifest's
    parent directory (matching how ``train_and_package`` writes it),
    so resolution is ``manifest_path.parent / artifact_path``.

    Codex P2 (review round 2): refuse to recalibrate when the artifact
    directory doesn't exist or is missing required files. Without
    this check, a manifest with a stale / mistyped ``artifact_path``
    would still let the CLI create ``artifact_dir/recalibrators/...``
    from scratch and bump the manifest's ``calibration_version`` —
    leaving the manifest pointing at a non-existent or incomplete
    model with only an orphan recalibrator beside it.

    Codex round 3 P2: require ALL three files the API's
    ``load_sklearn_artifact`` reads (``model.joblib`` +
    ``feature_spec.json`` + ``training_metadata.json``), not just
    ``model.joblib``. An artifact missing any of those is rejected
    by the runtime — recalibrating it would attach a sidecar to
    something the API can't actually load.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = _find_family_entries(manifest, serves_family_key)
    artifact_paths = {entry["artifact_path"] for entry in entries}
    if len(artifact_paths) > 1:
        raise ValueError(
            f"Family {serves_family_key!r} has multiple artifact paths in manifest: {sorted(artifact_paths)}"
        )
    relative = next(iter(artifact_paths))
    resolved = (manifest_path.parent / relative).resolve()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Artifact directory {resolved} (from manifest entry serving "
            f"{serves_family_key!r}) does not exist — refusing to recalibrate "
            f"against a non-existent model."
        )
    # Same triple required by ``apps/api/app/services/ml/artifact_loader.py``.
    required_files = ("model.joblib", "feature_spec.json", "training_metadata.json")
    missing = [name for name in required_files if not (resolved / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"Artifact directory {resolved} is missing required file(s) "
            f"{sorted(missing)} — the artifact is either incomplete or this "
            f"manifest entry points at the wrong path. Refusing to recalibrate."
        )
    return resolved


def _resolve_model_identity(
    manifest_path: Path, serves_family_key: str,
) -> tuple[str, str | None]:
    """Resolve the (model_name, model_version) the recalibration filters by.

    Both fields must be consistent across all manifest entries serving
    this family. ``model_version`` may be None for legacy manifests
    that pre-date the field; in that case the SQL query relaxes to
    "any model_version" so older artifacts can still be recalibrated.

    Codex P2 (review round 1): without a version filter, a retrain that
    keeps the same ``model_name`` (e.g. ``global_hist_gradient_boosting_residual``)
    and only bumps ``model_version`` would let the recalibrator pull
    rows from the previous artifact's distribution into the new
    artifact's sidecar.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = _find_family_entries(manifest, serves_family_key)
    model_names = {entry["model_name"] for entry in entries}
    if len(model_names) > 1:
        raise ValueError(
            f"Family {serves_family_key!r} has multiple model names in manifest: {sorted(model_names)}"
        )
    model_versions = {entry.get("model_version") for entry in entries}
    if len(model_versions) > 1:
        raise ValueError(
            f"Family {serves_family_key!r} has multiple model versions in manifest: "
            f"{sorted(str(v) for v in model_versions)}"
        )
    return next(iter(model_names)), next(iter(model_versions))


def _coerce_captured_at(value: object) -> datetime:
    """Coerce a captured_at value (datetime or ISO string) to a datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # SQLite returns ISO strings; Postgres returns datetimes. Both
        # paths land here. ``fromisoformat`` parses the trailing 'Z'
        # only on 3.11+; we already require 3.12.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Unexpected captured_at type: {type(value).__name__}")


def _load_settled_for_family(
    database_url: str | None,
    *,
    family_key: str,
    model_name: str,
    model_version: str | None,
) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
    """Load (raw_probability, target, captured_at) triples for the given
    family from BOTH the ``predictions`` and ``shadow_inferences`` tables.

    The CLI must work in both deployment phases:

    1. Live mode — model is in ``serving_mode="ml"`` for the family, so
       its outputs land directly in ``predictions``. We pull rows where
       ``predictions.model_name`` matches the manifest's model.

    2. Shadow mode — model is in ``serving_mode="shadow"`` (the typical
       pre-promotion state), so its outputs land in ``shadow_inferences``
       while the customer-facing prediction in ``predictions`` is still
       the heuristic. The settled outcome on the heuristic prediction
       (``predictions.prediction_outcome``) is the ground truth for
       whether YES won — same row, same market — so we JOIN
       ``shadow_inferences`` to its ``source_prediction_id`` to recover
       the outcome alongside the model's raw ``fair_yes_price``.

    Without (2), the CLI would silently report ``insufficient_samples``
    until the model promotes to live mode, which defeats the recalibration
    workflow's purpose (sharpen the calibrator BEFORE promotion).

    Filters applied to BOTH branches:
    - ``predictions.prediction_outcome IN ('won', 'lost')`` — ``push``
      and ``cancelled`` rows aren't recalibration signal.
    - ``model_name = <model_name>`` — exclude rows captured by a
      different model.
    - ``model_version = <model_version>`` (when not None) — exclude
      rows from prior retrains whose distribution differs.
    - ``fair_yes_price IS NOT NULL`` — defensive guard.
    - ``predictions.side IN ('yes', 'no')`` — anything else is bad data.
    - Family bucket derived in Python from ``sport_key`` +
      ``market_family`` so the query stays portable across SQLite /
      Postgres without a CASE expression.

    De-duplication: a single ``predictions`` row can in principle match
    BOTH branches if the model was once shadow-recorded against its
    own ML-mode pick during a transition window. Both branches expose
    the canonical ``predictions.id`` as ``source_id`` — the live branch
    surfaces its own ID; the shadow branch surfaces the parent prediction's
    ID via ``source_prediction_id``. Deduping by ``source_id`` collapses
    "same prediction recorded twice" into a single sample without
    dropping legitimately distinct rows that happen to share the
    rounded ``fair_yes_price`` (a real risk for batch captures since
    the API persists probabilities at 4-decimal precision; codex
    round 2 P2).

    Returns the three arrays aligned by index. Window-trimming happens
    downstream in ``recalibrate_with_rolling_window``.
    """
    resolved_url = normalize_database_url(
        database_url or os.environ.get("DATABASE_URL") or "sqlite:///../api/kalshi_sports_copilot.db"
    )
    engine = create_engine(resolved_url, future=True)
    # Two queries kept separate — UNION ALL across heterogeneous column
    # provenance (one comes from the row directly; the other from a JOIN)
    # is harder to read and the same NULL-handling needs to apply to both.
    # The model_version filter goes through a parameter when not None and
    # is dropped from the WHERE clause when None (manifest legacy case).
    version_clause = "AND model_version = :model_version" if model_version is not None else ""
    shadow_version_clause = (
        "AND si.model_version = :model_version" if model_version is not None else ""
    )
    predictions_sql = text(
        f"""
        SELECT id AS source_id,
               fair_yes_price, side, prediction_outcome, captured_at,
               sport_key, market_family
        FROM predictions
        WHERE prediction_outcome IN ('won', 'lost')
          AND model_name = :model_name
          {version_clause}
          AND fair_yes_price IS NOT NULL
        """
    )
    shadow_sql = text(
        f"""
        SELECT si.source_prediction_id AS source_id,
               si.fair_yes_price AS fair_yes_price,
               p.side AS side,
               p.prediction_outcome AS prediction_outcome,
               si.captured_at AS captured_at,
               si.sport_key AS sport_key,
               si.market_family AS market_family
        FROM shadow_inferences si
        JOIN predictions p ON si.source_prediction_id = p.id
        WHERE p.prediction_outcome IN ('won', 'lost')
          AND si.model_name = :model_name
          {shadow_version_clause}
          AND si.fair_yes_price IS NOT NULL
          AND si.inference_scope = 'single'
        """
    )
    params: dict[str, str] = {"model_name": model_name}
    if model_version is not None:
        params["model_version"] = model_version

    raw_probs: list[float] = []
    outcomes: list[float] = []
    timestamps: list[datetime] = []
    seen_source_ids: set[int] = set()
    with engine.connect() as conn:
        rows = list(conn.execute(predictions_sql, params).mappings().all())
        rows.extend(conn.execute(shadow_sql, params).mappings().all())
    for row in rows:
        side = (row["side"] or "").lower()
        if side not in ("yes", "no"):
            continue
        outcome = (row["prediction_outcome"] or "").lower()
        if outcome not in ("won", "lost"):
            continue
        if _family_key_for_row(row["sport_key"], row["market_family"]) != family_key:
            continue
        source_id = int(row["source_id"])
        if source_id in seen_source_ids:
            # Already counted this prediction (either as a live row or
            # via its shadow inference). Keep one sample per prediction
            # so the rolling window stays unbiased.
            continue
        seen_source_ids.add(source_id)
        # target = 1 iff YES won. Mirrors ``ml.dataset._prepare_frame``:
        # side == YES and outcome == won  →  YES won
        # side == NO  and outcome == lost →  YES won
        target = 1 if (side == "yes") == (outcome == "won") else 0
        raw_probs.append(float(row["fair_yes_price"]))
        outcomes.append(float(target))
        timestamps.append(_coerce_captured_at(row["captured_at"]))
    return np.asarray(raw_probs, dtype=float), np.asarray(outcomes, dtype=float), timestamps


def _annotate_sidecar_metadata_with_family(metadata_path: Path, family_key: str) -> None:
    """Inject ``family_key`` into the sidecar's JSON metadata.

    ``write_sidecar_recalibrator`` (phase 2a) doesn't know about
    families — it writes a generic provenance blob (window dates,
    sample size, before/after metrics). The CLI patches the file
    after the write so phase 2c can detect cross-family mismatch when
    multiple families share an artifact_dir.

    Sorted keys + 2-space indent match phase 2a's ``write_sidecar``
    formatting, keeping diffs readable across consecutive runs.
    """
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["family_key"] = family_key
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _bump_manifest_calibration_versions(
    manifest_path: Path,
    *,
    serves_family_key: str,
    today: datetime,
) -> str:
    """Update ``calibration_version`` on every manifest entry that serves
    ``serves_family_key`` and write the manifest back.

    Returns the new calibration_version string. All matching entries
    share the same version after the bump.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = _find_family_entries(manifest, serves_family_key)
    new_version: str | None = None
    for entry in entries:
        existing = entry.get("calibration_version") or "calibrated_v1"
        new_version = _bump_calibration_version(existing, today=today)
        entry["calibration_version"] = new_version
    assert new_version is not None  # _find_family_entries raises if empty
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return new_version


def _per_family_sidecar_dir(artifact_dir: Path, family_key: str) -> Path:
    """Resolve the per-family subdirectory the sidecar lives in.

    Codex P1 (review round 1): ``write_sidecar_recalibrator`` writes a
    single ``isotonic_recalibrator.joblib`` per directory it's pointed
    at, but the production ``global_v1_<date>/`` artifact directory
    serves multiple families. Writing every family's sidecar at the
    artifact root would overwrite the previous family's fit. Each
    family gets its own subdirectory under ``recalibrators/`` so the
    phase 2a I/O contract works unmodified — phase 2c calls
    ``load_sidecar_recalibrator(artifact_dir / "recalibrators" / family_key)``.
    """
    return artifact_dir / "recalibrators" / family_key


def _recalibrate(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest_path)
    artifact_dir = _resolve_artifact_dir(manifest_path, args.family_key)
    model_name, model_version = _resolve_model_identity(manifest_path, args.family_key)
    sidecar_dir = _per_family_sidecar_dir(artifact_dir, args.family_key)

    raw_probs, outcomes, timestamps = _load_settled_for_family(
        args.database_url,
        family_key=args.family_key,
        model_name=model_name,
        model_version=model_version,
    )

    now = _now()
    result = recalibrate_with_rolling_window(
        raw_probs,
        outcomes,
        timestamps,
        window_days=args.window_days,
        min_samples=args.min_samples,
        now=now,
    )

    summary: dict[str, object] = {
        "family_key": args.family_key,
        "manifest_path": str(manifest_path),
        "artifact_dir": str(artifact_dir),
        "sidecar_dir": str(sidecar_dir),
        "model_name": model_name,
        "model_version": model_version,
        "window_days": args.window_days,
        "min_samples": args.min_samples,
        "window_start": result.window_start.isoformat(),
        "window_end": result.window_end.isoformat(),
        "sample_size": result.sample_size,
        "insufficient_samples": result.insufficient_samples,
        "metrics_before": dataclasses.asdict(result.metrics_before),
        "metrics_after": dataclasses.asdict(result.metrics_after),
        "brier_improvement": result.brier_improvement,
        "ece_improvement": result.ece_improvement,
        "applied": False,
        "sidecar_paths": None,
        "new_calibration_version": None,
        "dry_run": args.dry_run,
    }

    # Decision tree: insufficient_samples first (no calibrator was fit),
    # then no_brier_improvement (calibrator was fit but didn't sharpen),
    # then dry_run (operator preview), then apply.
    if result.calibrator is None:
        summary["skip_reason"] = "insufficient_samples"
    elif result.brier_improvement <= 0:
        summary["skip_reason"] = "no_brier_improvement"
    elif args.dry_run:
        summary["skip_reason"] = "dry_run"
    else:
        joblib_path, metadata_path = write_sidecar_recalibrator(sidecar_dir, result)
        # Defensive marker — even though the per-family subdirectory
        # already isolates this family's sidecar, including ``family_key``
        # in the metadata lets phase 2c assert that the file it loaded
        # matches the family it's serving (catches misconfiguration if
        # an operator manually moves files around).
        _annotate_sidecar_metadata_with_family(metadata_path, args.family_key)
        new_version = _bump_manifest_calibration_versions(
            manifest_path,
            serves_family_key=args.family_key,
            today=now,
        )
        summary["applied"] = True
        summary["sidecar_paths"] = {
            "joblib": str(joblib_path),
            "metadata": str(metadata_path),
        }
        summary["new_calibration_version"] = new_version

    print(json.dumps(summary, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ml.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    train = subparsers.add_parser("train", help="Train and package the global v1 model.")
    train.add_argument("--database-url", default=None)
    train.add_argument("--artifact-root", default="artifacts")
    train.add_argument("--manifest-out", default=str(Path("manifests") / "current.json"))
    train.add_argument(
        "--serve-family-keys",
        type=_parse_serve_family_keys,
        default=_parse_serve_family_keys(_DEFAULT_SERVE_FAMILY_KEYS),
        help="Comma-separated family keys to emit manifest entries for.",
    )
    train.add_argument("--feature-set-version", default="public-feature-set-v2")
    train.add_argument("--model-version", default=None)
    train.add_argument(
        "--advanced-only",
        choices=tuple(_ADVANCED_ONLY_MAP.keys()),
        default="auto",
        help="auto: trigger when a family clears the threshold (default). yes: force advanced-only filter on. no: force off.",
    )
    train.add_argument("--dry-run", action="store_true")
    train.set_defaults(func=_train)

    recalibrate = subparsers.add_parser(
        "recalibrate",
        help="Refit the isotonic recalibrator on the last N days of "
        "settled predictions for one served family. Writes a sidecar "
        "joblib next to the artifact and bumps the manifest's "
        "calibration_version.",
    )
    recalibrate.add_argument(
        "--family-key",
        required=True,
        help="serves_family_key to recalibrate (e.g. nba_props, mlb_singles).",
    )
    recalibrate.add_argument(
        "--manifest-path",
        required=True,
        help="Path to the manifest whose entry for --family-key should be bumped.",
    )
    recalibrate.add_argument("--database-url", default=None)
    recalibrate.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Rolling window in days (default: {DEFAULT_WINDOW_DAYS}).",
    )
    recalibrate.add_argument(
        "--min-samples",
        type=int,
        default=MIN_RECALIBRATION_SAMPLES,
        help=f"Minimum samples in window before refitting (default: {MIN_RECALIBRATION_SAMPLES}).",
    )
    recalibrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Fit and report metrics but skip sidecar write and manifest bump.",
    )
    recalibrate.set_defaults(func=_recalibrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

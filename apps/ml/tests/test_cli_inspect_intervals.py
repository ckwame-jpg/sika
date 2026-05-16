"""Tests for Smarter #21 phase 2b operator UX — ``inspect-intervals``
CLI subcommand.

Surfaces what's been trained per family + stat key:

- Walks every family entry in the manifest.
- For each family's artifact_dir, lists ``interval_models/<stat>/metadata.json``.
- Emits a human-readable table (default) or JSON (``--format json``).
- Color-codes empirical coverage so operators see at a glance whether
  the regressor is well-calibrated: green when ~80% (0.70-0.90),
  yellow on the edge (0.60-0.70 or 0.90-0.95), red when out-of-range
  (< 0.60 or > 0.95).

This is a read-only inspection — no DB queries, no manifest mutation.
Operators run it after train-intervals to verify the artifact landed
where the loader (phase 2c) will find it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ml.cli import build_parser


def _build_manifest(
    manifest_path: Path, *, family_artifacts: list[tuple[str, Path]],
) -> None:
    """Manifest with ``serves_family_key`` + ``artifact_path`` per family.

    Mirrors the production manifest shape the recalibrate / train-intervals
    CLIs already consume.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    families = []
    for family_key, artifact_dir in family_artifacts:
        relative = Path(
            os.path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
        ).as_posix()
        families.append({
            "family_key": "global_v1",
            "serves_family_key": family_key,
            "model_name": "global_hist_gradient_boosting_residual",
            "model_version": "2026-05-15",
            "artifact_path": relative,
            "mode": "ml",
        })
    manifest_path.write_text(
        json.dumps({"version": "test", "families": families}), encoding="utf-8",
    )


def _seed_interval_artifact(
    artifact_dir: Path,
    *,
    stat_key: str,
    family_key: str,
    sample_size: int,
    empirical_coverage: float,
    trained_at: str = "2026-05-15T10:00:00+00:00",
    window_start: str = "2026-04-15T00:00:00+00:00",
    window_end: str = "2026-05-15T00:00:00+00:00",
) -> None:
    """Drop a metadata.json into the canonical phase 2a layout. The
    joblib files are not needed for inspect (which only reads metadata)
    but we create empty placeholders so the directory looks realistic.
    """
    stat_dir = artifact_dir / "interval_models" / stat_key
    stat_dir.mkdir(parents=True, exist_ok=True)
    (stat_dir / "p10.joblib").write_bytes(b"")
    (stat_dir / "p50.joblib").write_bytes(b"")
    (stat_dir / "p90.joblib").write_bytes(b"")
    metadata = {
        "family_key": family_key,
        "stat_key": stat_key,
        "quantiles": [0.1, 0.5, 0.9],
        "sample_size": sample_size,
        "empirical_coverage": empirical_coverage,
        "trained_at": trained_at,
        "window_start": window_start,
        "window_end": window_end,
    }
    (stat_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8",
    )


def _run_cli(argv: list[str]) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        rc = args.func(args)
    return rc, captured.getvalue()


def _run_cli_json(argv: list[str]) -> tuple[int, dict]:
    rc, output = _run_cli(argv)
    return rc, json.loads(output) if output.strip() else {}


# -- Empty / missing artifacts ---------------------------------------


def test_inspect_intervals_reports_no_artifacts_when_directory_missing(
    tmp_path: Path,
) -> None:
    """Manifest references an artifact_dir without an interval_models
    subdir — surface as "no artifacts" rather than crashing. This is
    the common pre-train state."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])

    rc, output = _run_cli([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
    ])

    assert rc == 0
    assert "no interval models" in output.lower()


def test_inspect_intervals_json_empty_when_nothing_trained(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert payload == {"interval_models": []}


# -- Happy path -----------------------------------------------------


def test_inspect_intervals_lists_trained_artifacts_with_metadata(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])
    _seed_interval_artifact(
        artifact_dir,
        family_key="nba_props",
        stat_key="points",
        sample_size=127,
        empirical_coverage=0.81,
    )

    rc, output = _run_cli([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
    ])

    assert rc == 0
    # Table contains the row's key fields.
    assert "nba_props" in output
    assert "points" in output
    assert "127" in output
    assert "0.81" in output


def test_inspect_intervals_json_includes_per_artifact_metadata(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])
    _seed_interval_artifact(
        artifact_dir,
        family_key="nba_props",
        stat_key="points",
        sample_size=127,
        empirical_coverage=0.81,
        trained_at="2026-05-15T10:00:00+00:00",
        window_start="2026-04-15T00:00:00+00:00",
        window_end="2026-05-15T00:00:00+00:00",
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert len(payload["interval_models"]) == 1
    entry = payload["interval_models"][0]
    assert entry["family_key"] == "nba_props"
    assert entry["stat_key"] == "points"
    assert entry["sample_size"] == 127
    assert entry["empirical_coverage"] == 0.81
    assert entry["trained_at"] == "2026-05-15T10:00:00+00:00"
    assert entry["window_start"] == "2026-04-15T00:00:00+00:00"
    assert entry["window_end"] == "2026-05-15T00:00:00+00:00"
    assert entry["artifact_dir"] == str(artifact_dir.resolve())


def test_inspect_intervals_sorts_rows_by_family_then_stat_key(
    tmp_path: Path,
) -> None:
    """Deterministic ordering — operators reading the table should see
    families grouped and stat keys alphabetized inside each family.
    """
    nba_dir = tmp_path / "artifacts" / "nba"
    mlb_dir = tmp_path / "artifacts" / "mlb"
    nba_dir.mkdir(parents=True)
    mlb_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(
        manifest_path,
        family_artifacts=[("nba_props", nba_dir), ("mlb_props", mlb_dir)],
    )
    # Insert in non-sorted order to verify the CLI sorts.
    for stat_key in ("rebounds", "points", "assists"):
        _seed_interval_artifact(
            nba_dir, family_key="nba_props", stat_key=stat_key,
            sample_size=100, empirical_coverage=0.80,
        )
    _seed_interval_artifact(
        mlb_dir, family_key="mlb_props", stat_key="hits",
        sample_size=100, empirical_coverage=0.80,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    sequence = [(e["family_key"], e["stat_key"]) for e in payload["interval_models"]]
    assert sequence == [
        ("mlb_props", "hits"),
        ("nba_props", "assists"),
        ("nba_props", "points"),
        ("nba_props", "rebounds"),
    ]


# -- Dedupe across shared artifact_dir ------------------------------
#
# sika's production manifest has a single ``global_v1_<date>``
# artifact_dir referenced by all 4 ``serves_family_key`` entries
# (nba_props, nba_singles, mlb_props, mlb_singles). The intervals
# under that artifact_dir's ``interval_models/`` are physical files
# shared across the manifest entries, but each is TRAINED for one
# family — recorded in metadata.json's ``family_key`` field. Without
# dedupe, walking the manifest reports the same physical file under
# every family entry that shares the dir → 4× over-reporting that
# also mis-labels rows (e.g. "mlb_props/made_threes" doesn't exist
# as an MLB prop). Fix: dedupe by (artifact_dir, stat_key) and
# attribute to metadata.json's ``family_key``.


def _multi_family_manifest(
    tmp_path: Path,
    *,
    shared_artifact_dir: Path,
    families: tuple[str, ...] = ("nba_props", "nba_singles", "mlb_props", "mlb_singles"),
) -> Path:
    """Build a manifest where multiple ``serves_family_key`` entries
    point at the SAME ``artifact_path`` — mirrors sika's production
    global_v1 manifest topology."""
    manifest_path = tmp_path / "manifests" / "current.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    relative = Path(
        os.path.relpath(shared_artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.write_text(
        json.dumps({
            "version": "test",
            "families": [
                {
                    "family_key": "global_v1",
                    "serves_family_key": family_key,
                    "model_name": "global-model",
                    "model_version": "v1",
                    "artifact_path": relative,
                    "mode": "ml",
                }
                for family_key in families
            ],
        }),
        encoding="utf-8",
    )
    return manifest_path


def test_inspect_intervals_does_not_duplicate_when_artifact_serves_multiple_families(
    tmp_path: Path,
) -> None:
    """One artifact_dir + one points/ subdir + 4 families sharing the
    artifact in the manifest → ONE row in the inspect output (not four).
    """
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = _multi_family_manifest(tmp_path, shared_artifact_dir=artifact_dir)
    _seed_interval_artifact(
        artifact_dir,
        family_key="nba_props",
        stat_key="points",
        sample_size=127,
        empirical_coverage=0.81,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert len(payload["interval_models"]) == 1
    # Family attribution comes from metadata.json's family_key, not
    # from the manifest's serves_family_key (which is ambiguous when
    # multiple entries share an artifact).
    assert payload["interval_models"][0]["family_key"] == "nba_props"


def test_inspect_intervals_attributes_each_stat_to_its_metadata_family(
    tmp_path: Path,
) -> None:
    """Two stat keys, each trained for a different family, sharing one
    artifact_dir referenced by 4 manifest entries → exactly 2 rows,
    each labeled by its metadata's family_key. This is the
    nba_props/points + mlb_props/total_bases case in sika's
    production setup."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = _multi_family_manifest(tmp_path, shared_artifact_dir=artifact_dir)
    _seed_interval_artifact(
        artifact_dir, family_key="nba_props", stat_key="points",
        sample_size=100, empirical_coverage=0.80,
    )
    _seed_interval_artifact(
        artifact_dir, family_key="mlb_props", stat_key="total_bases",
        sample_size=60, empirical_coverage=0.85,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    pairs = {(e["family_key"], e["stat_key"]) for e in payload["interval_models"]}
    assert pairs == {("nba_props", "points"), ("mlb_props", "total_bases")}


def test_inspect_intervals_family_filter_uses_metadata_family(
    tmp_path: Path,
) -> None:
    """``--family-key nba_props`` filters by metadata family_key, not
    by manifest serves_family_key. So when the manifest serves all 4
    families from one artifact_dir but only nba_props has trained
    intervals, the filter still works correctly."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = _multi_family_manifest(tmp_path, shared_artifact_dir=artifact_dir)
    _seed_interval_artifact(
        artifact_dir, family_key="nba_props", stat_key="points",
        sample_size=100, empirical_coverage=0.80,
    )
    _seed_interval_artifact(
        artifact_dir, family_key="mlb_props", stat_key="hits",
        sample_size=80, empirical_coverage=0.78,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--family-key", "nba_props",
        "--format", "json",
    ])

    assert rc == 0
    assert len(payload["interval_models"]) == 1
    assert payload["interval_models"][0]["family_key"] == "nba_props"
    assert payload["interval_models"][0]["stat_key"] == "points"


def test_inspect_intervals_unknown_family_when_metadata_missing(
    tmp_path: Path,
) -> None:
    """When metadata.json is missing we can't trust any single
    family attribution — label the row's family_key as the manifest's
    serves_family_key for the FIRST entry that maps to this
    artifact_dir, and surface ``coverage_status='unknown'`` so the
    operator sees the metadata is bad. Better than dropping the row
    entirely (which would hide the partial-write from the operator)."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    stat_dir = artifact_dir / "interval_models" / "points"
    stat_dir.mkdir(parents=True)
    (stat_dir / "p10.joblib").write_bytes(b"")
    (stat_dir / "p50.joblib").write_bytes(b"")
    (stat_dir / "p90.joblib").write_bytes(b"")
    manifest_path = _multi_family_manifest(tmp_path, shared_artifact_dir=artifact_dir)

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert len(payload["interval_models"]) == 1
    assert payload["interval_models"][0]["coverage_status"] == "unknown"


# -- Coverage banding -----------------------------------------------


def test_inspect_intervals_coverage_status_well_calibrated(
    tmp_path: Path,
) -> None:
    """Coverage in [0.70, 0.90] is green/ok — the 80% interval is
    behaving."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])
    _seed_interval_artifact(
        artifact_dir, family_key="nba_props", stat_key="points",
        sample_size=200, empirical_coverage=0.82,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert payload["interval_models"][0]["coverage_status"] == "ok"


def test_inspect_intervals_coverage_status_warn_on_edge(
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])
    _seed_interval_artifact(
        artifact_dir, family_key="nba_props", stat_key="points",
        sample_size=200, empirical_coverage=0.65,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert payload["interval_models"][0]["coverage_status"] == "warn"


def test_inspect_intervals_coverage_status_bad_when_out_of_range(
    tmp_path: Path,
) -> None:
    """Coverage < 0.60 or > 0.95 is red/bad — the regressor is
    significantly mis-calibrated. Operators must fix the upstream
    BEFORE shipping the consumer (phase 2d gate)."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    artifact_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])
    _seed_interval_artifact(
        artifact_dir, family_key="nba_props", stat_key="points",
        sample_size=200, empirical_coverage=0.45,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert payload["interval_models"][0]["coverage_status"] == "bad"


def test_inspect_intervals_coverage_status_unknown_when_metadata_missing(
    tmp_path: Path,
) -> None:
    """A stat directory with joblibs but no metadata.json (partial
    write, manual placement) — surface as ``unknown`` rather than
    crashing or guessing."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    stat_dir = artifact_dir / "interval_models" / "points"
    stat_dir.mkdir(parents=True)
    (stat_dir / "p10.joblib").write_bytes(b"")
    (stat_dir / "p50.joblib").write_bytes(b"")
    (stat_dir / "p90.joblib").write_bytes(b"")
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert payload["interval_models"][0]["coverage_status"] == "unknown"
    assert payload["interval_models"][0]["empirical_coverage"] is None


def test_inspect_intervals_skips_malformed_metadata_with_warning(
    tmp_path: Path,
) -> None:
    """A metadata.json that's not valid JSON should surface as
    ``unknown`` (same as missing) — never crash the entire
    inspection."""
    artifact_dir = tmp_path / "artifacts" / "global_v1"
    stat_dir = artifact_dir / "interval_models" / "points"
    stat_dir.mkdir(parents=True)
    (stat_dir / "p10.joblib").write_bytes(b"")
    (stat_dir / "p50.joblib").write_bytes(b"")
    (stat_dir / "p90.joblib").write_bytes(b"")
    (stat_dir / "metadata.json").write_text("not valid json {", encoding="utf-8")
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(manifest_path, family_artifacts=[("nba_props", artifact_dir)])

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    assert payload["interval_models"][0]["coverage_status"] == "unknown"


# -- Per-family aggregate -------------------------------------------


def test_inspect_intervals_table_shows_per_family_counts(
    tmp_path: Path,
) -> None:
    """The footer should summarize how many stat keys per family
    are trained — at-a-glance "is this family covered?" answer."""
    nba_dir = tmp_path / "artifacts" / "nba"
    mlb_dir = tmp_path / "artifacts" / "mlb"
    nba_dir.mkdir(parents=True)
    mlb_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(
        manifest_path,
        family_artifacts=[("nba_props", nba_dir), ("mlb_props", mlb_dir)],
    )
    for stat_key in ("points", "rebounds", "assists"):
        _seed_interval_artifact(
            nba_dir, family_key="nba_props", stat_key=stat_key,
            sample_size=100, empirical_coverage=0.80,
        )

    rc, output = _run_cli([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
    ])

    assert rc == 0
    # Per-family count surfaces somewhere in the output.
    assert "nba_props" in output
    assert "3" in output


# -- Missing artifact dir -------------------------------------------


def test_inspect_intervals_continues_when_one_artifact_dir_missing(
    tmp_path: Path,
) -> None:
    """A manifest entry whose ``artifact_path`` points at a non-existent
    directory should skip that family with a warning rather than fail
    the whole inspection. Other families' artifacts still surface."""
    nba_dir = tmp_path / "artifacts" / "nba"
    missing_dir = tmp_path / "artifacts" / "does-not-exist"
    nba_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(
        manifest_path,
        family_artifacts=[("nba_props", nba_dir), ("mlb_props", missing_dir)],
    )
    _seed_interval_artifact(
        nba_dir, family_key="nba_props", stat_key="points",
        sample_size=100, empirical_coverage=0.80,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--format", "json",
    ])

    assert rc == 0
    families_seen = {e["family_key"] for e in payload["interval_models"]}
    assert families_seen == {"nba_props"}


# -- Family filter --------------------------------------------------


def test_inspect_intervals_family_filter(tmp_path: Path) -> None:
    """``--family-key`` filters the output to one served family —
    useful for piping into shell scripts that act per-family."""
    nba_dir = tmp_path / "artifacts" / "nba"
    mlb_dir = tmp_path / "artifacts" / "mlb"
    nba_dir.mkdir(parents=True)
    mlb_dir.mkdir(parents=True)
    manifest_path = tmp_path / "manifests" / "current.json"
    _build_manifest(
        manifest_path,
        family_artifacts=[("nba_props", nba_dir), ("mlb_props", mlb_dir)],
    )
    _seed_interval_artifact(
        nba_dir, family_key="nba_props", stat_key="points",
        sample_size=100, empirical_coverage=0.80,
    )
    _seed_interval_artifact(
        mlb_dir, family_key="mlb_props", stat_key="hits",
        sample_size=100, empirical_coverage=0.80,
    )

    rc, payload = _run_cli_json([
        "inspect-intervals",
        "--manifest-path", str(manifest_path),
        "--family-key", "mlb_props",
        "--format", "json",
    ])

    assert rc == 0
    assert len(payload["interval_models"]) == 1
    assert payload["interval_models"][0]["family_key"] == "mlb_props"

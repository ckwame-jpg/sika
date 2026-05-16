"""Tests for Smarter #21 phase 2b operator UX — readiness panel
interval-model surface.

Extends ``/ops/models/readiness`` (and ``build_model_readiness_summary``)
with ``interval_models: list[IntervalModelStatusRead]`` so operators can
see in the browser what they can already see via the
``python -m ml.cli inspect-intervals`` CLI (PR #163).

Same source of truth as the CLI: walks the active manifest's families,
reads ``<artifact_dir>/interval_models/<stat>/metadata.json``, and
classifies empirical coverage into ok / warn / bad / unknown.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed_manifest_and_artifact(
    tmp_path: Path,
    *,
    family_key: str = "nba_props",
    stat_artifacts: list[tuple[str, dict]] | None = None,
) -> Path:
    """Drop a manifest at ``tmp_path/manifest.json`` pointing at an
    artifact_dir with interval_models/<stat>/metadata.json seeded for
    each (stat_key, metadata_payload) in ``stat_artifacts``."""
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260516"
    artifact_dir.mkdir(parents=True)
    for stat_key, metadata in stat_artifacts or []:
        stat_dir = artifact_dir / "interval_models" / stat_key
        stat_dir.mkdir(parents=True)
        (stat_dir / "p10.joblib").write_bytes(b"")
        (stat_dir / "p50.joblib").write_bytes(b"")
        (stat_dir / "p90.joblib").write_bytes(b"")
        (stat_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8",
        )
    manifest_path = tmp_path / "manifest.json"
    relative = Path(
        os.path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.write_text(
        json.dumps({
            "version": "test",
            "serving_mode": "shadow",
            "families": [
                {
                    "family_key": "global_v1",
                    "serves_family_key": family_key,
                    "model_name": "global-model",
                    "model_version": "v1",
                    "artifact_path": relative,
                    "mode": "ml",
                }
            ],
        }, indent=2),
        encoding="utf-8",
    )
    return manifest_path


def _pin_manifest_path(monkeypatch: pytest.MonkeyPatch, manifest_path: Path) -> None:
    """Override the settings.ml_manifest_path so the readiness builder's
    ``load_model_manifest()`` finds the test manifest. Mirrors the
    fixture pattern in other readiness tests."""
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    get_settings.cache_clear()


def _metadata(
    *,
    family_key: str = "nba_props",
    stat_key: str = "points",
    sample_size: int = 127,
    empirical_coverage: float = 0.81,
    trained_at: str = "2026-05-15T10:00:00+00:00",
    window_start: str = "2026-04-15T00:00:00+00:00",
    window_end: str = "2026-05-15T00:00:00+00:00",
) -> dict:
    return {
        "family_key": family_key,
        "stat_key": stat_key,
        "quantiles": [0.1, 0.5, 0.9],
        "sample_size": sample_size,
        "empirical_coverage": empirical_coverage,
        "trained_at": trained_at,
        "window_start": window_start,
        "window_end": window_end,
    }


# -- /ops/models/readiness response shape -----------------------------


def test_readiness_endpoint_includes_interval_models_field_when_none_trained(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with no interval artifacts the field is present (empty
    list) — keeps the schema stable for the UI's TypeScript types."""
    manifest_path = _seed_manifest_and_artifact(tmp_path, stat_artifacts=[])
    _pin_manifest_path(monkeypatch, manifest_path)

    response = client.get("/ops/models/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert "interval_models" in payload
    assert payload["interval_models"] == []


def test_readiness_endpoint_lists_trained_interval_models(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _seed_manifest_and_artifact(
        tmp_path,
        stat_artifacts=[
            ("points", _metadata(stat_key="points", empirical_coverage=0.81)),
            ("rebounds", _metadata(stat_key="rebounds", empirical_coverage=0.62)),
        ],
    )
    _pin_manifest_path(monkeypatch, manifest_path)

    response = client.get("/ops/models/readiness")

    assert response.status_code == 200
    payload = response.json()
    entries = payload["interval_models"]
    assert len(entries) == 2
    # Deterministic sort by (family_key, stat_key).
    assert [(e["family_key"], e["stat_key"]) for e in entries] == [
        ("nba_props", "points"),
        ("nba_props", "rebounds"),
    ]


def test_readiness_endpoint_classifies_coverage_status(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same band classification as the CLI (PR #163):
      [0.70, 0.90]               -> ok
      [0.60, 0.70) or (0.90, 0.95] -> warn
      < 0.60 or > 0.95           -> bad
    """
    manifest_path = _seed_manifest_and_artifact(
        tmp_path,
        stat_artifacts=[
            ("points",   _metadata(stat_key="points",   empirical_coverage=0.81)),  # ok
            ("rebounds", _metadata(stat_key="rebounds", empirical_coverage=0.62)),  # warn
            ("assists",  _metadata(stat_key="assists",  empirical_coverage=0.45)),  # bad
        ],
    )
    _pin_manifest_path(monkeypatch, manifest_path)

    response = client.get("/ops/models/readiness")
    statuses = {e["stat_key"]: e["coverage_status"] for e in response.json()["interval_models"]}
    assert statuses == {"points": "ok", "rebounds": "warn", "assists": "bad"}


def test_readiness_endpoint_surfaces_sample_size_and_trained_at(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _seed_manifest_and_artifact(
        tmp_path,
        stat_artifacts=[
            ("points", _metadata(
                stat_key="points",
                sample_size=200,
                empirical_coverage=0.80,
                trained_at="2026-05-16T10:00:00+00:00",
            )),
        ],
    )
    _pin_manifest_path(monkeypatch, manifest_path)

    entry = client.get("/ops/models/readiness").json()["interval_models"][0]
    assert entry["sample_size"] == 200
    assert entry["empirical_coverage"] == 0.80
    # Pydantic normalizes UTC to Z notation; parse instead of string-comparing
    # so the test isn't coupled to that serializer choice.
    assert datetime.fromisoformat(entry["trained_at"].replace("Z", "+00:00")) == datetime(
        2026, 5, 16, 10, 0, tzinfo=timezone.utc,
    )


def test_readiness_endpoint_marks_unknown_when_metadata_missing(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat directory with joblibs but no metadata.json (partial
    write) surfaces as ``unknown`` — same fallback as the CLI."""
    artifact_dir = tmp_path / "artifacts" / "global_v1_20260516"
    stat_dir = artifact_dir / "interval_models" / "points"
    stat_dir.mkdir(parents=True)
    (stat_dir / "p10.joblib").write_bytes(b"")
    (stat_dir / "p50.joblib").write_bytes(b"")
    (stat_dir / "p90.joblib").write_bytes(b"")
    manifest_path = tmp_path / "manifest.json"
    relative = Path(
        os.path.relpath(artifact_dir.resolve(), manifest_path.parent.resolve())
    ).as_posix()
    manifest_path.write_text(
        json.dumps({
            "version": "test",
            "serving_mode": "shadow",
            "families": [
                {
                    "family_key": "global_v1",
                    "serves_family_key": "nba_props",
                    "model_name": "global-model",
                    "model_version": "v1",
                    "artifact_path": relative,
                    "mode": "ml",
                }
            ],
        }, indent=2),
        encoding="utf-8",
    )
    _pin_manifest_path(monkeypatch, manifest_path)

    entries = client.get("/ops/models/readiness").json()["interval_models"]
    assert len(entries) == 1
    assert entries[0]["coverage_status"] == "unknown"
    assert entries[0]["empirical_coverage"] is None


def test_readiness_endpoint_skips_missing_artifact_dir_silently(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale manifest pointing at a non-existent artifact_dir → no
    crash; the family contributes zero interval entries. Mirrors the
    CLI's defensive behavior."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "version": "test",
            "serving_mode": "shadow",
            "families": [
                {
                    "family_key": "global_v1",
                    "serves_family_key": "nba_props",
                    "model_name": "global-model",
                    "model_version": "v1",
                    "artifact_path": "artifacts/does-not-exist",
                    "mode": "ml",
                }
            ],
        }, indent=2),
        encoding="utf-8",
    )
    _pin_manifest_path(monkeypatch, manifest_path)

    response = client.get("/ops/models/readiness")
    assert response.status_code == 200
    assert response.json()["interval_models"] == []


# -- Cross-package drift guard ----------------------------------------


def test_coverage_bands_match_ml_cli_constants(tmp_path: Path) -> None:
    """``apps/api/app/services/ml/interval_status.py`` and
    ``apps/ml/ml/cli.py`` both define INTERVAL_COVERAGE_* constants
    because apps/ml isn't on the API's import path. This test reads
    the apps/ml file as text and parses out the four constants so a
    silent drift between the two band definitions surfaces in CI.

    Without this, an operator running the CLI would see one
    classification and the UI panel a different one for the same
    artifact — confusing at best, misleading at worst.
    """
    from app.services.ml.interval_status import (
        INTERVAL_COVERAGE_OK_LOWER,
        INTERVAL_COVERAGE_OK_UPPER,
        INTERVAL_COVERAGE_WARN_LOWER,
        INTERVAL_COVERAGE_WARN_UPPER,
    )
    import re

    # Resolve apps/ml/ml/cli.py relative to the API's package root.
    repo_root = Path(__file__).resolve().parents[3]
    cli_path = repo_root / "apps" / "ml" / "ml" / "cli.py"
    assert cli_path.exists(), f"apps/ml/ml/cli.py not found at {cli_path}"
    source = cli_path.read_text(encoding="utf-8")

    pattern = re.compile(r"^(INTERVAL_COVERAGE_\w+)\s*=\s*([\d.]+)$", re.MULTILINE)
    ml_constants = {name: float(value) for name, value in pattern.findall(source)}

    assert ml_constants == {
        "INTERVAL_COVERAGE_OK_LOWER": INTERVAL_COVERAGE_OK_LOWER,
        "INTERVAL_COVERAGE_OK_UPPER": INTERVAL_COVERAGE_OK_UPPER,
        "INTERVAL_COVERAGE_WARN_LOWER": INTERVAL_COVERAGE_WARN_LOWER,
        "INTERVAL_COVERAGE_WARN_UPPER": INTERVAL_COVERAGE_WARN_UPPER,
    }


def test_readiness_endpoint_returns_empty_list_when_no_manifest_configured(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No manifest path configured / file missing → empty list. The
    pre-ML-rollout state."""
    monkeypatch.setenv("ML_MANIFEST_PATH", "")
    get_settings.cache_clear()

    response = client.get("/ops/models/readiness")
    assert response.status_code == 200
    assert response.json()["interval_models"] == []

"""Full-corpus retrain coverage.

Three behaviors that the 2026-05-11 retrain depends on:

1. ``_prepare_frame`` retains NBA + MLB singles rows alongside props
   (regression — the loader used to filter to ``market_family=="player_prop"``
   only, which silently dropped every singles row from training).
2. ``train_and_package`` emits one manifest entry per ``serve_family_keys``
   so ``manifests/current.json`` covers all four active families from a
   single global artifact.
3. The CLI maps ``--advanced-only {auto,yes,no}`` to the right
   ``advanced_only`` value and parses ``--serve-family-keys`` correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ml.cli import build_parser
from ml.dataset import settled_predictions_from_records
from ml.training import train_and_package


def _mixed_records(total: int = 240) -> list[dict]:
    """Half props, half singles, evenly split between NBA and MLB."""
    base = datetime(2026, 4, 17, 18, 0, tzinfo=timezone.utc)
    rows: list[dict] = []
    for index in range(total):
        sport = "MLB" if index % 2 == 0 else "NBA"
        is_prop = (index // 2) % 2 == 0
        market_family = "player_prop" if is_prop else "moneyline"
        if is_prop:
            family = "mlb_props" if sport == "MLB" else "nba_props"
        else:
            family = "mlb_singles" if sport == "MLB" else "nba_singles"
        recent_average = float(index % 12) + (2.0 if sport == "MLB" else 0.0)
        threshold = float(index % 10) + 4.5
        won = recent_average + (1.0 if sport == "MLB" else 0.0) > threshold
        rows.append(
            {
                "id": index + 1,
                "market_id": index + 1,
                "event_id": (index // 6) + 1,
                "ticker": f"TEST-{index}",
                "sport_key": sport,
                "event_name": f"Event {index // 6}",
                "market_family": market_family,
                "market_kind": market_family,
                "stat_key": "hits" if is_prop else "winner",
                "threshold": threshold,
                "subject_name": f"Player {index % 24}" if is_prop else None,
                "subject_team": f"Team {index % 8}",
                "capture_scope": "recommendation",
                "side": "yes",
                "suggested_price": 0.44 + ((index % 5) * 0.02),
                "fair_yes_price": 0.55 if won else 0.42,
                "edge": 0.08 if won else -0.05,
                "confidence": 0.62,
                "selection_score": 0.12,
                "features": {
                    "family_key": family,
                    "recent_average": recent_average,
                    "threshold": threshold,
                    "yes_probability": 0.62 if won else 0.41,
                    "has_team_context": True,
                    "latest_log_days_ago": index % 4,
                },
                "scoring_diagnostics": {},
                "market_status_at_capture": "active",
                "prediction_outcome": "won" if won else "lost",
                "settled_at": (base + timedelta(hours=index)).isoformat(),
                "realized_pnl": 0.56 if won else -0.44,
                "captured_at": (base + timedelta(minutes=index)).isoformat(),
            }
        )
    return rows


def test_prepare_frame_retains_singles_rows():
    frame = settled_predictions_from_records(_mixed_records(total=80))
    families = set(frame["family_key"].unique())
    assert {"nba_props", "mlb_props", "nba_singles", "mlb_singles"}.issubset(families)


def test_train_and_package_emits_one_manifest_entry_per_serve_family_key(tmp_path):
    frame = settled_predictions_from_records(_mixed_records(total=240))
    serve_keys = ("mlb_props", "nba_props", "mlb_singles", "nba_singles")

    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_keys=serve_keys,
        model_version="2026-05-11",
    )

    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    emitted_keys = tuple(family["serves_family_key"] for family in manifest["families"])
    assert emitted_keys == serve_keys
    artifact_paths = {family["artifact_path"] for family in manifest["families"]}
    assert len(artifact_paths) == 1, "All families share one global artifact path."


def test_train_and_package_back_compat_with_serve_family_key(tmp_path):
    frame = settled_predictions_from_records(_mixed_records(total=240))

    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-05-11",
    )

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert [family["serves_family_key"] for family in manifest["families"]] == ["mlb_props"]


def test_train_and_package_rejects_both_serve_args(tmp_path):
    frame = settled_predictions_from_records(_mixed_records(total=240))

    with pytest.raises(ValueError, match="either serve_family_keys or serve_family_key"):
        train_and_package(
            frame,
            artifact_root=tmp_path / "artifacts",
            manifest_out=tmp_path / "manifests" / "current.json",
            serve_family_key="mlb_props",
            serve_family_keys=("nba_props",),
            model_version="2026-05-11",
        )


def test_train_and_package_advanced_only_false_keeps_all_rows(tmp_path):
    """With advanced_only=False, training rows == prepared dataset rows even
    when a family has crossed what would otherwise be the auto-trigger
    threshold."""
    frame = settled_predictions_from_records(_mixed_records(total=240))
    expected_rows = len(frame)

    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_keys=("mlb_props", "nba_props", "mlb_singles", "nba_singles"),
        model_version="2026-05-11",
        advanced_only=False,
        advanced_only_threshold=10,
    )
    metadata = json.loads((result.artifact_dir / "training_metadata.json").read_text())
    assert metadata["advanced_only_active"] is False
    assert metadata["training_rows"] == expected_rows


# -----------------------------------------------------------------------------
# CLI argument parsing


def test_cli_train_defaults_cover_all_active_families():
    parser = build_parser()
    args = parser.parse_args(["train"])
    # Smarter WNBA PR 5 adds the WNBA families to the default. Order
    # matters: the tuple here mirrors the comma-separated string in
    # _DEFAULT_SERVE_FAMILY_KEYS — props first (NBA, MLB, WNBA), then
    # singles in the same sport order.
    assert args.serve_family_keys == (
        "mlb_props", "nba_props", "wnba_props",
        "mlb_singles", "nba_singles", "wnba_singles",
    )
    assert args.advanced_only == "auto"


def test_cli_train_parses_advanced_only_no_and_custom_keys():
    parser = build_parser()
    args = parser.parse_args(
        [
            "train",
            "--advanced-only",
            "no",
            "--serve-family-keys",
            "mlb_props,nba_props",
        ]
    )
    assert args.advanced_only == "no"
    assert args.serve_family_keys == ("mlb_props", "nba_props")


def test_cli_train_rejects_empty_serve_family_keys():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["train", "--serve-family-keys", ", ,"])


def test_default_serve_family_keys_include_wnba_props_and_singles():
    """Smarter WNBA PR 5 — the weekly retrain CLI's default serve set
    must include wnba_props + wnba_singles so the manifest auto-picks
    them up alongside NBA + MLB families. Without this, the readiness
    panel would never see WNBA models even after settled rows exist
    (the operator would have to explicitly pass --serve-family-keys).
    """
    parser = build_parser()
    args = parser.parse_args(["train"])
    assert "wnba_props" in args.serve_family_keys
    assert "wnba_singles" in args.serve_family_keys
    # Pre-existing NBA / MLB defaults must still be present (regression
    # pin — PR 5 adds WNBA, doesn't replace).
    assert "nba_props" in args.serve_family_keys
    assert "mlb_props" in args.serve_family_keys
    assert "nba_singles" in args.serve_family_keys
    assert "mlb_singles" in args.serve_family_keys


def test_train_and_package_emits_wnba_family_entries_when_in_serve_set(tmp_path):
    """End-to-end pin: when ``serve_family_keys`` includes the WNBA
    families, the manifest output gets a ``serves_family_key`` entry
    per WNBA family. The manifest consumer (apps/api readiness panel)
    keys off these entries to decide which families to surface — so
    a missing entry would silently drop WNBA from the panel.

    Reuses the existing NBA / MLB fixture data — the training path is
    sport-agnostic (one global artifact serving all families), so
    WNBA families being present in the manifest is what matters at
    this layer. Per-family WNBA rows accumulate in production once
    PR 6 lands.
    """
    frame = settled_predictions_from_records(_mixed_records(total=240))
    serve_keys = ("mlb_props", "nba_props", "wnba_props", "wnba_singles")

    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_keys=serve_keys,
        model_version="2026-05-17",
    )

    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    emitted_keys = {family["serves_family_key"] for family in manifest["families"]}
    assert {"wnba_props", "wnba_singles"}.issubset(emitted_keys)

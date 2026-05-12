from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import joblib

from ml.dataset import settled_predictions_from_records
from ml.features import FeatureSpec, vectorize
from ml.training import train_and_package


def _records(total: int = 240):
    base = datetime(2026, 4, 17, 18, 0, tzinfo=timezone.utc)
    rows = []
    for index in range(total):
        sport = "MLB" if index % 2 == 0 else "NBA"
        family = "mlb_props" if sport == "MLB" else "nba_props"
        recent_average = float(index % 12) + (2.0 if sport == "MLB" else 0.0)
        threshold = float(index % 10) + 4.5
        won = recent_average + (1.0 if family == "mlb_props" else 0.0) > threshold
        rows.append(
            {
                "id": index + 1,
                "market_id": index + 1,
                "event_id": (index // 6) + 1,
                "ticker": f"TEST-{index}",
                "sport_key": sport,
                "event_name": f"Event {index // 6}",
                "market_family": "player_prop",
                "market_kind": "player_prop",
                "stat_key": "hits" if sport == "MLB" else "points",
                "threshold": threshold,
                "subject_name": f"Player {index % 24}",
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


def test_training_smoke_writes_artifact_and_manifest(tmp_path):
    frame = settled_predictions_from_records(_records())

    result = train_and_package(
        frame,
        artifact_root=tmp_path / "artifacts",
        manifest_out=tmp_path / "manifests" / "current.json",
        serve_family_key="mlb_props",
        model_version="2026-04-24",
    )

    assert (result.artifact_dir / "model.joblib").exists()
    assert (result.artifact_dir / "feature_spec.json").exists()
    assert (result.artifact_dir / "training_metadata.json").exists()
    assert result.manifest_path is not None
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    family = manifest["families"][0]
    assert family["serves_family_key"] == "mlb_props"
    assert family["metadata"]["behavior"] == "sklearn_predict_proba"
    # Bug #2: manifest must record target_type so future runtime checks can
    # tell whether predict_proba[:,1] is P(YES) or the legacy P(selected-won).
    assert family["metadata"]["target_type"] == "yes_won"

    spec = FeatureSpec.from_dict(json.loads((result.artifact_dir / "feature_spec.json").read_text(encoding="utf-8")))
    model = joblib.load(result.artifact_dir / "model.joblib")
    vector = vectorize(frame.iloc[0]["features"], spec).reshape(1, -1)
    probability = float(model.predict_proba(vector)[0][1])
    assert 0.0 <= probability <= 1.0
    assert result.metrics["metrics"][result.metrics["winner"]]["player_group"]["rows"] > 0

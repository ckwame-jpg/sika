import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth, Prediction
from app.services.ml.runtime import resolve_family_runtime, run_serving_inference, run_shadow_inference
from app.services.operator_settings import set_ml_serving_mode


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_artifact(
    tmp_path,
    *,
    family_key: str,
    scope: str,
    probability: float = 0.64,
    behavior: str = "static_probability",
    serves_family_key: str | None = None,
):
    artifact_path = tmp_path / f"{family_key}.json"
    artifact_path.write_text(
        json.dumps(
            {
                "family_key": family_key,
                **({"serves_family_key": serves_family_key} if serves_family_key else {}),
                "scope": scope,
                "behavior": behavior,
                "probability": probability,
                "confidence": probability,
                "metadata": {"source": "test-artifact"},
            }
        ),
        encoding="utf-8",
    )
    return artifact_path


def _write_manifest(
    tmp_path,
    *,
    family_key: str,
    artifact_path: str,
    mode: str = "ml",
    serves_family_key: str | None = None,
):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": "test-manifest",
                "serving_mode": "ml",
                "families": [
                    {
                        "family_key": family_key,
                        **({"serves_family_key": serves_family_key} if serves_family_key else {}),
                        "model_name": f"{family_key}-model",
                        "model_version": "test-v1",
                        "calibration_version": "test-cal",
                        "feature_set_version": "test-features",
                        "artifact_path": artifact_path,
                        "mode": mode,
                        "metadata": {"source": "manifest"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _seed_nba_single_predictions(db_session, *, total: int, settled: int):
    for index in range(total):
        outcome = "pending"
        settlement_status = "pending"
        settled_at = None
        realized_pnl = None
        if index < settled:
            outcome = "won" if index % 2 == 0 else "lost"
            settlement_status = "settled"
            settled_at = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
            realized_pnl = 0.18 if outcome == "won" else -0.42

        db_session.add(
            Prediction(
                run_id=1,
                event_id=None,
                market_id=index + 1,
                ticker=f"NBA-RUNTIME-{index}",
                sport_key="NBA",
                event_name="Runtime test game",
                market_title="Runtime test market",
                market_family="winner",
                market_kind="game_winner",
                side="yes",
                action="buy",
                suggested_price=0.45,
                fair_yes_price=0.58,
                fair_no_price=0.42,
                edge=0.13,
                confidence=0.68,
                selection_score=0.17,
                model_name="heuristic-v1",
                rationale="Runtime rationale",
                reasons=["runtime"],
                features={"family_key": "nba_singles"},
                scoring_diagnostics={},
                market_status_at_capture="active",
                settlement_status=settlement_status,
                prediction_outcome=outcome,
                settled_at=settled_at,
                realized_pnl=realized_pnl,
                captured_at=datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc),
            )
        )
    db_session.flush()


def test_resolve_family_runtime_global_heuristic_overrides_family_modes(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="ml")
    monkeypatch.setenv("ML_SERVING_MODE", "heuristic")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "heuristic"
    assert decision.effective_mode == "heuristic"
    assert decision.fallback_active is False


def test_resolve_family_runtime_uses_operator_serving_mode_override(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "heuristic")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    set_ml_serving_mode(db_session, "shadow")
    db_session.commit()

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "shadow"
    assert decision.effective_mode == "shadow"


def test_ml_family_modes_override_manifest_mode(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "ml"
    assert decision.effective_mode == "ml"
    assert decision.runtime_health == "healthy"


def test_active_study_family_auto_promotes_to_shadow_when_history_and_artifact_are_ready(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="heuristic")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.delenv("ML_FAMILY_MODES_JSON", raising=False)
    _seed_nba_single_predictions(db_session, total=40, settled=40)

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "shadow"
    assert decision.effective_mode == "shadow"
    assert decision.runtime_health == "healthy"


def test_manual_ml_override_still_wins_over_auto_shadow(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="heuristic")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))
    _seed_nba_single_predictions(db_session, total=40, settled=40)

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "ml"
    assert decision.effective_mode == "ml"
    assert decision.runtime_health == "healthy"


def test_global_heuristic_mode_keeps_auto_shadow_off(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="heuristic")
    monkeypatch.setenv("ML_SERVING_MODE", "heuristic")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.delenv("ML_FAMILY_MODES_JSON", raising=False)
    _seed_nba_single_predictions(db_session, total=40, settled=40)

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "heuristic"
    assert decision.effective_mode == "heuristic"
    assert decision.fallback_active is False


def test_auto_shadow_stays_heuristic_when_no_manifest_is_available(db_session, monkeypatch):
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.delenv("ML_MANIFEST_PATH", raising=False)
    monkeypatch.delenv("ML_FAMILY_MODES_JSON", raising=False)
    _seed_nba_single_predictions(db_session, total=40, settled=40)

    decision = resolve_family_runtime(db_session, "nba_singles", scope="single")

    assert decision.desired_mode == "heuristic"
    assert decision.effective_mode == "heuristic"
    assert decision.runtime_health == "healthy"


def test_run_serving_inference_falls_back_when_artifact_missing(db_session, monkeypatch, tmp_path):
    missing_path = tmp_path / "missing-artifact.json"
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(missing_path), mode="ml")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    result, decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert result is None
    assert decision.desired_mode == "ml"
    assert decision.effective_mode == "heuristic"
    assert decision.runtime_health == "unavailable"
    assert decision.fallback_active is True


def test_run_serving_inference_degrades_and_recovers_after_cooldown(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single", behavior="raise")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="ml")
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "ml"}))

    third_decision = None
    for _ in range(3):
        _, third_decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert third_decision is not None
    assert third_decision.runtime_health == "degraded"
    assert third_decision.fallback_active is True

    artifact_path.write_text(
        json.dumps(
            {
                "family_key": "nba_singles",
                "scope": "single",
                "behavior": "static_probability",
                "probability": 0.61,
                "confidence": 0.61,
                "metadata": {"source": "recovered"},
            }
        ),
        encoding="utf-8",
    )
    runtime_row = db_session.query(ModelFamilyRuntimeHealth).filter_by(family_key="nba_singles").one()
    runtime_row.degraded_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.flush()

    result, recovered_decision = run_serving_inference(db_session, family_key="nba_singles", scope="single")

    assert result is not None
    assert recovered_decision.effective_mode == "ml"
    assert recovered_decision.runtime_health == "healthy"
    assert recovered_decision.fallback_active is False


def test_run_shadow_inference_failure_does_not_activate_serving_fallback(db_session, monkeypatch, tmp_path):
    artifact_path = _write_artifact(tmp_path, family_key="nba_singles", scope="single", behavior="raise")
    manifest_path = _write_manifest(tmp_path, family_key="nba_singles", artifact_path=str(artifact_path), mode="shadow")
    monkeypatch.setenv("ML_SERVING_MODE", "shadow")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({"nba_singles": "shadow"}))

    result, decision = run_shadow_inference(db_session, family_key="nba_singles", scope="single")

    assert result is None
    assert decision.desired_mode == "shadow"
    assert decision.fallback_active is False


@pytest.mark.parametrize(
    ("live_family_key", "artifact_family_key", "scope"),
    [
        ("mlb_singles", "mlb_singles_v1", "single"),
        ("nba_props", "nba_props_v1", "single"),
        ("mlb_props", "mlb_props_v1", "single"),
        ("nba_parlay_2leg", "nba_parlay_2leg_v1", "parlay"),
        ("mlb_parlay_2leg", "mlb_parlay_2leg_v1", "parlay"),
        ("mixed_parlay_2leg", "mixed_parlay_2leg_v1", "parlay"),
    ],
)
def test_versioned_manifest_family_can_serve_live_family_key(
    db_session,
    monkeypatch,
    tmp_path,
    live_family_key,
    artifact_family_key,
    scope,
):
    artifact_path = _write_artifact(
        tmp_path,
        family_key=artifact_family_key,
        scope=scope,
        serves_family_key=live_family_key,
    )
    manifest_path = _write_manifest(
        tmp_path,
        family_key=artifact_family_key,
        artifact_path=str(artifact_path),
        mode="ml",
        serves_family_key=live_family_key,
    )
    monkeypatch.setenv("ML_SERVING_MODE", "ml")
    monkeypatch.setenv("ML_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("ML_FAMILY_MODES_JSON", json.dumps({live_family_key: "ml"}))

    result, decision = run_serving_inference(db_session, family_key=live_family_key, scope=scope)

    assert result is not None
    assert decision.desired_mode == "ml"
    assert decision.effective_mode == "ml"
    assert decision.runtime_health == "healthy"
    assert result.lineage.model_metadata["artifact_family_key"] == artifact_family_key
    assert result.lineage.model_metadata["serves_family_key"] == live_family_key

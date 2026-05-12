"""Sklearn artifacts must declare ``target_type = "yes_won"`` in metadata.

Background — bug #2 follow-up from SIKA_PUNCH_LIST.md
=====================================================

The training pipeline now labels rows by P(YES wins) and emits
``metadata.target_type = "yes_won"`` on every new manifest. Legacy
manifests (trained against the selected-side-won target) lack the field;
serving them would re-introduce the silent NO-side flip the bug-#2 fix
just removed.

The runtime must refuse to load a sklearn artifact whose manifest is
missing or wrong on ``target_type`` — fail-loud so the operator notices
and retrains, instead of silently feeding miscalibrated probabilities
into scoring.
"""

from __future__ import annotations

from app.services.ml.runtime import _validate_artifact_payload


def test_sklearn_artifact_rejected_when_target_type_missing(tmp_path):
    """No target_type at all → reject with a target-type-specific error."""
    payload, error = _validate_artifact_payload(
        "nba_props",
        "single",
        str(tmp_path / "nonexistent_artifact_dir"),
        behavior="sklearn_predict_proba",
        target_type=None,
    )
    assert payload is None
    assert error is not None
    lowered = error.lower()
    assert "target_type" in lowered
    assert "yes_won" in lowered


def test_sklearn_artifact_rejected_when_target_type_is_legacy(tmp_path):
    """Old "selected_side_won" target → reject. The exact legacy name
    isn't enforced anywhere, but anything other than "yes_won" must fail."""
    payload, error = _validate_artifact_payload(
        "nba_props",
        "single",
        str(tmp_path / "nonexistent_artifact_dir"),
        behavior="sklearn_predict_proba",
        target_type="selected_side_won",
    )
    assert payload is None
    assert error is not None
    assert "target_type" in error.lower()


def test_static_probability_artifact_ignores_target_type(tmp_path):
    """Non-sklearn artifacts have no probability semantic to flip — they
    must keep working without target_type metadata."""
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        '{"family_key": "nba_props", "scope": "single", '
        '"behavior": "static_probability", "probability": 0.6}',
        encoding="utf-8",
    )
    payload, error = _validate_artifact_payload(
        "nba_props",
        "single",
        str(artifact),
        behavior="static_probability",
        target_type=None,
    )
    assert error is None
    assert payload is not None


def test_sklearn_target_type_check_runs_before_file_io(tmp_path):
    """target_type validation happens before file existence checks, so
    a missing artifact dir reports the target_type problem first — easier
    debugging for an operator who hasn't retrained yet."""
    payload, error = _validate_artifact_payload(
        "nba_props",
        "single",
        str(tmp_path / "definitely_does_not_exist"),
        behavior="sklearn_predict_proba",
        target_type=None,
    )
    assert error is not None
    assert "target_type" in error.lower()
    # Must NOT mention the missing artifact path — that check was skipped.
    assert "Artifact missing" not in error


def test_parlay_sklearn_artifact_does_not_require_yes_won_target_type(tmp_path):
    """Parlay scope sklearn artifacts predict combined parlay outcomes, not
    YES/NO side probabilities. The yes_won target_type requirement only
    applies to single-market scope. Without this exemption the existing
    parlay manifests (see apps/ml/manifests/public-shadow.example.json) get
    rejected wholesale."""
    payload, error = _validate_artifact_payload(
        "nba_2_leg",
        "parlay",
        str(tmp_path / "missing_parlay_artifact"),
        behavior="sklearn_predict_proba",
        target_type=None,
    )
    # target_type check skipped — the artifact-missing error takes over.
    assert error is not None
    assert "target_type" not in error.lower()
    assert "Artifact missing" in error

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth
from app.services.ml.artifact_loader import load_sklearn_artifact
from ml_features import vectorize
from app.services.ml.lineage import HEURISTIC_PARLAY_MODEL, HEURISTIC_SINGLE_MODEL, ModelLineage
from app.services.ml.registry import ModelManifest, ModelManifestFamily, load_model_manifest
from app.services.ml.study_progress import history_ready_for_shadow, is_active_study_family, settled_prediction_count_for_family
from app.services.model_families import FAMILY_DEFINITIONS, family_definition
from app.services.operator_settings import effective_ml_serving_mode


logger = logging.getLogger(__name__)


RuntimeMode = Literal["heuristic", "shadow", "ml"]
RuntimeHealth = Literal["healthy", "degraded", "unavailable"]

FAILURE_THRESHOLD = 3
COOLDOWN_MINUTES = 15

# Smarter #20 phase 2c — recalibration activation marker.
#
# The CLI bumps a manifest entry's ``calibration_version`` to
# ``<existing>+iso30d-<YYYY-MM-DD>`` only after writing a sidecar.
# The serve-time loader gates application on this tag so the
# manifest remains the operator-controlled activation switch:
# even if a sidecar is on disk for a family whose manifest entry
# still reads bare ``calibrated_v1``, we DON'T apply it. Catches
# the partial-deploy / failed-bump / rolled-back-manifest cases
# (codex review round 5 P2).
#
# We require the FULL ``+iso30d-YYYY-MM-DD`` shape (subagent review
# follow-up): a bare ``+iso30d-`` substring, e.g. from a partial
# write, a copy-paste error, or a manual edit, must NOT activate
# recalibration. Using a strict regex match lets the gate fail open
# (serve raw) on any malformed tag.
_RECALIBRATION_ACTIVATION_PATTERN = re.compile(r"\+iso30d-\d{4}-\d{2}-\d{2}")


# Smarter #27 — train/serve feature dictionary drift detection.
#
# ``vectorize`` silently substitutes a default for any key in
# ``feature_spec.ordered_keys`` that's missing from the serving features
# dict, and silently drops any serving key that wasn't in the trained
# spec. Both directions misalign the feature vector if they happen
# unintentionally — but some misses ARE intentional: ``dataset.py`` adds
# row-derived keys at training-time normalization that scoring never
# emits, and ``vectorize`` reads ``family_key`` from a separate slot
# instead of ``ordered_keys``.
#
# These two sets enumerate the known-legitimate cases so the drift
# detector only WARNs on unexpected drift.
_TRAINING_ONLY_FEATURE_KEYS: frozenset[str] = frozenset({
    # Added by ``apps/ml/ml/dataset.py:normalize_features`` from row
    # metadata. They appear in trained feature specs but the scoring
    # path's features dict will never contain them.
    "sport_is_nba",
    "sport_is_mlb",
    "suggested_price",
    "heuristic_fair_yes_price",
    "heuristic_edge",
    "heuristic_confidence",
    "heuristic_selection_score",
})

_VECTORIZE_NON_ORDERED_KEYS: frozenset[str] = frozenset({
    # ``vectorize`` reads ``family_key`` separately to drive the
    # family-one-hot append; it's intentionally absent from ordered_keys.
    "family_key",
})


def _detect_feature_drift(
    features: dict[str, Any],
    ordered_keys: Sequence[str],
) -> tuple[set[str], set[str]]:
    """Return ``(serving_extra, serving_missing_unexpected)``.

    ``serving_extra`` — keys present in the serving features dict but
    NOT in the trained feature spec. ``vectorize`` silently drops these,
    which means a newly-emitted scoring feature is invisible to the
    model until the next retrain ingests it.

    ``serving_missing_unexpected`` — keys in the trained spec that are
    missing from serving features AND are not in the known training-only
    set. The vector falls back to ``default_values`` for these, biasing
    inference.

    Known-legitimate misses (``sport_is_*``, ``heuristic_*``,
    ``family_key``, etc.) are filtered out before the comparison so the
    detector only WARNs on unexpected drift.
    """
    serve_keys = set(features.keys()) - _VECTORIZE_NON_ORDERED_KEYS
    spec_keys = set(ordered_keys)
    serving_extra = serve_keys - spec_keys
    serving_missing = (spec_keys - serve_keys) - _TRAINING_ONLY_FEATURE_KEYS
    return serving_extra, serving_missing


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class FamilyRuntimeDecision:
    family_key: str
    desired_mode: RuntimeMode
    effective_mode: RuntimeMode
    runtime_health: RuntimeHealth
    fallback_active: bool
    artifact_path: str | None
    last_check_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    consecutive_failures: int
    lineage: ModelLineage
    promotion_mode: RuntimeMode | None = None
    promotion_stability_days: int = 0
    promotion_baseline_brier: float | None = None
    promotion_metrics: dict[str, Any] | None = None
    promotion_updated_at: datetime | None = None


@dataclass(slots=True)
class ModelInferenceResult:
    probability: float
    confidence: float
    lineage: ModelLineage
    artifact_path: str | None
    metadata: dict[str, Any]


def _heuristic_lineage_for_scope(scope: str) -> ModelLineage:
    return HEURISTIC_PARLAY_MODEL if scope == "parlay" else HEURISTIC_SINGLE_MODEL


def _safe_modes_mapping() -> dict[str, RuntimeMode]:
    raw = (get_settings().ml_family_modes_json or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    result: dict[str, RuntimeMode] = {}
    for family_key, mode in dict(payload or {}).items():
        normalized = str(mode or "").strip().lower()
        if normalized in {"heuristic", "shadow", "ml"}:
            result[str(family_key)] = normalized  # type: ignore[assignment]
    return result


def _manifest_family_map(manifest: ModelManifest | None) -> dict[str, ModelManifestFamily]:
    return {
        (item.serves_family_key or item.family_key): item
        for item in (manifest.families if manifest else [])
    }


def _family_override_requested_mode(family_key: str) -> RuntimeMode | None:
    override_mode = _safe_modes_mapping().get(family_key)
    if override_mode in {"shadow", "ml"}:
        return override_mode
    return None


def _promotion_requested_mode(db: Session, family_key: str) -> RuntimeMode | None:
    row = _runtime_row_or_none(db, family_key)
    promotion_mode = str(getattr(row, "promotion_mode", "") or "").strip().lower() if row else ""
    if promotion_mode in {"shadow", "ml"}:
        return promotion_mode  # type: ignore[return-value]
    return None


def _manifest_requested_mode(manifest_family: ModelManifestFamily | None) -> RuntimeMode | None:
    manifest_mode = str(manifest_family.mode or "").strip().lower() if manifest_family else ""
    if manifest_mode in {"shadow", "ml"}:
        return manifest_mode  # type: ignore[return-value]
    return None


def _auto_shadow_requested_mode(
    db: Session,
    family_key: str,
    *,
    manifest_family: ModelManifestFamily | None,
) -> RuntimeMode | None:
    if manifest_family is None or not is_active_study_family(family_key):
        return None
    settled_predictions = settled_prediction_count_for_family(db, family_key)
    if not history_ready_for_shadow(settled_predictions):
        return None
    return "shadow"


def _resolve_requested_mode(
    db: Session,
    family_key: str,
    *,
    manifest_family: ModelManifestFamily | None,
) -> RuntimeMode:
    global_mode = effective_ml_serving_mode(db)
    resolved = _family_override_requested_mode(family_key)
    if resolved is None:
        resolved = _promotion_requested_mode(db, family_key)
    if resolved is None:
        resolved = _manifest_requested_mode(manifest_family)
    if resolved is None:
        resolved = _auto_shadow_requested_mode(db, family_key, manifest_family=manifest_family) or "heuristic"

    if global_mode == "heuristic":
        return "heuristic"
    if global_mode == "shadow":
        return "heuristic" if resolved == "heuristic" else "shadow"
    return resolved


def _resolve_artifact_path(manifest: ModelManifest | None, family: ModelManifestFamily | None) -> str | None:
    if family is None or not family.artifact_path:
        return None
    path = Path(family.artifact_path)
    if path.is_absolute():
        return str(path)
    if manifest and manifest.source_path:
        return str((Path(manifest.source_path).parent / path).resolve())
    return str((Path.cwd() / path).resolve())


def shadow_capture_blocker(
    family_key: str,
    *,
    scope: str,
    db: Session | None = None,
) -> str | None:
    if effective_ml_serving_mode(db) == "heuristic":
        return "This family has enough settled history, but global ML mode is locked to heuristic, so shadow capture will not start automatically."

    manifest = load_model_manifest()
    manifest_family = _manifest_family_map(manifest).get(family_key)
    if manifest_family is None:
        return "This family has enough settled history, but no shadow artifact manifest entry is configured for it yet."

    artifact_path = _resolve_artifact_path(manifest, manifest_family)
    if not artifact_path:
        return "This family has enough settled history, but no shadow artifact is configured for it yet."

    lineage = _lineage_from_manifest_family(manifest_family, scope)
    _payload, error = _validate_artifact_payload(
        family_key,
        scope,
        artifact_path,
        artifact_family_key=str(lineage.model_metadata.get("artifact_family_key") or family_key),
        behavior=str(lineage.model_metadata.get("behavior") or "static_probability"),
        target_type=lineage.model_metadata.get("target_type"),
        feature_set_version=lineage.feature_set_version,
    )
    if error is not None:
        return f"This family has enough settled history, but shadow runtime is unavailable: {error}"
    return None


def _runtime_row(db: Session, family_key: str) -> ModelFamilyRuntimeHealth:
    row = db.scalar(select(ModelFamilyRuntimeHealth).where(ModelFamilyRuntimeHealth.family_key == family_key))
    if row is None:
        row = ModelFamilyRuntimeHealth(family_key=family_key)
        db.add(row)
        db.flush()
    return row


def _runtime_row_or_none(db: Session, family_key: str) -> ModelFamilyRuntimeHealth | None:
    return db.scalar(select(ModelFamilyRuntimeHealth).where(ModelFamilyRuntimeHealth.family_key == family_key))


def _lineage_from_manifest_family(family: ModelManifestFamily | None, scope: str) -> ModelLineage:
    if family is None:
        return _heuristic_lineage_for_scope(scope)
    metadata = dict(family.metadata or {})
    metadata.setdefault("family_key", family.serves_family_key or family.family_key)
    metadata.setdefault("artifact_family_key", family.family_key)
    metadata.setdefault("serves_family_key", family.serves_family_key or family.family_key)
    metadata.setdefault("serving_mode", family.mode)
    return ModelLineage(
        model_name=family.model_name,
        model_version=family.model_version,
        calibration_version=family.calibration_version,
        feature_set_version=family.feature_set_version,
        model_metadata=metadata,
    )


_REQUIRED_SKLEARN_TARGET_TYPE = "yes_won"


def _validate_artifact_payload(
    family_key: str,
    scope: str,
    artifact_path: str | None,
    *,
    artifact_family_key: str | None = None,
    behavior: str | None = None,
    target_type: str | None = None,
    feature_set_version: str | None = None,
    calibration_version: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not artifact_path:
        return None, "No artifact_path configured for this family."
    normalized_behavior = str(behavior or "static_probability").strip().lower()
    # Bug #2: single-market sklearn artifacts must declare target_type=yes_won
    # so legacy models trained against selected-side-won can't silently flip
    # NO-side predictions. Parlay-scope sklearn artifacts predict a combined
    # parlay outcome rather than a YES/NO side, so the yes_won requirement
    # doesn't apply to them — see public-shadow.example.json for the parlay
    # manifest shape this guard would otherwise reject wholesale.
    if normalized_behavior == "sklearn_predict_proba" and scope == "single":
        normalized_target_type = str(target_type or "").strip().lower()
        if normalized_target_type != _REQUIRED_SKLEARN_TARGET_TYPE:
            return None, (
                f"Sklearn artifact requires metadata.target_type="
                f"'{_REQUIRED_SKLEARN_TARGET_TYPE}' "
                f"(got {target_type!r}). Retrain with the updated pipeline "
                f"(see bug #2 in SIKA_PUNCH_LIST.md)."
            )
    path = Path(artifact_path)
    if not path.exists():
        return None, f"Artifact missing at {artifact_path}."
    if normalized_behavior == "sklearn_predict_proba":
        if not path.is_dir():
            return None, f"Sklearn artifact path must be a directory: {artifact_path}."
        model_path = path / "model.joblib"
        feature_spec_path = path / "feature_spec.json"
        metadata_path = path / "training_metadata.json"
        for required_path in (model_path, feature_spec_path, metadata_path):
            if not required_path.exists():
                return None, f"Sklearn artifact missing {required_path.name}."
        try:
            feature_spec = json.loads(feature_spec_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, f"Feature spec load failed: {exc}"
        spec_version = str(feature_spec.get("version") or "")
        if feature_set_version and spec_version != feature_set_version:
            return None, f"Feature spec version mismatch for {family_key}."
        return {
            "family_key": artifact_family_key or family_key,
            "serves_family_key": family_key,
            "scope": scope,
            "behavior": normalized_behavior,
            "artifact_dir": str(path.resolve()),
            "calibration_version": calibration_version,
            "metadata": {"feature_spec_version": spec_version},
        }, None

    if normalized_behavior not in {"static_probability", ""}:
        return None, f"Unsupported artifact behavior: {normalized_behavior}."
    if path.is_dir():
        return None, f"Static artifact path must be a JSON file: {artifact_path}."
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"Artifact load failed: {exc}"

    payload_family_key = str(payload.get("family_key") or "")
    expected_artifact_key = artifact_family_key or family_key
    if payload_family_key not in {family_key, expected_artifact_key}:
        return None, f"Artifact family mismatch for {family_key}."
    payload_serves_key = str(payload.get("serves_family_key") or "").strip()
    if payload_serves_key and payload_serves_key != family_key:
        return None, f"Artifact serving key mismatch for {family_key}."
    if str(payload.get("scope") or "").strip().lower() != scope:
        return None, f"Artifact scope mismatch for {family_key}."
    if str(payload.get("behavior") or "").strip().lower() == "raise_on_load":
        return None, "Artifact declared raise_on_load behavior."
    return payload, None


def _apply_runtime_state(
    row: ModelFamilyRuntimeHealth,
    *,
    desired_mode: RuntimeMode,
    effective_mode: RuntimeMode,
    runtime_health: RuntimeHealth,
    fallback_active: bool,
    artifact_path: str | None,
    lineage: ModelLineage,
    error: str | None = None,
) -> None:
    row.desired_mode = desired_mode
    row.effective_mode = effective_mode
    row.runtime_health = runtime_health
    row.fallback_active = fallback_active
    row.artifact_path = artifact_path
    row.model_name = lineage.model_name
    row.model_version = lineage.model_version
    row.calibration_version = lineage.calibration_version
    row.feature_set_version = lineage.feature_set_version
    row.model_metadata = dict(lineage.model_metadata or {})
    row.last_check_at = _now_utc()
    if error is not None:
        row.last_error = error
        row.last_error_at = row.last_check_at
    elif row.runtime_health == "healthy":
        row.last_error = None


def _decision_from_row(row: ModelFamilyRuntimeHealth, scope: str) -> FamilyRuntimeDecision:
    lineage = ModelLineage(
        model_name=row.model_name or _heuristic_lineage_for_scope(scope).model_name,
        model_version=row.model_version,
        calibration_version=row.calibration_version,
        feature_set_version=row.feature_set_version,
        model_metadata=dict(row.model_metadata or {}),
    )
    return FamilyRuntimeDecision(
        family_key=row.family_key,
        desired_mode=(row.desired_mode or "heuristic"),  # type: ignore[arg-type]
        effective_mode=(row.effective_mode or "heuristic"),  # type: ignore[arg-type]
        runtime_health=(row.runtime_health or "unavailable"),  # type: ignore[arg-type]
        fallback_active=bool(row.fallback_active),
        artifact_path=row.artifact_path,
        last_check_at=row.last_check_at,
        last_success_at=row.last_success_at,
        last_error=row.last_error,
        last_error_at=row.last_error_at,
        consecutive_failures=int(row.consecutive_failures or 0),
        lineage=lineage,
        promotion_mode=(row.promotion_mode if row.promotion_mode in {"shadow", "ml"} else None),  # type: ignore[arg-type]
        promotion_stability_days=int(row.promotion_stability_days or 0),
        promotion_baseline_brier=row.promotion_baseline_brier,
        promotion_metrics=dict(row.promotion_metrics or {}),
        promotion_updated_at=row.promotion_updated_at,
    )


def read_family_runtime(
    db: Session,
    family_key: str,
    *,
    scope: str,
) -> FamilyRuntimeDecision:
    row = _runtime_row_or_none(db, family_key)
    if row is None:
        lineage = _heuristic_lineage_for_scope(scope)
        return FamilyRuntimeDecision(
            family_key=family_key,
            desired_mode="heuristic",
            effective_mode="heuristic",
            runtime_health="unavailable",
            fallback_active=False,
            artifact_path=None,
            last_check_at=None,
            last_success_at=None,
            last_error=None,
            last_error_at=None,
            consecutive_failures=0,
            lineage=lineage,
            promotion_mode=None,
            promotion_stability_days=0,
            promotion_baseline_brier=None,
            promotion_metrics={},
            promotion_updated_at=None,
        )
    return _decision_from_row(row, scope)


def resolve_family_runtime(
    db: Session,
    family_key: str,
    *,
    scope: str,
) -> FamilyRuntimeDecision:
    manifest = load_model_manifest()
    manifest_family = _manifest_family_map(manifest).get(family_key)
    desired_mode = _resolve_requested_mode(db, family_key, manifest_family=manifest_family)
    row = _runtime_row(db, family_key)
    artifact_path = _resolve_artifact_path(manifest, manifest_family)
    lineage = _lineage_from_manifest_family(manifest_family, scope)
    now = _now_utc()

    if desired_mode == "heuristic":
        _apply_runtime_state(
            row,
            desired_mode="heuristic",
            effective_mode="heuristic",
            runtime_health="healthy",
            fallback_active=False,
            artifact_path=artifact_path,
            lineage=_heuristic_lineage_for_scope(scope),
            error=None,
        )
        db.flush()
        return _decision_from_row(row, scope)

    if row.degraded_until and row.degraded_until > now:
        _apply_runtime_state(
            row,
            desired_mode=desired_mode,
            effective_mode="heuristic",
            runtime_health="degraded",
            fallback_active=desired_mode == "ml",
            artifact_path=artifact_path,
            lineage=lineage,
            error=row.last_error,
        )
        db.flush()
        return _decision_from_row(row, scope)

    payload, error = _validate_artifact_payload(
        family_key,
        scope,
        artifact_path,
        artifact_family_key=str(lineage.model_metadata.get("artifact_family_key") or family_key),
        behavior=str(lineage.model_metadata.get("behavior") or "static_probability"),
        target_type=lineage.model_metadata.get("target_type"),
        feature_set_version=lineage.feature_set_version,
    )
    if error is not None or payload is None:
        _apply_runtime_state(
            row,
            desired_mode=desired_mode,
            effective_mode="heuristic",
            runtime_health="unavailable",
            fallback_active=desired_mode == "ml",
            artifact_path=artifact_path,
            lineage=lineage,
            error=error,
        )
        db.flush()
        return _decision_from_row(row, scope)

    _apply_runtime_state(
        row,
        desired_mode=desired_mode,
        effective_mode=desired_mode,
        runtime_health="healthy",
        fallback_active=False,
        artifact_path=artifact_path,
        lineage=lineage,
        error=None,
    )
    if row.degraded_until and row.degraded_until <= now:
        row.degraded_until = None
        row.consecutive_failures = 0
    db.flush()
    return _decision_from_row(row, scope)


def sync_family_runtime_health(db: Session) -> list[FamilyRuntimeDecision]:
    decisions: list[FamilyRuntimeDecision] = []
    family_keys = {item.key for item in FAMILY_DEFINITIONS}
    manifest = load_model_manifest()
    family_keys.update(_manifest_family_map(manifest).keys())
    for family_key in sorted(family_keys):
        definition = family_definition(family_key)
        decisions.append(resolve_family_runtime(db, family_key, scope=definition.scope))
    return decisions


def _artifact_payload_for_decision(decision: FamilyRuntimeDecision, scope: str) -> dict[str, Any]:
    metadata = dict(decision.lineage.model_metadata or {})
    payload, error = _validate_artifact_payload(
        decision.family_key,
        scope,
        decision.artifact_path,
        artifact_family_key=str(metadata.get("artifact_family_key") or decision.family_key),
        behavior=str(metadata.get("behavior") or "static_probability"),
        target_type=metadata.get("target_type"),
        feature_set_version=decision.lineage.feature_set_version,
        calibration_version=decision.lineage.calibration_version,
    )
    if error is not None or payload is None:
        raise RuntimeError(error or "Artifact payload unavailable.")
    return payload


def _run_artifact_inference(payload: dict[str, Any], *, features: dict[str, Any] | None = None) -> tuple[float, float, dict[str, Any]]:
    behavior = str(payload.get("behavior") or "static_probability").strip().lower()
    if behavior == "raise":
        raise RuntimeError("Artifact declared raise behavior.")
    if behavior == "sklearn_predict_proba":
        artifact_dir = str(payload.get("artifact_dir") or "").strip()
        if not artifact_dir:
            raise RuntimeError("Sklearn artifact directory missing from payload.")
        artifact = load_sklearn_artifact(artifact_dir)
        features_view = dict(features or {})
        # Smarter #27: surface train/serve schema drift before it
        # silently misaligns the feature vector. Log-only — never alter
        # the inference path.
        serving_extra, serving_missing = _detect_feature_drift(
            features_view, artifact.feature_spec.ordered_keys
        )
        if serving_extra or serving_missing:
            logger.warning(
                "ml.feature_drift detected: extra=%s missing=%s "
                "(feature_spec_version=%s, artifact_dir=%s)",
                sorted(serving_extra),
                sorted(serving_missing),
                artifact.feature_spec.version,
                str(artifact.artifact_dir),
                extra={
                    "feature_spec_version": artifact.feature_spec.version,
                    "artifact_dir": str(artifact.artifact_dir),
                    "serving_extra": sorted(serving_extra),
                    "serving_missing": sorted(serving_missing),
                },
            )
        vector = vectorize(features_view, artifact.feature_spec).reshape(1, -1)
        raw_probability = float(artifact.pipeline.predict_proba(vector)[0][1])
        # Smarter #20 phase 2c — post-process raw probability through
        # the per-family isotonic recalibrator when the artifact ships
        # a sidecar for the family being served. Phase 2b's CLI writes
        # one sidecar per served family; the loader (artifact_loader.py)
        # already verified the metadata's family_key matches the
        # subdirectory name, so we can apply unconditionally here.
        # Falls through to the raw probability when no sidecar exists.
        #
        # Codex round 3 P2: validate the RAW output is finite + in
        # [0, 1] BEFORE applying the recalibrator. The CLI-fitted
        # isotonic uses ``out_of_bounds='clip'``, so an invalid
        # ``predict_proba`` output (e.g. ``1.2`` from a corrupted
        # artifact) would be silently clamped and served instead of
        # tripping the existing finite/[0,1] guard in
        # ``run_serving_inference``. Raise here so the runtime fails
        # the inference and the operator sees the broken artifact.
        if not isfinite(raw_probability) or raw_probability < 0.0 or raw_probability > 1.0:
            raise RuntimeError(
                f"Model raw probability {raw_probability!r} is invalid (NaN, inf, or outside [0, 1]) "
                f"— refusing to recalibrate. Underlying artifact at {artifact.artifact_dir} "
                f"is broken."
            )
        serves_family_key = str(payload.get("serves_family_key") or "").strip()
        # Codex round 5 P2: gate sidecar application on the manifest's
        # ``calibration_version`` carrying the iso30d tag. The CLI
        # writes the sidecar BEFORE bumping the version; during that
        # window — or after a failed / rolled-back manifest update —
        # the file might be on disk while the manifest still reads
        # the bare ``calibrated_v1``. Treating the bare manifest as
        # authoritative keeps the activation contract single-sourced
        # at the manifest.
        calibration_version = str(payload.get("calibration_version") or "")
        recalibration_active = bool(_RECALIBRATION_ACTIVATION_PATTERN.search(calibration_version))
        recalibrator = (
            artifact.recalibrators.get(serves_family_key)
            if serves_family_key and recalibration_active
            else None
        )
        if recalibrator is not None:
            probability = float(recalibrator.predict([raw_probability])[0])
            recalibration_applied = True
        else:
            probability = raw_probability
            recalibration_applied = False
        confidence = probability
        metadata: dict[str, Any] = {
            "feature_spec_version": artifact.feature_spec.version,
            "artifact_dir": str(artifact.artifact_dir),
            "training_metadata": dict(artifact.training_metadata or {}),
            "recalibration_applied": recalibration_applied,
        }
        if recalibration_applied:
            # Operator-facing provenance: the raw output BEFORE recalibration
            # is preserved alongside the post-process result so audit logs and
            # the readiness diagnostic can show the model's drift correction
            # in flight. ``recalibration_metadata`` mirrors the sidecar JSON
            # so phase 2b's window dates / sample size / Brier improvement
            # are visible at serve time without a filesystem round-trip.
            metadata["raw_probability"] = raw_probability
            sidecar_metadata_path = (
                artifact.artifact_dir
                / "recalibrators"
                / serves_family_key
                / "isotonic_recalibration_metadata.json"
            )
            if sidecar_metadata_path.exists():
                try:
                    metadata["recalibration_metadata"] = json.loads(
                        sidecar_metadata_path.read_text(encoding="utf-8")
                    )
                except Exception:  # pragma: no cover - defensive
                    metadata["recalibration_metadata"] = {}
            else:
                metadata["recalibration_metadata"] = {}
        return probability, confidence, metadata
    probability = float(payload.get("probability") if payload.get("probability") is not None else payload.get("yes_probability"))
    confidence = float(payload.get("confidence") if payload.get("confidence") is not None else probability)
    metadata = dict(payload.get("metadata") or {})
    return probability, confidence, metadata


def _mark_runtime_success(db: Session, decision: FamilyRuntimeDecision, scope: str) -> FamilyRuntimeDecision:
    row = _runtime_row(db, decision.family_key)
    row.consecutive_failures = 0
    row.degraded_until = None
    row.last_success_at = _now_utc()
    row.last_error = None
    row.last_error_at = None
    _apply_runtime_state(
        row,
        desired_mode=decision.desired_mode,
        effective_mode=decision.effective_mode,
        runtime_health="healthy",
        fallback_active=False,
        artifact_path=decision.artifact_path,
        lineage=decision.lineage,
        error=None,
    )
    db.flush()
    return _decision_from_row(row, scope)


def _mark_runtime_failure(
    db: Session,
    decision: FamilyRuntimeDecision,
    *,
    scope: str,
    error: str,
) -> FamilyRuntimeDecision:
    row = _runtime_row(db, decision.family_key)
    row.consecutive_failures = int(row.consecutive_failures or 0) + 1
    runtime_health: RuntimeHealth = "unavailable"
    degraded_until: datetime | None = None
    if row.consecutive_failures >= FAILURE_THRESHOLD:
        runtime_health = "degraded"
        degraded_until = _now_utc() + timedelta(minutes=COOLDOWN_MINUTES)
    row.degraded_until = degraded_until
    lineage = decision.lineage if decision.lineage.model_name else _heuristic_lineage_for_scope(scope)
    _apply_runtime_state(
        row,
        desired_mode=decision.desired_mode,
        effective_mode="heuristic",
        runtime_health=runtime_health,
        fallback_active=decision.desired_mode == "ml",
        artifact_path=decision.artifact_path,
        lineage=lineage,
        error=error,
    )
    db.flush()
    return _decision_from_row(row, scope)


def run_serving_inference(
    db: Session,
    *,
    family_key: str,
    scope: str,
    features: dict[str, Any] | None = None,
) -> tuple[ModelInferenceResult | None, FamilyRuntimeDecision]:
    decision = resolve_family_runtime(db, family_key, scope=scope)
    if decision.effective_mode != "ml":
        return None, decision

    try:
        payload = _artifact_payload_for_decision(decision, scope)
        probability, confidence, metadata = _run_artifact_inference(payload, features=features)
        if not isfinite(probability) or not isfinite(confidence):
            raise RuntimeError("Model output contained non-finite values.")
        if probability < 0.0 or probability > 1.0:
            raise RuntimeError("Model probability fell outside [0, 1].")
        if confidence < 0.0 or confidence > 1.0:
            raise RuntimeError("Model confidence fell outside [0, 1].")
        if not metadata:
            raise RuntimeError("Model metadata is missing from the artifact output.")
    except Exception as exc:
        updated = _mark_runtime_failure(db, decision, scope=scope, error=str(exc))
        return None, updated

    updated = _mark_runtime_success(db, decision, scope)
    return (
        ModelInferenceResult(
            probability=probability,
            confidence=confidence,
            lineage=decision.lineage,
            artifact_path=decision.artifact_path,
            metadata=metadata,
        ),
        updated,
    )


def run_shadow_inference(
    db: Session,
    *,
    family_key: str,
    scope: str,
    features: dict[str, Any] | None = None,
) -> tuple[ModelInferenceResult | None, FamilyRuntimeDecision]:
    decision = resolve_family_runtime(db, family_key, scope=scope)
    if decision.desired_mode not in {"shadow", "ml"}:
        return None, decision
    if decision.runtime_health != "healthy":
        return None, decision

    try:
        payload = _artifact_payload_for_decision(decision, scope)
        probability, confidence, metadata = _run_artifact_inference(payload, features=features)
        if not isfinite(probability) or not isfinite(confidence):
            raise RuntimeError("Model output contained non-finite values.")
        if probability < 0.0 or probability > 1.0:
            raise RuntimeError("Model probability fell outside [0, 1].")
        if confidence < 0.0 or confidence > 1.0:
            raise RuntimeError("Model confidence fell outside [0, 1].")
    except Exception as exc:
        updated = _mark_runtime_failure(db, decision, scope=scope, error=str(exc))
        return None, updated

    updated = _mark_runtime_success(db, decision, scope)
    return (
        ModelInferenceResult(
            probability=probability,
            confidence=confidence,
            lineage=decision.lineage,
            artifact_path=decision.artifact_path,
            metadata=metadata,
        ),
        updated,
    )

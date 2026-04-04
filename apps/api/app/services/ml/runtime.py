from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ModelFamilyRuntimeHealth
from app.services.ml.lineage import HEURISTIC_PARLAY_MODEL, HEURISTIC_SINGLE_MODEL, ModelLineage
from app.services.ml.registry import ModelManifest, ModelManifestFamily, load_model_manifest
from app.services.model_families import FAMILY_DEFINITIONS, family_definition

RuntimeMode = Literal["heuristic", "shadow", "ml"]
RuntimeHealth = Literal["healthy", "degraded", "unavailable"]

FAILURE_THRESHOLD = 3
COOLDOWN_MINUTES = 15


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
    return {item.family_key: item for item in (manifest.families if manifest else [])}


def _resolve_requested_mode(
    family_key: str,
    *,
    manifest_family: ModelManifestFamily | None,
) -> RuntimeMode:
    settings = get_settings()
    global_mode = settings.ml_serving_mode
    override_mode = _safe_modes_mapping().get(family_key)
    family_mode = (override_mode or (manifest_family.mode if manifest_family else None) or "heuristic").lower()
    resolved: RuntimeMode = "heuristic"
    if family_mode in {"heuristic", "shadow", "ml"}:
        resolved = family_mode  # type: ignore[assignment]

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
    metadata.setdefault("family_key", family.family_key)
    metadata.setdefault("serving_mode", family.mode)
    return ModelLineage(
        model_name=family.model_name,
        model_version=family.model_version,
        calibration_version=family.calibration_version,
        feature_set_version=family.feature_set_version,
        model_metadata=metadata,
    )


def _validate_artifact_payload(
    family_key: str,
    scope: str,
    artifact_path: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not artifact_path:
        return None, "No artifact_path configured for this family."
    path = Path(artifact_path)
    if not path.exists():
        return None, f"Artifact missing at {artifact_path}."
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"Artifact load failed: {exc}"

    if str(payload.get("family_key") or "") != family_key:
        return None, f"Artifact family mismatch for {family_key}."
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
    desired_mode = _resolve_requested_mode(family_key, manifest_family=manifest_family)
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

    payload, error = _validate_artifact_payload(family_key, scope, artifact_path)
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
    payload, error = _validate_artifact_payload(decision.family_key, scope, decision.artifact_path)
    if error is not None or payload is None:
        raise RuntimeError(error or "Artifact payload unavailable.")
    return payload


def _run_artifact_inference(payload: dict[str, Any]) -> tuple[float, float, dict[str, Any]]:
    behavior = str(payload.get("behavior") or "static_probability").strip().lower()
    if behavior == "raise":
        raise RuntimeError("Artifact declared raise behavior.")
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
) -> tuple[ModelInferenceResult | None, FamilyRuntimeDecision]:
    decision = resolve_family_runtime(db, family_key, scope=scope)
    if decision.effective_mode != "ml":
        return None, decision

    try:
        payload = _artifact_payload_for_decision(decision, scope)
        probability, confidence, metadata = _run_artifact_inference(payload)
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
) -> tuple[ModelInferenceResult | None, FamilyRuntimeDecision]:
    decision = resolve_family_runtime(db, family_key, scope=scope)
    if decision.desired_mode not in {"shadow", "ml"}:
        return None, decision
    if decision.runtime_health != "healthy":
        return None, decision

    try:
        payload = _artifact_payload_for_decision(decision, scope)
        probability, confidence, metadata = _run_artifact_inference(payload)
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

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib

from ml_features import FeatureSpec


logger = logging.getLogger(__name__)


# Smarter #20 phase 2c — sidecar conventions.
#
# Phase 2b's CLI writes per-family recalibrators under:
#
#     <artifact_dir>/recalibrators/<family_key>/isotonic_recalibrator.joblib
#     <artifact_dir>/recalibrators/<family_key>/isotonic_recalibration_metadata.json
#
# (The `<family_key>` subdirectory keeps each served family's fit
# isolated even when the global_v1 manifest serves multiple families
# from one artifact_dir.) Phase 2c (this module) discovers and loads
# every per-family sidecar at artifact-load time and caches them on
# the SklearnArtifact dataclass so the hot serving path is just a
# dict lookup.
_RECALIBRATORS_SUBDIR = "recalibrators"
_SIDECAR_RECALIBRATOR_FILENAME = "isotonic_recalibrator.joblib"
_SIDECAR_METADATA_FILENAME = "isotonic_recalibration_metadata.json"

# Smarter #21 phase 2a defined the on-disk layout:
#
#     <artifact_dir>/interval_models/<stat_key>/p10.joblib
#     <artifact_dir>/interval_models/<stat_key>/p50.joblib
#     <artifact_dir>/interval_models/<stat_key>/p90.joblib
#     <artifact_dir>/interval_models/<stat_key>/metadata.json
#
# (Subdirectory per stat key so phase 2b's training pipeline can fit
# independent quantile regressors per prop family without one
# stat-key swap clobbering the others.) Phase 2c (this module)
# discovers and loads every per-stat sidecar at artifact-load time
# and caches the triples on ``SklearnArtifact`` so the hot serving
# path is just a dict lookup.
_INTERVAL_MODELS_SUBDIR = "interval_models"
_INTERVAL_P10_FILENAME = "p10.joblib"
_INTERVAL_P50_FILENAME = "p50.joblib"
_INTERVAL_P90_FILENAME = "p90.joblib"
_INTERVAL_METADATA_FILENAME = "metadata.json"


@dataclass(frozen=True, slots=True)
class SklearnArtifact:
    artifact_dir: Path
    pipeline: Any
    feature_spec: FeatureSpec
    training_metadata: dict[str, Any]
    # Smarter #20 phase 2c — per-family isotonic recalibrators loaded
    # from ``<artifact_dir>/recalibrators/<family>/``. Keyed by the
    # served family the sidecar applies to. Empty dict when no sidecars
    # are present (the common pre-recalibration state). Each entry's
    # value is a fitted ``sklearn.isotonic.IsotonicRegression``.
    recalibrators: dict[str, Any] = field(default_factory=dict)
    # Smarter #21 phase 2c — per-stat-key prediction-interval models
    # loaded from ``<artifact_dir>/interval_models/<stat_key>/``. Each
    # entry is a ``(p10_model, p50_model, p90_model)`` tuple matching
    # the shape ``ml.quantile_regression.compute_prediction_interval``
    # consumes. Empty dict when no sidecars are present (pre-phase-2b
    # state).
    interval_models: dict[str, tuple[Any, Any, Any]] = field(default_factory=dict)


_CACHE: dict[
    tuple[str, tuple[tuple[str, float], ...], tuple[tuple[str, float, int, str], ...]],
    SklearnArtifact,
] = {}
_LOCK = threading.Lock()


def _sha256_of_file(path: Path) -> str:
    """SHA-256 of a file's contents, hex-encoded.

    Used to disambiguate sidecar replacements that preserve the path,
    mtime, AND size — a real risk for ``cp --preserve=timestamps``
    workflows or for two distinct ``joblib.dump`` outputs that happen
    to round-trip to the same byte length within the filesystem's
    timestamp resolution (codex review round 4 P2). Reads the file
    in 64 KiB chunks so very large sidecars don't blow up memory;
    typical isotonic recalibrators are a few KB each.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sidecar_fingerprint(artifact_dir: Path) -> tuple[tuple[str, float, int, str], ...]:
    """Per-file fingerprint of every sidecar under ``recalibrators/``
    AND ``interval_models/``.

    Returns a tuple of ``(relative_path, mtime, size, sha256_hex)``
    sorted by path. Any sidecar add / remove / rewrite produces a
    different tuple, so folding this into the cache key forces a
    re-load. The SHA-256 catches the otherwise-invisible case where
    a file is replaced with different content but identical
    ``(mtime, size)`` (codex review round 4 P2).

    Returns ``()`` when neither subdirectory exists (common
    pre-recalibration / pre-interval state).
    """
    fingerprints: list[tuple[str, float, int, str]] = []
    for subdir_name in (_RECALIBRATORS_SUBDIR, _INTERVAL_MODELS_SUBDIR):
        sidecar_root = artifact_dir / subdir_name
        if not sidecar_root.exists() or not sidecar_root.is_dir():
            continue
        for inner_dir in sorted(sidecar_root.iterdir()):
            if not inner_dir.is_dir():
                continue
            for sidecar_file in sorted(inner_dir.iterdir()):
                if not sidecar_file.is_file():
                    continue
                stat = sidecar_file.stat()
                fingerprints.append(
                    (
                        str(sidecar_file.relative_to(artifact_dir)),
                        stat.st_mtime,
                        stat.st_size,
                        _sha256_of_file(sidecar_file),
                    )
                )
    fingerprints.sort()
    return tuple(fingerprints)


def _artifact_cache_key(
    abs_dir: Path,
) -> tuple[str, tuple[tuple[str, float], ...], tuple[tuple[str, float, int, str], ...]]:
    """Composite cache key.

    Three components, all required:
    - ``str(abs_dir)`` — disambiguates artifacts at different paths.
    - per-file (name, mtime) for the base triple — invalidates when
      ``model.joblib`` / ``feature_spec.json`` / ``training_metadata.json``
      change.
    - sidecar fingerprint with SHA-256 — invalidates on any sidecar
      add / remove / rewrite, including same-mtime same-size content
      replacements (codex review rounds 2 + 4 P2).
    """
    base = tuple(
        (filename, (abs_dir / filename).stat().st_mtime)
        for filename in ("model.joblib", "feature_spec.json", "training_metadata.json")
    )
    return (str(abs_dir), base, _sidecar_fingerprint(abs_dir))


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _load_sidecar_recalibrators(artifact_dir: Path) -> dict[str, Any]:
    """Discover and load every per-family sidecar under ``recalibrators/``.

    Each sidecar lives in ``recalibrators/<family>/`` with a fixed
    filename pair (joblib + JSON metadata). The family is taken from
    the directory name — that's the source of truth at load time. The
    metadata's ``family_key`` marker MUST be present AND match the
    directory name (codex review round 1 P2: a missing or null marker
    indicates the sidecar wasn't written by the phase 2b CLI, so we
    can't safely associate it with the served family — refuse rather
    than risk applying the wrong calibrator).

    Sidecars whose joblib payload fails to load are skipped with a
    logger.warning. They DON'T fail the artifact load — the rest of
    the artifact is still serviceable; the affected family just falls
    back to the raw (un-recalibrated) probability.
    """
    sidecar_root = artifact_dir / _RECALIBRATORS_SUBDIR
    if not sidecar_root.exists() or not sidecar_root.is_dir():
        return {}
    loaded: dict[str, Any] = {}
    for family_dir in sorted(sidecar_root.iterdir()):
        if not family_dir.is_dir():
            continue
        family_key = family_dir.name
        joblib_path = family_dir / _SIDECAR_RECALIBRATOR_FILENAME
        metadata_path = family_dir / _SIDECAR_METADATA_FILENAME
        if not joblib_path.exists():
            continue
        # Require metadata + marker BEFORE loading the joblib. Phase 2b's
        # CLI writes both in sync; absence indicates a non-CLI source
        # (manual placement, pre-CLI partial write, future tool with a
        # different convention) — skip rather than apply blindly.
        if not metadata_path.exists():
            logger.warning(
                "ml.recalibrator_skipped: metadata file missing "
                "(family=%s, path=%s) — refusing to apply unannotated sidecar.",
                family_key, metadata_path,
            )
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ml.recalibrator_skipped: metadata unparseable "
                "(family=%s, path=%s, error=%s)",
                family_key, metadata_path, exc,
            )
            continue
        # Codex round 3 P2: a metadata file that's valid JSON but not
        # an object (``null``, an array, a scalar) would crash
        # ``.get(...)`` with AttributeError and abort the whole
        # artifact load, not just this one sidecar. Treat any non-dict
        # payload as "unannotated" and skip the family.
        if not isinstance(metadata, dict):
            logger.warning(
                "ml.recalibrator_skipped: metadata is not a JSON object "
                "(family=%s, path=%s, type=%s) — refusing to apply.",
                family_key, metadata_path, type(metadata).__name__,
            )
            continue
        metadata_family = metadata.get("family_key")
        if metadata_family != family_key:
            logger.warning(
                "ml.recalibrator_skipped: metadata.family_key=%r does not "
                "match directory name %r (path=%s) — refusing to apply.",
                metadata_family, family_key, family_dir,
            )
            continue
        try:
            recalibrator = joblib.load(joblib_path)
        except Exception as exc:
            logger.warning(
                "ml.recalibrator_skipped: joblib load failed "
                "(family=%s, path=%s, error=%s)",
                family_key, joblib_path, exc,
            )
            continue
        # Codex round 5 P2: probe the loaded object before caching it.
        # An unpicklable-as-something-else (e.g., a stray list, an
        # unfitted estimator) would load fine but raise on every
        # subsequent ``predict`` call, marking the family failed /
        # degraded rather than falling back to raw. The intent for a
        # broken sidecar is "skip and serve raw," not "kill ML
        # serving" — so reject anything that doesn't pass the probe
        # at load time.
        #
        # Subagent review follow-up: probe at 0.0, 0.5, AND 1.0 so a
        # recalibrator that happens to handle the midpoint but
        # crashes / returns NaN at the unit-interval boundaries (a
        # hidden risk if a future CLI ships a non-clipping
        # IsotonicRegression) is rejected here too.
        try:
            probe = recalibrator.predict([0.0, 0.5, 1.0])
            probe_values = [float(value) for value in probe]
            if not all(0.0 <= value <= 1.0 for value in probe_values):
                raise ValueError(
                    f"Probe predict([0, 0.5, 1]) returned {probe_values!r}, all expected in [0, 1]"
                )
        except Exception as exc:
            logger.warning(
                "ml.recalibrator_skipped: probe predict failed "
                "(family=%s, path=%s, error=%s) — bad sidecar; serving raw.",
                family_key, joblib_path, exc,
            )
            continue
        loaded[family_key] = recalibrator
    return loaded


def _load_sidecar_interval_models(artifact_dir: Path) -> dict[str, tuple[Any, Any, Any]]:
    """Discover and load every per-stat-key sidecar under
    ``interval_models/``.

    Each sidecar lives in ``interval_models/<stat_key>/`` with the
    canonical filename triple ``p10.joblib`` / ``p50.joblib`` /
    ``p90.joblib``. Metadata.json is optional but present-by-default
    (phase 2a writes it for provenance); the loader does NOT require
    it. The stat key is taken from the directory name — that's the
    source of truth at load time.

    Each loaded triple is probed at a 1-row zero-feature matrix to
    catch joblib-loads-but-predict-crashes corruption early (same
    pattern as the recalibrator sidecar). Sidecars that fail the
    probe are skipped with a logger.warning; the rest of the
    artifact loads fine.
    """
    sidecar_root = artifact_dir / _INTERVAL_MODELS_SUBDIR
    if not sidecar_root.exists() or not sidecar_root.is_dir():
        return {}
    loaded: dict[str, tuple[Any, Any, Any]] = {}
    for stat_dir in sorted(sidecar_root.iterdir()):
        if not stat_dir.is_dir():
            continue
        stat_key = stat_dir.name
        joblib_paths = [
            stat_dir / _INTERVAL_P10_FILENAME,
            stat_dir / _INTERVAL_P50_FILENAME,
            stat_dir / _INTERVAL_P90_FILENAME,
        ]
        if not all(path.exists() for path in joblib_paths):
            # Partial sidecar (e.g. a deploy script that copied
            # p10/p50 but not p90) must not load. Skip silently so
            # the artifact serves without intervals for this stat.
            continue
        try:
            triple = tuple(joblib.load(path) for path in joblib_paths)
        except Exception as exc:  # noqa: BLE001 — corruption is runtime
            logger.warning(
                "ml.interval_models_skipped: joblib load failed "
                "(stat=%s, dir=%s, error=%s)",
                stat_key, stat_dir, exc,
            )
            continue
        # Probe at a 1-row zero matrix to surface
        # joblib-loads-but-predict-crashes corruption.
        try:
            probe_cols = int(triple[0].n_features_in_)
        except AttributeError:
            # Older sklearn / unusual estimator without
            # ``n_features_in_`` — skip probe rather than reject.
            loaded[stat_key] = triple  # type: ignore[assignment]
            continue
        import numpy as np

        probe = np.zeros((1, probe_cols), dtype=float)
        try:
            for model in triple:
                model.predict(probe)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ml.interval_models_skipped: probe predict failed "
                "(stat=%s, dir=%s, error=%s) — bad sidecar; "
                "serving without intervals.",
                stat_key, stat_dir, exc,
            )
            continue
        loaded[stat_key] = triple  # type: ignore[assignment]
    return loaded


def load_sklearn_artifact(abs_dir: str | Path) -> SklearnArtifact:
    artifact_dir = Path(abs_dir).resolve()
    key = _artifact_cache_key(artifact_dir)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    feature_spec = FeatureSpec.from_dict(json.loads((artifact_dir / "feature_spec.json").read_text(encoding="utf-8")))
    training_metadata = json.loads((artifact_dir / "training_metadata.json").read_text(encoding="utf-8"))
    pipeline = joblib.load(artifact_dir / "model.joblib")
    recalibrators = _load_sidecar_recalibrators(artifact_dir)
    interval_models = _load_sidecar_interval_models(artifact_dir)
    artifact = SklearnArtifact(
        artifact_dir=artifact_dir,
        pipeline=pipeline,
        feature_spec=feature_spec,
        training_metadata=dict(training_metadata or {}),
        recalibrators=recalibrators,
        interval_models=interval_models,
    )
    with _LOCK:
        _CACHE[key] = artifact
    return artifact


def apply_interval_models(
    artifact: SklearnArtifact,
    stat_key: str,
    features,
) -> tuple[float, float, float] | None:
    """Smarter #21 phase 2c — serve-time helper that returns the
    (p10, p50, p90) prediction interval for ``stat_key`` if interval
    models are loaded on this artifact.

    Returns ``None`` when:
    - The artifact has no interval models for ``stat_key`` (the
      common case — most stat keys don't have intervals trained).
    - ``predict`` raises on the loaded triple (probe-level
      corruption that survived load-time check). The caller falls
      back to the point estimate.

    Triple ordering is monotonized: independently-fit quantile
    regressors can in rare cases "cross" (a p10 prediction higher
    than the p50 for noisy inputs). The returned tuple is always
    sorted so consumers can rely on ``p10 <= p50 <= p90`` without
    per-call defensive checks.
    """
    triple = artifact.interval_models.get(stat_key)
    if triple is None:
        return None
    import numpy as np

    # Accept either a flat 1-D vector or an explicit 2-D row.
    array = np.asarray(features, dtype=float)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.shape[0] != 1:
        raise ValueError(
            f"apply_interval_models expects a single-row input; got {array.shape[0]} rows"
        )
    try:
        raw = [float(model.predict(array)[0]) for model in triple]
    except Exception as exc:  # noqa: BLE001 — defensive at serve time
        logger.warning(
            "ml.interval_models_predict_failed: stat=%s error=%s",
            stat_key, exc,
        )
        return None
    sorted_triple = sorted(raw)
    return (
        round(sorted_triple[0], 4),
        round(sorted_triple[1], 4),
        round(sorted_triple[2], 4),
    )

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
    """Per-file fingerprint of every sidecar under ``recalibrators/``.

    Returns a tuple of ``(relative_path, mtime, size, sha256_hex)``
    sorted by path. Any sidecar add / remove / rewrite produces a
    different tuple, so folding this into the cache key forces a
    re-load. The SHA-256 catches the otherwise-invisible case where
    a file is replaced with different content but identical
    ``(mtime, size)`` (codex review round 4 P2).

    Returns ``()`` when the subdirectory doesn't exist (common
    pre-recalibration state).
    """
    sidecar_root = artifact_dir / _RECALIBRATORS_SUBDIR
    if not sidecar_root.exists() or not sidecar_root.is_dir():
        return ()
    fingerprints: list[tuple[str, float, int, str]] = []
    for family_dir in sorted(sidecar_root.iterdir()):
        if not family_dir.is_dir():
            continue
        for sidecar_file in sorted(family_dir.iterdir()):
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
    artifact = SklearnArtifact(
        artifact_dir=artifact_dir,
        pipeline=pipeline,
        feature_spec=feature_spec,
        training_metadata=dict(training_metadata or {}),
        recalibrators=recalibrators,
    )
    with _LOCK:
        _CACHE[key] = artifact
    return artifact

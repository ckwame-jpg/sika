"""Smarter #21 (phase 2b) — DB-side dataset extraction for prop
prediction-interval training.

The classifier already has a per-row binary target (YES won / lost).
Quantile regression needs the underlying continuous stat output —
"LeBron actually scored 28 points" — which lives in the ESPN gamelog
cache, not in ``predictions`` directly.

This module joins:

- ``predictions`` (settled player_prop rows whose ``features`` blob
  we already vectorize for classifier training) →
- ``espn_player_search_cache`` (subject_name → athlete_id payload) →
- ``espn_player_gamelog_cache`` (athlete_id × season → per-game stat
  blob) →

extracts the actual stat output for the game played near
``predictions.captured_at``, and returns a vectorized
(features_matrix, continuous_target_vector) pair that the phase 2a
``train_prop_interval_models`` helper consumes directly.

## ESPN payload parsing — duplication note

``apps/api/app/services/stats_query.py:_build_game_logs`` parses the
same cached ESPN gamelog payload for the prop-stats resolver. We do
NOT import from ``apps/api`` here because ``apps/ml`` does not depend
on the API package — coupling the offline training workspace to the
runtime's full dependency graph would conflict with the train/serve
separation the repo already enforces. Instead, we re-implement the
narrow slice we need (one stat × one game date) here. The slice is
small enough that ordinary unit tests give confidence; cross-app
drift is mitigated by both paths reading the same cached ESPN
``seasonTypes/categories/events`` payload shape.

## What's deferred

- Phase 2c (already shipped): serve-time loader on ``SklearnArtifact``
  (see ``apps/api/app/services/ml/artifact_loader.py``).
- Phase 2d: scoring-kernel consumer + UI band.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from ml.dataset import _enrich_prediction_features, _family_key, normalize_database_url
from ml_features import FeatureSpec, vectorize


logger = logging.getLogger(__name__)


__all__ = [
    "GAME_MATCH_WINDOW_HOURS",
    "INTERVAL_DATASET_SKIP_REASONS",
    "IntervalDatasetExtract",
    "build_interval_training_rows",
    "season_for_captured_at",
]


# Game-matching windows.
#
# Anchor preference: when ``Event.starts_at`` is known for the
# prediction's ``event_id`` we anchor the gamelog match to that
# explicit game start (±EVENT_ANCHOR_TOLERANCE_HOURS) — this is
# essential for MLB doubleheaders where both games on the same day
# fall in the captured_at-relative window (codex round 3 P2).
#
# Fallback (event_id NULL / starts_at missing): asymmetric window
# around ``captured_at``.
# - Forward (36h): covers same-day games (typically ~12h before
#   tip-off; sometimes 30+ hours for early next-day slates), time-zone
#   skew, and slate scheduling.
# - Backward (6h): predictions are always captured BEFORE tip-off,
#   so legitimate "past" matches are tiny clock-skew artifacts only.
#   A tighter past window prevents the NBA back-to-back misattribution
#   codex round 2 P2 flagged (yesterday's already-played game
#   out-ranking today's upcoming game on smallest-abs-delta).
GAME_MATCH_FORWARD_HOURS = 36
GAME_MATCH_BACKWARD_HOURS = 6
EVENT_ANCHOR_TOLERANCE_HOURS = 2
# Back-compat alias for callers reading the legacy symmetric symbol.
GAME_MATCH_WINDOW_HOURS = GAME_MATCH_FORWARD_HOURS


# Per-sport stat-key → raw_metrics field mapping. Keys here are the
# canonical sika stat_keys (see ``apps/api/app/services/market_support.py``
# NBA_PROP_ALIASES / MLB_PROP_ALIASES); values are the raw_metrics
# dict keys this module emits. ``made_threes`` is the sika alias for
# ESPN's three-point-makes; both spellings map to the same field so a
# stat_key spelled either way resolves identically.
_NBA_STAT_TO_RAW = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "made_threes": "three_points_made",
    "three_points_made": "three_points_made",
    "field_goals_made": "field_goals_made",
}
_MLB_STAT_TO_RAW = {
    "hits": "hits",
    "runs": "runs",
    "home_runs": "home_runs",
    "rbis": "rbis",
    "walks": "walks",
    "strikeouts": "strikeouts",
    "hit_by_pitch": "hit_by_pitch",
}


# Documented skip taxonomy — every key in
# ``IntervalDatasetExtract.skipped`` is one of these. The CLI summary
# surfaces the per-reason counts so the operator sees WHY the extract
# is the size it is. A drift guard test pins this constant so future
# additions can't sneak in.
INTERVAL_DATASET_SKIP_REASONS: tuple[str, ...] = (
    "no_features",        # prediction.features blob empty / null
    "no_athlete_id",      # subject_name not resolvable via search cache
    "no_gamelog",         # (athlete_id, season) not in gamelog cache
    "no_matching_game",   # gamelog had no game within ±GAME_MATCH_WINDOW_HOURS
    "no_stat_value",      # game found but the requested stat extracted as None
)


@dataclass(frozen=True, slots=True)
class IntervalDatasetExtract:
    """Output of ``build_interval_training_rows``.

    - ``features``: (n, k) float matrix vectorized through the
      caller-supplied ``feature_spec``. ``k`` equals
      ``len(feature_spec.ordered_keys) + len(feature_spec.family_one_hot_keys)``.
    - ``targets``: (n,) float array of CONTINUOUS stat outputs
      (e.g. actual points scored in the game).
    - ``sample_size``: ``len(targets)``; convenience for the caller's
      ``min_samples`` gate.
    - ``window_start`` / ``window_end``: rolling window applied to
      ``predictions.captured_at`` (UTC, half-open ``[start, end)``).
    - ``skipped``: per-reason count of rows excluded from the extract.
      Keys are exactly ``INTERVAL_DATASET_SKIP_REASONS``; values default
      to 0 when no rows hit that reason.
    """

    features: np.ndarray
    targets: np.ndarray
    sample_size: int
    window_start: datetime
    window_end: datetime
    skipped: dict[str, int]


def season_for_captured_at(sport_key: str, captured_at: datetime) -> int:
    """Resolve the gamelog season for a prediction captured at
    ``captured_at``. Mirrors
    ``apps/api/app/services/stats_query.py:default_season_for_sport``
    so a row captured in November 2025 (NBA) looks up the same 2026
    season the live resolver populated.
    """
    sport = (sport_key or "").upper()
    month = captured_at.month
    year = captured_at.year
    if sport == "NBA":
        return year + 1 if month >= 10 else year
    if sport == "MLB":
        return year if month >= 3 else year - 1
    return year


def build_interval_training_rows(
    database_url: str | None,
    *,
    family_key: str,
    stat_key: str,
    feature_spec: FeatureSpec,
    lookback_days: int = 30,
    min_samples: int = 50,
    now: datetime | None = None,
) -> IntervalDatasetExtract | None:
    """Build a (features, continuous_target) extract for one prop family
    + stat key.

    Walks settled ``Prediction`` rows for the family + stat key in the
    last ``lookback_days``, joins each row to its ESPN gamelog cache
    entry, extracts the actual stat value, and vectorizes the
    prediction features through ``feature_spec``.

    Returns ``None`` when the extracted sample size is below
    ``min_samples`` so the caller (typically the CLI) short-circuits
    rather than fitting a regressor on too-thin data. The per-reason
    skip count is logged before the early return so operators can
    diagnose insufficient extracts without re-running with a lower
    gate.
    """
    if lookback_days <= 0:
        raise ValueError(f"lookback_days must be positive, got {lookback_days}")
    if min_samples < 0:
        raise ValueError(f"min_samples must be >= 0, got {min_samples}")
    if not family_key.strip():
        raise ValueError("family_key must be non-empty")
    if not stat_key.strip():
        raise ValueError("stat_key must be non-empty")

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    window_end = now_utc
    window_start = window_end - timedelta(days=lookback_days)

    resolved_url = normalize_database_url(
        database_url
        or os.environ.get("DATABASE_URL")
        or "sqlite:///../api/kalshi_sports_copilot.db"
    )
    engine = create_engine(resolved_url, future=True)

    skipped: dict[str, int] = {reason: 0 for reason in INTERVAL_DATASET_SKIP_REASONS}
    feature_rows: list[np.ndarray] = []
    target_values: list[float] = []

    with engine.connect() as conn:
        prediction_rows = _load_player_prop_predictions(
            conn,
            stat_key=stat_key,
            family_key=family_key,
            window_start=window_start,
            window_end=window_end,
        )
        athlete_resolver = _build_athlete_resolver(conn, prediction_rows)
        gamelog_index = _load_gamelog_index(conn, prediction_rows, athlete_resolver)
        event_starts_at_index = _load_event_starts_at_index(conn, prediction_rows)

    for row in prediction_rows:
        sport_key = (row["sport_key"] or "").upper()
        subject_normalized = _normalize_subject(row["subject_name"])
        team_hint = _normalize_team_hint(row.get("subject_team"))
        captured_at = _coerce_utc(row["captured_at"])
        features_blob = _coerce_json_dict(row.get("features"))
        if not features_blob:
            skipped["no_features"] += 1
            continue

        athlete_id = athlete_resolver(sport_key, subject_normalized, team_hint)
        if not athlete_id:
            skipped["no_athlete_id"] += 1
            continue

        season = season_for_captured_at(sport_key, captured_at)
        gamelog_payload = gamelog_index.get((sport_key, athlete_id, season))
        if not gamelog_payload:
            skipped["no_gamelog"] += 1
            continue

        event_id = row.get("event_id")
        event_starts_at = event_starts_at_index.get(event_id) if event_id else None
        raw_metrics = _select_game_near(
            sport_key,
            gamelog_payload,
            captured_at,
            event_starts_at=event_starts_at,
            forward_hours=GAME_MATCH_FORWARD_HOURS,
            backward_hours=GAME_MATCH_BACKWARD_HOURS,
        )
        if raw_metrics is None:
            skipped["no_matching_game"] += 1
            continue

        stat_value = _stat_value_from_raw_metrics(sport_key, stat_key, raw_metrics)
        if stat_value is None:
            skipped["no_stat_value"] += 1
            continue

        enriched_features = _enrich_prediction_features(row, features_blob)
        feature_rows.append(vectorize(enriched_features, feature_spec))
        target_values.append(float(stat_value))

    sample_size = len(target_values)
    feature_width = len(feature_spec.ordered_keys) + len(feature_spec.family_one_hot_keys)
    if sample_size == 0:
        features_matrix = np.zeros((0, feature_width), dtype=np.float64)
    else:
        features_matrix = np.vstack(feature_rows)
    targets_array = np.asarray(target_values, dtype=np.float64)

    if sample_size < min_samples:
        logger.info(
            "interval_dataset.insufficient_samples family_key=%s stat_key=%s "
            "sample_size=%d min_samples=%d skipped=%s",
            family_key,
            stat_key,
            sample_size,
            min_samples,
            skipped,
        )
        return None

    return IntervalDatasetExtract(
        features=features_matrix,
        targets=targets_array,
        sample_size=sample_size,
        window_start=window_start,
        window_end=window_end,
        skipped=skipped,
    )


# -- SQL loaders -------------------------------------------------------


def _load_player_prop_predictions(
    conn: Connection,
    *,
    stat_key: str,
    family_key: str,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Pull settled player_prop predictions for ``stat_key`` whose
    ``captured_at`` falls in the half-open window
    ``[window_start, window_end)``.

    Filter sequencing (codex round 1 P2 fixes):

    - **Window filter applied in Python**, not SQL: SQLite stores
      ``DateTime(timezone=True)`` as ``"YYYY-MM-DD HH:MM:SS.ffffff+00:00"``
      (space separator) while a Python ``.isoformat()`` bind emits the
      ``"T"`` separator. Lexical comparison of those two formats
      misorders same-day rows (space sorts before ``T`` in ASCII), so
      a SQL ``WHERE captured_at >= :iso_string`` silently drops
      legitimate rows on SQLite. Filter in Python after
      ``_coerce_utc`` to get dialect-agnostic correctness.
    - **Family bucket** derived in Python via ``ml.dataset._family_key``
      (mirrors the recalibrate CLI) so the SQL stays portable across
      SQLite / Postgres without a CASE expression.
    - **Coverage exclusion + market_id dedupe**: mirror
      ``ml.dataset._prepare_frame`` so the interval training corpus
      matches the classifier training corpus (codex round 1 P2 #3) —
      without this, coverage rows or repeated market snapshots can
      train the regressor against rows the classifier never saw.
    """
    sql = text(
        """
        SELECT id, market_id, event_id, sport_key, market_family,
               subject_name, subject_team, stat_key, capture_scope,
               captured_at, features, suggested_price, fair_yes_price,
               edge, confidence, selection_score, threshold,
               prediction_outcome
        FROM predictions
        WHERE market_family = 'player_prop'
          AND prediction_outcome IN ('won', 'lost')
          AND subject_name IS NOT NULL
          AND stat_key = :stat_key
        """
    )
    raw_rows = conn.execute(sql, {"stat_key": stat_key}).mappings().all()

    surviving: list[dict[str, Any]] = []
    for row in raw_rows:
        if _family_key(row["sport_key"], row["market_family"]) != family_key:
            continue
        scope = row.get("capture_scope")
        if scope is not None and str(scope) == "coverage":
            continue
        captured_at = _coerce_utc(row["captured_at"])
        if captured_at < window_start or captured_at >= window_end:
            continue
        surviving.append(dict(row))

    # Dedupe by market_id, keeping the earliest captured_at (matches
    # ``_prepare_frame``'s sort + drop_duplicates(keep='first')).
    surviving.sort(key=lambda r: (_coerce_utc(r["captured_at"]), r["id"]))
    seen_markets: set[Any] = set()
    deduped: list[dict[str, Any]] = []
    for row in surviving:
        market_id = row.get("market_id")
        if market_id is not None and market_id in seen_markets:
            continue
        if market_id is not None:
            seen_markets.add(market_id)
        deduped.append(row)
    return deduped


def _build_athlete_resolver(
    conn: Connection, prediction_rows: list[dict[str, Any]],
):
    """Return a ``(sport_key, subject_bare, team_hint) → athlete_id``
    resolver function backed by ``espn_player_search_cache``.

    Strict team-hint policy (codex round 1 P2 #1):

    - **With team hint** — only the cache row keyed
      ``"<bare>|<TEAM>"`` is accepted. A bare cache row could be a
      DIFFERENT person of the same name (e.g. two ``"John Smith"``s on
      separate teams); the live resolver disambiguates via the hint at
      capture time, and refusing-on-miss here forces the operator to
      warm the cache with the hinted lookup rather than risking
      misattribution. Misses surface as ``skipped["no_athlete_id"]``.
    - **Without team hint** — only the bare cache row is accepted,
      and only when **no** hinted variants exist for the same bare
      subject. If hinted variants exist we don't know which team the
      unhinted prediction referred to, so we refuse rather than guess
      (also ``skipped["no_athlete_id"]``).

    The strict policy may drop some legitimately-resolvable rows on
    cold caches; operators see the count in the CLI summary and can
    re-warm the player cache with team hints.
    """
    unique_subjects: set[tuple[str, str]] = {
        ((row["sport_key"] or "").upper(), _normalize_subject(row["subject_name"]))
        for row in prediction_rows
        if row["sport_key"] and row["subject_name"]
    }
    if not unique_subjects:
        return lambda sport, subject, hint: None

    subjects_by_sport: dict[str, set[str]] = {}
    for sport_key, subject in unique_subjects:
        subjects_by_sport.setdefault(sport_key, set()).add(subject)

    sql = text(
        """
        SELECT sport_key, query_normalized, payload
        FROM espn_player_search_cache
        WHERE sport_key = :sport_key
        """
    )
    # Index keyed by (sport, bare, team_hint_or_None).
    index: dict[tuple[str, str, str | None], str] = {}
    # Per (sport, bare): number of hinted variants present in the cache.
    # >0 means an unhinted prediction is ambiguous and must be skipped.
    hinted_variants: dict[tuple[str, str], int] = {}

    for sport_key, subjects in subjects_by_sport.items():
        cached = conn.execute(sql, {"sport_key": sport_key}).mappings().all()
        for cached_row in cached:
            normalized = str(cached_row["query_normalized"] or "")
            bare, sep, hint_suffix = normalized.partition("|")
            if bare not in subjects:
                continue
            payload = _coerce_json_dict(cached_row["payload"])
            athlete_id = str(payload.get("athlete_id") or "").strip()
            if not athlete_id:
                # Search-miss marker the live resolver wrote.
                continue
            hint_key = hint_suffix.strip().upper() or None if sep else None
            index.setdefault((sport_key, bare, hint_key), athlete_id)
            if hint_key is not None:
                hinted_variants[(sport_key, bare)] = (
                    hinted_variants.get((sport_key, bare), 0) + 1
                )

    def resolve(sport: str, bare: str, team_hint: str | None) -> str | None:
        if team_hint:
            # Strict: only a hinted cache row for this exact team is acceptable.
            return index.get((sport, bare, team_hint))
        # Unhinted prediction — refuse if any hinted variant exists
        # (we don't know which team the prediction referred to).
        if hinted_variants.get((sport, bare), 0) > 0:
            return None
        return index.get((sport, bare, None))

    return resolve


def _load_event_starts_at_index(
    conn: Connection, prediction_rows: list[dict[str, Any]],
) -> dict[Any, datetime]:
    """Map ``event_id → events.starts_at`` for the events referenced
    by the prediction set.

    Used by ``_select_game_near`` to anchor on the exact game start
    when possible (codex round 3 P2 — MLB doubleheaders: both games
    are future-within-window from captured_at, so a captured_at-only
    heuristic mislabels Game 2 with Game 1's stats). When the event
    row is missing or ``starts_at`` is null we fall back to the
    captured_at-based asymmetric window.
    """
    event_ids = {
        row["event_id"]
        for row in prediction_rows
        if row.get("event_id") is not None
    }
    if not event_ids:
        return {}
    # Issue one SELECT per event_id — events.id is the PK, so each is
    # an O(1) index probe. Typical 30-day window has < 1k events; not
    # worth the IN-clause complexity (and SQLAlchemy ``text()`` would
    # need ``bindparam(expanding=True)`` to expand a list).
    sql = text("SELECT id, starts_at FROM events WHERE id = :event_id")
    index: dict[Any, datetime] = {}
    for event_id in event_ids:
        row = conn.execute(sql, {"event_id": event_id}).mappings().first()
        if row is None:
            continue
        starts_at = row.get("starts_at")
        if starts_at is None:
            continue
        try:
            index[event_id] = _coerce_utc(starts_at)
        except (TypeError, ValueError):
            # A malformed starts_at means we silently fall back to the
            # captured_at heuristic for that row — same path as a
            # missing event row, no observability lost.
            continue
    return index


def _load_gamelog_index(
    conn: Connection,
    prediction_rows: list[dict[str, Any]],
    athlete_resolver,
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Map ``(sport_key, athlete_id, season) → cached gamelog payload``.

    Issues one SELECT per unique (sport_key, athlete_id, season) triple
    the prediction set requires. For a 30-day window with a few hundred
    predictions across ~50 athletes that's ~50 SELECTs — much cheaper
    than re-issuing once per prediction.
    """
    triples: set[tuple[str, str, int]] = set()
    for row in prediction_rows:
        sport_key = (row["sport_key"] or "").upper()
        subject = _normalize_subject(row["subject_name"])
        team_hint = _normalize_team_hint(row.get("subject_team"))
        athlete_id = athlete_resolver(sport_key, subject, team_hint)
        if not athlete_id:
            continue
        captured_at = _coerce_utc(row["captured_at"])
        season = season_for_captured_at(sport_key, captured_at)
        triples.add((sport_key, athlete_id, season))
    if not triples:
        return {}

    sql = text(
        """
        SELECT sport_key, athlete_id, season, payload
        FROM espn_player_gamelog_cache
        WHERE sport_key = :sport_key
          AND athlete_id = :athlete_id
          AND season = :season
        """
    )
    index: dict[tuple[str, str, int], dict[str, Any]] = {}
    for sport_key, athlete_id, season in triples:
        cached_row = conn.execute(
            sql,
            {"sport_key": sport_key, "athlete_id": athlete_id, "season": season},
        ).mappings().first()
        if cached_row is None:
            continue
        payload = _coerce_json_dict(cached_row["payload"])
        if payload:
            index[(sport_key, athlete_id, season)] = payload
    return index


# -- Gamelog parsing --------------------------------------------------


def _select_game_near(
    sport_key: str,
    gamelog_payload: dict[str, Any],
    captured_at: datetime,
    *,
    event_starts_at: datetime | None = None,
    forward_hours: int,
    backward_hours: int,
    event_anchor_tolerance_hours: int = EVENT_ANCHOR_TOLERANCE_HOURS,
) -> dict[str, float | None] | None:
    """Find the game in ``gamelog_payload`` matching the prediction's
    target event and return its parsed ``raw_metrics`` dict.

    Two anchor modes:

    1. ``event_starts_at`` is provided → exact-event match. Pick the
       gamelog entry whose ``gameDate`` is within
       ``±event_anchor_tolerance_hours`` of ``event_starts_at``.
       Required for MLB doubleheaders where two games on the same
       calendar day both fall in the captured_at forward window
       (codex round 3 P2). ESPN's reported ``gameDate`` can lag /
       lead the canonical event start by ~1h (time-zone artifacts
       and pre-game reporting), so the small tolerance absorbs that
       slack without losing the disambiguation.

    2. ``event_starts_at`` is None → captured_at-based asymmetric
       window (legacy / event_id-missing rows). Predictions are always
       captured BEFORE tip-off, so future games beat past games. Past
       games are accepted only as a tiny clock-skew fallback when no
       future game lands in the +forward_hours window (codex round 2
       P2 fix for NBA back-to-back misattribution).

    Returns ``None`` when no candidate falls in the window.
    """
    parsed = _parse_gamelog_entries(sport_key, gamelog_payload)
    if not parsed:
        return None

    if event_starts_at is not None:
        tolerance = timedelta(hours=event_anchor_tolerance_hours)
        anchor_candidates: list[tuple[timedelta, dict[str, float | None]]] = []
        for game_date, raw_metrics in parsed:
            delta = abs(game_date - event_starts_at)
            if delta <= tolerance:
                anchor_candidates.append((delta, raw_metrics))
        if not anchor_candidates:
            return None
        anchor_candidates.sort(key=lambda item: item[0])
        return anchor_candidates[0][1]

    forward_cutoff = timedelta(hours=forward_hours)
    backward_cutoff = timedelta(hours=backward_hours)
    future_candidates: list[tuple[timedelta, dict[str, float | None]]] = []
    past_candidates: list[tuple[timedelta, dict[str, float | None]]] = []
    for game_date, raw_metrics in parsed:
        delta = game_date - captured_at
        if delta >= timedelta(0):
            if delta <= forward_cutoff:
                future_candidates.append((delta, raw_metrics))
        else:
            if -delta <= backward_cutoff:
                past_candidates.append((-delta, raw_metrics))
    if future_candidates:
        future_candidates.sort(key=lambda item: item[0])
        return future_candidates[0][1]
    if past_candidates:
        past_candidates.sort(key=lambda item: item[0])
        return past_candidates[0][1]
    return None


def _parse_gamelog_entries(
    sport_key: str, payload: dict[str, Any],
) -> list[tuple[datetime, dict[str, float | None]]]:
    """Walk an ESPN gamelog payload and return
    ``[(game_date, raw_metrics)]`` pairs. Returns ``[]`` for
    unsupported sports or malformed payloads. Mirrors the narrow slice
    we need from ``apps/api/app/services/stats_query.py:_build_game_logs``
    (see module docstring for the cross-app rationale)."""
    if sport_key not in {"NBA", "MLB"}:
        return []
    stat_names = list(payload.get("names") or [])
    events_metadata = dict(payload.get("events") or {})
    games: list[tuple[datetime, dict[str, float | None]]] = []
    for season_type in payload.get("seasonTypes") or []:
        for category in season_type.get("categories") or []:
            for event_stats in category.get("events") or []:
                event_id = str(event_stats.get("eventId") or "")
                if not event_id:
                    continue
                metadata = events_metadata.get(event_id) or {}
                game_date = _parse_iso_datetime(metadata.get("gameDate"))
                if game_date is None:
                    continue
                stats = list(event_stats.get("stats") or [])
                stat_map = {
                    name: stats[index] if index < len(stats) else None
                    for index, name in enumerate(stat_names)
                }
                if sport_key == "NBA":
                    raw_metrics = _nba_raw_metrics_from_stat_map(stat_map)
                else:  # MLB
                    raw_metrics = _mlb_raw_metrics_from_stat_map(stat_map)
                games.append((game_date, raw_metrics))
    return games


def _nba_raw_metrics_from_stat_map(stat_map: dict[str, Any]) -> dict[str, float | None]:
    """ESPN NBA gamelog → raw_metrics. Mirrors
    ``_build_nba_game_logs`` for the count stats we use as interval
    targets. Made/attempted pairs come back as ``"11-20"`` strings; we
    keep only the made side (``three_points_made``,
    ``field_goals_made``).
    """
    return {
        "points": _parse_number(stat_map.get("points")),
        "rebounds": _parse_number(stat_map.get("totalRebounds")),
        "assists": _parse_number(stat_map.get("assists")),
        "steals": _parse_number(stat_map.get("steals")),
        "blocks": _parse_number(stat_map.get("blocks")),
        "turnovers": _parse_number(stat_map.get("turnovers")),
        "three_points_made": _parse_made_attempted(
            stat_map.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
        )[0],
        "field_goals_made": _parse_made_attempted(
            stat_map.get("fieldGoalsMade-fieldGoalsAttempted")
        )[0],
    }


def _mlb_raw_metrics_from_stat_map(stat_map: dict[str, Any]) -> dict[str, float | None]:
    """ESPN MLB gamelog → raw_metrics. Includes the components
    ``total_bases`` is computed from (hits, doubles, triples, home_runs)
    so callers requesting ``total_bases`` can do the math without an
    additional fetch.
    """
    return {
        "at_bats": _parse_number(stat_map.get("atBats")),
        "runs": _parse_number(stat_map.get("runs")),
        "hits": _parse_number(stat_map.get("hits")),
        "doubles": _parse_number(stat_map.get("doubles")),
        "triples": _parse_number(stat_map.get("triples")),
        "home_runs": _parse_number(stat_map.get("homeRuns")),
        "rbis": _parse_number(stat_map.get("RBIs")),
        "walks": _parse_number(stat_map.get("walks")),
        "hit_by_pitch": _parse_number(stat_map.get("hitByPitch")),
        "strikeouts": _parse_number(stat_map.get("strikeouts")),
    }


def _stat_value_from_raw_metrics(
    sport_key: str, stat_key: str, raw_metrics: dict[str, float | None],
) -> float | None:
    """Resolve a sika ``stat_key`` to a single float from
    ``raw_metrics``.

    Supports the canonical single stats per sport (see
    ``_NBA_STAT_TO_RAW`` / ``_MLB_STAT_TO_RAW``) plus underscore-joined
    component combos (``points_rebounds``, ``points_rebounds_assists``,
    etc.) which sum the per-component values. MLB ``total_bases`` is
    computed from singles (= hits − extra-base hits) + 2×doubles +
    3×triples + 4×home_runs.

    Returns ``None`` when any required component is missing. A single
    missing component makes the sum ambiguous; we skip the row rather
    than substitute zero — substituting zero biases the regressor
    toward "lower outputs when stats are partial".
    """
    if sport_key == "MLB" and stat_key == "total_bases":
        return _mlb_total_bases(raw_metrics)

    # Direct alias hit (e.g. ``points`` → ``points``, ``made_threes`` →
    # ``three_points_made``). Single-stat keys resolve here without
    # falling through to the combo-sum branch.
    direct_value = _direct_lookup(sport_key, stat_key, raw_metrics)
    if direct_value is not None:
        return direct_value

    # Combo stat (e.g. ``points_rebounds`` or
    # ``points_rebounds_assists``). Split by underscore, require every
    # component to resolve, sum them. A missing component → None.
    components = stat_key.split("_") if "_" in stat_key else [stat_key]
    component_values: list[float] = []
    for component in components:
        value = _direct_lookup(sport_key, component, raw_metrics)
        if value is None:
            return None
        component_values.append(value)
    if not component_values:
        return None
    return float(sum(component_values))


def _direct_lookup(
    sport_key: str, key: str, raw_metrics: dict[str, float | None],
) -> float | None:
    table = _NBA_STAT_TO_RAW if sport_key == "NBA" else _MLB_STAT_TO_RAW
    raw_field = table.get(key)
    if raw_field is None:
        return None
    value = raw_metrics.get(raw_field)
    if value is None:
        return None
    return float(value)


def _mlb_total_bases(raw_metrics: dict[str, float | None]) -> float | None:
    """Total bases = singles + 2·doubles + 3·triples + 4·home_runs.

    Singles = hits − (doubles + triples + home_runs) because extra-base
    hits are already counted in ``hits``. Returns ``None`` when any
    component is missing (we can't fall back to "no doubles" — that's a
    silent under-count).
    """
    hits = raw_metrics.get("hits")
    doubles = raw_metrics.get("doubles")
    triples = raw_metrics.get("triples")
    home_runs = raw_metrics.get("home_runs")
    if any(value is None for value in (hits, doubles, triples, home_runs)):
        return None
    singles = hits - doubles - triples - home_runs  # type: ignore[operator]
    return float(singles + 2 * doubles + 3 * triples + 4 * home_runs)


# -- Coercion / parsing helpers ---------------------------------------


def _coerce_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        # SQLite returns ISO strings; Postgres returns datetimes. Both
        # land here.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"Unexpected captured_at type: {type(value).__name__}")


def _coerce_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_made_attempted(value: Any) -> tuple[float | None, float | None]:
    if not isinstance(value, str) or "-" not in value:
        return (None, None)
    made_str, _, attempted_str = value.partition("-")
    return (_parse_number(made_str), _parse_number(attempted_str))


def _normalize_subject(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalize_team_hint(value: Any) -> str | None:
    """Normalize a ``subject_team`` value to the cache-key format
    (uppercased, whitespace-trimmed). Returns ``None`` for empty /
    null / blank inputs so unhinted predictions land in the strict
    no-hint branch of the athlete resolver.
    """
    if value is None:
        return None
    text_value = str(value).strip().upper()
    return text_value or None

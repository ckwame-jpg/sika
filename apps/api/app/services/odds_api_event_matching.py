"""Smarter #18 phase 2b — Odds-API-event ↔ sika-event matching.

The Odds API publishes events with ``home_team`` / ``away_team``
as upstream-provider strings ("Los Angeles Lakers", "Saint Louis
Cardinals"). ESPN (sika's primary event source) uses different
conventions ("LA Lakers", "St. Louis Cardinals"). The cache layer
shipped in phase 2a returned raw Odds API events; this module
translates them to sika ``Event`` rows so the deferred parts (2c
scoring-diagnostic + 2d suppression rule) can compare apples to
apples.

## Matching strategy

1. **Time window**: only consider sika events whose ``starts_at``
   is within ``time_window_hours`` of Odds API's ``commence_time``.
   ±2h handles tip-off slippage from cancelled-to-rescheduled
   events while still ruling out next-day games.

2. **Sport filter**: translate the Odds API slug
   (``"basketball_nba"``) back to sika's sport key (``"NBA"``)
   and limit candidates to that sport. The ``Settings``
   already-mapped reverse-lookup lives here.

3. **Team-name similarity**: normalize both names (lowercase,
   punctuation stripped, common abbreviations expanded via the
   alias map below), then compute a ``SequenceMatcher.ratio()``.

4. **Pair scoring**: compute home/away similarity in the natural
   order AND with the home/away swapped (some providers flip the
   convention), take the max. The matched orientation is returned
   so callers know whether to flip.

5. **Threshold**: callers pass ``min_similarity`` (0.7 default —
   conservative; "Lakers" alone scores ~0.65 against "Los Angeles
   Lakers", so 0.7 forces at least one shared anchor word).

## What this PR doesn't include

- ``score_event_pair``-based scoring-diagnostic emission (2c).
- Suppression rule when consensus disagrees with sika's model (2d).
- A persistence layer for the matching results — callers re-match
  on every read. The Odds API event count per sport is in the low
  hundreds at peak, so the per-call cost is negligible.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Event, EventParticipant


logger = logging.getLogger(__name__)


# Reverse-lookup from Odds API sport slug → sika sport key. Mirrors
# ``the_odds_api._SPORT_KEY_TO_ODDS_API_KEY`` but inverted; kept
# local so consumers don't depend on the client module's private
# state.
_ODDS_API_SLUG_TO_SIKA_SPORT: dict[str, str] = {
    "basketball_nba": "NBA",
    "baseball_mlb": "MLB",
    "americanfootball_nfl": "NFL",
    "tennis_atp_french_open": "TENNIS",
}


# City-abbreviation aliases for sports markets. Bidirectional map —
# both forms normalize to the longer canonical form so a match on
# the abbreviated string lines up with the full one. Keys are
# stripped/lowercased; values are also stripped/lowercased.
_CITY_ABBREVIATIONS: dict[str, str] = {
    "la": "los angeles",
    "ny": "new york",
    "sf": "san francisco",
    "okc": "oklahoma city",
    "no": "new orleans",
    "stl": "st louis",
    "saint": "st",
    "philly": "philadelphia",
    "dc": "washington",
}


# Generic words to drop from team strings before similarity. Helps
# "St. Louis Cardinals (MLB)" match "Saint Louis Cardinals" once
# both pass through the alias map + word-drop. ``city`` is NOT in
# this set because it's a legitimate part of names like "Oklahoma
# City Thunder" and "Kansas City Royals" — dropping it would break
# matching between "OKC Thunder" (expanded to "oklahoma city
# thunder") and the full canonical form.
_DROP_WORDS: frozenset[str] = frozenset({"the", "fc", "ii"})


@dataclass(frozen=True, slots=True)
class EventMatchResult:
    """A single sika-Event candidate's matching score against an
    Odds API event.

    ``orientation`` is ``"same"`` when the Odds API home_team matches
    the sika home_team (and away matches away); ``"swapped"`` when
    they swap. Callers reading the bookmaker data need to know the
    orientation to correctly attribute the consensus YES probability
    to the right side.
    """
    event_id: int
    similarity: float
    orientation: str  # "same" | "swapped"


def odds_api_slug_to_sika_sport(slug: str) -> str | None:
    """Translate an Odds API sport slug to sika's sport key.

    Returns ``None`` for unmapped slugs — callers should skip rather
    than guess.
    """
    return _ODDS_API_SLUG_TO_SIKA_SPORT.get(slug.lower())


def normalize_team_name(name: str) -> str:
    """Normalize a team name string for cross-provider comparison.

    Steps:
    1. NFKD normalize unicode (handles diacritics from soccer teams).
    2. Strip leading/trailing whitespace.
    3. Lowercase.
    4. Remove punctuation (keep alphanumeric + spaces).
    5. Expand city abbreviations ("LA Lakers" → "los angeles lakers").
    6. Drop generic words (``the``, ``fc``, ``ii``). ``city`` is
       intentionally kept — see ``_DROP_WORDS`` for the rationale.
    7. Collapse consecutive whitespace.

    Returns the empty string for non-string / empty input — the
    similarity function treats that as zero similarity so no false
    matches.
    """
    if not isinstance(name, str):
        return ""
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    normalized = normalized.strip().lower()
    # Strip punctuation by replacing non-alphanumeric (besides space)
    # with a single space; collapse runs of whitespace below.
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    tokens: list[str] = []
    for token in normalized.split():
        # Expand abbreviation, drop generic words.
        canonical = _CITY_ABBREVIATIONS.get(token, token)
        for expanded in canonical.split():
            if expanded in _DROP_WORDS:
                continue
            tokens.append(expanded)
    return " ".join(tokens)


def team_name_similarity(a: str, b: str) -> float:
    """Return a [0, 1] similarity score between two team name strings.

    Both inputs are normalized first. Uses ``SequenceMatcher.ratio()``
    so token order matters somewhat but exact-token matches dominate
    — "Boston Celtics" vs "Celtics Boston" still scores well; "Lakers"
    vs "Boston Celtics" scores near zero.
    """
    norm_a = normalize_team_name(a)
    norm_b = normalize_team_name(b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    return round(SequenceMatcher(None, norm_a, norm_b).ratio(), 4)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_odds_api_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return _coerce_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _sika_event_home_away_names(event: Event) -> tuple[str | None, str | None]:
    """Return ``(home_team_name, away_team_name)`` from the event's
    ``EventParticipant`` rows.

    Returns ``(None, None)`` when participant data is missing —
    callers skip those events. Some sports (e.g. tennis singles)
    don't have a true home/away; we treat the first competitor as
    "home" and the second as "away" in those cases (the Odds API
    slug for tennis already encodes this).
    """
    competitors = [
        ep
        for ep in event.participants
        if (ep.role or "").lower() in {"competitor", "team", "athlete"}
    ]
    if not competitors:
        return None, None
    home_ep = next((ep for ep in competitors if ep.is_home), None)
    away_ep = next((ep for ep in competitors if not ep.is_home), None)
    # Fallback when ``is_home`` is unreliable: positional order.
    if home_ep is None and competitors:
        home_ep = competitors[0]
    if away_ep is None and len(competitors) > 1:
        away_ep = competitors[1]
    home_name = home_ep.participant.display_name if home_ep is not None and home_ep.participant else None
    away_name = away_ep.participant.display_name if away_ep is not None and away_ep.participant else None
    return home_name, away_name


def _candidate_events(
    db: Session,
    *,
    sport_key: str,
    commence_time: datetime,
    time_window_hours: float,
) -> list[Event]:
    """Return sika events for ``sport_key`` whose ``starts_at`` is
    within ``time_window_hours`` of ``commence_time``."""
    window = timedelta(hours=time_window_hours)
    return list(
        db.scalars(
            select(Event)
            .where(
                Event.sport_key == sport_key,
                Event.starts_at >= commence_time - window,
                Event.starts_at <= commence_time + window,
            )
            .options(selectinload(Event.participants).selectinload(EventParticipant.participant))
        )
    )


def _score_pair(
    odds_home: str,
    odds_away: str,
    sika_home: str | None,
    sika_away: str | None,
) -> tuple[float, str]:
    """Return ``(best_similarity, orientation)`` for one Odds-API
    event vs one sika event.

    Tries the natural orientation (home↔home, away↔away) first; if
    swapped scores higher we return that orientation so callers know
    to flip when reading the bookmaker payload.
    """
    if not sika_home or not sika_away:
        return 0.0, "same"
    same_h = team_name_similarity(odds_home, sika_home)
    same_a = team_name_similarity(odds_away, sika_away)
    swap_h = team_name_similarity(odds_home, sika_away)
    swap_a = team_name_similarity(odds_away, sika_home)
    # Take the min of each pair (both sides must match), then the
    # max across orientations.
    same_score = min(same_h, same_a)
    swap_score = min(swap_h, swap_a)
    if same_score >= swap_score:
        return round(same_score, 4), "same"
    return round(swap_score, 4), "swapped"


def match_odds_api_event(
    db: Session,
    odds_event: dict[str, Any],
    *,
    time_window_hours: float = 2.0,
    min_similarity: float = 0.7,
) -> EventMatchResult | None:
    """Return the best sika ``Event`` match for an Odds API event, or
    ``None`` when no candidate clears ``min_similarity``.

    ``odds_event`` must have ``home_team``, ``away_team``,
    ``commence_time`` (ISO timestamp string), and ``sport_key`` (the
    Odds API slug). Missing fields → ``None``.
    """
    if not isinstance(odds_event, dict):
        return None
    home = odds_event.get("home_team")
    away = odds_event.get("away_team")
    if not isinstance(home, str) or not isinstance(away, str):
        return None
    commence_at = _parse_odds_api_timestamp(odds_event.get("commence_time"))
    if commence_at is None:
        return None
    sport_slug = odds_event.get("sport_key")
    if not isinstance(sport_slug, str):
        return None
    sika_sport = odds_api_slug_to_sika_sport(sport_slug)
    if sika_sport is None:
        return None

    candidates = _candidate_events(
        db,
        sport_key=sika_sport,
        commence_time=commence_at,
        time_window_hours=time_window_hours,
    )
    if not candidates:
        return None

    best: EventMatchResult | None = None
    for event in candidates:
        sika_home, sika_away = _sika_event_home_away_names(event)
        similarity, orientation = _score_pair(home, away, sika_home, sika_away)
        if similarity < min_similarity:
            continue
        if best is None or similarity > best.similarity:
            best = EventMatchResult(
                event_id=event.id, similarity=similarity, orientation=orientation,
            )
    return best


def find_matching_odds_event(
    sika_event: Event,
    odds_events: Iterable[dict[str, Any]],
    *,
    time_window_hours: float = 2.0,
    min_similarity: float = 0.7,
) -> tuple[dict[str, Any], EventMatchResult] | None:
    """Reverse-direction matcher: given a sika ``Event``, find the
    best-matching Odds API event from the supplied list.

    Useful in the scoring path, where the caller already has a sika
    event in hand and wants to look up the corresponding upstream
    quote. Returns ``(odds_event, EventMatchResult)`` on a clearing
    match, or ``None`` when no odds event clears ``min_similarity``.

    Performs the same sport-filter + time-window + pair-score logic
    as ``match_odds_api_event`` but without the SQL candidate query
    (the sika event is fixed).
    """
    sika_home, sika_away = _sika_event_home_away_names(sika_event)
    if not sika_home or not sika_away:
        return None
    sika_starts_at = _coerce_utc(sika_event.starts_at)
    if sika_starts_at is None:
        return None
    window_seconds = time_window_hours * 3600

    best: tuple[dict[str, Any], EventMatchResult] | None = None
    for odds_event in odds_events:
        if not isinstance(odds_event, dict):
            continue
        # Sport filter — only consider odds events whose slug maps to
        # this sika event's sport_key.
        slug = odds_event.get("sport_key")
        if not isinstance(slug, str):
            continue
        if odds_api_slug_to_sika_sport(slug) != sika_event.sport_key:
            continue
        # Time window.
        commence_at = _parse_odds_api_timestamp(odds_event.get("commence_time"))
        if commence_at is None:
            continue
        if abs((commence_at - sika_starts_at).total_seconds()) > window_seconds:
            continue
        # Team-name pair score.
        odds_home = odds_event.get("home_team")
        odds_away = odds_event.get("away_team")
        if not isinstance(odds_home, str) or not isinstance(odds_away, str):
            continue
        similarity, orientation = _score_pair(odds_home, odds_away, sika_home, sika_away)
        if similarity < min_similarity:
            continue
        if best is None or similarity > best[1].similarity:
            best = (
                odds_event,
                EventMatchResult(
                    event_id=sika_event.id,
                    similarity=similarity,
                    orientation=orientation,
                ),
            )
    return best


def match_odds_api_events_batch(
    db: Session,
    odds_events: Iterable[dict[str, Any]],
    *,
    time_window_hours: float = 2.0,
    min_similarity: float = 0.7,
) -> dict[str, EventMatchResult]:
    """Bulk-match a list of Odds API events; returns a dict keyed by
    each event's ``id`` (Odds API's UUID-ish string) → best
    ``EventMatchResult``.

    Unmatched events are omitted from the result. Callers can compare
    ``len(odds_events)`` against ``len(result)`` to gauge mapping
    coverage.
    """
    results: dict[str, EventMatchResult] = {}
    for odds_event in odds_events:
        if not isinstance(odds_event, dict):
            continue
        odds_id = odds_event.get("id")
        if not isinstance(odds_id, str):
            continue
        match = match_odds_api_event(
            db, odds_event,
            time_window_hours=time_window_hours,
            min_similarity=min_similarity,
        )
        if match is not None:
            results[odds_id] = match
    return results

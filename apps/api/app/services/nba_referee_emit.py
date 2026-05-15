"""Smarter #13 phase 2c — feature emitter for NBA referee tendencies.

Joins the daily ASSIGNMENTS cache (PR #101 — which crew works which
game) with the per-season TENDENCY cache (PR #114 — per-referee
foul/FT-rate stats) to emit per-event referee-tendency features.

Phase 2d (deferred) wires the resulting features into a heuristic
factor on points / fouls / FT props in ``scoring.py``. Phase 2b-2
(deferred) wires the BR scraper to the tendency loader's fetcher
contract — until then, the tendency cache stays empty and this
emitter returns ``{}`` (the data-complete check never fires, so
phase 2d's factor degrades to a no-op).

## Output features

::

    {
        "referee_avg_fouls_per_game": float,    # mean across matched crew
        "referee_avg_fta_per_game": float,
        "referee_crew_count": int,              # how many of the 3 main slots had tendency data
        "referee_data_complete": float,         # 1.0 when crew_count >= 2; 0.0 otherwise
    }

The ``data_complete`` threshold of 2-of-3 reflects that a single ref's
tendency is too noisy a signal to act on (one ref's calling pattern
varies considerably depending on which crew chief they're with). The
factor downstream gates on ``data_complete == 1.0``.

## Crew composition

The emitter averages over the THREE main slots (crew chief, referee,
umpire). The alternate is on standby — they only step in mid-game if
another official is injured, so their tendencies don't shape the call
pattern of the actual on-court crew. Including them would silently
bias the average.

## Matching

Team-name matching uses the shared ``normalize_team_name`` helper from
``odds_api_event_matching`` (PR #104) for unicode + city-abbreviation
expansion (``LA Lakers`` → ``Los Angeles Lakers``), followed by an
NBA-SPECIFIC canonicalization step that maps city-only labels to full
``"<city> <mascot>"`` strings. official.nba.com routinely lists
``"Brooklyn @ Boston"`` while sika's event payloads have
``"Brooklyn Nets"`` / ``"Boston Celtics"`` — without canonicalization,
``team_name_similarity("Brooklyn", "Brooklyn Nets")`` only scores
~0.76 (penalized for length difference) and slips below the 0.85
threshold (codex round 1 P1 catch).

The 30 NBA cities are unique-by-team for the city-only canonicalization
pass — no two teams share a city — except ``LA`` (Lakers vs Clippers)
and ``New York`` (Knicks; the Nets are Brooklyn). Those pairs already
require the full team name in production payloads, so the alias map
intentionally omits ``LA`` / ``Los Angeles`` / ``New York`` to avoid
disambiguating to the wrong team.

The emitter scores both orientations (sika's away/home AND the swap)
since the assignment listing's "Away @ Home" labels can disagree
with sika's normalization. Refs are the same regardless of orientation;
we just need to find the right assignment row.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.espn import ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME
from app.services.odds_api_event_matching import normalize_team_name, team_name_similarity


logger = logging.getLogger(__name__)


# Crew slots that contribute to the tendency average. Excludes
# ``alternate`` because the alternate is on standby — their
# tendencies don't shape the actual on-court calling pattern.
_ON_COURT_CREW_SLOTS: tuple[str, ...] = ("crew_chief", "referee", "umpire")


# NBA city → full canonical "<city> <mascot>" string. Used to bridge
# official.nba.com's frequent city-only labels (``"Brooklyn @ Boston"``)
# with sika's full-team-name event payloads (``"Brooklyn Nets"``).
#
# Codex round 1 P1: without this canonicalization the default 0.85
# similarity threshold rejected production rows because
# ``team_name_similarity("Brooklyn", "Brooklyn Nets") = 0.76`` and
# ``("Boston", "Boston Celtics") = 0.74`` — both below threshold,
# average ~0.75, no match.
#
# Keys + values are NORMALIZED form (lowercase, no punctuation,
# expanded city abbreviations) so ``normalize_team_name`` runs first
# and the lookup is exact. ``LA``/``Los Angeles`` is intentionally
# omitted — Lakers vs Clippers ambiguity requires the full team
# name in the input. ``New York`` unambiguously means the Knicks
# in NBA context (the Brooklyn Nets are listed separately as
# ``Brooklyn``), so it IS canonicalized — codex review round 4 P2.
_NBA_CITY_TO_TEAM: dict[str, str] = {
    "atlanta": "atlanta hawks",
    "boston": "boston celtics",
    "brooklyn": "brooklyn nets",
    "charlotte": "charlotte hornets",
    "chicago": "chicago bulls",
    "cleveland": "cleveland cavaliers",
    "dallas": "dallas mavericks",
    "denver": "denver nuggets",
    "detroit": "detroit pistons",
    "golden state": "golden state warriors",
    "houston": "houston rockets",
    "indiana": "indiana pacers",
    "memphis": "memphis grizzlies",
    "miami": "miami heat",
    "milwaukee": "milwaukee bucks",
    "minnesota": "minnesota timberwolves",
    "new orleans": "new orleans pelicans",
    "new york": "new york knicks",
    "oklahoma city": "oklahoma city thunder",
    "orlando": "orlando magic",
    "philadelphia": "philadelphia 76ers",
    "phoenix": "phoenix suns",
    "portland": "portland trail blazers",
    "sacramento": "sacramento kings",
    "san antonio": "san antonio spurs",
    "toronto": "toronto raptors",
    "utah": "utah jazz",
    "washington": "washington wizards",
}

# Minimum 2-of-3 crew matches before the data-complete flag fires.
# Phase 2d's factor will gate on data_complete=1.0 so a single-ref
# match (high variance in solo tendencies) doesn't drive a factor.
_MIN_CREW_FOR_DATA_COMPLETE: int = 2

# Minimum team-name similarity for an assignment to be considered a
# match. Tuned per the Odds API matcher: 0.85 catches abbreviation
# expansions and minor diacritic differences while rejecting
# unrelated teams (Brooklyn Nets vs Boston Celtics ~ 0.5).
DEFAULT_MIN_SIMILARITY: float = 0.85


def _canonicalize_nba_team_name(name: str) -> str:
    """Normalize + canonicalize an NBA team name.

    Steps (in order):
    1. **Ticker-code lookup** (``"BOS"`` → ``"Boston Celtics"``) before
       normalization, since normalize lowercases + strips punctuation
       and would lose ticker-shape info. Reuses the canonical
       ``ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME["NBA"]`` map so the
       ticker vocabulary stays consistent with the rest of the
       codebase. Codex review round 2 P1.
    2. Apply the shared ``normalize_team_name`` helper (unicode
       normalization, lowercase, punctuation strip, city abbreviation
       expansion via the Odds API map).
    3. If the normalized result is a known NBA city-only label
       (``"brooklyn"``), expand to the canonical
       ``"<city> <mascot>"`` form (``"brooklyn nets"``).

    Returns the empty string for non-string / empty input.
    """
    if not isinstance(name, str):
        return ""
    stripped = name.strip()
    if not stripped:
        return ""
    # Ticker lookup first — must run BEFORE normalize, which strips
    # casing and punctuation that the ticker map keys on.
    nba_tickers = ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME.get("NBA", {})
    full_from_ticker = nba_tickers.get(stripped.upper())
    source = full_from_ticker if full_from_ticker else stripped
    normalized = normalize_team_name(source)
    if not normalized:
        return ""
    return _NBA_CITY_TO_TEAM.get(normalized, normalized)


def _nba_team_similarity(a: str, b: str) -> float:
    """NBA-specific team-name similarity. Canonicalizes both inputs
    via ``_canonicalize_nba_team_name`` so city-only labels match
    full ``"<city> <mascot>"`` payloads, then defers to the shared
    ``team_name_similarity`` for the SequenceMatcher.ratio comparison.
    """
    canonical_a = _canonicalize_nba_team_name(a)
    canonical_b = _canonicalize_nba_team_name(b)
    if not canonical_a or not canonical_b:
        return 0.0
    if canonical_a == canonical_b:
        return 1.0
    return team_name_similarity(canonical_a, canonical_b)


def _matched_assignment(
    assignments: list[dict[str, Any]],
    *,
    away_team_name: str,
    home_team_name: str,
    min_similarity: float,
) -> dict[str, Any] | None:
    """Pick the assignment whose (away, home) pair best matches the
    sika event's team labels. Considers BOTH orientations — refs are
    the same regardless of which side is labeled home.

    Both sides must INDIVIDUALLY clear ``min_similarity`` (not just
    the average) — otherwise a perfect home-side match could drag a
    weak away-side match through. Example: assignment ``LA Lakers @
    Boston Celtics`` vs sika ``Los Angeles Clippers @ Boston
    Celtics`` averages above 0.85 because Boston scores 1.0, but the
    away side at ~0.79 indicates wrong team — reject. Codex review
    round 1 plus follow-up self-review for the cross-LA case.

    Returns None when no assignment clears the per-side threshold.
    Returns the highest-scoring assignment (by minimum-side score)
    otherwise — picks the safest match when multiple games on a slate
    both clear the floor.
    """
    best: tuple[float, dict[str, Any]] | None = None
    for entry in assignments:
        if not isinstance(entry, dict):
            continue
        entry_away = str(entry.get("away_team") or "")
        entry_home = str(entry.get("home_team") or "")
        if not entry_away or not entry_home:
            continue
        # Score both orientations — refs don't care about home/away
        # labels, but we want to find the assignment where BOTH teams
        # match (not just one).
        forward_away = _nba_team_similarity(entry_away, away_team_name)
        forward_home = _nba_team_similarity(entry_home, home_team_name)
        backward_away = _nba_team_similarity(entry_away, home_team_name)
        backward_home = _nba_team_similarity(entry_home, away_team_name)
        forward_min = min(forward_away, forward_home)
        backward_min = min(backward_away, backward_home)
        score = max(forward_min, backward_min)
        if score < min_similarity:
            continue
        if best is None or score > best[0]:
            best = (score, entry)
    return best[1] if best is not None else None


def _normalized_referee_name_tokens(name: str) -> list[str]:
    """Tokenize a referee name for surname-initial fallback matching.

    Lowercases, drops periods, splits on whitespace. Empty input
    returns ``[]``. Used by ``_resolve_tendency_row`` to bridge the
    common ``"C. Watson"`` vs ``"Charles Watson"`` mismatch.
    """
    if not isinstance(name, str):
        return []
    return name.lower().replace(".", "").replace(",", "").split()


def _matches_by_initial_and_surname(
    assignment_tokens: list[str], tendency_tokens: list[str],
) -> bool:
    """Return True when ``assignment_tokens`` could plausibly identify
    the same official as ``tendency_tokens`` via the initial+surname
    convention.

    Match rule: surnames (last token) match exactly AND the first
    token's first letter matches. Catches ``"C. Watson"`` ↔
    ``"Charles Watson"`` and the symmetric case where the assignment
    has the full name but the tendency is shortened.

    Returns False on empty inputs or single-token names (where
    surname-only would over-match unrelated refs sharing a last
    name).
    """
    if len(assignment_tokens) < 2 or len(tendency_tokens) < 2:
        return False
    if assignment_tokens[-1] != tendency_tokens[-1]:
        return False
    a_first = assignment_tokens[0]
    t_first = tendency_tokens[0]
    if not a_first or not t_first:
        return False
    return a_first[0] == t_first[0]


def _resolve_tendency_row(
    member_name: str, referees: dict[str, Any],
) -> dict[str, Any] | None:
    """Find ``member_name``'s row in the tendency cache.

    Tries (in order):
    1. Exact dict-key lookup (fast path; works when scraper and BR
       agree on the display name spelling).
    2. Normalized-form match (handles whitespace / punctuation /
       diacritic differences via ``normalize_team_name``).
    3. Initial+surname fallback (handles ``"C. Watson"`` ↔
       ``"Charles Watson"`` — codex review round 3 P2).

    Returns the row dict on match, None otherwise. The fallback
    requires a UNIQUE surname+initial match across the cache —
    multiple candidates (two refs with the same surname and first
    initial) return None to avoid mis-attributing tendencies.
    """
    direct = referees.get(member_name)
    if isinstance(direct, dict):
        return direct
    normalized_target = normalize_team_name(member_name)
    if normalized_target:
        for cand_name, cand_row in referees.items():
            if not isinstance(cand_row, dict):
                continue
            if normalize_team_name(cand_name) == normalized_target:
                return cand_row
    member_tokens = _normalized_referee_name_tokens(member_name)
    if len(member_tokens) < 2:
        return None
    candidates: list[dict[str, Any]] = []
    for cand_name, cand_row in referees.items():
        if not isinstance(cand_row, dict):
            continue
        cand_tokens = _normalized_referee_name_tokens(cand_name)
        if _matches_by_initial_and_surname(member_tokens, cand_tokens):
            candidates.append(cand_row)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _crew_member_name(slot: Any) -> str | None:
    """Pull the display name out of a crew-slot dict.

    The serialized slot is ``{"name": str, "number": int}`` or None
    (the alternate column is frequently empty). Defensive against
    other shapes — returns None for anything unexpected.
    """
    if not isinstance(slot, dict):
        return None
    name = slot.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    return name or None


def _safe_float(value: Any) -> float | None:
    """Filter None / non-numeric / NaN / inf — same shape as
    ``nba_referee_tendencies._safe_float`` so the average path
    doesn't accidentally include garbage values from the cache."""
    import math

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        as_float = float(value)
    else:
        return None
    return as_float if math.isfinite(as_float) else None


def emit_nba_referee_features(
    *,
    assignments_payload: dict[str, Any] | None,
    tendencies_payload: dict[str, Any] | None,
    away_team_name: str | None,
    home_team_name: str | None,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> dict[str, float]:
    """Emit per-event NBA referee-tendency features.

    Returns ``{}`` when:
    - either payload is missing or malformed
    - team names are missing
    - no assignment matches the (away, home) pair above
      ``min_similarity``
    - no on-court crew member has tendencies in the cache (or all
      have None foul values)

    See module docstring for the output feature shape.
    """
    if not isinstance(assignments_payload, dict) or not isinstance(tendencies_payload, dict):
        return {}
    if not (isinstance(away_team_name, str) and away_team_name.strip()):
        return {}
    if not (isinstance(home_team_name, str) and home_team_name.strip()):
        return {}

    assignments = assignments_payload.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        return {}

    referees = tendencies_payload.get("referees")
    if not isinstance(referees, dict) or not referees:
        return {}

    matched = _matched_assignment(
        assignments,
        away_team_name=away_team_name.strip(),
        home_team_name=home_team_name.strip(),
        min_similarity=min_similarity,
    )
    if matched is None:
        return {}

    fouls: list[float] = []
    ftas: list[float] = []
    crew_count = 0
    for slot_name in _ON_COURT_CREW_SLOTS:
        member_name = _crew_member_name(matched.get(slot_name))
        if member_name is None:
            continue
        # Lookup is delegated so the resolution rules (direct,
        # normalized, initial+surname) stay in one place. See
        # ``_resolve_tendency_row`` for the cascade.
        tendency_row = _resolve_tendency_row(member_name, referees)
        if not isinstance(tendency_row, dict):
            continue

        fouls_value = _safe_float(tendency_row.get("fouls_per_game"))
        fta_value = _safe_float(tendency_row.get("fta_per_game"))
        # ``fouls_per_game`` is the load-bearing field for phase 2d's
        # heuristic factor (FT-rate is secondary; total-points adjusts
        # primarily on foul rate). Codex round 1 P2: a row with
        # fta_per_game but null fouls_per_game would otherwise
        # increment crew_count and trip data_complete=1.0 even though
        # ``referee_avg_fouls_per_game`` wouldn't be in the output —
        # consumer would see "complete" data with no foul signal.
        # Require fouls_per_game to be present before counting.
        if fouls_value is None:
            continue
        fouls.append(fouls_value)
        if fta_value is not None:
            ftas.append(fta_value)
        crew_count += 1

    if crew_count == 0:
        return {}

    out: dict[str, float] = {
        "referee_crew_count": float(crew_count),
        "referee_data_complete": 1.0 if crew_count >= _MIN_CREW_FOR_DATA_COMPLETE else 0.0,
    }
    if fouls:
        out["referee_avg_fouls_per_game"] = round(sum(fouls) / len(fouls), 4)
    if ftas:
        out["referee_avg_fta_per_game"] = round(sum(ftas) / len(ftas), 4)
    return out

"""Smarter #18 phase 2c — sportsbook consensus scoring diagnostic.

Phases 2a (cache) and 2b (event matching) shipped the building
blocks. This module composes them into the scoring-time emitter:

- Read the cached H2H odds for the sika event's sport.
- Find the matching Odds API event (by team name + time window).
- De-vig + average across bookmakers to compute consensus YES
  probability (using ``consensus_yes_probability`` from phase 1).
- Return a small diagnostics dict to merge into the scoring kernel's
  ``scoring_diagnostics`` payload.

The diagnostic is **informational** in phase 2c — it surfaces what
the sportsbook consensus thinks but does NOT gate or modify
recommendations. Phase 2d (deferred) adds the suppression rule on
model-vs-book disagreement threshold.

## Emit shape

``{
    "sportsbook_consensus_prob": <float, 0-1>,
    "sportsbook_book_count": <int>,
    "sportsbook_match_orientation": "same" | "swapped",
    "sportsbook_match_similarity": <float, 0-1>,
}``

The consensus is always reported from the perspective of the **sika
event's home team**: when the matcher detected a home/away swap
(some upstream providers flip the convention), the YES probability
is correctly attributed to the sika home team regardless. Phase 2d
will compare against ``probability_yes`` from the scoring kernel,
which is also home-team-oriented for game-line / team-winner
markets.

## Why a separate module

Scoring.py is sika's busiest file; keeping the consensus composition
in its own service module lets the wiring be a one-liner there
(``diagnostics.update(emit_sportsbook_consensus_diagnostics(db, event))``)
and isolates the cross-dependency on the cache + matcher + de-vig
helpers.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.clients.the_odds_api import consensus_yes_probability
from app.models import Event
from app.services.odds_api_cache import cached_h2h_odds
from app.services.odds_api_event_matching import find_matching_odds_event


logger = logging.getLogger(__name__)


def emit_sportsbook_consensus_diagnostics(
    db: Session,
    event: Event,
    *,
    time_window_hours: float = 2.0,
    min_similarity: float = 0.7,
    min_book_count: int = 1,
) -> dict[str, Any]:
    """Return the scoring-diagnostic dict for the sportsbook consensus
    on ``event``, or ``{}`` when no usable signal is available.

    Empty-return is the no-signal sentinel — the caller can
    ``diagnostics.update(emit_sportsbook_consensus_diagnostics(...))``
    unconditionally and the absence of the keys signals "no
    sportsbook prior for this pick."

    ``min_book_count`` (reviewer MEDIUM follow-up): when the consensus
    is averaged from fewer than ``min_book_count`` books, return
    ``{}`` rather than emit a thin-but-confident-looking diagnostic.
    Default 1 keeps phase 2c behavior matching the documented "any
    consensus is informational" intent; phase 2d (suppression rule,
    deferred) should pass ``min_book_count=3`` or similar so a
    single-book quote can't drive a suppression decision.

    No-signal cases (all return ``{}``):
    - No cached odds for the sport (e.g. unsupported sport or empty
      API key).
    - No Odds API event matches the sika event above
      ``min_similarity``.
    - Matched odds event has no usable bookmaker data.
    - Consensus averaged from fewer than ``min_book_count`` books.

    Network is never invoked here — only the cached payload is
    read. The cache refresh job (Smarter #18 phase 2a tick from
    PR #100 + a future scheduler entry) is responsible for keeping
    the cache warm.
    """
    odds_events = cached_h2h_odds(db, event.sport_key, allow_network=False)
    if not odds_events:
        return {}

    match = find_matching_odds_event(
        event,
        odds_events,
        time_window_hours=time_window_hours,
        min_similarity=min_similarity,
    )
    if match is None:
        return {}

    odds_event, result = match
    raw_home = odds_event.get("home_team")
    raw_away = odds_event.get("away_team")
    if not isinstance(raw_home, str) or not isinstance(raw_away, str):
        return {}
    # When the matcher detected a home/away swap, the upstream
    # ``home_team`` is actually sika's away team. To report the
    # consensus from the sika-home perspective, swap the names we
    # pass to the de-vig consensus helper so ``yes_team_name``
    # always names sika's home team.
    if result.orientation == "swapped":
        yes_team_name = raw_away
        no_team_name = raw_home
    else:
        yes_team_name = raw_home
        no_team_name = raw_away

    bookmakers = odds_event.get("bookmakers") or []
    if not isinstance(bookmakers, list):
        return {}

    consensus = consensus_yes_probability(
        bookmakers,
        yes_team_name=yes_team_name,
        no_team_name=no_team_name,
    )
    if consensus is None:
        return {}
    yes_prob, book_count = consensus
    if book_count < min_book_count:
        return {}
    return {
        "sportsbook_consensus_prob": round(float(yes_prob), 4),
        "sportsbook_book_count": int(book_count),
        "sportsbook_match_orientation": result.orientation,
        "sportsbook_match_similarity": round(float(result.similarity), 4),
    }

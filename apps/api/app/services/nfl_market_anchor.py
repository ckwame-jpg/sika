"""Smarter NFL PR 4 — the sportsbook consensus anchor for NFL pricing.

Composes the 3-market Odds API cache (``cached_event_lines``) with the
existing event matcher into one home-oriented struct the game-line
model (Smarter NFL PR 5) consumes:

- ``win_prob_home`` — de-vigged h2h consensus (simple average across
  books, matching the Smarter #18 approach).
- ``spread_home`` — median closing-style spread point for the home
  team (negative = home favored). One anchor line prices every
  alternate-threshold KXNFLSPREAD market via the margin distribution.
- ``total_line`` — median totals point.

Unlike the NBA/MLB sportsbook-consensus path (diagnostic-only), this
anchor is a first-class scoring INPUT for NFL: the books' NFL lines
are the sharpest public estimate available and the internal EPA model
blends toward them (the user-confirmed "market-anchored + situational"
design).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.clients.the_odds_api import (
    TheOddsApiClient,
    consensus_spread_point,
    consensus_total_point,
    consensus_yes_probability,
)
from app.models import Event
from app.services.odds_api_cache import cached_event_lines
from app.services.odds_api_event_matching import find_matching_odds_event


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NflConsensusAnchor:
    """Home-oriented consensus snapshot for one NFL event."""

    win_prob_home: float | None
    spread_home: float | None  # negative = home favored (book convention)
    total_line: float | None
    book_count: int  # h2h books behind win_prob_home
    spread_book_count: int
    total_book_count: int
    fetched_at: datetime | None

    @property
    def has_any_signal(self) -> bool:
        return any(
            value is not None
            for value in (self.win_prob_home, self.spread_home, self.total_line)
        )


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def nfl_consensus_anchor(
    db: Session,
    event: Event,
    *,
    client: TheOddsApiClient | None = None,
    allow_network: bool = False,
    now: datetime | None = None,
) -> NflConsensusAnchor | None:
    """Return the consensus anchor for a sika NFL event, or ``None``
    when the Odds API has no matching event (unmatched game, cache
    empty, no key configured). Sub-signals degrade independently — a
    book set quoting h2h but not spreads still yields
    ``win_prob_home`` with ``spread_home=None``."""
    lines = cached_event_lines(
        db, "NFL", client=client, allow_network=allow_network, now=now
    )
    odds_events = lines.get("events") or []
    if not odds_events:
        return None

    match = find_matching_odds_event(event, odds_events)
    if match is None:
        return None
    odds_event, match_result = match

    odds_home = str(odds_event.get("home_team") or "")
    odds_away = str(odds_event.get("away_team") or "")
    # orientation == "same" → the Odds API home team IS sika's home
    # team; "swapped" → providers disagree on orientation and sika's
    # home team is the Odds API's away string.
    if match_result.orientation == "same":
        home_name, away_name = odds_home, odds_away
    else:
        home_name, away_name = odds_away, odds_home

    bookmakers = odds_event.get("bookmakers") or []
    h2h = consensus_yes_probability(
        bookmakers, yes_team_name=home_name, no_team_name=away_name
    )
    spread = consensus_spread_point(bookmakers, team_name=home_name)
    total = consensus_total_point(bookmakers)

    anchor = NflConsensusAnchor(
        win_prob_home=h2h[0] if h2h else None,
        spread_home=spread[0] if spread else None,
        total_line=total[0] if total else None,
        book_count=h2h[1] if h2h else 0,
        spread_book_count=spread[1] if spread else 0,
        total_book_count=total[1] if total else 0,
        fetched_at=_parse_iso(lines.get("fetched_at")),
    )
    if not anchor.has_any_signal:
        return None
    return anchor

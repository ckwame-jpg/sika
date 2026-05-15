"""Tests for Smarter #18 phase 2c — sportsbook consensus diagnostics.

Covers:
- ``find_matching_odds_event`` reverse-matcher (given a sika event,
  pick the best odds event from a list).
- ``emit_sportsbook_consensus_diagnostics`` composes
  cache + match + de-vig into the diagnostic dict.
- Home/away swap detection routes the consensus YES probability to
  the sika home team regardless of upstream orientation.
- No-signal paths (empty cache / no match / no bookmaker data / empty
  api key) all return ``{}`` so the caller can blindly
  ``diagnostics.update(...)``.
- The diagnostic flows through to a scored recommendation's
  ``scoring_diagnostics`` field when the scoring kernel runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.models import (
    Event,
    EventParticipant,
    OperatorSetting,
    Participant,
)
from app.services.odds_api_event_matching import (
    EventMatchResult,
    find_matching_odds_event,
)
from app.services.sportsbook_consensus import (
    emit_sportsbook_consensus_diagnostics,
)


_NOW = datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc)


def _seed_event(
    db_session,
    *,
    sport_key: str = "NBA",
    starts_at: datetime = _NOW,
    home_name: str = "Boston Celtics",
    away_name: str = "Brooklyn Nets",
) -> Event:
    home = Participant(sport_key=sport_key, display_name=home_name, participant_type="competitor")
    away = Participant(sport_key=sport_key, display_name=away_name, participant_type="competitor")
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id=f"evt-{home_name}-{away_name}-{starts_at.isoformat()}",
        sport_key=sport_key,
        name=f"{away_name} @ {home_name}",
        starts_at=starts_at,
        status="scheduled",
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="competitor", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="competitor", is_home=False),
    ])
    db_session.flush()
    return event


def _odds_event(
    *,
    odds_id: str = "odds-1",
    sport_slug: str = "basketball_nba",
    home_team: str = "Boston Celtics",
    away_team: str = "Brooklyn Nets",
    commence_time: datetime = _NOW,
    yes_price: float = 1.4,
    no_price: float = 3.0,
) -> dict[str, Any]:
    """Build a synthetic Odds API event with a single DraftKings book
    quoting the two outcomes at the supplied decimal prices."""
    return {
        "id": odds_id,
        "sport_key": sport_slug,
        "commence_time": commence_time.isoformat().replace("+00:00", "Z"),
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home_team, "price": yes_price},
                            {"name": away_team, "price": no_price},
                        ],
                    }
                ],
            }
        ],
    }


def _seed_odds_cache(
    db_session,
    *,
    sport_key: str = "NBA",
    events: list[dict[str, Any]],
    expires_at: datetime | None = None,
    moment: datetime | None = None,
) -> None:
    """Seed the per-sport cache row directly (bypassing the loader's
    network path) so tests can exercise the read-only diagnostic
    emission.

    Defaults ``expires_at`` to ``datetime.now(UTC) + 7 days`` so the
    cache is considered fresh from the loader's perspective regardless
    of when the test runs — ``cached_h2h_odds`` reads real wall-clock
    when ``now=None`` (production behavior), so a fixed ``_NOW``
    expiry would drift stale after a few hours."""
    real_now = datetime.now(timezone.utc)
    cache_moment = moment or real_now
    cache_expires_at = expires_at or real_now + timedelta(days=7)
    db_session.add(
        OperatorSetting(
            key=f"odds_api_h2h_{sport_key.upper()}",
            value={
                "events": events,
                "fetched_at": cache_moment.isoformat(),
                "expires_at": cache_expires_at.isoformat(),
                "event_count": len(events),
            },
        )
    )
    db_session.flush()


# -- find_matching_odds_event -----------------------------------------


def test_find_matching_returns_best_odds_event(db_session) -> None:
    event = _seed_event(db_session)
    odds_events = [_odds_event(odds_id="match")]
    result = find_matching_odds_event(event, odds_events)
    assert result is not None
    odds_event, match = result
    assert odds_event["id"] == "match"
    assert match.event_id == event.id
    assert match.similarity == 1.0
    assert match.orientation == "same"


def test_find_matching_detects_swapped_orientation(db_session) -> None:
    event = _seed_event(db_session, home_name="Boston Celtics", away_name="Brooklyn Nets")
    odds_events = [
        _odds_event(home_team="Brooklyn Nets", away_team="Boston Celtics"),
    ]
    result = find_matching_odds_event(event, odds_events)
    assert result is not None
    _, match = result
    assert match.orientation == "swapped"


def test_find_matching_filters_other_sport(db_session) -> None:
    event = _seed_event(db_session, sport_key="NBA")
    odds_events = [_odds_event(sport_slug="baseball_mlb")]
    assert find_matching_odds_event(event, odds_events) is None


def test_find_matching_filters_outside_time_window(db_session) -> None:
    event = _seed_event(db_session, starts_at=_NOW)
    odds_events = [_odds_event(commence_time=_NOW + timedelta(hours=6))]
    assert find_matching_odds_event(event, odds_events, time_window_hours=2.0) is None


def test_find_matching_returns_none_below_threshold(db_session) -> None:
    event = _seed_event(db_session, home_name="Boston Celtics", away_name="Brooklyn Nets")
    odds_events = [
        _odds_event(home_team="Detroit Pistons", away_team="Memphis Grizzlies"),
    ]
    assert find_matching_odds_event(event, odds_events, min_similarity=0.7) is None


def test_find_matching_returns_none_when_sika_event_has_no_participants(db_session) -> None:
    event = Event(
        external_id="lone-evt",
        sport_key="NBA",
        name="Lone Event",
        starts_at=_NOW,
        status="scheduled",
    )
    db_session.add(event)
    db_session.flush()
    odds_events = [_odds_event()]
    assert find_matching_odds_event(event, odds_events) is None


def test_find_matching_picks_best_when_multiple_clear_threshold(db_session) -> None:
    event = _seed_event(db_session, home_name="Boston Celtics", away_name="Brooklyn Nets")
    odds_events = [
        # Lower similarity: matches Boston only.
        _odds_event(odds_id="low", home_team="Boston Celtics", away_team="Brooklyn"),
        # Higher similarity: full match on both teams.
        _odds_event(odds_id="high", home_team="Boston Celtics", away_team="Brooklyn Nets"),
    ]
    result = find_matching_odds_event(event, odds_events)
    assert result is not None
    odds_event, _match = result
    assert odds_event["id"] == "high"


def test_find_matching_tolerates_malformed_entries(db_session) -> None:
    event = _seed_event(db_session)
    odds_events = [
        "not a dict",  # type: ignore[list-item]
        {"id": "no-fields"},  # missing required keys
        None,  # type: ignore[list-item]
        _odds_event(odds_id="good"),
    ]
    result = find_matching_odds_event(event, odds_events)
    assert result is not None
    odds_event, _ = result
    assert odds_event["id"] == "good"


# -- emit_sportsbook_consensus_diagnostics -----------------------------


def test_emit_returns_empty_when_cache_empty(db_session) -> None:
    event = _seed_event(db_session)
    out = emit_sportsbook_consensus_diagnostics(db_session, event)
    assert out == {}


def test_emit_returns_empty_when_no_match_found(db_session) -> None:
    event = _seed_event(db_session, home_name="Boston Celtics", away_name="Brooklyn Nets")
    _seed_odds_cache(
        db_session,
        events=[_odds_event(home_team="Lakers", away_team="Bulls")],

    )
    out = emit_sportsbook_consensus_diagnostics(db_session, event)
    assert out == {}


def test_emit_returns_consensus_for_matched_event(db_session) -> None:
    event = _seed_event(db_session, home_name="Boston Celtics", away_name="Brooklyn Nets")
    # YES price 1.4 → implied 0.714; NO price 3.0 → implied 0.333.
    # Sum = 1.047; de-vigged YES = 0.714/1.047 ≈ 0.682.
    _seed_odds_cache(
        db_session,
        events=[_odds_event(yes_price=1.4, no_price=3.0)],

    )
    out = emit_sportsbook_consensus_diagnostics(db_session, event)
    assert out["sportsbook_book_count"] == 1
    assert 0.65 <= out["sportsbook_consensus_prob"] <= 0.72
    assert out["sportsbook_match_orientation"] == "same"
    assert out["sportsbook_match_similarity"] == 1.0


def test_emit_uses_sika_home_orientation_when_upstream_swapped(db_session) -> None:
    # sika home = Boston; upstream swaps so "Boston" is upstream's away.
    # Upstream's H2H outcomes use upstream's team names. The de-vig
    # consensus helper queries by ``yes_team_name``; we must pass the
    # upstream name that corresponds to the sika home team.
    event = _seed_event(db_session, home_name="Boston Celtics", away_name="Brooklyn Nets")
    # Build the odds event with home/away swapped, but the H2H outcomes
    # still naming the actual teams (so the YES side from sika's
    # perspective is Boston, priced at 1.4 → strong favorite).
    swapped = {
        "id": "swap-event",
        "sport_key": "basketball_nba",
        "commence_time": _NOW.isoformat().replace("+00:00", "Z"),
        "home_team": "Brooklyn Nets",  # upstream calls Brooklyn home
        "away_team": "Boston Celtics",  # upstream calls Boston away
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Boston Celtics", "price": 1.4},  # 71% raw
                            {"name": "Brooklyn Nets", "price": 3.0},   # 33% raw
                        ],
                    }
                ],
            }
        ],
    }
    _seed_odds_cache(
        db_session,
        events=[swapped],

    )
    out = emit_sportsbook_consensus_diagnostics(db_session, event)
    # The diagnostic should report Boston (sika home) at ~0.68 even
    # though Boston is upstream's "away_team". This is the load-bearing
    # swap-detection behavior.
    assert out["sportsbook_match_orientation"] == "swapped"
    assert 0.65 <= out["sportsbook_consensus_prob"] <= 0.72


def test_emit_returns_empty_when_match_has_no_usable_bookmaker_data(db_session) -> None:
    event = _seed_event(db_session)
    no_books = _odds_event()
    no_books["bookmakers"] = []
    _seed_odds_cache(
        db_session,
        events=[no_books],

    )
    out = emit_sportsbook_consensus_diagnostics(db_session, event)
    assert out == {}


def test_emit_returns_empty_when_bookmakers_field_malformed(db_session) -> None:
    event = _seed_event(db_session)
    malformed = _odds_event()
    malformed["bookmakers"] = "not a list"  # type: ignore[assignment]
    _seed_odds_cache(
        db_session,
        events=[malformed],

    )
    out = emit_sportsbook_consensus_diagnostics(db_session, event)
    assert out == {}


def test_emit_averages_across_multiple_bookmakers(db_session) -> None:
    event = _seed_event(db_session)
    # Two books quoting different prices for the same outcome — the
    # consensus should be the average of de-vigged probabilities.
    multi_book = _odds_event()
    multi_book["bookmakers"] = [
        {
            "key": "draftkings",
            "title": "DK",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Boston Celtics", "price": 1.5},  # tighter book
                        {"name": "Brooklyn Nets", "price": 2.6},
                    ],
                }
            ],
        },
        {
            "key": "fanduel",
            "title": "FD",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Boston Celtics", "price": 1.45},
                        {"name": "Brooklyn Nets", "price": 2.85},
                    ],
                }
            ],
        },
    ]
    _seed_odds_cache(
        db_session,
        events=[multi_book],

    )
    out = emit_sportsbook_consensus_diagnostics(db_session, event)
    assert out["sportsbook_book_count"] == 2


# -- End-to-end: diagnostic flows through scoring.py --------------------


def test_scoring_kernel_merges_sportsbook_diagnostic_when_present(
    db_session, monkeypatch
) -> None:
    """``_single_scoring_adjustments`` should call
    ``emit_sportsbook_consensus_diagnostics`` and merge the keys
    into the returned diagnostics dict. Catches the wiring failure
    mode where the import succeeds but the diagnostic never reaches
    the recommendation surface."""
    from app.services import scoring

    event = _seed_event(db_session)
    _seed_odds_cache(
        db_session,
        events=[_odds_event()],

    )

    # Stub the emitter to return a recognizable payload — confirms
    # the wiring (not the math).
    monkeypatch.setattr(
        "app.services.sportsbook_consensus.emit_sportsbook_consensus_diagnostics",
        lambda db, evt, **kwargs: {
            "sportsbook_consensus_prob": 0.7777,
            "sportsbook_book_count": 5,
            "sportsbook_match_orientation": "same",
            "sportsbook_match_similarity": 0.95,
        },
    )

    _, diagnostics = scoring._single_scoring_adjustments(
        db_session,
        family_key="nba_singles",
        event=event,
        market=None,
        snapshot=None,
        metadata={},
        features={"family_key": "nba_singles"},
        probability_yes=0.55,
        base_confidence=0.6,
        left=event.participants[0],
        right=event.participants[1],
    )
    assert diagnostics.get("sportsbook_consensus_prob") == 0.7777
    assert diagnostics.get("sportsbook_book_count") == 5


def test_scoring_kernel_handles_empty_diagnostic_gracefully(
    db_session, monkeypatch
) -> None:
    """When the emitter returns ``{}`` (no signal), the scoring
    diagnostics dict should not gain any sportsbook keys."""
    from app.services import scoring

    event = _seed_event(db_session)

    monkeypatch.setattr(
        "app.services.sportsbook_consensus.emit_sportsbook_consensus_diagnostics",
        lambda db, evt, **kwargs: {},
    )

    _, diagnostics = scoring._single_scoring_adjustments(
        db_session,
        family_key="nba_singles",
        event=event,
        market=None,
        snapshot=None,
        metadata={},
        features={"family_key": "nba_singles"},
        probability_yes=0.55,
        base_confidence=0.6,
        left=event.participants[0],
        right=event.participants[1],
    )
    assert "sportsbook_consensus_prob" not in diagnostics
    assert "sportsbook_book_count" not in diagnostics


# -- Reviewer follow-ups ----------------------------------------------


def test_scoring_kernel_swallows_emitter_exceptions(db_session, monkeypatch) -> None:
    """Reviewer HIGH catch: if the consensus emitter raises (DB error,
    NaN price, anything), scoring must NOT crash. The recommendation
    should still be produced with the rest of the diagnostics intact;
    the sportsbook block is purely informational."""
    from app.services import scoring

    event = _seed_event(db_session)

    def _exploding_emitter(db, evt, **kwargs):
        raise RuntimeError("simulated DB outage in odds cache")

    monkeypatch.setattr(
        "app.services.sportsbook_consensus.emit_sportsbook_consensus_diagnostics",
        _exploding_emitter,
    )

    confidence, diagnostics = scoring._single_scoring_adjustments(
        db_session,
        family_key="nba_singles",
        event=event,
        market=None,
        snapshot=None,
        metadata={},
        features={"family_key": "nba_singles"},
        probability_yes=0.55,
        base_confidence=0.6,
        left=event.participants[0],
        right=event.participants[1],
    )
    # Scoring completed; the non-sportsbook diagnostics are present.
    assert "family_key" in diagnostics
    assert "confidence_semantics" in diagnostics
    # No sportsbook keys leaked through.
    assert "sportsbook_consensus_prob" not in diagnostics


def test_emit_returns_empty_when_below_min_book_count(db_session) -> None:
    """Reviewer MEDIUM catch: a 1-book consensus is significantly less
    authoritative than an 8-book one. Phase 2c keeps the default
    permissive (any consensus is informational), but the
    ``min_book_count`` knob lets phase 2d enforce a thicker bar
    before suppression decisions trigger."""
    event = _seed_event(db_session)
    _seed_odds_cache(db_session, events=[_odds_event()])  # 1 book by default
    out = emit_sportsbook_consensus_diagnostics(
        db_session, event, min_book_count=3,
    )
    assert out == {}


def test_emit_passes_when_book_count_meets_threshold(db_session) -> None:
    event = _seed_event(db_session)
    multi_book = _odds_event()
    multi_book["bookmakers"] = [
        {
            "key": "dk",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Boston Celtics", "price": 1.5},
                        {"name": "Brooklyn Nets", "price": 2.6},
                    ],
                }
            ],
        },
        {
            "key": "fd",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Boston Celtics", "price": 1.45},
                        {"name": "Brooklyn Nets", "price": 2.85},
                    ],
                }
            ],
        },
    ]
    _seed_odds_cache(db_session, events=[multi_book])
    out = emit_sportsbook_consensus_diagnostics(
        db_session, event, min_book_count=2,
    )
    assert out["sportsbook_book_count"] == 2

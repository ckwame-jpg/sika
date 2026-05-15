"""Tests for Smarter #18 phase 2b — Odds API ↔ sika event matching.

Covers:
- ``normalize_team_name`` strips punctuation, expands abbreviations,
  handles unicode diacritics, drops generic words.
- ``team_name_similarity`` returns 1.0 for identical names, ~0 for
  unrelated, and >0.7 for common cross-provider variants
  ("Los Angeles Lakers" vs "LA Lakers").
- ``odds_api_slug_to_sika_sport`` round-trips known slugs.
- ``match_odds_api_event``:
  - Returns the best match within the time window when team names
    align in either orientation.
  - Returns ``None`` for unmapped sports, missing fields,
    out-of-window events, or below-threshold similarity.
  - Detects home/away swap and returns the correct orientation.
- ``match_odds_api_events_batch`` filters unmatched events from the
  result so callers can compute coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.models import Event, EventParticipant, Participant
from app.services.odds_api_event_matching import (
    EventMatchResult,
    match_odds_api_event,
    match_odds_api_events_batch,
    normalize_team_name,
    odds_api_slug_to_sika_sport,
    team_name_similarity,
)


_NOW = datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc)


# -- normalize_team_name ----------------------------------------------


def test_normalize_lowercases_and_strips_whitespace() -> None:
    assert normalize_team_name("  Boston Celtics  ") == "boston celtics"


def test_normalize_removes_punctuation() -> None:
    assert normalize_team_name("St. Louis Cardinals") == "st louis cardinals"


def test_normalize_expands_city_abbreviations() -> None:
    assert normalize_team_name("LA Lakers") == "los angeles lakers"
    assert normalize_team_name("NY Knicks") == "new york knicks"
    assert normalize_team_name("OKC Thunder") == "oklahoma city thunder"


def test_normalize_handles_unicode_diacritics() -> None:
    # MLB has "Padres" etc. that occasionally appear with diacritics
    # in upstream feeds.
    assert normalize_team_name("Padrés") == "padres"


def test_normalize_drops_generic_words() -> None:
    # Soccer "FC" suffix should drop so "Atlanta United FC" matches
    # "Atlanta United".
    assert normalize_team_name("Atlanta United FC") == "atlanta united"


def test_normalize_handles_empty_or_non_string_input() -> None:
    assert normalize_team_name("") == ""
    assert normalize_team_name(None) == ""  # type: ignore[arg-type]
    assert normalize_team_name(42) == ""  # type: ignore[arg-type]


def test_normalize_st_saint_alias_round_trip() -> None:
    # "St Louis Cardinals" and "Saint Louis Cardinals" should normalize
    # to the same form after the saint→st alias fires.
    assert normalize_team_name("St Louis Cardinals") == normalize_team_name(
        "Saint Louis Cardinals"
    )


# -- team_name_similarity ---------------------------------------------


def test_similarity_identical_names() -> None:
    assert team_name_similarity("Boston Celtics", "Boston Celtics") == 1.0


def test_similarity_handles_case_and_whitespace() -> None:
    assert team_name_similarity("Boston Celtics", "  boston   celtics  ") == 1.0


def test_similarity_expansion_aligns_la_with_los_angeles() -> None:
    score = team_name_similarity("LA Lakers", "Los Angeles Lakers")
    assert score == 1.0


def test_similarity_st_vs_saint_alignment() -> None:
    assert team_name_similarity("St. Louis Cardinals", "Saint Louis Cardinals") == 1.0


def test_similarity_unrelated_names_score_low() -> None:
    # "Lakers" vs "Celtics" — nothing in common after normalization.
    score = team_name_similarity("Lakers", "Celtics")
    assert score < 0.5


def test_similarity_empty_input_zero() -> None:
    assert team_name_similarity("", "Boston Celtics") == 0.0
    assert team_name_similarity("Boston Celtics", "") == 0.0


def test_similarity_partial_match_below_one() -> None:
    # "Lakers" alone vs "Los Angeles Lakers" — shared tail but
    # different lengths.
    score = team_name_similarity("Lakers", "Los Angeles Lakers")
    assert 0.0 < score < 1.0


# -- odds_api_slug_to_sika_sport --------------------------------------


def test_slug_translation_known_sports() -> None:
    assert odds_api_slug_to_sika_sport("basketball_nba") == "NBA"
    assert odds_api_slug_to_sika_sport("baseball_mlb") == "MLB"
    assert odds_api_slug_to_sika_sport("americanfootball_nfl") == "NFL"


def test_slug_translation_case_insensitive() -> None:
    assert odds_api_slug_to_sika_sport("BASKETBALL_NBA") == "NBA"


def test_slug_translation_unknown_returns_none() -> None:
    assert odds_api_slug_to_sika_sport("cricket_t20") is None


# -- match_odds_api_event: happy path ---------------------------------


def _seed_event(
    db_session,
    *,
    sport_key: str = "NBA",
    starts_at: datetime,
    home_name: str = "Boston Celtics",
    away_name: str = "Brooklyn Nets",
    ext_id: str | None = None,
) -> Event:
    home = Participant(sport_key=sport_key, display_name=home_name, participant_type="competitor")
    away = Participant(sport_key=sport_key, display_name=away_name, participant_type="competitor")
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id=ext_id or f"evt-{id(starts_at)}-{home_name}-vs-{away_name}",
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
    commence_time: datetime,
) -> dict[str, Any]:
    return {
        "id": odds_id,
        "sport_key": sport_slug,
        "commence_time": commence_time.isoformat().replace("+00:00", "Z"),
        "home_team": home_team,
        "away_team": away_team,
    }


def test_match_finds_exact_team_name_in_window(db_session) -> None:
    event = _seed_event(db_session, starts_at=_NOW)
    odds = _odds_event(commence_time=_NOW)
    result = match_odds_api_event(db_session, odds, time_window_hours=1.0)
    assert result is not None
    assert result.event_id == event.id
    assert result.similarity == 1.0
    assert result.orientation == "same"


def test_match_uses_alias_expansion_for_la_lakers(db_session) -> None:
    event = _seed_event(
        db_session,
        starts_at=_NOW,
        home_name="Los Angeles Lakers",
        away_name="Brooklyn Nets",
    )
    odds = _odds_event(
        commence_time=_NOW,
        home_team="LA Lakers",
        away_team="Brooklyn Nets",
    )
    result = match_odds_api_event(db_session, odds)
    assert result is not None
    assert result.event_id == event.id
    assert result.similarity >= 0.9
    assert result.orientation == "same"


def test_match_detects_home_away_swap(db_session) -> None:
    event = _seed_event(
        db_session,
        starts_at=_NOW,
        home_name="Boston Celtics",
        away_name="Brooklyn Nets",
    )
    odds = _odds_event(
        commence_time=_NOW,
        home_team="Brooklyn Nets",  # swapped
        away_team="Boston Celtics",
    )
    result = match_odds_api_event(db_session, odds)
    assert result is not None
    assert result.event_id == event.id
    assert result.orientation == "swapped"


def test_match_returns_none_for_unmapped_sport(db_session) -> None:
    _seed_event(db_session, starts_at=_NOW)
    odds = _odds_event(commence_time=_NOW, sport_slug="cricket_t20")
    assert match_odds_api_event(db_session, odds) is None


def test_match_returns_none_when_no_events_in_window(db_session) -> None:
    # Event is 6 hours from commence_time; 2h window excludes it.
    _seed_event(db_session, starts_at=_NOW + timedelta(hours=6))
    odds = _odds_event(commence_time=_NOW)
    assert match_odds_api_event(db_session, odds, time_window_hours=2.0) is None


def test_match_returns_none_below_threshold(db_session) -> None:
    # Completely different team names — should fall below 0.7.
    _seed_event(
        db_session,
        starts_at=_NOW,
        home_name="Boston Celtics",
        away_name="Brooklyn Nets",
    )
    odds = _odds_event(
        commence_time=_NOW,
        home_team="Detroit Pistons",
        away_team="Memphis Grizzlies",
    )
    assert match_odds_api_event(db_session, odds, min_similarity=0.7) is None


def test_match_returns_none_for_missing_fields(db_session) -> None:
    _seed_event(db_session, starts_at=_NOW)
    assert match_odds_api_event(db_session, {"home_team": "X"}) is None
    assert match_odds_api_event(db_session, "not a dict") is None  # type: ignore[arg-type]


def test_match_returns_none_for_malformed_commence_time(db_session) -> None:
    _seed_event(db_session, starts_at=_NOW)
    odds = _odds_event(commence_time=_NOW)
    odds["commence_time"] = "not a timestamp"
    assert match_odds_api_event(db_session, odds) is None


def test_match_picks_best_when_multiple_candidates(db_session) -> None:
    # Two events in window: the second has higher team-name similarity.
    _seed_event(
        db_session,
        starts_at=_NOW + timedelta(minutes=20),
        home_name="Brooklyn Nets",  # wrong matchup for the odds query
        away_name="Toronto Raptors",
        ext_id="evt-low",
    )
    target = _seed_event(
        db_session,
        starts_at=_NOW + timedelta(minutes=10),
        home_name="Boston Celtics",
        away_name="Philadelphia 76ers",
        ext_id="evt-high",
    )
    odds = _odds_event(
        commence_time=_NOW,
        home_team="Boston Celtics",
        away_team="Philadelphia 76ers",
    )
    result = match_odds_api_event(db_session, odds, time_window_hours=2.0)
    assert result is not None
    assert result.event_id == target.id


def test_match_filters_other_sports(db_session) -> None:
    # MLB game in window — shouldn't be picked up by an NBA Odds API query.
    _seed_event(
        db_session,
        sport_key="MLB",
        starts_at=_NOW,
        home_name="Boston Celtics",  # silly name on purpose
        away_name="Brooklyn Nets",
    )
    odds = _odds_event(
        commence_time=_NOW,
        sport_slug="basketball_nba",
        home_team="Boston Celtics",
        away_team="Brooklyn Nets",
    )
    assert match_odds_api_event(db_session, odds) is None


# -- match_odds_api_events_batch --------------------------------------


def test_batch_returns_matched_events_keyed_by_id(db_session) -> None:
    evt_a = _seed_event(
        db_session,
        starts_at=_NOW,
        home_name="Boston Celtics",
        away_name="Brooklyn Nets",
    )
    evt_b = _seed_event(
        db_session,
        starts_at=_NOW + timedelta(hours=1),
        home_name="Los Angeles Lakers",
        away_name="Phoenix Suns",
    )
    odds_a = _odds_event(odds_id="o-a", commence_time=_NOW)
    odds_b = _odds_event(
        odds_id="o-b",
        commence_time=_NOW + timedelta(hours=1),
        home_team="LA Lakers",
        away_team="Phoenix Suns",
    )
    odds_c = _odds_event(
        odds_id="o-c",
        commence_time=_NOW,
        home_team="Detroit Pistons",
        away_team="Memphis Grizzlies",
    )  # no matching sika event

    result = match_odds_api_events_batch(db_session, [odds_a, odds_b, odds_c])
    assert set(result.keys()) == {"o-a", "o-b"}
    assert result["o-a"].event_id == evt_a.id
    assert result["o-b"].event_id == evt_b.id


def test_batch_tolerates_malformed_entries(db_session) -> None:
    _seed_event(db_session, starts_at=_NOW)
    result = match_odds_api_events_batch(
        db_session,
        [
            "not a dict",  # type: ignore[list-item]
            {"id": "no-fields"},  # missing required keys
            None,  # type: ignore[list-item]
            _odds_event(odds_id="ok", commence_time=_NOW),
        ],
    )
    assert set(result.keys()) == {"ok"}


def test_batch_empty_input_returns_empty(db_session) -> None:
    assert match_odds_api_events_batch(db_session, []) == {}

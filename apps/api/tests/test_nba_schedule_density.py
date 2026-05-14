"""Tests for Smarter #10 — NBA rest / travel / B2B granular factors.

Covers:
- ``_schedule_context`` derives the new Smarter #10 fields
  (``games_last_3``, ``games_last_5``, ``is_third_in_four``,
  ``is_fourth_in_six``, ``last_game_away``) without disturbing the
  existing keys consumed by the game-line winner path.
- ``_nba_rest_factor`` returns the right multiplier for each
  mutually-exclusive case (4th-in-6, 3rd-in-4, 3+ days rest, deadband).
- ``_nba_travel_factor`` fires only on the handoff's explicit case
  (away today AND last away).
- Per-stat gating + the drift guard pattern from Smarter #5.
"""

from datetime import datetime, timedelta

import pytest

from app.models import Event, EventParticipant, Participant
from app.services.heuristic_factors import (
    _NBA_FACTOR_FNS,
    _NBA_FACTORS_BY_STAT,
    _nba_rest_factor,
    _nba_travel_factor,
    factor_applies,
)
from app.services.scoring import _schedule_context


# -- _schedule_context fixtures -------------------------------------------


def _seed_completed_games(
    db_session,
    participant_id: int,
    *,
    starts_at_offsets_days: list[float],
    home_states: list[bool] | None = None,
    sport_key: str = "NBA",
) -> datetime:
    """Insert completed-game events for ``participant_id`` with the given
    day offsets prior to a fixed anchor. Returns the anchor (``before``)."""
    # Naive datetime to match what SQLite returns on read for the
    # production tz-aware ``DateTime(timezone=True)`` columns. In Postgres
    # both sides remain aware; the helper subtracts naive-from-naive or
    # aware-from-aware indistinguishably.
    anchor = datetime(2026, 5, 14, 19, 0)
    if home_states is None:
        home_states = [True] * len(starts_at_offsets_days)
    for index, offset in enumerate(starts_at_offsets_days):
        event = Event(
            sport_key=sport_key,
            external_id=f"ext-{participant_id}-{index}",
            name=f"Game {index}",
            starts_at=anchor - timedelta(days=offset),
            status="completed",
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            EventParticipant(
                event_id=event.id,
                participant_id=participant_id,
                is_home=home_states[index],
                role="home" if home_states[index] else "away",
            )
        )
    db_session.flush()
    return anchor


@pytest.fixture()
def participant(db_session):
    person = Participant(
        sport_key="NBA",
        display_name="Test Team",
        short_name="TT",
        external_id="schedule-density-team",
        participant_type="team",
    )
    db_session.add(person)
    db_session.flush()
    return person


# -- _schedule_context branches ------------------------------------------


def test_schedule_context_returns_zero_filled_struct_when_before_missing(db_session, participant):
    out = _schedule_context(db_session, participant.id, None)
    assert out["days_rest"] is None
    assert out["games_last_3"] == 0
    assert out["games_last_5"] == 0
    assert out["is_third_in_four"] is False
    assert out["is_fourth_in_six"] is False
    assert out["last_game_away"] is None


def test_schedule_context_detects_third_in_four_nights(db_session, participant):
    # Tonight is the 3rd game in 4 nights: 2 prior games in the last 3 days
    # (offsets 1 and 2 days back).
    anchor = _seed_completed_games(
        db_session,
        participant.id,
        starts_at_offsets_days=[1.0, 2.0],
    )
    out = _schedule_context(db_session, participant.id, anchor)
    assert out["games_last_3"] == 2
    assert out["is_third_in_four"] is True
    # 2 games doesn't make tonight the 4th in 6 — that needs 3 prior games.
    assert out["is_fourth_in_six"] is False


def test_schedule_context_third_in_four_strictly_below_three_days(db_session, participant):
    # Two prior games but both > 3 days back: NOT 3rd-in-4. The
    # ``_games_in_recent_window`` window is strict (< days). Use 3.5 / 4.0
    # to avoid boundary ambiguity in either direction.
    anchor = _seed_completed_games(
        db_session,
        participant.id,
        starts_at_offsets_days=[3.5, 4.0],
    )
    out = _schedule_context(db_session, participant.id, anchor)
    assert out["games_last_3"] == 0
    assert out["is_third_in_four"] is False


def test_schedule_context_fourth_in_six_inside_window(db_session, participant):
    anchor = _seed_completed_games(
        db_session,
        participant.id,
        starts_at_offsets_days=[1.0, 3.0, 4.5],
    )
    out = _schedule_context(db_session, participant.id, anchor)
    assert out["games_last_5"] == 3
    assert out["is_fourth_in_six"] is True


def test_schedule_context_fourth_in_six_excludes_games_outside_window(db_session, participant):
    # 3 games but one is outside the 5-day window — not 4th-in-6.
    anchor = _seed_completed_games(
        db_session,
        participant.id,
        starts_at_offsets_days=[1.0, 3.0, 6.0],
    )
    out = _schedule_context(db_session, participant.id, anchor)
    assert out["games_last_5"] == 2
    assert out["is_fourth_in_six"] is False


def test_schedule_context_last_game_away_when_previous_was_away(db_session, participant):
    anchor = _seed_completed_games(
        db_session,
        participant.id,
        starts_at_offsets_days=[1.0],
        home_states=[False],
    )
    out = _schedule_context(db_session, participant.id, anchor)
    assert out["last_game_away"] is True
    assert out["last_home_state"] is False


def test_schedule_context_last_game_home_when_previous_was_home(db_session, participant):
    anchor = _seed_completed_games(
        db_session,
        participant.id,
        starts_at_offsets_days=[1.0],
        home_states=[True],
    )
    out = _schedule_context(db_session, participant.id, anchor)
    assert out["last_game_away"] is False
    assert out["last_home_state"] is True


def test_schedule_context_last_game_away_is_none_when_no_history(db_session, participant):
    out = _schedule_context(db_session, participant.id, datetime(2026, 5, 14))
    assert out["last_game_away"] is None


def test_schedule_context_preserves_existing_keys_for_game_line_consumers(
    db_session, participant
):
    # Codex Pattern 9 — make sure my additive changes don't disturb the
    # keys the game-line winner path reads (days_rest, games_last_4,
    # back_to_back, games_last_7, last_home_state).
    anchor = _seed_completed_games(
        db_session,
        participant.id,
        starts_at_offsets_days=[1.0, 3.0],
        home_states=[True, False],
    )
    out = _schedule_context(db_session, participant.id, anchor)
    for required_key in (
        "days_rest",
        "games_last_4",
        "games_last_7",
        "back_to_back",
        "last_home_state",
    ):
        assert required_key in out


# -- _nba_rest_factor ----------------------------------------------------


def test_rest_factor_fourth_in_six_wins_over_third_in_four() -> None:
    # When both could apply, take the strongest suppression.
    out = _nba_rest_factor({
        "team_is_fourth_in_six": True,
        "team_is_third_in_four": True,
    })
    assert out == 0.94


def test_rest_factor_third_in_four_suppresses() -> None:
    out = _nba_rest_factor({"team_is_third_in_four": True})
    assert out == 0.96


def test_rest_factor_three_plus_days_rest_boosts() -> None:
    out = _nba_rest_factor({"team_days_rest": 3.0})
    assert out == 1.02


def test_rest_factor_three_days_rest_inclusive_boundary() -> None:
    # 3.0 days exactly fires the boost (inclusive boundary).
    out = _nba_rest_factor({"team_days_rest": 3.0})
    assert out == 1.02


def test_rest_factor_two_point_nine_days_rest_in_deadband() -> None:
    out = _nba_rest_factor({"team_days_rest": 2.9})
    assert out == 1.0


def test_rest_factor_missing_signals_returns_unity() -> None:
    assert _nba_rest_factor({}) == 1.0


def test_rest_factor_suppressor_wins_over_rest_boost() -> None:
    # In practice 3rd-in-4 and 3+ days rest can't co-occur (3rd-in-4
    # requires 2 games in last 3 days, so days_rest < 3). But if a caller
    # passed both, the suppressor must win — that's the explicit ordering
    # in the implementation.
    out = _nba_rest_factor({
        "team_is_third_in_four": True,
        "team_days_rest": 4.0,
    })
    assert out == 0.96


# -- _nba_travel_factor --------------------------------------------------


def test_travel_factor_fires_on_continuous_road_trip() -> None:
    # Today away AND last game also away — Phase 1's only fire case.
    out = _nba_travel_factor({"team_is_home": False, "team_last_game_away": True})
    assert out == 0.98


def test_travel_factor_omits_when_returned_home() -> None:
    # Today home AND last was away — the handoff's "no travel today" case.
    out = _nba_travel_factor({"team_is_home": True, "team_last_game_away": True})
    assert out == 1.0


def test_travel_factor_omits_when_continuous_home_stand() -> None:
    out = _nba_travel_factor({"team_is_home": True, "team_last_game_away": False})
    assert out == 1.0


def test_travel_factor_omits_when_fresh_away_after_home_stand() -> None:
    # Today away AND last was home — Phase 2 (mileage-aware) will likely
    # fire here; Phase 1 leaves it at 1.0 per the handoff's literal reading.
    out = _nba_travel_factor({"team_is_home": False, "team_last_game_away": False})
    assert out == 1.0


def test_travel_factor_omits_when_missing_signals() -> None:
    assert _nba_travel_factor({}) == 1.0
    assert _nba_travel_factor({"team_is_home": False}) == 1.0
    assert _nba_travel_factor({"team_last_game_away": True}) == 1.0


def test_travel_factor_skips_when_last_game_state_unknown() -> None:
    # last_home_state is None when no prior game exists → last_game_away
    # is None → no travel signal.
    out = _nba_travel_factor({"team_is_home": False, "team_last_game_away": None})
    assert out == 1.0


# -- per-stat gating + drift guard ---------------------------------------


@pytest.mark.parametrize(
    "stat",
    (
        "points",
        "rebounds",
        "assists",
        "made_threes",
        "three_points_made",
        "field_goals_made",
        "points_assists",
        "points_rebounds",
        "rebounds_assists",
        "points_rebounds_assists",
    ),
)
def test_rest_and_travel_factors_gated_on_fatigue_sensitive_stats(stat: str) -> None:
    assert factor_applies("NBA", stat, "nba_rest_factor"), (
        f"nba_rest_factor should be gated on {stat}"
    )
    assert factor_applies("NBA", stat, "nba_travel_factor"), (
        f"nba_travel_factor should be gated on {stat}"
    )


@pytest.mark.parametrize("stat", ("steals", "blocks", "turnovers"))
def test_rest_and_travel_factors_excluded_from_non_fatigue_stats(stat: str) -> None:
    # Codex Pattern 3 — defensive counting stats and turnovers aren't
    # materially fatigue-suppressed in NBA data. Keep the factors off so
    # we don't dilute meaningful signal with conservative ±2-6% nudges.
    assert not factor_applies("NBA", stat, "nba_rest_factor"), (
        f"nba_rest_factor should NOT be gated on {stat}"
    )
    assert not factor_applies("NBA", stat, "nba_travel_factor"), (
        f"nba_travel_factor should NOT be gated on {stat}"
    )


def test_rest_and_travel_factor_fns_wired() -> None:
    """Drift guard: a factor name in ``_NBA_FACTORS_BY_STAT`` that is missing
    from ``_NBA_FACTOR_FNS`` silently no-ops. Mirror of the canonical
    platoon-factor wiring test from Smarter #5."""
    gated = {name for tup in _NBA_FACTORS_BY_STAT.values() for name in tup}
    assert "nba_rest_factor" in gated
    assert "nba_rest_factor" in _NBA_FACTOR_FNS
    assert "nba_travel_factor" in gated
    assert "nba_travel_factor" in _NBA_FACTOR_FNS


# -- compute_advanced_factors integration --------------------------------


def test_compute_advanced_factors_emits_rest_factor_at_3rd_in_four() -> None:
    from app.services.heuristic_factors import compute_advanced_factors

    out = compute_advanced_factors("NBA", "points", {"team_is_third_in_four": True})
    assert out.get("nba_rest_factor") == 0.96


def test_compute_advanced_factors_emits_travel_factor_on_continuous_road() -> None:
    from app.services.heuristic_factors import compute_advanced_factors

    out = compute_advanced_factors(
        "NBA",
        "points",
        {"team_is_home": False, "team_last_game_away": True},
    )
    assert out.get("nba_travel_factor") == 0.98


def test_compute_advanced_factors_skips_rest_factor_for_steals() -> None:
    from app.services.heuristic_factors import compute_advanced_factors

    out = compute_advanced_factors(
        "NBA",
        "steals",
        {"team_is_fourth_in_six": True},
    )
    assert "nba_rest_factor" not in out

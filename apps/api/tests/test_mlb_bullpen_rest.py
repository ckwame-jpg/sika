"""Smarter #6 — MLB bullpen rest index (3-day window)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Event, EventParticipant, Participant
from app.services import mlb_advanced
from app.services.heuristic_factors import (
    _MLB_FACTORS_BY_STAT,
    _mlb_opposing_bullpen_rest_factor,
    factor_applies,
)


# -- bullpen_rest_index_from_games ------------------------------------------


@pytest.mark.parametrize(
    "games,expected",
    [
        (0, 1.0),    # fully rested
        (1, 0.6667),  # one game in 3-day window
        (2, 0.3333),
        (3, 0.0),     # saturated
        (4, 0.0),     # doubleheader pushed beyond saturation — still tired
        (-1, 1.0),    # defensive — negative treated as no games
    ],
)
def test_bullpen_rest_index_from_games(games: int, expected: float) -> None:
    assert mlb_advanced.bullpen_rest_index_from_games(games) == pytest.approx(expected, abs=1e-3)


def test_bullpen_rest_index_zero_days_returns_max() -> None:
    """Defensive — a 0-day window can't compute saturation so we report
    'rested' rather than dividing by zero."""

    assert mlb_advanced.bullpen_rest_index_from_games(2, days=0) == 1.0


# -- emit_mlb_bullpen_features ----------------------------------------------


def test_emit_bullpen_features_both_sides() -> None:
    out = mlb_advanced.emit_mlb_bullpen_features(home_games_in_window=1, away_games_in_window=3)
    assert out["home_bullpen_rest_index_3d"] == pytest.approx(0.6667, abs=1e-3)
    assert out["away_bullpen_rest_index_3d"] == pytest.approx(0.0)
    assert out["bullpen_rest_data_complete"] == 1.0


def test_emit_bullpen_features_only_one_side() -> None:
    out = mlb_advanced.emit_mlb_bullpen_features(home_games_in_window=None, away_games_in_window=2)
    assert "home_bullpen_rest_index_3d" not in out
    assert out["away_bullpen_rest_index_3d"] == pytest.approx(0.3333, abs=1e-3)
    assert out["bullpen_rest_data_complete"] == 1.0


def test_emit_bullpen_features_neither_side_returns_empty() -> None:
    assert mlb_advanced.emit_mlb_bullpen_features(None, None) == {}


# -- count_team_games_in_window ---------------------------------------------


def _seed_team(db_session, *, name: str) -> Participant:
    participant = Participant(
        external_id=f"mlb-team-{name}",
        sport_key="MLB",
        display_name=name,
        participant_type="team",
    )
    db_session.add(participant)
    db_session.flush()
    return participant


_event_counter = {"n": 0}


def _seed_event(
    db_session,
    *,
    home: Participant,
    away: Participant,
    starts_at: datetime,
    status: str = "completed",
) -> Event:
    _event_counter["n"] += 1
    event = Event(
        external_id=f"mlb-event-{_event_counter['n']}",
        sport_key="MLB",
        name=f"{away.display_name} at {home.display_name}",
        starts_at=starts_at,
        status=status,
        raw_data={},
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True))
    db_session.add(EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False))
    db_session.flush()
    return event


def test_count_team_games_in_window_counts_completed_games_in_window(db_session) -> None:
    yankees = _seed_team(db_session, name="Yankees")
    red_sox = _seed_team(db_session, name="Red Sox")
    blue_jays = _seed_team(db_session, name="Blue Jays")

    today = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    # 1 day ago — completed, in window
    _seed_event(db_session, home=yankees, away=red_sox, starts_at=today - timedelta(days=1))
    # 2 days ago — completed, in window
    _seed_event(db_session, home=blue_jays, away=yankees, starts_at=today - timedelta(days=2))
    # 5 days ago — completed but OUTSIDE 3-day window
    _seed_event(db_session, home=yankees, away=red_sox, starts_at=today - timedelta(days=5))
    # Same day as scoring (in the future) — strictly excluded
    _seed_event(db_session, home=yankees, away=red_sox, starts_at=today)
    # Scheduled (not completed) yesterday — excluded
    _seed_event(
        db_session,
        home=yankees,
        away=blue_jays,
        starts_at=today - timedelta(days=1, hours=2),
        status="scheduled",
    )
    db_session.flush()

    result = mlb_advanced.count_team_games_in_window(db_session, participant_id=yankees.id, end_at=today)
    assert result == 2


def test_count_team_games_in_window_returns_zero_for_no_history(db_session) -> None:
    yankees = _seed_team(db_session, name="Yankees-alone")
    today = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    assert mlb_advanced.count_team_games_in_window(db_session, participant_id=yankees.id, end_at=today) == 0


def test_count_team_games_in_window_handles_none_participant(db_session) -> None:
    today = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    assert mlb_advanced.count_team_games_in_window(db_session, participant_id=None, end_at=today) == 0


def test_count_team_games_in_window_strict_less_than_end_at(db_session) -> None:
    """Same-day re-evaluation shouldn't count the game being scored as
    part of its own rest window."""

    yankees = _seed_team(db_session, name="Yankees-strict")
    red_sox = _seed_team(db_session, name="Red Sox-strict")
    today = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    # A completed game at EXACTLY the cutoff is excluded.
    _seed_event(db_session, home=yankees, away=red_sox, starts_at=today)
    assert mlb_advanced.count_team_games_in_window(db_session, participant_id=yankees.id, end_at=today) == 0


# -- _mlb_opposing_bullpen_rest_factor ---------------------------------------


def test_opposing_bullpen_rest_factor_fully_rested_returns_slight_suppression() -> None:
    """A fully rested opposing pen suppresses batter runs/RBIs slightly
    — fresh arms shut down late innings."""

    assert _mlb_opposing_bullpen_rest_factor({"opposing_bullpen_rest_index_3d": 1.0}) == pytest.approx(0.95)


def test_opposing_bullpen_rest_factor_saturated_returns_boost() -> None:
    """A tired opposing pen boosts the batter's late-inning offense."""

    assert _mlb_opposing_bullpen_rest_factor({"opposing_bullpen_rest_index_3d": 0.0}) == pytest.approx(1.05)


def test_opposing_bullpen_rest_factor_neutral_at_half() -> None:
    assert _mlb_opposing_bullpen_rest_factor({"opposing_bullpen_rest_index_3d": 0.5}) == pytest.approx(1.0)


def test_opposing_bullpen_rest_factor_returns_one_when_missing() -> None:
    assert _mlb_opposing_bullpen_rest_factor({}) == 1.0
    assert _mlb_opposing_bullpen_rest_factor({"opposing_bullpen_rest_index_3d": None}) == 1.0
    assert _mlb_opposing_bullpen_rest_factor({"opposing_bullpen_rest_index_3d": "bad"}) == 1.0


def test_opposing_bullpen_rest_factor_clamps_out_of_range_input() -> None:
    """Defensive: a malformed upstream value outside [0,1] should still
    produce a bounded multiplier."""

    assert _mlb_opposing_bullpen_rest_factor({"opposing_bullpen_rest_index_3d": 5.0}) == pytest.approx(0.95)
    assert _mlb_opposing_bullpen_rest_factor({"opposing_bullpen_rest_index_3d": -3.0}) == pytest.approx(1.05)


# -- gating -----------------------------------------------------------------


@pytest.mark.parametrize("stat", ["runs", "rbis"])
def test_opposing_bullpen_rest_gated_on_late_inning_offense_stats(stat: str) -> None:
    assert factor_applies("MLB", stat, "opposing_bullpen_rest_factor")


@pytest.mark.parametrize("stat", ["hits", "home_runs", "total_bases", "strikeouts", "walks"])
def test_opposing_bullpen_rest_not_gated_on_other_stats(stat: str) -> None:
    """Hits/HR/TB are too dispersed across innings for bullpen rest to be
    a clean signal. Strikeouts/walks have starter-side factors already."""

    assert not factor_applies("MLB", stat, "opposing_bullpen_rest_factor")


def test_opposing_bullpen_rest_factor_fns_wired() -> None:
    """Drift guard mirroring the platoon-factor pattern."""

    from app.services.heuristic_factors import _MLB_FACTOR_FNS

    gated = {name for tup in _MLB_FACTORS_BY_STAT.values() for name in tup}
    assert "opposing_bullpen_rest_factor" in gated
    assert "opposing_bullpen_rest_factor" in _MLB_FACTOR_FNS

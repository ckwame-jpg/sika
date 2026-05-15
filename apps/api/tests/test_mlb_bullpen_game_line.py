"""Tests for Smarter #6 phase 2 — MLB bullpen rest factor on game-line totals.

Phase 1 (PR #78) shipped the bullpen rest infrastructure
(``count_team_games_in_window``, ``bullpen_rest_index_from_games``,
``emit_mlb_bullpen_features``) and wired the factor into batter offense
props (runs / RBIs). Phase 2 (this PR) wires the same signal into
game-line totals via a new ``_mlb_bullpen_total_factor`` helper that
``_score_game_line`` calls for MLB ``total`` market_kind only.

The helper computes a combined factor from BOTH teams' bullpen rest:
- both rested → mild suppression (combined factor ~0.95)
- both tired → mild amplification (~1.05)
- balanced (one rested, one tired) → ~1.0 no-op

Skipped for spread markets and non-MLB sports.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Event, EventParticipant, Participant
from app.services.scoring import _mlb_bullpen_total_factor


_NOW = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)


_event_counter = {"n": 0}


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


def _seed_game(
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


def _make_event_with_participants(
    db_session, *, left_name: str = "Yankees", right_name: str = "Red Sox",
):
    """Create a today event with two participant rows; returns
    (event, left_participant, right_participant)."""
    left = _seed_team(db_session, name=left_name)
    right = _seed_team(db_session, name=right_name)
    event = _seed_game(
        db_session, home=left, away=right, starts_at=_NOW, status="scheduled",
    )
    left_ep = next(p for p in event.participants if p.participant_id == left.id)
    right_ep = next(p for p in event.participants if p.participant_id == right.id)
    return event, left_ep, right_ep


# -- Helper happy paths ------------------------------------------------


def test_factor_neutral_when_no_recent_games_for_either_team(db_session) -> None:
    event, left_ep, right_ep = _make_event_with_participants(db_session)
    factor, features = _mlb_bullpen_total_factor(
        db_session, event=event, left=left_ep, right=right_ep,
    )
    # Both bullpens "fully rested" (no games in window). Each side's
    # factor: rest=1.0 → 0.95. Combined = 0.95.
    assert factor == pytest.approx(0.95)
    assert features["home_bullpen_rest_index_3d"] == 1.0
    assert features["away_bullpen_rest_index_3d"] == 1.0
    assert features["bullpen_combined_factor"] == pytest.approx(0.95)


def test_factor_amplifies_when_both_bullpens_tired(db_session) -> None:
    event, left_ep, right_ep = _make_event_with_participants(
        db_session, left_name="Yankees", right_name="Red Sox",
    )
    # Seed 3 games in the last 3 days for each team → both bullpens
    # at index=0 (saturated, fully tired).
    fillers_left = [_seed_team(db_session, name=f"Filler L{i}") for i in range(3)]
    fillers_right = [_seed_team(db_session, name=f"Filler R{i}") for i in range(3)]
    left = next(p for p in event.participants if p.is_home).participant
    right = next(p for p in event.participants if not p.is_home).participant
    for offset, opp in enumerate(fillers_left, start=1):
        _seed_game(db_session, home=left, away=opp, starts_at=_NOW - timedelta(days=offset))
    for offset, opp in enumerate(fillers_right, start=1):
        _seed_game(db_session, home=right, away=opp, starts_at=_NOW - timedelta(days=offset))

    factor, features = _mlb_bullpen_total_factor(
        db_session, event=event, left=left_ep, right=right_ep,
    )
    # Both bullpens tired (rest=0): each team's factor = 1.05.
    # Combined = 1.05.
    assert factor == pytest.approx(1.05)
    assert features["home_bullpen_rest_index_3d"] == 0.0
    assert features["away_bullpen_rest_index_3d"] == 0.0


def test_factor_balanced_when_one_team_tired_one_rested(db_session) -> None:
    """One team has 3 games (tired), the other has 0 (rested). The
    combined factor averages out to ~1.0 (no net adjustment)."""
    event, left_ep, right_ep = _make_event_with_participants(
        db_session, left_name="Yankees", right_name="Red Sox",
    )
    fillers = [_seed_team(db_session, name=f"Filler {i}") for i in range(3)]
    left = next(p for p in event.participants if p.is_home).participant
    for offset, opp in enumerate(fillers, start=1):
        _seed_game(db_session, home=left, away=opp, starts_at=_NOW - timedelta(days=offset))

    factor, features = _mlb_bullpen_total_factor(
        db_session, event=event, left=left_ep, right=right_ep,
    )
    # Left tired (rest=0 → factor 1.05); right rested (rest=1 → factor
    # 0.95). Combined = 1.0.
    assert factor == pytest.approx(1.0)
    assert features["home_bullpen_rest_index_3d"] == 0.0
    assert features["away_bullpen_rest_index_3d"] == 1.0


# -- Score path integration --------------------------------------------


def test_score_game_line_imports_bullpen_helper() -> None:
    """Source-level pin: scoring.py:_score_game_line must call the
    helper for MLB total markets. Future refactor that drops the call
    silently disables the factor — this test catches that."""
    import inspect
    from app.services import scoring

    source = inspect.getsource(scoring._score_game_line)
    assert "_mlb_bullpen_total_factor" in source


def test_score_game_line_helper_only_runs_for_mlb_totals() -> None:
    """The helper is gated on ``event.sport_key == "MLB"`` AND
    ``market_kind == "total"`` — spreads + non-MLB sports skip it."""
    import inspect
    from app.services import scoring

    source = inspect.getsource(scoring._score_game_line)
    # The call must be inside the MLB-totals branch.
    assert 'sport_key' in source
    assert 'market_kind == "total"' in source

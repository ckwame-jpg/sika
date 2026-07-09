"""Smarter NFL PR 6 — NFL suppression gates.

- ``nfl_injury``: OUT/DOUBTFUL prop gate (shared helper, family-gated
  to ``nfl_props`` — codex Pattern 9 cross-sport no-fire).
- ``nfl_qb_status``: the questionable-QB gate on NFL winner/game-line
  markets. OUT/Doubtful is priced by the margin model; QUESTIONABLE
  suppresses.
- The game model emits the qb-status group from the official report +
  ESPN intraday feed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.scoring.feature_groups import (
    SuppressionContext,
    check_suppressions,
    nfl_injury_suppress_when,
    nfl_qb_status_suppress_when,
)


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _ctx(features: dict, metadata: dict | None = None, family_key: str = "nfl_singles"):
    return SuppressionContext(
        features=features,
        metadata=metadata or {"copilot_market_family": "winner"},
        family_key=family_key,
    )


def test_qb_questionable_suppresses_fresh_winner() -> None:
    reason = nfl_qb_status_suppress_when(_ctx({
        "nfl_qb_status_data_complete": 1.0,
        "nfl_qb_report_is_fresh": 1.0,
        "nfl_qb_status_questionable": 1.0,
    }))
    assert reason == "starting_qb_questionable"


def test_qb_gate_requires_freshness_and_family() -> None:
    stale = _ctx({
        "nfl_qb_status_data_complete": 1.0,
        "nfl_qb_report_is_fresh": 0.0,
        "nfl_qb_status_questionable": 1.0,
    })
    assert nfl_qb_status_suppress_when(stale) is None
    wrong_family = _ctx(
        {"nfl_qb_status_data_complete": 1.0, "nfl_qb_report_is_fresh": 1.0,
         "nfl_qb_status_questionable": 1.0},
        family_key="nba_singles",
    )
    assert nfl_qb_status_suppress_when(wrong_family) is None
    prop_market = _ctx(
        {"nfl_qb_status_data_complete": 1.0, "nfl_qb_report_is_fresh": 1.0,
         "nfl_qb_status_questionable": 1.0},
        metadata={"copilot_market_family": "player_prop"},
    )
    assert nfl_qb_status_suppress_when(prop_market) is None


def test_qb_out_does_not_trip_the_questionable_gate() -> None:
    """OUT is priced (margin adjustment), not suppressed."""
    reason = nfl_qb_status_suppress_when(_ctx({
        "nfl_qb_status_data_complete": 1.0,
        "nfl_qb_report_is_fresh": 1.0,
        "nfl_qb_status_questionable": 0.0,  # emitter sets 0 for out/doubtful
    }))
    assert reason is None


def test_nfl_prop_injury_gate_family_scoped() -> None:
    features = {
        "injury_data_complete": 1.0,
        "injury_report_is_fresh": 1.0,
        "player_injury_status_out": 1.0,
    }
    assert nfl_injury_suppress_when(_ctx(features, family_key="nfl_props")) == "player_injury_out"
    # Pattern 9: identical features on another sport's family never fire.
    assert nfl_injury_suppress_when(_ctx(features, family_key="nba_props")) is None


def test_check_suppressions_routes_nfl_groups() -> None:
    result = check_suppressions(_ctx({
        "nfl_qb_status_data_complete": 1.0,
        "nfl_qb_report_is_fresh": 1.0,
        "nfl_qb_status_questionable": 1.0,
    }))
    assert result.get("nfl_qb_status") == "starting_qb_questionable"


def test_game_model_emits_questionable_qb_group(db_session) -> None:
    """End-to-end through the game model: official report says the home
    QB1 is Questionable → the group's flag + freshness are set and the
    winner path exposes them for the SUPPRESS callback."""
    from datetime import timedelta as td

    from app.models import (
        Event, EventParticipant, NflDepthChartCache, NflOfficialInjuryCache,
        NflTeamRatingCache, Participant,
    )
    from app.services.scoring.nfl_game_model import score_nfl_team_winner

    event = Event(
        external_id="espn:nfl:401888", sport_key="NFL",
        name="Dallas Cowboys at Philadelphia Eagles",
        status="scheduled", starts_at=NOW + td(hours=30),
    )
    db_session.add(event)
    db_session.flush()
    entries = []
    for name, is_home in (("Philadelphia Eagles", True), ("Dallas Cowboys", False)):
        participant = Participant(external_id=f"e:{name}", sport_key="NFL", display_name=name)
        db_session.add(participant)
        db_session.flush()
        entry = EventParticipant(
            event_id=event.id, participant_id=participant.id,
            role="competitor", is_home=is_home,
        )
        db_session.add(entry)
        entries.append(entry)
    db_session.add(NflTeamRatingCache(
        season=2026, payload={"teams": {}}, cached_at=NOW, expires_at=NOW + td(days=1),
    ))
    db_session.add(NflDepthChartCache(
        season=2026, team="PHI",
        payload={"rows": [{"team": "PHI", "player_name": "Jalen Hurts",
                           "gsis_id": "00-0036389", "pos_abb": "QB", "pos_rank": "1"}]},
        cached_at=NOW, expires_at=NOW + td(days=1),
    ))
    db_session.add(NflOfficialInjuryCache(
        season=2026, week=2,
        payload={"rows": [{"gsis_id": "00-0036389", "full_name": "Jalen Hurts",
                           "team": "PHI", "position": "QB",
                           "report_status": "Questionable"}]},
        cached_at=NOW, expires_at=NOW + td(days=1),
    ))
    db_session.flush()
    db_session.refresh(event)

    _prob, _conf, _reasons, features, groups = score_nfl_team_winner(
        db_session, event, entries[0], entries[1],
    )
    assert features["nfl_qb_status_questionable"] == 1.0
    assert features["nfl_qb_status_data_complete"] == 1.0
    assert features["nfl_qb_report_is_fresh"] == 1.0  # official cache is fresh
    assert "nfl_qb_status" in groups
    # The registry callback fires on exactly these features.
    suppressions = check_suppressions(_ctx(features))
    assert suppressions.get("nfl_qb_status") == "starting_qb_questionable"

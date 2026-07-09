"""Smarter NFL PR 7 — player-prop pricing regression.

The core assertion set: yardage props price on a Normal tail (the
Poisson variance=mean pathology fix), counts stay Poisson, combined
keys never go through MLB's split("_") shredder, the participation
gate reads snap shares, and the factor registry gates NFL stats.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.heuristic_factors import compute_advanced_factors, factor_applies
from app.services.scoring import (
    _nfl_prop_sd,
    _player_prop_participation_gate,
    _poisson_yes_probability,
    _prop_value_from_raw,
    _prop_yes_probability,
)


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


# -- Distribution dispatch -----------------------------------------------------

def test_yardage_prices_normal_not_poisson() -> None:
    features: dict = {"recent_values": []}
    p_normal = _prop_yes_probability("NFL", "passing_yards", 260.0, 250.0, features)
    assert features["distribution_model"] == "normal"
    assert 0.50 < p_normal < 0.60, f"expected coin-flip-ish, got {p_normal}"
    p_poisson = _poisson_yes_probability(260.0, 250.0)
    assert p_poisson > 0.70  # the pathology the Normal model fixes


def test_counts_stay_poisson() -> None:
    features: dict = {"recent_values": []}
    p = _prop_yes_probability("NFL", "receptions", 6.0, 6.0, features)
    assert features["distribution_model"] == "poisson"
    assert p == _poisson_yes_probability(6.0, 6.0)
    nba_features: dict = {"recent_values": []}
    _prop_yes_probability("NBA", "points", 25.0, 25.0, nba_features)
    assert nba_features["distribution_model"] == "poisson"


def test_sd_shrinkage_pulls_toward_prior() -> None:
    from ml_features.nfl_pricing import NFL_STAT_SD_PRIORS

    prior = NFL_STAT_SD_PRIORS["passing_yards"]  # PR 9 tunes this value
    # No sample → prior exactly.
    assert _nfl_prop_sd("passing_yards", []) == prior
    # A tight 4-game sample shrinks partway, never all the way.
    tight = _nfl_prop_sd("passing_yards", [250.0, 255.0, 245.0, 250.0])
    assert 40.0 < tight < prior
    # A wild sample can exceed the prior.
    wild = _nfl_prop_sd("rushing_yards", [10.0, 150.0, 20.0, 140.0])
    assert wild > 26.0


def test_prop_value_combos_never_split_shredded() -> None:
    raw = {"passing_yards": 250.0, "rushing_yards": 40.0, "receiving_yards": 0.0,
           "receptions": 0.0}
    assert _prop_value_from_raw("NFL", "passing_yards", raw) == 250.0
    assert _prop_value_from_raw("NFL", "rushing_yards_receiving_yards", raw) == 40.0
    assert _prop_value_from_raw("NFL", "passing_yards_rushing_yards", raw) == 290.0


# -- Participation gate ---------------------------------------------------------

def _logs(n: int) -> list[dict]:
    return [{"raw_metrics": {"receiving_yards": 60.0}} for _ in range(n)]


def test_nfl_gate_four_log_floor() -> None:
    ok, _ = _player_prop_participation_gate("NFL", _logs(4))
    assert ok is True
    blocked, reason = _player_prop_participation_gate("NFL", _logs(3))
    assert blocked is False and "recent appearances" in reason


def test_nfl_gate_snap_share_role_check() -> None:
    stable, _ = _player_prop_participation_gate(
        "NFL", _logs(6), snap_shares=[88.0, 92.0, 85.0, 90.0, 80.0],
    )
    assert stable is True
    unstable, reason = _player_prop_participation_gate(
        "NFL", _logs(6), snap_shares=[25.0, 30.0, 85.0, 20.0],
    )
    assert unstable is False and "snap counts" in reason
    # No snap data (weeks 1-2 / cold cache) → log floor only.
    no_data, _ = _player_prop_participation_gate("NFL", _logs(5), snap_shares=[])
    assert no_data is True


def test_nba_gate_unchanged() -> None:
    logs = [{"raw_metrics": {"minutes": 34.0}} for _ in range(6)]
    ok, _ = _player_prop_participation_gate("NBA", logs)
    assert ok is True


# -- Factor registry -------------------------------------------------------------

def test_nfl_factor_gating_and_values() -> None:
    features = {
        "nfl_opp_def_epa_per_play": 0.06,   # generous defense
        "nfl_wind_mph": 22.0,               # gale
        "nfl_snap_share_factor_raw": 0.90,  # shrinking role
    }
    factors = compute_advanced_factors("NFL", "receiving_yards", features)
    assert factors["nfl_opp_def_factor"] > 1.05
    assert factors["nfl_weather_passing_factor"] == 0.90
    assert factors["nfl_snap_share_factor"] == 0.90
    # Rushing never sees the wind factor.
    rush = compute_advanced_factors("NFL", "rushing_yards", features)
    assert "nfl_weather_passing_factor" not in rush
    assert factor_applies("NFL", "passing_yards", "nfl_weather_passing_factor")
    assert not factor_applies("NFL", "rushing_yards", "nfl_weather_passing_factor")
    # Missing source data → clean no-op.
    assert compute_advanced_factors("NFL", "receptions", {}) == {}


# -- Context emission -------------------------------------------------------------

def test_emit_nfl_prop_context_reads_caches(db_session) -> None:
    from app.models import (
        Event, EventParticipant, NflTeamRatingCache, NflWeatherCache, Participant,
    )
    from app.services.scoring import _emit_nfl_prop_context, _nfl_recent_snap_shares
    from app.models import NflSnapCountsCache

    event = Event(
        external_id="espn:nfl:401666", sport_key="NFL",
        name="Dallas Cowboys at Philadelphia Eagles",
        status="scheduled", starts_at=NOW + timedelta(hours=20),
    )
    db_session.add(event)
    db_session.flush()
    entries = []
    for name, is_home in (("Philadelphia Eagles", True), ("Dallas Cowboys", False)):
        participant = Participant(external_id=f"p:{name}", sport_key="NFL", display_name=name)
        db_session.add(participant)
        db_session.flush()
        entry = EventParticipant(
            event_id=event.id, participant_id=participant.id,
            role="competitor", is_home=is_home,
        )
        db_session.add(entry)
        entries.append(entry)
    db_session.add(NflTeamRatingCache(
        season=2026,
        payload={"teams": {"DAL": {"def_epa_per_play_allowed": 0.05}}},
        cached_at=NOW, expires_at=NOW + timedelta(days=1),
    ))
    db_session.add(NflWeatherCache(
        event_id="1",  # event ids start at 1 in a fresh test DB
        payload={"wind_speed_mph": 18.0, "is_dome": False},
        cached_at=NOW, expires_at=NOW + timedelta(hours=2),
    ))
    for week, pct in ((5, "90.0"), (6, "88.0"), (7, "85.0"), (8, "91.0")):
        db_session.add(NflSnapCountsCache(
            season=2026, week=week,
            payload={"rows": [{"player": "CeeDee Lamb", "team": "DAL", "offense_pct": pct}]},
            cached_at=NOW, expires_at=NOW + timedelta(days=1),
        ))
    db_session.flush()
    db_session.refresh(event)

    shares = _nfl_recent_snap_shares(db_session, event, "CeeDee Lamb")
    assert shares == [91.0, 85.0, 88.0, 90.0]  # latest week first

    features: dict = {}
    groups: dict = {}
    # Weather cache row keys on the real event id.
    db_session.query(NflWeatherCache).update({"event_id": str(event.id)})
    db_session.flush()
    _emit_nfl_prop_context(db_session, event, entries[1], shares, features, groups)
    assert features["nfl_opp_def_epa_per_play"] == 0.05
    assert features["nfl_wind_mph"] == 18.0
    assert "nfl_snap_share_factor_raw" in features
    assert {"nfl_team_ratings", "nfl_weather", "nfl_snap_counts"} <= set(groups)

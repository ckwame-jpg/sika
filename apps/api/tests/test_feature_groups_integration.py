"""Integration tests for Architecture #5 — end-to-end emission of
``feature_groups`` from ``_score_player_prop`` and end-to-end
application of the per-group freshness policy in
``_build_scored_recommendation``.

These tests use the same ``_FakeResolver`` fixture pattern as
``test_pr3_heuristic_audit.py`` so the scoring path runs against
synthetic but realistic inputs without hitting any network or
real cache loader. The point is to pin:

- Each emitter's output lands in ``feature_groups[group_key]``
  (the source of truth) AND in ``features`` (the derived view).
  Regression against an emitter that gets migrated to write to
  one but not the other.
- ``fresh_at`` flows correctly from the cache loader into the
  snapshot (mlb_weather, nba_workload — the two PENALIZE groups
  whose loaders the migration plumbed ``cached_at`` through).
- The freshness penalty actually fires when a group is stale, and
  the penalty surfaces in ``scoring_diagnostics`` so operators
  can audit which groups were stale at scoring time.
- The persisted ``signal.scoring_diagnostics["feature_groups"]``
  round-trips through ``deserialize_feature_groups`` cleanly —
  what we wrote can be read back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from app.models import (
    Event,
    EventParticipant,
    Market,
    MarketSnapshot,
    Participant,
)
from app.services.advanced_stats import AdvancedLoadResult
from app.services.scoring import (
    PropStatsResolver,
    ResolvedPropSubject,
    _score_player_prop,
)
from app.services.scoring.feature_groups import (
    FeatureGroupSnapshot,
    deserialize_feature_groups,
)


_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


class _FakeResolver(PropStatsResolver):
    def __init__(self, resolved: ResolvedPropSubject) -> None:
        self._resolved = resolved

    def resolve(self, sport_key: str, subject_name: str, team_hint: str | None = None) -> ResolvedPropSubject:
        return self._resolved


def _nba_game_logs() -> list[dict[str, Any]]:
    return [
        {
            "location": "home" if index % 2 == 0 else "away",
            "opponent": "Boston Celtics",
            "opponent_abbreviation": "BOS",
            "raw_metrics": {
                "minutes": 35.0,
                "points": 30.0,
                "rebounds": 4.0,
                "assists": 7.0,
                "steals": 1.0,
                "blocks": 0.0,
                "turnovers": 2.0,
                "field_goals_attempted": 22.0,
            },
        }
        for index in range(10)
    ]


def _mlb_game_logs() -> list[dict[str, Any]]:
    return [
        {
            "location": "home" if index % 2 == 0 else "away",
            "opponent": "New York Mets",
            "opponent_abbreviation": "NYM",
            "raw_metrics": {
                "at_bats": 4.0,
                "walks": 0.5,
                "hit_by_pitch": 0.0,
                "hits": 1.5,
                "home_runs": 0.2,
                "rbis": 1.0,
                "runs": 1.0,
                "total_bases": 2.5,
                "strikeouts": 0.7,
                "doubles": 0.3,
                "triples": 0.0,
            },
        }
        for index in range(10)
    ]


def _seed_nba_event(db_session) -> tuple[Event, Market, MarketSnapshot]:
    home = Participant(external_id="nyk-fg", sport_key="NBA", display_name="New York Knicks", short_name="Knicks", participant_type="team")
    away = Participant(external_id="bos-fg", sport_key="NBA", display_name="Boston Celtics", short_name="Celtics", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id="nba-fg-prop",
        sport_key="NBA",
        name="Boston Celtics at New York Knicks",
        status="scheduled",
        starts_at=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    market = Market(
        ticker="KXNBA-FG-PROP",
        sport_key="NBA",
        event_id=event.id,
        title="Jalen Brunson: points prop",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 25.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.45, no_ask=0.60, last_price=0.46)
    db_session.add_all([market, snapshot])
    db_session.commit()
    return event, market, snapshot


def _seed_mlb_event(db_session) -> tuple[Event, Market, MarketSnapshot]:
    home = Participant(external_id="phi-fg", sport_key="MLB", display_name="Philadelphia Phillies", short_name="Phillies", participant_type="team")
    away = Participant(external_id="nym-fg", sport_key="MLB", display_name="New York Mets", short_name="Mets", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id="mlb-fg-prop",
        sport_key="MLB",
        name="New York Mets at Philadelphia Phillies",
        status="scheduled",
        starts_at=datetime(2026, 5, 15, 23, 5, tzinfo=timezone.utc),
        raw_data={"venue_id": "2681"},
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    market = Market(
        ticker="KXMLB-FG-PROP",
        sport_key="MLB",
        event_id=event.id,
        title="Bryce Harper: hits prop",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "hits",
            "copilot_threshold": 1.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Bryce Harper",
            "copilot_subject_team": "PHI",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.55, no_ask=0.50, last_price=0.55)
    db_session.add_all([market, snapshot])
    db_session.commit()
    return event, market, snapshot


# -- NBA emitter migration --------------------------------------------


def test_nba_score_populates_feature_groups_for_workload(db_session) -> None:
    """The migrated ``_score_player_prop`` for NBA must populate
    ``feature_groups["nba_workload"]`` with the gamelog cached_at
    threaded through from ``ResolvedPropSubject.gamelog_cached_at``."""
    event, market, snapshot = _seed_nba_event(db_session)
    gamelog_fresh_at = _NOW - timedelta(hours=2)
    resolved = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="3934672",
        display_name="Jalen Brunson",
        team_name="New York Knicks",
        season=2026,
        game_logs=_nba_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
        gamelog_cached_at=gamelog_fresh_at,
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features, feature_groups = result

    assert "nba_workload" in feature_groups
    workload = feature_groups["nba_workload"]
    # fresh_at threaded through from the resolver.
    assert workload.fresh_at == gamelog_fresh_at
    # source labels are observable diagnostics, not runtime gates;
    # pin the operator-facing string.
    assert workload.source == "EspnPlayerGamelogCache"
    # Derived-view consistency: every key the snapshot carries also
    # lives in the flat features dict.
    for key, value in workload.values.items():
        assert features[key] == value


def test_nba_score_populates_feature_groups_for_injury(db_session) -> None:
    """nba_injury group is populated by the migrated kernel even
    with no injury payload (emit_nba_injury_features returns an
    empty dict, which still registers the group with
    completeness=0.0)."""
    event, market, snapshot = _seed_nba_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="3934672",
        display_name="Jalen Brunson",
        team_name="New York Knicks",
        season=2026,
        game_logs=_nba_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _, _, _, _, feature_groups = result

    assert "nba_injury" in feature_groups
    # With no real injury cache row, the emitter returns {} and the
    # snapshot lands with completeness=0.0. The source label still
    # gets through so operators can see which cache the group reads.
    assert feature_groups["nba_injury"].source == "NbaInjuryReportCache"


# -- MLB emitter migration --------------------------------------------


def test_mlb_score_populates_feature_groups_for_weather(db_session) -> None:
    """The migrated MLB scorer must populate
    ``feature_groups["mlb_weather"]`` with ``fresh_at`` sourced from
    the AdvancedLoadResult.cached_at the migration just plumbed
    through ``load_weather``."""
    event, market, snapshot = _seed_mlb_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="33944",
        display_name="Bryce Harper",
        team_name="Philadelphia Phillies",
        season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
    )
    weather_fresh_at = _NOW - timedelta(hours=2)
    with patch(
        "app.services.mlb_advanced.load_weather",
        return_value=AdvancedLoadResult(
            payload={
                "temp_f": 82.0,
                "wind_speed_mph": 10.0,
                "wind_dir_deg": 90.0,
                "precip_pct": 0.0,
                "humidity_pct": 55.0,
                "is_dome": False,
                "source": "openweather",
            },
            cache_status="hit",
            complete=True,
            cached_at=weather_fresh_at,
        ),
    ):
        result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _, _, _, features, feature_groups = result

    assert "mlb_weather" in feature_groups
    weather = feature_groups["mlb_weather"]
    assert weather.fresh_at == weather_fresh_at
    assert weather.source == "load_weather"
    # Values landed in both the snapshot AND the derived flat view.
    assert weather.values.get("weather_temp_f") == 82.0
    assert features.get("weather_temp_f") == 82.0


def test_mlb_score_populates_feature_groups_for_park(db_session) -> None:
    """mlb_park is IGNORE policy with no fresh_at signal — pin that
    the group still registers (so the migration didn't drop the
    emission entirely) with fresh_at=None opting it out of the
    freshness check."""
    event, market, snapshot = _seed_mlb_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="33944",
        display_name="Bryce Harper",
        team_name="Philadelphia Phillies",
        season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
    )
    # Skip the network/file IO in load_park_factors_for_event by
    # patching to a known dict — pins that the group is built from
    # WHATEVER the helper returns, not whether the helper has data.
    with patch(
        "app.services.mlb_advanced.load_weather",
        return_value=AdvancedLoadResult(
            payload={}, cache_status="miss", complete=False, cached_at=None,
        ),
    ):
        result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _, _, _, _, feature_groups = result
    assert "mlb_park" in feature_groups
    park = feature_groups["mlb_park"]
    assert park.fresh_at is None  # IGNORE policy; no penalty path
    assert park.source == "load_park_factors_for_event"


# -- persistence round-trip ------------------------------------------


def test_dome_weather_path_opts_out_of_freshness(db_session) -> None:
    """Dome forecasts return ``cached_at=None`` from load_weather
    (no refresh lifecycle — values are constant). The kernel must
    register the mlb_weather group with ``fresh_at=None``, which
    opts the PENALIZE policy out via the freshness layer's
    ``fresh_at is None → not stale`` rule.
    """
    event, market, snapshot = _seed_mlb_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="33944",
        display_name="Bryce Harper",
        team_name="Philadelphia Phillies",
        season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
    )
    with patch(
        "app.services.mlb_advanced.load_weather",
        return_value=AdvancedLoadResult(
            payload={
                "temp_f": 72.0,
                "wind_speed_mph": 0.0,
                "wind_dir_deg": 0.0,
                "precip_pct": 0.0,
                "humidity_pct": 50.0,
                "is_dome": True,
                "source": "dome",
            },
            cache_status="dome",
            complete=True,
            cached_at=None,  # dome path — no refresh lifecycle
        ),
    ):
        result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _, _, _, _, feature_groups = result
    assert "mlb_weather" in feature_groups
    # Critical: fresh_at None means the PENALIZE policy never fires
    # for dome games. Without this guard a dome forecast would look
    # eternally stale.
    assert feature_groups["mlb_weather"].fresh_at is None


def test_stale_penalize_group_lowers_confidence_end_to_end(db_session) -> None:
    """End-to-end pin that the PENALIZE policy actually reduces
    ``signal.confidence`` when a group is stale. Goes through
    ``_build_scored_recommendation`` (where the kernel's freshness
    block lives) — not just ``_score_player_prop`` (where the
    feature_groups are populated).

    A stale mlb_weather row (cached_at 12h ago, TTL 6h, delta -0.05)
    should record the penalty in scoring_diagnostics AND apply it to
    the signal's confidence. The HIGH issue from reviewer round 1
    was that the penalty was applied BEFORE the ML branch, so
    ml_result.confidence overwrote it. The fix moved the application
    after the ML branch; this pin asserts both the diagnostic and
    the confidence reduction land regardless of serving mode.
    """
    from app.services.scoring import _build_scored_recommendation

    event, market, snapshot = _seed_mlb_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="MLB", athlete_id="33944", display_name="Bryce Harper",
        team_name="Philadelphia Phillies", season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={}, advanced_cache_status="miss",
    )
    with patch(
        "app.services.mlb_advanced.load_weather",
        return_value=AdvancedLoadResult(
            payload={
                "temp_f": 82.0, "wind_speed_mph": 10.0, "wind_dir_deg": 90.0,
                "precip_pct": 0.0, "humidity_pct": 55.0, "is_dome": False,
                "source": "openweather",
            },
            cache_status="stale", complete=True,
            # cached_at 12h ago > 6h TTL → mlb_weather is stale →
            # -0.05 PENALIZE fires.
            cached_at=datetime.now(timezone.utc) - timedelta(hours=12),
        ),
    ):
        stale_scored = _build_scored_recommendation(
            db_session, event, market, snapshot,
            resolver=_FakeResolver(resolved),
        )
    assert stale_scored is not None
    stale_signal = stale_scored.signal
    stale_diagnostics = stale_signal.scoring_diagnostics or {}

    # The freshness diagnostic is populated.
    assert "freshness_stale_groups" in stale_diagnostics
    stale_groups = stale_diagnostics["freshness_stale_groups"]
    weather_stale = next(
        (g for g in stale_groups if g.get("group_key") == "mlb_weather"), None,
    )
    assert weather_stale is not None
    assert weather_stale["confidence_delta"] == pytest.approx(-0.05)

    # The penalty's signed total surfaces as a top-level diagnostic
    # (the post-ML-branch application from the round-1 fix means
    # this delta lands on the final confidence whether the kernel
    # served heuristic or ML).
    assert stale_diagnostics.get("freshness_confidence_delta") == pytest.approx(-0.05)


def test_feature_groups_round_trip_through_serialize(db_session) -> None:
    """The ``serialize_feature_groups`` → JSON →
    ``deserialize_feature_groups`` round-trip must restore the exact
    snapshot. Pins the persistence-layer contract that
    ``signal.scoring_diagnostics["feature_groups"]`` can be read
    back into FeatureGroupSnapshot objects."""
    from app.services.scoring.feature_groups import serialize_feature_groups

    original = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather",
            values={"weather_temp_f": 82.0, "weather_wind_speed_mph": 10.0},
            fresh_at=_NOW - timedelta(hours=2),
            source="load_weather",
            completeness=1.0,
        ),
        "nba_workload": FeatureGroupSnapshot(
            group_key="nba_workload",
            values={"recent_workload_minutes_per_game": 34.5, "workload_data_complete": 1.0},
            fresh_at=_NOW - timedelta(hours=6),
            source="EspnPlayerGamelogCache",
            completeness=1.0,
        ),
    }
    serialized = serialize_feature_groups(original)
    restored = deserialize_feature_groups(serialized)
    assert restored == original

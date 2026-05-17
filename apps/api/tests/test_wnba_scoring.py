"""Smarter WNBA PR 4 — scoring kernel WNBA branch tests.

PR 4 wires WNBA into ``_score_player_prop`` with a minimum-viable
branch that emits ``wnba_workload`` (mirror of nba_workload) and
nothing else. Long-tail / advanced / injury / referee groups are
deliberately skipped because the underlying data sources are
NBA-only today (documented in SMARTER_WNBA_PREP.md §4); follow-up
PRs generalize the loaders.

These tests pin the minimum-viable shape:

- WNBA player prop produces a recommendation with ``wnba_workload``
  populated and NBA-only groups absent.
- The ``wnba_props`` / ``wnba_singles`` heuristic profiles resolve.
- The ``wnba_props`` / ``wnba_singles`` family definitions exist and
  are study_track="active".
- ``single_family_key`` dispatches WNBA correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import (
    Event,
    EventParticipant,
    Market,
    MarketSnapshot,
    Participant,
)
from app.services.model_families import (
    FAMILY_DEFINITION_BY_KEY,
    single_family_key,
)
from app.services.scoring import (
    PropStatsResolver,
    ResolvedPropSubject,
    _score_player_prop,
)
from app.services.scoring.resolver import (
    SINGLE_HEURISTIC_PROFILES,
    _profile_for_single_family,
)


_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


class _FakeResolver(PropStatsResolver):
    def __init__(self, resolved: ResolvedPropSubject) -> None:
        self._resolved = resolved

    def resolve(self, sport_key: str, subject_name: str, team_hint: str | None = None) -> ResolvedPropSubject:
        return self._resolved


def _wnba_game_logs() -> list[dict[str, Any]]:
    # WNBA shares NBA's per-game raw_metrics shape (basketball box-score
    # stats) per PR 3's _build_game_logs WNBA dispatch.
    return [
        {
            "location": "home" if index % 2 == 0 else "away",
            "opponent": "Las Vegas Aces",
            "opponent_abbreviation": "LV",
            "raw_metrics": {
                "minutes": 33.0,
                "points": 24.0,
                "rebounds": 5.0,
                "assists": 7.0,
                "steals": 1.0,
                "blocks": 0.0,
                "turnovers": 3.0,
                "field_goals_attempted": 18.0,
            },
        }
        for index in range(10)
    ]


def _seed_wnba_event(db_session) -> tuple[Event, Market, MarketSnapshot]:
    home = Participant(external_id="ind-wnba", sport_key="WNBA", display_name="Indiana Fever", short_name="Fever", participant_type="team")
    away = Participant(external_id="nyl-wnba", sport_key="WNBA", display_name="New York Liberty", short_name="Liberty", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id="wnba-fg-prop",
        sport_key="WNBA",
        name="New York Liberty at Indiana Fever",
        status="scheduled",
        starts_at=datetime(2026, 5, 16, 23, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
    ])
    market = Market(
        ticker="KXWNBAPTS-26MAY16INDNYL-INDCCLARK22-22",
        sport_key="WNBA",
        event_id=event.id,
        title="Caitlin Clark: 22+ points",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 22.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Caitlin Clark",
            "copilot_subject_team": "IND",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.50, no_ask=0.55, last_price=0.51)
    db_session.add_all([market, snapshot])
    db_session.commit()
    return event, market, snapshot


# -- WNBA scoring branch ---------------------------------------------


def test_wnba_score_populates_wnba_workload_group(db_session) -> None:
    """The PR 4 WNBA branch in ``_score_player_prop`` emits the
    ``wnba_workload`` group from the resolver's gamelog. fresh_at
    threads through from ``ResolvedPropSubject.gamelog_cached_at`` so
    the PENALIZE policy can fire when gamelog data is stale.
    """
    event, market, snapshot = _seed_wnba_event(db_session)
    gamelog_fresh_at = _NOW - timedelta(hours=2)
    resolved = ResolvedPropSubject(
        sport_key="WNBA",
        athlete_id="4433403",
        display_name="Caitlin Clark",
        team_name="Indiana Fever",
        season=2026,
        game_logs=_wnba_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
        gamelog_cached_at=gamelog_fresh_at,
    )

    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))

    assert result is not None
    _prob, _confidence, _reasons, features, feature_groups = result

    assert "wnba_workload" in feature_groups
    workload = feature_groups["wnba_workload"]
    assert workload.fresh_at == gamelog_fresh_at
    assert workload.source == "EspnPlayerGamelogCache"
    # Derived-view consistency: workload keys also live in flat features.
    for key, value in workload.values.items():
        assert features[key] == value


def test_wnba_score_does_not_populate_nba_only_groups(db_session) -> None:
    """The PR 4 WNBA branch deliberately skips NBA-only groups
    (advanced, opponent_team, interaction, injury, hustle, drives,
    clutch, referee) because the underlying loaders are NBA-only.
    Pin that none of them leak into a WNBA score's feature_groups.

    When PR 6+ generalizes the loaders, this test will need updating
    — but the failure is the whole point: silently picking up an
    NBA-only group on a WNBA score would route the wrong data into
    the freshness layer.
    """
    event, market, snapshot = _seed_wnba_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="WNBA",
        athlete_id="4433403",
        display_name="Caitlin Clark",
        team_name="Indiana Fever",
        season=2026,
        game_logs=_wnba_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
    )

    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _, _, _, _, feature_groups = result

    nba_only_groups = {
        "nba_advanced", "nba_injury", "nba_workload",
        "nba_opponent_team", "nba_interaction",
        "nba_hustle", "nba_drives", "nba_clutch",
        "nba_referee",
    }
    leaked = nba_only_groups & set(feature_groups)
    assert not leaked, (
        f"WNBA scoring leaked NBA-only feature groups: {leaked}"
    )


def test_wnba_score_basketball_proxy_features_populated(db_session) -> None:
    """WNBA shares NBA's basketball stat surface (minutes, FGA,
    assists, turnovers), so the minute_factor / usage_factor /
    pace_factor proxies fire for WNBA via the same code path NBA
    uses. Pin that ``sport_key in {NBA, WNBA}`` correctly routes
    WNBA into the basketball proxy block (not the MLB else branch,
    which would silently produce plate_appearance_factor=0).
    """
    event, market, snapshot = _seed_wnba_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="WNBA",
        athlete_id="4433403",
        display_name="Caitlin Clark",
        team_name="Indiana Fever",
        season=2026,
        game_logs=_wnba_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
    )

    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _, _, _, features, _ = result

    # Basketball proxies fire — NOT MLB plate-appearance features.
    assert "recent_minutes" in features
    assert "season_minutes" in features
    assert "minute_factor" in features
    assert "usage_factor" in features
    assert "recent_plate_appearances" not in features
    assert "starter_era_factor" not in features


# -- registry pins ----------------------------------------------------


def test_single_family_key_dispatches_wnba_player_prop_to_wnba_props() -> None:
    assert single_family_key("WNBA", "player_prop") == "wnba_props"


def test_single_family_key_dispatches_wnba_non_prop_to_wnba_singles() -> None:
    assert single_family_key("WNBA", "winner") == "wnba_singles"
    assert single_family_key("WNBA", None) == "wnba_singles"


def test_family_definitions_include_wnba_props_and_singles() -> None:
    """PR 5 will add these to _DEFAULT_SERVE_FAMILY_KEYS so the
    weekly retrain workflow picks them up. PR 4 registers the
    definitions so the readiness panel + training pipeline can find
    them via FAMILY_DEFINITION_BY_KEY."""
    wnba_props = FAMILY_DEFINITION_BY_KEY.get("wnba_props")
    wnba_singles = FAMILY_DEFINITION_BY_KEY.get("wnba_singles")
    assert wnba_props is not None
    assert wnba_singles is not None
    assert wnba_props.sport_scope == "WNBA"
    assert wnba_singles.sport_scope == "WNBA"
    assert wnba_props.scope == "single"
    assert wnba_singles.scope == "single"
    assert wnba_props.study_track == "active"
    assert wnba_singles.study_track == "active"


def test_single_heuristic_profiles_include_wnba_props_and_singles() -> None:
    """Heuristic-path scoring keys off ``SINGLE_HEURISTIC_PROFILES``
    via ``_profile_for_single_family``. Without the WNBA entries the
    fallback hands back nba_singles' profile — wrong defaults for
    every WNBA recommendation."""
    assert "wnba_props" in SINGLE_HEURISTIC_PROFILES
    assert "wnba_singles" in SINGLE_HEURISTIC_PROFILES
    wnba_props = _profile_for_single_family("wnba_props")
    wnba_singles = _profile_for_single_family("wnba_singles")
    assert wnba_props.family_key == "wnba_props"
    assert wnba_singles.family_key == "wnba_singles"

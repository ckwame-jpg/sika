"""PR 3a — heuristic factor audit/refactor.

These tests pin the "advanced primary, proxies fallback only" rule from
PR3_HANDOFF.md. The scoring kernel emits advanced features first, then
runs the proxy block with a gate: when a real advanced replacement is
present in the features dict, the corresponding proxy must NOT multiply
``expected`` (otherwise we double-count the same concept).

Mapping:
- NBA usage: ``recent_usage_pct`` / ``season_usage_pct`` → skip ``usage_factor`` proxy
- NBA opp pace: ``opponent_pace_recent_5`` / ``opponent_pace_season`` → skip ``pace_factor`` proxy
- MLB starter: ``opposing_starter_xfip`` / ``opposing_starter_fip`` → skip ``starter_era_factor`` proxy

The gating writes ``*_proxy_superseded: True`` into features for downstream
debugging/observability, and pins ``usage_factor`` / ``pace_factor`` /
``starter_era_factor`` to ``1.0`` so the advanced factor is the only
multiplier seen for that concept.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.models import (
    Event,
    EventParticipant,
    Market,
    MarketSnapshot,
    Participant,
)
from app.services.scoring import (
    PropStatsResolver,
    ResolvedPropSubject,
    _score_player_prop,
)


# -----------------------------------------------------------------------------
# Fakes


class _FakeResolver(PropStatsResolver):
    """A PropStatsResolver subclass that returns a pre-baked
    ``ResolvedPropSubject`` and skips network/cache I/O.
    """

    def __init__(self, resolved: ResolvedPropSubject) -> None:
        self._resolved = resolved

    def resolve(self, sport_key: str, subject_name: str, team_hint: str | None = None) -> ResolvedPropSubject:
        return self._resolved


def _nba_game_logs(*, points: float = 30.0) -> list[dict[str, Any]]:
    """Ten plausible NBA player logs averaging ~30 points / 35 minutes."""
    return [
        {
            "location": "home" if index % 2 == 0 else "away",
            "opponent": "Boston Celtics" if index < 2 else "Miami Heat",
            "opponent_abbreviation": "BOS" if index < 2 else "MIA",
            "raw_metrics": {
                "minutes": 35.0,
                "points": points,
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


def _mlb_game_logs(*, hits: float = 1.5) -> list[dict[str, Any]]:
    return [
        {
            "location": "home" if index % 2 == 0 else "away",
            "opponent": "New York Mets",
            "opponent_abbreviation": "NYM",
            "raw_metrics": {
                "at_bats": 4.0,
                "walks": 0.5,
                "hit_by_pitch": 0.0,
                "hits": hits,
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


def _seed_nba_event(db_session, *, stat_key: str = "points", threshold: float = 30.0) -> tuple[Event, Market, MarketSnapshot]:
    home = Participant(external_id=f"nyk-{stat_key}", sport_key="NBA", display_name="New York Knicks", short_name="Knicks", participant_type="team")
    away = Participant(external_id=f"bos-{stat_key}", sport_key="NBA", display_name="Boston Celtics", short_name="Celtics", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id=f"nba-prop-fixture-{stat_key}",
        sport_key="NBA",
        name="Boston Celtics at New York Knicks",
        status="scheduled",
        starts_at=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )
    market = Market(
        ticker=f"KXNBA-{stat_key.upper()}-FIXTURE",
        sport_key="NBA",
        event_id=event.id,
        title=f"Jalen Brunson: {stat_key} prop",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": stat_key,
            "copilot_threshold": threshold,
            "copilot_direction": "over",
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.45, no_ask=0.60, last_price=0.46)
    db_session.add_all([market, snapshot])
    db_session.commit()
    return event, market, snapshot


def _seed_mlb_event(db_session, *, stat_key: str = "hits", threshold: float = 1.0) -> tuple[Event, Market, MarketSnapshot]:
    home = Participant(external_id=f"phi-{stat_key}", sport_key="MLB", display_name="Philadelphia Phillies", short_name="Phillies", participant_type="team")
    away = Participant(external_id=f"nym-{stat_key}", sport_key="MLB", display_name="New York Mets", short_name="Mets", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id=f"mlb-prop-fixture-{stat_key}",
        sport_key="MLB",
        name="New York Mets at Philadelphia Phillies",
        status="scheduled",
        starts_at=datetime(2026, 5, 15, 23, 5, tzinfo=timezone.utc),
        raw_data={"venue_id": "2681"},
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )
    market = Market(
        ticker=f"KXMLB-{stat_key.upper()}-FIXTURE",
        sport_key="MLB",
        event_id=event.id,
        title=f"Bryce Harper: {stat_key} prop",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": stat_key,
            "copilot_threshold": threshold,
            "copilot_direction": "over",
            "copilot_subject_name": "Bryce Harper",
            "copilot_subject_team": "PHI",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.55, no_ask=0.50, last_price=0.55)
    db_session.add_all([market, snapshot])
    db_session.commit()
    return event, market, snapshot


# -----------------------------------------------------------------------------
# NBA gating tests


def test_nba_usage_proxy_fires_when_no_advanced_data(db_session):
    """Without advanced USG%, the box-score usage proxy should fire and
    write a usage_factor != 1.0 (since the synthetic logs are uniform,
    short_term vs season usage matches and the factor lands at exactly
    1.0 — but ``usage_factor_proxy_superseded`` must be absent)."""
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
    _prob, _confidence, _reasons, features = result
    # Proxy is computed and stored even when the value lands at 1.0.
    assert features["usage_factor"] == 1.0
    # Crucial: the gate must NOT mark the proxy as superseded — there is
    # no advanced replacement to defer to.
    assert "usage_factor_proxy_superseded" not in features


def test_nba_usage_proxy_skipped_when_advanced_usage_present(db_session):
    """When advanced USG% (recent + season) is emitted into features, the
    proxy must NOT multiply expected. We verify this by asserting:
    1. ``usage_factor`` is pinned to 1.0
    2. ``usage_factor_proxy_superseded`` is True
    3. The advanced ``usage_factor_advanced`` shows up in advanced_factors
    """
    event, market, snapshot = _seed_nba_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="3934672",
        display_name="Jalen Brunson",
        team_name="New York Knicks",
        season=2026,
        game_logs=_nba_game_logs(),
        advanced_payload={
            "season_avg": {"usg_pct": 0.28, "ts_pct": 0.60},
            "recent_10_avg": {"usg_pct": 0.32, "ts_pct": 0.62},
        },
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    assert features["usage_factor"] == 1.0
    assert features["usage_factor_proxy_superseded"] is True
    # Advanced USG% must have been emitted into features.
    assert features["recent_usage_pct"] == pytest.approx(0.32)
    assert features["season_usage_pct"] == pytest.approx(0.28)
    # And the advanced factor (USG ratio) appears in advanced_factors.
    advanced_factors = features.get("advanced_factors") or {}
    assert "usage_factor_advanced" in advanced_factors


def test_nba_pace_proxy_falls_back_when_no_advanced_pace(db_session):
    """Without advanced opponent pace, the score-based pace proxy may run
    (depending on DB results) but the supersede flag must be absent."""
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
    _prob, _confidence, _reasons, features = result
    assert "pace_factor_proxy_superseded" not in features


def test_nba_pace_proxy_skipped_when_advanced_opp_pace_present(db_session, monkeypatch):
    """When emit_nba_opponent_team_features writes ``opponent_pace_recent_5``
    into the features dict, the score-based pace proxy must NOT multiply
    expected. Pin pace_factor to 1.0 and set the supersede flag."""
    from app.services import advanced_stats

    event, market, snapshot = _seed_nba_event(db_session)
    fake_team_payload = {
        "season_avg": {"off_rating": 116.0, "def_rating": 110.0, "pace": 99.0},
        "recent_5_avg": {"off_rating": 118.0, "def_rating": 105.0, "pace": 102.0},
    }
    monkeypatch.setattr(advanced_stats, "find_nba_team_id_by_name", lambda *args, **kwargs: "1610612738")
    monkeypatch.setattr(
        advanced_stats,
        "load_nba_team_gamelog",
        lambda *args, **kwargs: type("R", (), {"payload": fake_team_payload, "cache_status": "hit"})(),
    )
    resolved = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="3934672",
        display_name="Jalen Brunson",
        team_name="New York Knicks",
        season=2026,
        game_logs=_nba_game_logs(),
        advanced_payload={},
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # Advanced opponent pace must have been emitted.
    assert features["opponent_pace_recent_5"] == pytest.approx(102.0)
    # Gate must skip the box-score pace proxy.
    assert features["pace_factor"] == 1.0
    assert features["pace_factor_proxy_superseded"] is True
    # And the advanced pace factor appears in advanced_factors.
    advanced_factors = features.get("advanced_factors") or {}
    assert "pace_factor_advanced" in advanced_factors


# -----------------------------------------------------------------------------
# MLB gating tests


def test_mlb_starter_proxy_fires_when_no_advanced_data(db_session):
    """Without advanced xFIP/FIP, the ERA proxy is the only starter signal
    and ``starter_era_factor_proxy_superseded`` must be absent."""
    event, market, snapshot = _seed_mlb_event(db_session)
    resolved = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="3408",
        display_name="Bryce Harper",
        team_name="Philadelphia Phillies",
        season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={},
        advanced_cache_status="miss",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # ERA proxy is None-valued because the event has no probables wiring,
    # so era_factor stays at 1.0. The supersede flag must be absent.
    assert features["starter_era_factor"] == 1.0
    assert "starter_era_factor_proxy_superseded" not in features


def test_mlb_starter_proxy_skipped_when_advanced_xfip_present(db_session, monkeypatch):
    """When ``opposing_starter_xfip`` is in features, the ERA proxy must
    not multiply expected. We monkeypatch ``_probable_pitcher_era`` to
    return a non-None ERA so the unguarded path WOULD have applied a
    factor, then verify the gate suppresses it."""
    from app.services import scoring as scoring_module

    event, market, snapshot = _seed_mlb_event(db_session)
    # Force a probable pitcher ERA so the proxy WOULD apply if the gate
    # were missing. We pick 5.50 to push era_factor above 1.0 (5.5 - 4.0
    # = 1.5; 1.5 * 0.03 = 0.045 → 1.045).
    monkeypatch.setattr(scoring_module, "_probable_pitcher_era", lambda *args, **kwargs: 5.50)
    # Force advanced xFIP to be emitted: emit_mlb_pitcher_features keys
    # off (payload, statcast). We monkeypatch the load functions to return
    # canned payloads, and monkeypatch resolve_mlb_stats_player_id to
    # return a non-empty ID so the load path runs.
    from app.services import mlb_advanced

    fake_pitcher_payload = type("R", (), {"payload": {"season_avg": {"xfip": 4.40, "k_per_9": 9.0}}, "cache_status": "hit"})()
    fake_statcast_payload = type("R", (), {"payload": {"season_avg": {"csw_pct": 0.30, "whiff_pct": 0.25}}, "cache_status": "hit"})()
    monkeypatch.setattr(mlb_advanced, "resolve_mlb_stats_player_id", lambda *args, **kwargs: "543037")
    monkeypatch.setattr(mlb_advanced, "load_mlb_pitcher_advanced", lambda *args, **kwargs: fake_pitcher_payload)
    monkeypatch.setattr(mlb_advanced, "load_mlb_statcast_pitcher", lambda *args, **kwargs: fake_statcast_payload)
    # Also need a probable starter NAME so the starter block executes.
    monkeypatch.setattr(
        scoring_module,
        "_probable_pitcher_identity",
        lambda *args, **kwargs: ("Kodai Senga", "12345"),
    )

    resolved = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="3408",
        display_name="Bryce Harper",
        team_name="Philadelphia Phillies",
        season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={},
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # Advanced xFIP must be in features.
    assert features["opposing_starter_xfip"] == pytest.approx(4.40)
    # Gate must skip the ERA proxy.
    assert features["starter_era_factor"] == 1.0
    assert features["starter_era_factor_proxy_superseded"] is True
    # And the advanced starter factor (xFIP-based) appears in advanced_factors.
    advanced_factors = features.get("advanced_factors") or {}
    assert "starter_factor_advanced" in advanced_factors


def test_mlb_pa_factor_unaffected_by_advanced_starter_data(db_session, monkeypatch):
    """The plate-appearance volume proxy has no advanced replacement, so
    even when xFIP exists the PA factor must continue to apply."""
    from app.services import scoring as scoring_module
    from app.services import mlb_advanced

    event, market, snapshot = _seed_mlb_event(db_session)
    monkeypatch.setattr(scoring_module, "_probable_pitcher_era", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        scoring_module,
        "_probable_pitcher_identity",
        lambda *args, **kwargs: ("Kodai Senga", "12345"),
    )
    fake_pitcher_payload = type("R", (), {"payload": {"season_avg": {"xfip": 4.40}}, "cache_status": "hit"})()
    fake_statcast_payload = type("R", (), {"payload": {"season_avg": {}}, "cache_status": "hit"})()
    monkeypatch.setattr(mlb_advanced, "resolve_mlb_stats_player_id", lambda *args, **kwargs: "543037")
    monkeypatch.setattr(mlb_advanced, "load_mlb_pitcher_advanced", lambda *args, **kwargs: fake_pitcher_payload)
    monkeypatch.setattr(mlb_advanced, "load_mlb_statcast_pitcher", lambda *args, **kwargs: fake_statcast_payload)

    resolved = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="3408",
        display_name="Bryce Harper",
        team_name="Philadelphia Phillies",
        season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={},
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # PA proxy still ran (uniform synthetic logs → pa_factor at exactly 1.0).
    assert "plate_appearance_factor" in features
    # And advanced data did make it into features.
    assert features["opposing_starter_xfip"] == pytest.approx(4.40)


# -----------------------------------------------------------------------------
# Per-stat gating regressions (Codex round-1 ultrareview, merged_bug_001)
#
# Pre-fix: the proxy gates suppressed usage_factor / pace_factor / era_factor
# whenever the SOURCE advanced feature (recent_usage_pct / opponent_pace_recent_5
# / opposing_starter_xfip) was present in features, but compute_advanced_factors
# only emits the replacement when it's listed in the per-stat tuple. For stats
# that didn't list the replacement, BOTH dropped out — strictly worse than pre-PR.
#
# These tests pin the corrected behaviour: when the replacement isn't wired
# for a stat_key, the proxy must continue to apply.


def test_nba_rebounds_keeps_usage_proxy_when_replacement_not_wired(db_session, monkeypatch):
    """``rebounds`` gating tuple does NOT include ``usage_factor_advanced`` —
    the proxy must continue to fire even when advanced USG% is cached."""
    from app.services import advanced_stats

    event, market, snapshot = _seed_nba_event(db_session, stat_key="rebounds", threshold=8.0)
    fake_team_payload = {
        "season_avg": {"off_rating": 116.0, "def_rating": 110.0, "pace": 99.0},
        "recent_5_avg": {"off_rating": 118.0, "def_rating": 105.0, "pace": 102.0},
    }
    monkeypatch.setattr(advanced_stats, "find_nba_team_id_by_name", lambda *a, **kw: "1610612738")
    monkeypatch.setattr(
        advanced_stats,
        "load_nba_team_gamelog",
        lambda *a, **kw: type("R", (), {"payload": fake_team_payload, "cache_status": "hit"})(),
    )
    resolved = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="3934672",
        display_name="Jalen Brunson",
        team_name="New York Knicks",
        season=2026,
        game_logs=_nba_game_logs(),
        advanced_payload={
            "season_avg": {"usg_pct": 0.28, "ts_pct": 0.60},
            "recent_10_avg": {"usg_pct": 0.32, "ts_pct": 0.62},
        },
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # USG% data IS in features...
    assert features["recent_usage_pct"] == pytest.approx(0.32)
    # ...but rebounds doesn't wire usage_factor_advanced, so the proxy must
    # NOT have been suppressed.
    assert "usage_factor_proxy_superseded" not in features
    # Same for pace: rebounds DOES include pace_factor_advanced, so the
    # proxy IS superseded for pace specifically.
    assert features.get("pace_factor_proxy_superseded") is True


def test_nba_turnovers_keeps_pace_proxy_when_replacement_not_wired(db_session, monkeypatch):
    """``turnovers`` gating tuple has only ``usage_factor_advanced`` — pace
    is NOT in it. With opponent_pace_recent_5 cached, the pace proxy must
    continue to apply."""
    from app.services import advanced_stats

    event, market, snapshot = _seed_nba_event(db_session, stat_key="turnovers", threshold=2.5)
    fake_team_payload = {
        "season_avg": {"off_rating": 116.0, "def_rating": 110.0, "pace": 99.0},
        "recent_5_avg": {"off_rating": 118.0, "def_rating": 105.0, "pace": 102.0},
    }
    monkeypatch.setattr(advanced_stats, "find_nba_team_id_by_name", lambda *a, **kw: "1610612738")
    monkeypatch.setattr(
        advanced_stats,
        "load_nba_team_gamelog",
        lambda *a, **kw: type("R", (), {"payload": fake_team_payload, "cache_status": "hit"})(),
    )
    resolved = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="3934672",
        display_name="Jalen Brunson",
        team_name="New York Knicks",
        season=2026,
        game_logs=_nba_game_logs(),
        advanced_payload={"season_avg": {"usg_pct": 0.28}, "recent_10_avg": {"usg_pct": 0.32}},
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # Pace IS in features...
    assert features["opponent_pace_recent_5"] == pytest.approx(102.0)
    # ...but turnovers doesn't wire pace_factor_advanced — proxy stays.
    assert "pace_factor_proxy_superseded" not in features
    # USG% IS wired for turnovers, so usage proxy IS superseded.
    assert features.get("usage_factor_proxy_superseded") is True


def test_nba_made_threes_alias_routes_to_three_points_made_gating(db_session, monkeypatch):
    """``made_threes`` is the canonical key from market_support; the gating
    tables historically only listed ``three_points_made``. Both must map to
    the same tuple so the advanced replacement actually fires."""
    from app.services import advanced_stats

    event, market, snapshot = _seed_nba_event(db_session, stat_key="made_threes", threshold=3.0)
    fake_team_payload = {
        "season_avg": {"off_rating": 116.0, "def_rating": 110.0, "pace": 99.0},
        "recent_5_avg": {"off_rating": 118.0, "def_rating": 105.0, "pace": 102.0},
    }
    monkeypatch.setattr(advanced_stats, "find_nba_team_id_by_name", lambda *a, **kw: "1610612738")
    monkeypatch.setattr(
        advanced_stats,
        "load_nba_team_gamelog",
        lambda *a, **kw: type("R", (), {"payload": fake_team_payload, "cache_status": "hit"})(),
    )
    resolved = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="3934672",
        display_name="Jalen Brunson",
        team_name="New York Knicks",
        season=2026,
        game_logs=_nba_game_logs(),
        advanced_payload={
            "season_avg": {"usg_pct": 0.28, "ts_pct": 0.60},
            "recent_10_avg": {"usg_pct": 0.32, "ts_pct": 0.62},
        },
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # Both proxies should be superseded (made_threes wires both replacements)...
    assert features.get("usage_factor_proxy_superseded") is True
    assert features.get("pace_factor_proxy_superseded") is True
    # ...and BOTH advanced factors must actually appear in advanced_factors.
    advanced_factors = features.get("advanced_factors") or {}
    assert "usage_factor_advanced" in advanced_factors
    assert "pace_factor_advanced" in advanced_factors


def test_mlb_runs_keeps_starter_era_proxy_when_replacement_not_wired(db_session, monkeypatch):
    """``runs`` gating tuple has only ``lineup_factor`` and
    ``park_factor_runs_mult`` — opposing-pitcher quality (
    ``starter_factor_advanced``) is NOT in it. With xFIP cached and a
    probable ERA, the ERA proxy must continue to multiply expected."""
    from app.services import scoring as scoring_module
    from app.services import mlb_advanced

    event, market, snapshot = _seed_mlb_event(db_session, stat_key="runs", threshold=1.0)
    monkeypatch.setattr(scoring_module, "_probable_pitcher_era", lambda *a, **kw: 5.50)
    monkeypatch.setattr(
        scoring_module,
        "_probable_pitcher_identity",
        lambda *a, **kw: ("Kodai Senga", "12345"),
    )
    fake_pitcher_payload = type("R", (), {"payload": {"season_avg": {"xfip": 4.40}}, "cache_status": "hit"})()
    fake_statcast_payload = type("R", (), {"payload": {"season_avg": {}}, "cache_status": "hit"})()
    monkeypatch.setattr(mlb_advanced, "resolve_mlb_stats_player_id", lambda *a, **kw: "543037")
    monkeypatch.setattr(mlb_advanced, "load_mlb_pitcher_advanced", lambda *a, **kw: fake_pitcher_payload)
    monkeypatch.setattr(mlb_advanced, "load_mlb_statcast_pitcher", lambda *a, **kw: fake_statcast_payload)
    resolved = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="3408",
        display_name="Bryce Harper",
        team_name="Philadelphia Phillies",
        season=2026,
        game_logs=_mlb_game_logs(),
        advanced_payload={},
        advanced_cache_status="hit",
    )
    result = _score_player_prop(db_session, event, market, snapshot, _FakeResolver(resolved))
    assert result is not None
    _prob, _confidence, _reasons, features = result
    # xFIP IS in features...
    assert features["opposing_starter_xfip"] == pytest.approx(4.40)
    # ...but runs doesn't wire starter_factor_advanced — proxy stays.
    assert "starter_era_factor_proxy_superseded" not in features
    # The ERA proxy actually fired (5.50 vs 4.00 baseline → factor > 1.0).
    assert features["starter_era_factor"] > 1.0


def test_factor_applies_helper_returns_correct_per_stat_decisions():
    """Direct unit coverage of the helper that powers the gates."""
    from app.services.heuristic_factors import factor_applies

    # Wired:
    assert factor_applies("NBA", "points", "usage_factor_advanced") is True
    assert factor_applies("NBA", "made_threes", "usage_factor_advanced") is True
    assert factor_applies("MLB", "hits", "starter_factor_advanced") is True

    # Not wired (the regression cases):
    assert factor_applies("NBA", "rebounds", "usage_factor_advanced") is False
    assert factor_applies("NBA", "turnovers", "pace_factor_advanced") is False
    assert factor_applies("MLB", "runs", "starter_factor_advanced") is False
    assert factor_applies("MLB", "walks", "starter_factor_advanced") is False

    # Unknown sport / stat:
    assert factor_applies("NFL", "passing_yards", "usage_factor_advanced") is False
    assert factor_applies("NBA", "made_up_stat", "usage_factor_advanced") is False

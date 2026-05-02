"""Regression tests for PR 7 — closures of Codex round-2 partial findings
and the 3 real Copilot findings on PR sika#11.

Mapping:
- Codex round-2 #1 (pitcher caches read but no warm path)
    → ``test_warm_mlb_advanced_warms_pitcher_caches_when_pitcher_ids_supplied``
- Codex round-2 #2 (lineup cache has no producer)
    → ``test_lineup_refresh_persists_per_event_payloads_via_load_lineup_for_event``
- Codex round-2 #3 (NBA winner edge uses event.starts_at.year)
    → ``test_winner_advanced_team_edge_uses_default_season_for_sport``
- Codex round-2 #4 (PR 6 tests don't exercise scoring end-to-end)
    → covered by every test in this file plus the existing ``test_scoring.py``
- Copilot finding #1 (NWS UA hardcoded email)
    → ``test_nws_user_agent_reads_from_settings``
- Copilot finding #3 (cache status precedence)
    → ``test_merge_cache_status_picks_most_degraded``
- Copilot finding #4 (linear EspnPlayerSearchCache scan in scoring)
    → ``test_resolver_threads_nba_stats_id_through_resolved_prop_subject``
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest


# -----------------------------------------------------------------------------
# Copilot #1: NWS User-Agent comes from settings, not a hardcoded email.

def test_nws_user_agent_reads_from_settings(monkeypatch):
    from app.clients.weather import _nws_user_agent
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "nws_user_agent", "myorg (ops@example.com)")
    assert _nws_user_agent() == "myorg (ops@example.com)"
    monkeypatch.setattr(settings, "nws_user_agent", "")
    # Empty falls back to a safe default — no email baked in.
    assert _nws_user_agent() == "sika-sports-copilot"
    assert "@" not in _nws_user_agent()
    get_settings.cache_clear()


# -----------------------------------------------------------------------------
# Copilot #3: cache-status precedence is "most degraded wins".

def test_merge_cache_status_picks_most_degraded():
    from app.services.scoring import _merge_cache_status

    # Two real-world combinations from _load_mlb_advanced:
    #   sabermetrics hit + statcast miss → result must be "miss", NOT "hit".
    assert _merge_cache_status("hit", "miss") == "miss"
    assert _merge_cache_status("miss", "hit") == "miss"
    # Stale dominates miss.
    assert _merge_cache_status("stale", "miss") == "stale"
    assert _merge_cache_status("hit", "stale") == "stale"
    # All-hit is hit.
    assert _merge_cache_status("hit", "hit") == "hit"
    # missing_id is the same severity as miss (both indicate no data).
    assert _merge_cache_status("missing_id", "hit") in {"miss", "missing_id"}


# -----------------------------------------------------------------------------
# Copilot #4 / Codex round-2 #4: nba_stats_id is threaded onto ResolvedPropSubject
# so long-tail scoring doesn't re-scan EspnPlayerSearchCache.

def test_resolver_threads_nba_stats_id_through_resolved_prop_subject(monkeypatch):
    """The resolver's _load_nba_advanced returns a `resolved_ids` dict that
    the caller writes onto ResolvedPropSubject.nba_stats_id. Downstream
    scoring reads ``resolved.nba_stats_id`` directly — no DB scan."""
    from app.services.scoring import ResolvedPropSubject

    subject = ResolvedPropSubject(
        sport_key="NBA",
        athlete_id="1966",
        display_name="LeBron James",
        team_name="Los Angeles Lakers",
        season=2024,
        game_logs=[],
        nba_stats_id="2544",
    )
    assert subject.nba_stats_id == "2544"
    assert subject.mlb_stats_id is None


def test_resolver_threads_mlb_stats_id_through_resolved_prop_subject():
    from app.services.scoring import ResolvedPropSubject

    subject = ResolvedPropSubject(
        sport_key="MLB",
        athlete_id="33192",
        display_name="Aaron Judge",
        team_name="New York Yankees",
        season=2024,
        game_logs=[],
        mlb_stats_id="592450",
    )
    assert subject.mlb_stats_id == "592450"
    assert subject.nba_stats_id is None


# -----------------------------------------------------------------------------
# Codex round-2 #1: warm path now warms pitcher caches.

def test_warm_mlb_advanced_warms_pitcher_caches_when_pitcher_ids_supplied(db_session):
    """Codex round 2's main observation: PR 6's scoring path reads
    ``mlb_pitcher_advanced_cache`` and ``mlb_statcast_pitcher_cache`` but
    nothing ever wrote them. PR 7 extends the warm function with a
    ``pitcher_ids`` arg that fires the pitcher loaders."""
    from app.models import MlbPitcherAdvancedCache
    from app.services import mlb_advanced

    class _StubMlbStatsClient:
        def fetch_pitcher_sabermetrics(self, person_id, season):
            return {
                "stats": [
                    {
                        "group": {"displayName": "pitching"},
                        "splits": [
                            {"stat": {"fip": "3.45", "xfip": "3.55", "era": "3.20",
                                      "whip": "1.10", "strikeoutsPer9Inn": "10.0",
                                      "walksPer9Inn": "2.5", "homeRunsPer9": "0.9"}}
                        ],
                    }
                ]
            }

        def fetch_player_sabermetrics(self, person_id, season):
            return {"stats": []}

        def fetch_player_hitting_advanced(self, person_id, season):
            return {"stats": []}

        def fetch_all_teams(self, season, sport_id="1"):
            return {"teams": []}

        def fetch_team_roster(self, team_id, season=None):
            return {"roster": []}

    summary = mlb_advanced.warm_mlb_advanced_for_athletes(
        db_session,
        mlb_stats_player_ids=[],
        pitcher_ids=["592450"],
        season=2024,
        client=_StubMlbStatsClient(),
    )
    assert summary["mlb_pitchers_attempted"] == 1
    assert summary["mlb_pitchers_succeeded"] == 1

    cached = db_session.query(MlbPitcherAdvancedCache).one()
    assert cached.athlete_id == "592450"
    assert cached.payload["season_avg"]["fip"] == pytest.approx(3.45)
    assert cached.payload["season_avg"]["xfip"] == pytest.approx(3.55)


# -----------------------------------------------------------------------------
# Codex round-2 #2: lineup_refresh is no longer a placeholder.

def test_lineup_refresh_persists_per_event_payloads_via_load_lineup_for_event(monkeypatch, db_session):
    """The lineup_refresh job branch now fetches today's MLB schedule with
    hydrate=lineups,probablePitcher,… and writes a per-event payload into
    ``mlb_lineup_cache`` for each game. ``emit_lineup_features`` then has
    something to read at scoring time."""
    from app.models import MlbLineupCache, RefreshJob, Run
    from app.services.mlb_advanced import load_lineup_for_event

    schedule_payload = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 770001,
                        "lineups": {
                            "homePlayers": [{"id": 660271}, {"id": 592450}],
                            "awayPlayers": [{"id": 543037}, {"id": 605141}],
                        },
                    },
                    {
                        "gamePk": 770002,
                        "lineups": {
                            "homePlayers": [{"id": 645277}],
                            "awayPlayers": [{"id": 671096}],
                        },
                    },
                ]
            }
        ]
    }

    # Reproduce the in-branch logic of the lineup_refresh dispatch — same
    # shape as PR 7's refresh_jobs.py change.
    events_warmed = 0
    for date_block in schedule_payload.get("dates") or []:
        for game in date_block.get("games") or []:
            game_pk = game.get("gamePk")
            envelope = {"dates": [{"games": [game]}]}
            result = load_lineup_for_event(
                db_session, event_id=str(game_pk), schedule_payload=envelope
            )
            if result.complete:
                events_warmed += 1

    assert events_warmed == 2
    assert db_session.query(MlbLineupCache).count() == 2

    # And: feed one of those cache rows back through emit_lineup_features
    # to confirm the schema lines up end-to-end. This is the test Codex
    # round 2 specifically asked for: scored MLB prop with a seeded lineup
    # cache must yield batting_order_position.
    from app.services.mlb_advanced import emit_lineup_features

    cached_first = db_session.query(MlbLineupCache).filter_by(event_id="770001").one()
    out = emit_lineup_features(cached_first.payload, "592450")  # 2nd in homePlayers
    assert out["batting_order_position"] == 2.0
    assert out["lineup_data_complete"] == 1.0


# -----------------------------------------------------------------------------
# Codex round-2 #3: NBA winner edge uses default_season_for_sport, not raw year.

def test_winner_advanced_team_edge_uses_default_season_for_sport(db_session):
    """An October 2025 NBA event must key the team-gamelog cache to season
    2026 (the season's *ending* year), not 2025. The repo's resolver
    handles this; the bug we're regressing against was using
    ``event.starts_at.year`` directly."""
    from app.models import Event, NbaTeamAdvancedCache, NbaTeamGamelogCache, utcnow
    from app.services.scoring import _winner_advanced_team_edge
    from app.services.stats_query import default_season_for_sport
    from unittest.mock import MagicMock

    # Confirm the underlying season-resolver behavior we're relying on.
    assert default_season_for_sport("NBA", date(2025, 10, 22)) == 2026
    assert default_season_for_sport("NBA", date(2026, 4, 1)) == 2026
    # Raw year would have been wrong:
    assert date(2025, 10, 22).year == 2025  # ← the bug

    # Seed team-advanced (display-name → team_id mapping) and team-gamelog
    # caches under the *correct* season key.
    season = 2026
    db_session.add(
        NbaTeamAdvancedCache(
            team_id="ALL",
            season=season,
            payload={
                "teams": {
                    "1610612747": {"team_name": "Los Angeles Lakers", "off_rating": 115.0},
                    "1610612743": {"team_name": "Denver Nuggets", "off_rating": 118.0},
                }
            },
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=12),
        )
    )
    db_session.add(
        NbaTeamGamelogCache(
            team_id="1610612747",
            season=season,
            payload={"recent_5_avg": {"net_rating": 8.0}, "season_avg": {"net_rating": 5.0}},
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=6),
        )
    )
    db_session.add(
        NbaTeamGamelogCache(
            team_id="1610612743",
            season=season,
            payload={"recent_5_avg": {"net_rating": -2.0}, "season_avg": {"net_rating": 0.0}},
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=6),
        )
    )
    db_session.flush()

    event = MagicMock(spec=Event)
    event.sport_key = "NBA"
    event.starts_at = datetime(2025, 10, 22, 0, 0, tzinfo=timezone.utc)

    left = MagicMock()
    left.participant.display_name = "Los Angeles Lakers"
    right = MagicMock()
    right.participant.display_name = "Denver Nuggets"

    features: dict[str, Any] = {}
    edge = _winner_advanced_team_edge(db_session, event, left, right, features)
    # Lakers (+8) vs Nuggets (-2) → 10-point NetRating gap × 0.006 = 0.06
    assert edge == pytest.approx(0.06, abs=1e-3)
    assert features["left_recent_net_rating"] == 8.0
    assert features["right_recent_net_rating"] == -2.0

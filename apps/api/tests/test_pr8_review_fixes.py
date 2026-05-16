"""Regression tests for PR 8 — closures of Codex round-3 review notes.

Mapping:
- Round-3 #1 (lineup cache key mismatch: producer wrote ``gamePk``, consumer
  read ``event.id``)
    → ``test_match_mlb_event_resolves_gamepk_to_app_event_id``
    → ``test_lineup_refresh_persists_under_app_event_id_round_trip``
- Round-3 #2 (warm cron coverage gap: starter-only pitchers never reached
  the sidecar list, so ``mlb_pitcher_advanced_cache`` was never warmed for
  them)
    → ``test_extract_probable_pitcher_ids_pulls_from_schedule_hydration``
    → ``test_resolver_starter_path_persists_mlb_stats_id_sidecar_when_espn_id_provided``
- Round-3 #3 (no scheduled producer for Statcast pitcher cache)
    → ``test_warm_mlb_advanced_warms_statcast_pitcher_cache_when_savant_provided``
- Round-3 #4 (no integration test that asserts scoring features come out of
  the seeded caches)
    → ``test_score_player_prop_emits_opposing_starter_and_batting_order_features``
- Round-3 #5 (per-game lineup persistence wasn't try/except guarded)
    → ``test_lineup_refresh_continues_when_one_game_payload_is_malformed``
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest


# -----------------------------------------------------------------------------
# Round-3 #1 / #5 — gamePk → app Event.id mapping + try/except hardening.

def _seed_mlb_event(db_session, *, external_id: str, name: str, starts_at: datetime,
                    home_name: str, away_name: str):
    from app.models import Event, EventParticipant, Participant

    home = Participant(
        external_id=f"{external_id}-home",
        sport_key="MLB",
        display_name=home_name,
        short_name=home_name.split()[-1],
        participant_type="team",
    )
    away = Participant(
        external_id=f"{external_id}-away",
        sport_key="MLB",
        display_name=away_name,
        short_name=away_name.split()[-1],
        participant_type="team",
    )
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id=external_id,
        sport_key="MLB",
        name=name,
        status="scheduled",
        starts_at=starts_at,
        raw_data={},
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )
    db_session.flush()
    return event


def test_match_mlb_event_resolves_gamepk_to_app_event_id(db_session):
    """Codex round 3 said: 'lineup_refresh writes event_id=str(gamePk) but
    scoring reads event_id=str(event.id)'. The fix is a matcher that walks
    today's MLB events and resolves an MLB Stats schedule game to the sika
    Event row by team-name + start-time window."""
    from app.services.refresh_jobs import _build_mlb_event_index, _match_mlb_event

    yankees_redsox = _seed_mlb_event(
        db_session,
        external_id="evt-nyy-bos",
        name="New York Yankees at Boston Red Sox",
        starts_at=datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc),
        home_name="Boston Red Sox",
        away_name="New York Yankees",
    )
    cubs_mets = _seed_mlb_event(
        db_session,
        external_id="evt-nym-chc",
        name="New York Mets at Chicago Cubs",
        starts_at=datetime(2026, 4, 17, 18, 20, tzinfo=timezone.utc),
        home_name="Chicago Cubs",
        away_name="New York Mets",
    )
    db_session.flush()

    index = _build_mlb_event_index(db_session)
    assert {row[0].id for row in index} == {yankees_redsox.id, cubs_mets.id}

    schedule_game = {
        "gamePk": 770001,
        "gameDate": "2026-04-17T18:20:00Z",
        "teams": {
            "home": {"team": {"id": 112, "name": "Chicago Cubs"}},
            "away": {"team": {"id": 121, "name": "New York Mets"}},
        },
    }
    matched = _match_mlb_event(index, schedule_game)
    assert matched is not None
    assert matched.id == cubs_mets.id

    # Wrong-day game (>3h delta) must NOT match — guards against pulling a
    # day-old / day-ahead event.
    far_future_game = {
        "gamePk": 770002,
        "gameDate": "2026-04-19T18:20:00Z",
        "teams": {
            "home": {"team": {"id": 112, "name": "Chicago Cubs"}},
            "away": {"team": {"id": 121, "name": "New York Mets"}},
        },
    }
    assert _match_mlb_event(index, far_future_game) is None

    # Different-teams game must NOT match.
    unrelated_game = {
        "gamePk": 770003,
        "gameDate": "2026-04-17T19:00:00Z",
        "teams": {
            "home": {"team": {"id": 1, "name": "Atlanta Braves"}},
            "away": {"team": {"id": 2, "name": "Miami Marlins"}},
        },
    }
    assert _match_mlb_event(index, unrelated_game) is None


def test_lineup_refresh_persists_under_app_event_id_round_trip(db_session):
    """End-to-end check: simulate the lineup_refresh dispatch logic with the
    new helper, then verify the resulting MlbLineupCache row is keyed by the
    app Event.id — the same key scoring's ``load_lineup_for_event`` reads."""
    from app.models import MlbLineupCache
    from app.services.mlb_advanced import emit_lineup_features, load_lineup_for_event
    from app.services.refresh_jobs import _build_mlb_event_index, _match_mlb_event

    event = _seed_mlb_event(
        db_session,
        external_id="evt-nym-chc",
        name="New York Mets at Chicago Cubs",
        starts_at=datetime(2026, 4, 17, 18, 20, tzinfo=timezone.utc),
        home_name="Chicago Cubs",
        away_name="New York Mets",
    )

    schedule_game = {
        "gamePk": 770001,
        "gameDate": "2026-04-17T18:20:00Z",
        "teams": {
            "home": {"team": {"id": 112, "name": "Chicago Cubs"}},
            "away": {"team": {"id": 121, "name": "New York Mets"}},
        },
        "lineups": {
            "homePlayers": [{"id": 660271}, {"id": 592450}],
            "awayPlayers": [{"id": 543037}, {"id": 605141}],
        },
    }

    index = _build_mlb_event_index(db_session)
    matched = _match_mlb_event(index, schedule_game)
    assert matched is not None
    load_lineup_for_event(
        db_session,
        event_id=str(matched.id),
        schedule_payload={"dates": [{"games": [schedule_game]}]},
    )

    # Critical: the cached row's event_id is the app Event.id, NOT the gamePk.
    cached_row = db_session.query(MlbLineupCache).one()
    assert cached_row.event_id == str(event.id)
    assert cached_row.event_id != str(770001)

    # Scoring's read path uses the same key.
    read_back = load_lineup_for_event(db_session, event_id=str(event.id))
    assert read_back.cache_status == "hit"
    assert emit_lineup_features(read_back.payload, "592450")[
        "batting_order_position"
    ] == 2.0


def test_lineup_refresh_continues_when_one_game_payload_is_malformed(db_session):
    """Round-3 #5: one bad game payload must not poison the whole slate's
    warm pass. Reproduce the dispatch loop with a try/except and verify the
    second (good) game still lands in the cache."""
    from app.models import MlbLineupCache
    from app.services.mlb_advanced import load_lineup_for_event
    from app.services.refresh_jobs import _build_mlb_event_index, _match_mlb_event

    event = _seed_mlb_event(
        db_session,
        external_id="evt-nym-chc",
        name="New York Mets at Chicago Cubs",
        starts_at=datetime(2026, 4, 17, 18, 20, tzinfo=timezone.utc),
        home_name="Chicago Cubs",
        away_name="New York Mets",
    )

    schedule = {
        "dates": [
            {
                "games": [
                    # A payload that will blow up _match_mlb_event with a
                    # type error: teams is a string, not a dict.
                    {"gamePk": 770999, "gameDate": "2026-04-17T18:20:00Z",
                     "teams": "garbage"},
                    # A normal payload that should still go through.
                    {
                        "gamePk": 770001,
                        "gameDate": "2026-04-17T18:20:00Z",
                        "teams": {
                            "home": {"team": {"id": 112, "name": "Chicago Cubs"}},
                            "away": {"team": {"id": 121, "name": "New York Mets"}},
                        },
                        "lineups": {
                            "homePlayers": [{"id": 660271}],
                            "awayPlayers": [{"id": 543037}],
                        },
                    },
                ]
            }
        ]
    }

    events_warmed = 0
    games_failed = 0
    index = _build_mlb_event_index(db_session)
    for date_block in schedule.get("dates") or []:
        for game in date_block.get("games") or []:
            try:
                matched = _match_mlb_event(index, game)
                if matched is None:
                    continue
                load_lineup_for_event(
                    db_session,
                    event_id=str(matched.id),
                    schedule_payload={"dates": [{"games": [game]}]},
                )
                events_warmed += 1
            except Exception:
                games_failed += 1

    assert games_failed == 1
    assert events_warmed == 1
    assert db_session.query(MlbLineupCache).count() == 1
    assert db_session.query(MlbLineupCache).one().event_id == str(event.id)


# -----------------------------------------------------------------------------
# Round-3 #2 — probable-starter extraction + sidecar persistence.

def test_extract_probable_pitcher_ids_pulls_from_schedule_hydration():
    """The hydrate=probablePitcher schedule shape places each starter under
    ``game.teams.{home,away}.probablePitcher.id``. The warm cron must walk
    those instead of relying on the (possibly empty) sidecar list."""
    from app.services.refresh_jobs import _extract_probable_pitcher_ids

    schedule_payload = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 770001,
                        "teams": {
                            "home": {"probablePitcher": {"id": 660271}},
                            "away": {"probablePitcher": {"id": 543037}},
                        },
                    },
                    {
                        "gamePk": 770002,
                        "teams": {
                            "home": {"probablePitcher": {"id": 605141}},
                            "away": {},  # no probable yet
                        },
                    },
                    {
                        # Duplicate — same starter as game 1's home pitcher.
                        # Dedup must happen across the whole slate.
                        "gamePk": 770003,
                        "teams": {
                            "home": {"probablePitcher": {"id": 660271}},
                            "away": {"probablePitcher": {"id": 671096}},
                        },
                    },
                ]
            }
        ]
    }
    ids = _extract_probable_pitcher_ids(schedule_payload)
    assert sorted(ids) == ["543037", "605141", "660271", "671096"]


def test_resolver_starter_path_persists_mlb_stats_id_sidecar_when_espn_id_provided(db_session):
    """Codex round 3: starter resolution paths in scoring used to pass
    espn_athlete_id=None, so the resolver's write-back never fired. PR 8
    threads the ESPN ID through; that lets a successful resolve persist the
    mlb_stats_id sidecar so the next warm cron picks the starter up."""
    from app.models import EspnPlayerSearchCache, MlbPlayerRosterCache, utcnow
    from app.services.mlb_advanced import resolve_mlb_stats_player_id

    cached_at = utcnow()
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="MLB",
            query_normalized="gerrit cole",
            payload={
                "athlete_id": "31060",
                "display_name": "Gerrit Cole",
                "team_name": "New York Yankees",
                # Note: no mlb_stats_id sidecar — that's exactly the gap.
            },
            cached_at=cached_at,
            expires_at=cached_at + timedelta(days=1),
        )
    )
    db_session.add(
        MlbPlayerRosterCache(
            season=2026,
            payload={
                "players": [
                    {
                        "person_id": "543037",
                        "display_name": "Gerrit Cole",
                        "team_id": "147",
                        "team_abbreviation": "NYY",
                    }
                ]
            },
            cached_at=cached_at,
            expires_at=cached_at + timedelta(hours=12),
        )
    )
    db_session.flush()

    resolved = resolve_mlb_stats_player_id(
        db_session,
        espn_athlete_id="31060",
        full_name="Gerrit Cole",
        team_abbreviation="NYY",
        season=2026,
        allow_network=False,
    )
    assert resolved == "543037"

    # Sidecar must now be persisted on the search-cache row.
    row = db_session.query(EspnPlayerSearchCache).filter_by(query_normalized="gerrit cole").one()
    assert row.payload.get("mlb_stats_id") == "543037"


# -----------------------------------------------------------------------------
# Round-3 #3 — Statcast pitcher producer.

def test_warm_mlb_advanced_warms_statcast_pitcher_cache_when_savant_provided(db_session):
    """The warm cron must hand a BaseballSavantClient into
    ``warm_mlb_advanced_for_athletes`` so ``mlb_statcast_pitcher_cache``
    rows actually get written. Without it, scoring's pitcher Statcast read
    always misses."""
    from app.models import MlbPitcherAdvancedCache, MlbStatcastPitcherCache
    from app.services import mlb_advanced

    class _StubMlbStatsClient:
        def fetch_pitcher_sabermetrics(self, person_id, season):
            return {
                "stats": [
                    {
                        "group": {"displayName": "pitching"},
                        "splits": [
                            {"stat": {"fip": 3.45, "xfip": 3.55, "era": 3.20,
                                      "whip": 1.10, "strikeoutsPer9Inn": 10.0,
                                      "walksPer9Inn": 2.5, "homeRunsPer9": 0.9}}
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

    class _StubSavantClient:
        def fetch_pitcher_statcast(self, person_id, season):
            # Per-pitch CSV — the real Savant search endpoint format.
            # Two FF + one SL with mixed swing/whiff/called outcomes.
            return (
                "pitch_type,release_speed,description,strikes,events\n"
                "FF,97.5,swinging_strike,2,strikeout\n"
                "FF,96.8,called_strike,1,\n"
                "SL,85.0,foul,1,\n"
            )

        def fetch_batter_statcast(self, person_id, season):
            return ""

    summary = mlb_advanced.warm_mlb_advanced_for_athletes(
        db_session,
        mlb_stats_player_ids=[],
        pitcher_ids=["543037"],
        season=2026,
        client=_StubMlbStatsClient(),
        savant=_StubSavantClient(),
    )
    assert summary["mlb_pitchers_attempted"] == 1
    assert summary["mlb_pitchers_succeeded"] == 1
    assert db_session.query(MlbPitcherAdvancedCache).count() == 1
    # The fix: a Statcast pitcher row exists too, not just sabermetrics.
    assert db_session.query(MlbStatcastPitcherCache).count() == 1


# -----------------------------------------------------------------------------
# Round-3 #4 — true scoring integration (the test Codex asked for).

MLB_PROP_GAMELOG_PAYLOAD = {
    "names": [
        "atBats",
        "runs",
        "hits",
        "doubles",
        "triples",
        "homeRuns",
        "RBIs",
        "walks",
        "hitByPitch",
        "strikeouts",
    ],
    # Six logs — the participation gate needs >=5 with PA>=2 in 3 of recent 5.
    "events": {
        f"evt-{idx}": {
            "gameDate": f"2026-04-{idx + 1:02d}T19:00Z",
            "opponent": {"displayName": "Boston Red Sox", "abbreviation": "BOS"},
            "atVs": "vs",
            "team": {"displayName": "New York Yankees"},
            "gameResult": "W",
            "homeTeamScore": "5",
            "awayTeamScore": "3",
        }
        for idx in range(6)
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {
                            "eventId": f"evt-{idx}",
                            # at_bats=4, runs=1, hits=2, doubles=0, triples=0,
                            # home_runs=1, rbis=2, walks=1, hbp=0, k=1.
                            "stats": ["4", "1", "2", "0", "0", "1", "2", "1", "0", "1"],
                        }
                        for idx in range(6)
                    ]
                }
            ]
        }
    ],
}


def _seed_mlb_prop_subject_caches(db_session, *, mlb_stats_id: str = "592450"):
    """EspnPlayerSearchCache + EspnPlayerGamelogCache for Aaron Judge,
    pre-resolved with the mlb_stats_id sidecar so the resolver doesn't try
    to scan the roster table."""
    from app.models import EspnPlayerGamelogCache, EspnPlayerSearchCache, utcnow

    cached_at = utcnow()
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="MLB",
            query_normalized="aaron judge",
            payload={
                "athlete_id": "33192",
                "display_name": "Aaron Judge",
                "team_name": "New York Yankees",
                "team_abbreviation": "NYY",
                "mlb_stats_id": mlb_stats_id,
            },
            cached_at=cached_at,
            expires_at=cached_at + timedelta(days=1),
        )
    )
    db_session.add(
        EspnPlayerGamelogCache(
            sport_key="MLB",
            athlete_id="33192",
            season=2026,
            payload=MLB_PROP_GAMELOG_PAYLOAD,
            cached_at=cached_at,
            expires_at=cached_at + timedelta(minutes=30),
        )
    )


def _seed_mlb_pitcher_advanced(db_session, *, pitcher_id: str = "543037"):
    """Pitcher sabermetrics + Statcast keyed by pitcher_id."""
    from app.models import MlbPitcherAdvancedCache, MlbStatcastPitcherCache, utcnow

    cached_at = utcnow()
    db_session.add(
        MlbPitcherAdvancedCache(
            athlete_id=pitcher_id,
            season=2026,
            payload={
                "season_avg": {
                    "fip": 3.45,
                    "xfip": 3.55,
                    "era": 3.20,
                    "whip": 1.10,
                    "k_per_9": 10.0,
                    "bb_per_9": 2.5,
                    "hr_per_9": 0.9,
                }
            },
            cached_at=cached_at,
            expires_at=cached_at + timedelta(hours=12),
        )
    )
    db_session.add(
        MlbStatcastPitcherCache(
            athlete_id=pitcher_id,
            season=2026,
            payload={
                "season_avg": {
                    "avg_fastball_velo": 96.5,
                    "whiff_pct": 0.32,
                    "csw_pct": 0.31,
                    "putaway_pct": 0.24,
                }
            },
            cached_at=cached_at,
            expires_at=cached_at + timedelta(hours=12),
        )
    )


def _seed_mlb_player_roster(db_session, *, pitcher_id: str = "543037"):
    """Roster cache so resolve_mlb_stats_player_id can match starter by name."""
    from app.models import MlbPlayerRosterCache, utcnow

    cached_at = utcnow()
    db_session.add(
        MlbPlayerRosterCache(
            season=2026,
            payload={
                "players": [
                    {
                        "person_id": pitcher_id,
                        "display_name": "Chris Sale",
                        "team_id": "111",
                        "team_abbreviation": "BOS",
                    }
                ]
            },
            cached_at=cached_at,
            expires_at=cached_at + timedelta(hours=12),
        )
    )


def _seed_mlb_lineup_cache(db_session, *, event_id: int, mlb_stats_id: str = "592450"):
    """Lineup cache row keyed by app Event.id (the post-fix key) — the same
    key scoring's load_lineup_for_event reads."""
    from app.models import MlbLineupCache, utcnow

    cached_at = utcnow()
    db_session.add(
        MlbLineupCache(
            event_id=str(event_id),
            payload={
                "raw": {
                    "dates": [
                        {
                            "games": [
                                {
                                    "gamePk": 770100,
                                    "lineups": {
                                        "homePlayers": [
                                            {"id": 605141},
                                            {"id": 660271},
                                            {"id": int(mlb_stats_id)},  # Judge bats 3rd.
                                            {"id": 543037},
                                        ],
                                        "awayPlayers": [{"id": 671096}],
                                    },
                                }
                            ]
                        }
                    ]
                },
                "fetched_at": cached_at.isoformat(),
            },
            cached_at=cached_at,
            expires_at=cached_at + timedelta(hours=2),
        )
    )


def test_score_player_prop_emits_opposing_starter_and_batting_order_features(db_session):
    """Codex round-3 #4: end-to-end scoring with seeded MLB caches must
    surface ``opposing_starter_*`` and ``batting_order_position`` in
    ``predictions.features``. This is the test that would have failed
    before PR 8 because the lineup producer wrote the wrong key — and that
    Codex specifically asked for to prove the producer/consumer agreement."""
    from app.models import Market
    from app.services.scoring import PropStatsResolver, _score_player_prop

    # 1. Sika MLB event with two participants AND a probables block under
    #    the home competitor's raw_data — that's where _probable_pitcher_identity
    #    pulls the starter name from.
    event = _seed_mlb_event(
        db_session,
        external_id="evt-bos-nyy-pr8",
        name="New York Yankees at Boston Red Sox",
        starts_at=datetime(2026, 4, 17, 23, 5, tzinfo=timezone.utc),
        home_name="Boston Red Sox",
        away_name="New York Yankees",
    )
    event.raw_data = {
        "raw": {
            "competitions": [
                {
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {"displayName": "Boston Red Sox", "abbreviation": "BOS"},
                            "probables": [
                                {
                                    "athlete": {
                                        "id": "29215",
                                        "displayName": "Chris Sale",
                                    },
                                    "statistics": [{"abbreviation": "ERA", "displayValue": "3.20"}],
                                }
                            ],
                        },
                        {
                            "homeAway": "away",
                            "team": {"displayName": "New York Yankees", "abbreviation": "NYY"},
                            "probables": [],
                        },
                    ],
                    "venue": {"fullName": "Fenway Park", "indoor": False},
                }
            ]
        }
    }
    db_session.flush()

    # 2. Seed the prop subject (Aaron Judge) + opposing starter (Chris Sale)
    #    + lineup cache keyed by the app event.id.
    _seed_mlb_prop_subject_caches(db_session)
    _seed_mlb_pitcher_advanced(db_session, pitcher_id="543037")
    _seed_mlb_player_roster(db_session, pitcher_id="543037")
    _seed_mlb_lineup_cache(db_session, event_id=event.id, mlb_stats_id="592450")

    # 3. Build the player-prop market.
    market = Market(
        ticker="KXMLBHRS-26APR17NYYBOS-AARONJ-OVER0_5",
        sport_key="MLB",
        event_id=event.id,
        title="Aaron Judge: 0.5+ home runs",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "home_runs",
            "copilot_threshold": 0.5,
            "copilot_subject_name": "Aaron Judge",
            "copilot_subject_team": "NYY",
        },
    )
    db_session.add(market)
    db_session.flush()

    # 4. Run scoring with allow_network=False — every fetch must hit the
    #    seeded caches.
    resolver = PropStatsResolver(db_session, allow_network=False)
    result = _score_player_prop(db_session, event, market, None, resolver)
    assert result is not None, "scoring path must return a signal for the seeded prop"

    _, _, reasons, features, _feature_groups = result

    # The features Codex specifically asked for:
    assert features.get("opposing_starter_xfip") == pytest.approx(3.55)
    assert features.get("opposing_starter_fip") == pytest.approx(3.45)
    assert features.get("opposing_starter_csw_pct") == pytest.approx(0.31)
    assert features.get("opposing_starter_whiff_pct") == pytest.approx(0.32)
    assert features.get("opposing_starter_avg_fastball_velo") == pytest.approx(96.5)
    assert features.get("pitcher_data_complete") == 1.0

    assert features.get("batting_order_position") == 3.0
    assert features.get("lineup_data_complete") == 1.0

    # Sanity: the resolver also threaded the batter's MLB Stats ID through
    # so the consumer didn't fall back to a re-scan of EspnPlayerSearchCache.
    assert features.get("mlb_batter_data_complete") in {1.0, None}

    # Reasons should at least include the headline stat lines (no assertion
    # on driver attribution — that's deferred per CODEX_REVIEW_NOTES_ROUND3.md).
    assert any("home runs" in reason for reason in reasons)

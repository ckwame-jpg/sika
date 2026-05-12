from datetime import date

from app.api.routes import get_stats_query_service
from app.main import app
from app.services.stats_query import StatsQueryService, default_season_for_sport, parse_stats_question


NBA_GAMELOG_PAYLOAD = {
    "names": [
        "minutes",
        "fieldGoalsMade-fieldGoalsAttempted",
        "fieldGoalPct",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
        "threePointPct",
        "freeThrowsMade-freeThrowsAttempted",
        "freeThrowPct",
        "totalRebounds",
        "assists",
        "blocks",
        "steals",
        "fouls",
        "turnovers",
        "points",
    ],
    "events": {
        "4013": {
            "gameDate": "2026-03-29T23:30:00+00:00",
            "atVs": "@",
            "gameResult": "W",
            "homeTeamScore": "110",
            "awayTeamScore": "116",
            "opponent": {"displayName": "Boston Celtics", "abbreviation": "BOS"},
        },
        "4012": {
            "gameDate": "2026-03-27T23:30:00+00:00",
            "atVs": "vs",
            "gameResult": "W",
            "homeTeamScore": "121",
            "awayTeamScore": "111",
            "opponent": {"displayName": "Miami Heat", "abbreviation": "MIA"},
        },
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {"eventId": "4013", "stats": ["36", "11-19", "57.9", "4-8", "50.0", "6-7", "85.7", "5", "9", "1", "2", "2", "3", "32"]},
                        {"eventId": "4012", "stats": ["35", "9-18", "50.0", "2-6", "33.3", "7-8", "87.5", "4", "8", "0", "1", "1", "2", "27"]},
                    ]
                }
            ]
        }
    ],
}

NFL_GAMELOG_PAYLOAD = {
    "names": [
        "completions",
        "passingAttempts",
        "passingYards",
        "completionPct",
        "yardsPerPassAttempt",
        "passingTouchdowns",
        "interceptions",
        "longPassing",
        "sacks",
        "QBRating",
        "adjQBR",
        "rushingAttempts",
        "rushingYards",
        "yardsPerRushAttempt",
        "rushingTouchdowns",
        "longRushing",
    ],
    "events": {
        "nfl2": {
            "gameDate": "2025-12-14T18:00:00+00:00",
            "atVs": "vs",
            "gameResult": "W",
            "homeTeamScore": "31",
            "awayTeamScore": "24",
            "opponent": {"displayName": "Los Angeles Chargers", "abbreviation": "LAC"},
        },
        "nfl1": {
            "gameDate": "2025-12-07T18:00:00+00:00",
            "atVs": "@",
            "gameResult": "L",
            "homeTeamScore": "28",
            "awayTeamScore": "21",
            "opponent": {"displayName": "Denver Broncos", "abbreviation": "DEN"},
        },
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {"eventId": "nfl2", "stats": ["24", "35", "280", "68.6", "8.0", "2", "1", "42", "2", "95.0", "70.5", "5", "22", "4.4", "1", "10"]},
                        {"eventId": "nfl1", "stats": ["18", "27", "240", "66.7", "8.9", "1", "0", "35", "1", "92.0", "64.5", "4", "18", "4.5", "0", "9"]},
                    ]
                }
            ]
        }
    ],
}

MLB_GAMELOG_PAYLOAD = {
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
        "stolenBases",
        "caughtStealing",
        "avg",
        "onBasePct",
        "slugAvg",
        "OPS",
    ],
    "events": {
        "mlb2": {
            "gameDate": "2026-03-28T23:15:00+00:00",
            "atVs": "@",
            "gameResult": "W",
            "homeTeamScore": "1",
            "awayTeamScore": "3",
            "opponent": {"displayName": "San Francisco Giants", "abbreviation": "SF"},
        },
        "mlb1": {
            "gameDate": "2026-03-27T23:15:00+00:00",
            "atVs": "@",
            "gameResult": "W",
            "homeTeamScore": "2",
            "awayTeamScore": "5",
            "opponent": {"displayName": "San Francisco Giants", "abbreviation": "SF"},
        },
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {"eventId": "mlb2", "stats": ["4", "1", "2", "1", "0", "1", "3", "1", "0", "1", "0", "0", ".500", ".600", "1.500", "2.100"]},
                        {"eventId": "mlb1", "stats": ["4", "0", "1", "0", "0", "0", "1", "0", "0", "2", "0", "0", ".250", ".250", ".250", ".500"]},
                    ]
                }
            ]
        }
    ],
}

SOCCER_OVERVIEW_PAYLOAD = {
    "page": {
        "content": {
            "player": {
                "plyrHdr": {
                    "statsBlck": {
                        "hdr": "2026 MLS Stats",
                        "vals": [
                            {"name": "Starts-Substitute Appearances", "lbl": "START (SUB)", "val": "4 (0)"},
                            {"name": "Total Goals", "lbl": "G", "val": "4"},
                            {"name": "Assists", "lbl": "A", "val": "2"},
                            {"name": "Shots", "lbl": "SH", "val": "15"},
                        ],
                    }
                },
                "stats": {
                    "tm": "20232",
                    "lg": "770",
                    "splts": {
                        "lbls": [
                            {"data": "STRT", "ttl": "Starts"},
                            {"data": "FC", "ttl": "Fouls Committed"},
                            {"data": "FA", "ttl": "Fouls Suffered"},
                            {"data": "YC", "ttl": "Yellow Cards"},
                            {"data": "RC", "ttl": "Red Cards"},
                            {"data": "G", "ttl": "Goals"},
                            {"data": "A", "ttl": "Assists"},
                            {"data": "SH", "ttl": "Shots"},
                            {"data": "ST", "ttl": "Shots On Target"},
                            {"data": "OF", "ttl": "Offsides"},
                        ],
                        "stats": {
                            "770-20232": ["2026 MLS", "4", "1", "5", "1", "0", "4", "2", "15", "7", "2"]
                        },
                    },
                },
                "gmlg": {
                    "stats": [
                        {
                            "title": "",
                            "headings": [
                                {"data": "APP", "ttl": "Appearances"},
                                {"data": "G", "ttl": "Goals"},
                                {"data": "A", "ttl": "Assists"},
                                {"data": "SH", "ttl": "Shots"},
                                {"data": "ST", "ttl": "Shots On Target"},
                                {"data": "FC", "ttl": "Fouls Committed"},
                                {"data": "FA", "ttl": "Fouls Suffered"},
                                {"data": "OF", "ttl": "Offsides"},
                                {"data": "YC", "ttl": "Yellow Cards"},
                                {"data": "RC", "ttl": "Red Cards"},
                            ],
                            "rows": [
                                {
                                    "id": "soc4",
                                    "dt": "2026-03-27T23:15:00.000+00:00",
                                    "res": {"abbr": "W", "score": "2-1"},
                                    "opp": {"abbr": "ORL", "name": "Orlando City SC", "atVs": "@"},
                                    "stats": ["Started", "1", "1", "5", "2", "0", "2", "0", "0", "0"],
                                    "comp": "MLS",
                                    "tm": {"name": "MIA"},
                                },
                                {
                                    "id": "soc3",
                                    "dt": "2026-03-20T23:15:00.000+00:00",
                                    "res": {"abbr": "D", "score": "1-1"},
                                    "opp": {"abbr": "ATL", "name": "Atlanta United FC", "atVs": "vs"},
                                    "stats": ["Started", "1", "0", "4", "2", "1", "1", "1", "0", "0"],
                                    "comp": "MLS",
                                    "tm": {"name": "MIA"},
                                },
                                {
                                    "id": "soc2",
                                    "dt": "2026-03-14T23:15:00.000+00:00",
                                    "res": {"abbr": "W", "score": "3-0"},
                                    "opp": {"abbr": "CLB", "name": "Columbus Crew", "atVs": "@"},
                                    "stats": ["Started", "2", "1", "4", "2", "0", "1", "1", "1", "0"],
                                    "comp": "MLS",
                                    "tm": {"name": "MIA"},
                                },
                                {
                                    "id": "soc1",
                                    "dt": "2026-03-08T23:15:00.000+00:00",
                                    "res": {"abbr": "L", "score": "0-1"},
                                    "opp": {"abbr": "DC", "name": "D.C. United", "atVs": "vs"},
                                    "stats": ["Started", "0", "0", "2", "1", "0", "1", "0", "0", "0"],
                                    "comp": "MLS",
                                    "tm": {"name": "MIA"},
                                },
                            ],
                        }
                    ]
                },
            }
        }
    }
}

TENNIS_ATHLETE_PROFILE = {
    "statistics": {"$ref": "https://example.test/tennis/296/statistics"},
    "eventLog": {"$ref": "https://example.test/tennis/296/eventlog"},
}

TENNIS_STATISTICS_PAYLOAD = {
    "splits": {
        "categories": [
            {
                "stats": [
                    {"name": "singlesWon", "value": 3.0},
                    {"name": "singlesLost", "value": 1.0},
                    {"name": "singlesTitles", "value": 1.0},
                    {"name": "doublesTitles", "value": 0.0},
                    {"name": "prize", "value": 987654.0},
                ]
            }
        ]
    }
}

TENNIS_EVENTLOG_PAYLOAD = {
    "events": {
        "items": [
            {
                "event": {"$ref": "https://example.test/tennis/events/miami-open"},
                "competition": {"$ref": "https://example.test/tennis/competitions/miami-final"},
                "played": True,
            },
            {
                "event": {"$ref": "https://example.test/tennis/events/miami-open"},
                "competition": {"$ref": "https://example.test/tennis/competitions/miami-semifinal"},
                "played": True,
            },
            {
                "event": {"$ref": "https://example.test/tennis/events/indian-wells"},
                "competition": {"$ref": "https://example.test/tennis/competitions/indian-wells-semifinal"},
                "played": True,
            },
            {
                "event": {"$ref": "https://example.test/tennis/events/indian-wells"},
                "competition": {"$ref": "https://example.test/tennis/competitions/indian-wells-quarterfinal"},
                "played": True,
            },
            {
                "event": {"$ref": "https://example.test/tennis/events/miami-open"},
                "competition": {"$ref": "https://example.test/tennis/competitions/miami-doubles"},
                "played": True,
            },
        ]
    }
}

TENNIS_REF_PAYLOADS = {
    "https://example.test/tennis/296/statistics": TENNIS_STATISTICS_PAYLOAD,
    "https://example.test/tennis/296/eventlog": TENNIS_EVENTLOG_PAYLOAD,
    "https://example.test/tennis/events/miami-open": {"shortName": "Miami Open"},
    "https://example.test/tennis/events/indian-wells": {"shortName": "BNP Paribas Open"},
    "https://example.test/tennis/competitions/miami-final": {
        "id": "miami-final",
        "date": "2026-03-30T20:00:00+00:00",
        "type": {"type": "singles"},
        "round": {"description": "Final"},
        "competitors": [
            {"id": "296", "name": "Novak Djokovic", "winner": True},
            {"id": "3782", "name": "Carlos Alcaraz", "winner": False},
        ],
        "notes": [{"text": "Novak Djokovic (SRB) bt Carlos Alcaraz (ESP) 6-4 7-6 (7-5)"}],
    },
    "https://example.test/tennis/competitions/miami-semifinal": {
        "id": "miami-semifinal",
        "date": "2026-03-28T20:00:00+00:00",
        "type": {"type": "singles"},
        "round": {"description": "Semifinal"},
        "competitors": [
            {"id": "296", "name": "Novak Djokovic", "winner": True},
            {"id": "2383", "name": "Daniil Medvedev", "winner": False},
        ],
        "notes": [{"text": "Novak Djokovic (SRB) bt Daniil Medvedev (RUS) 4-6 6-3 6-4"}],
    },
    "https://example.test/tennis/competitions/indian-wells-semifinal": {
        "id": "indian-wells-semifinal",
        "date": "2026-03-15T20:00:00+00:00",
        "type": {"type": "singles"},
        "round": {"description": "Semifinal"},
        "competitors": [
            {"id": "3623", "name": "Jannik Sinner", "winner": True},
            {"id": "296", "name": "Novak Djokovic", "winner": False},
        ],
        "notes": [{"text": "Jannik Sinner (ITA) bt Novak Djokovic (SRB) 6-3 3-6 7-5"}],
    },
    "https://example.test/tennis/competitions/indian-wells-quarterfinal": {
        "id": "indian-wells-quarterfinal",
        "date": "2026-03-13T20:00:00+00:00",
        "type": {"type": "singles"},
        "round": {"description": "Quarterfinal"},
        "competitors": [
            {"id": "296", "name": "Novak Djokovic", "winner": True},
            {"id": "2375", "name": "Alexander Zverev", "winner": False},
        ],
        "notes": [{"text": "Novak Djokovic (SRB) bt Alexander Zverev (GER) 6-2 6-4"}],
    },
    "https://example.test/tennis/competitions/miami-doubles": {
        "id": "miami-doubles",
        "date": "2026-03-26T20:00:00+00:00",
        "type": {"type": "doubles"},
        "round": {"description": "Quarterfinal"},
        "competitors": [
            {"id": "296-111", "name": "Novak Djokovic/Partner", "winner": True},
            {"id": "222-333", "name": "Doubles Team", "winner": False},
        ],
        "notes": [{"text": "Novak Djokovic/Partner bt Doubles Team 6-3 6-2"}],
    },
}

UFC_HISTORY_PAYLOAD = {
    "page": {
        "content": {
            "player": {
                "plyrHdr": {
                    "statsBlck": {
                        "hdr": "Stats",
                        "vals": [
                            {"lbl": "W-L-D", "val": "13-3-0"},
                            {"lbl": "(T)KO", "val": "11-1"},
                            {"lbl": "SUB", "val": "0-1"},
                        ],
                    }
                },
                "fghtHstr": [
                    {
                        "dcsn": "KO/TKO",
                        "evnt": "UFC 320: Ankalaev vs. Pereira 2",
                        "evntLnk": "https://www.espn.com/mma/fightcenter/_/id/600054473/league/ufc",
                        "hdate": "2025-10-04T22:00:00.000+00:00",
                        "htime": "1:20",
                        "opp": "Magomed Ankalaev",
                        "oppLnk": "https://www.espn.com/mma/fighter/_/id/4273399/magomed-ankalaev",
                        "oppUid": "s:3301~a:4273399",
                        "rnd": 1,
                        "rslt": "W",
                        "ttlFght": True,
                    },
                    {
                        "dcsn": "Decision - Unanimous",
                        "evnt": "UFC 313: Pereira vs. Ankalaev",
                        "evntLnk": "https://www.espn.com/mma/fightcenter/_/id/600051447/league/ufc",
                        "hdate": "2025-03-08T23:00:00.000+00:00",
                        "htime": "5:00",
                        "opp": "Magomed Ankalaev",
                        "oppLnk": "https://www.espn.com/mma/fighter/_/id/4273399/magomed-ankalaev",
                        "oppUid": "s:3301~a:4273399",
                        "rnd": 5,
                        "rslt": "L",
                        "ttlFght": True,
                    },
                    {
                        "dcsn": "KO/TKO",
                        "evnt": "UFC 307: Pereira vs. Rountree Jr.",
                        "evntLnk": "https://www.espn.com/mma/fightcenter/_/id/600048801/league/ufc",
                        "hdate": "2024-10-05T22:30:00.000+00:00",
                        "htime": "4:32",
                        "opp": "Khalil Rountree Jr.",
                        "oppLnk": "https://www.espn.com/mma/fighter/_/id/4028627/khalil-rountree-jr",
                        "oppUid": "s:3301~a:4028627",
                        "rnd": 4,
                        "rslt": "W",
                        "ttlFght": True,
                    },
                    {
                        "dcsn": "KO/TKO",
                        "evnt": "UFC 303: Pereira vs. Procházka 2",
                        "evntLnk": "https://www.espn.com/mma/fightcenter/_/id/600043173/league/ufc",
                        "hdate": "2024-06-29T22:00:00.000+00:00",
                        "htime": "0:13",
                        "opp": "Jiří Procházka",
                        "oppLnk": "https://www.espn.com/mma/fighter/_/id/3156612/jiri-prochazka",
                        "oppUid": "s:3301~a:3156612",
                        "rnd": 2,
                        "rslt": "W",
                        "ttlFght": True,
                    },
                ],
            }
        }
    }
}


class FakeEspnClient:
    def search_player(self, query: str, sport_key: str = "NBA", *, team_hint: str | None = None):
        player_map = {
            "NBA": {"athlete_id": "3934672", "display_name": "Jalen Brunson", "team_name": "New York Knicks"},
            "NFL": {"athlete_id": "3139477", "display_name": "Patrick Mahomes", "team_name": "Kansas City Chiefs"},
            "MLB": {"athlete_id": "33192", "display_name": "Aaron Judge", "team_name": "New York Yankees"},
            "SOCCER": {
                "athlete_id": "45843",
                "display_name": "Lionel Messi",
                "team_name": "Inter Miami CF",
                "page_slug": "lionel-messi",
            },
            "TENNIS": {"athlete_id": "296", "display_name": "Novak Djokovic", "team_name": None},
            "UFC": {"athlete_id": "4705658", "display_name": "Alex Pereira", "team_name": None, "page_slug": "alex-pereira"},
        }
        return player_map[sport_key]

    def fetch_player_gamelog(self, sport_key: str, athlete_id: str, season: int):
        assert athlete_id
        if sport_key == "NBA":
            assert season == 2026
            return NBA_GAMELOG_PAYLOAD
        if sport_key == "NFL":
            assert season == 2025
            return NFL_GAMELOG_PAYLOAD
        if sport_key == "MLB":
            assert season == 2026
            return MLB_GAMELOG_PAYLOAD
        raise AssertionError("unexpected sport")

    def fetch_soccer_player_overview(self, athlete_id: str, page_slug: str | None = None):
        assert athlete_id == "45843"
        assert page_slug == "lionel-messi"
        return SOCCER_OVERVIEW_PAYLOAD

    def fetch_tennis_athlete_profile(self, athlete_id: str):
        assert athlete_id == "296"
        return TENNIS_ATHLETE_PROFILE

    def fetch_json_ref(self, ref_url: str):
        return TENNIS_REF_PAYLOADS[ref_url]

    def fetch_mma_fighter_history(self, athlete_id: str, page_slug: str | None = None):
        assert athlete_id == "4705658"
        assert page_slug == "alex-pereira"
        return UFC_HISTORY_PAYLOAD


def test_parse_stats_question_last_n_games():
    parsed = parse_stats_question("What is Jalen Brunson's stats in the last 10 games?", season=2026)

    assert parsed.sport_key == "NBA"
    assert parsed.player_name == "Jalen Brunson"
    assert parsed.query_type == "last_n_games"
    assert parsed.games_requested == 10
    assert parsed.season == 2026


def test_parse_stats_question_supports_soccer_matches_wording():
    parsed = parse_stats_question("What is Lionel Messi's stats in the last 5 matches?", sport_key="SOCCER", season=2026)

    assert parsed.sport_key == "SOCCER"
    assert parsed.player_name == "Lionel Messi"
    assert parsed.games_requested == 5


def test_parse_stats_question_supports_ufc_fights_wording():
    parsed = parse_stats_question("Alex Pereira last 5 fights", sport_key="UFC", season=2025)

    assert parsed.sport_key == "UFC"
    assert parsed.player_name == "Alex Pereira"
    assert parsed.games_requested == 5


def test_default_season_for_nfl_uses_previous_year_before_training_camp():
    assert default_season_for_sport("NFL", reference_date=date(2026, 3, 30)) == 2025


def test_default_season_for_tennis_uses_calendar_year():
    assert default_season_for_sport("TENNIS", reference_date=date(2026, 3, 31)) == 2026


def test_default_season_for_ufc_uses_calendar_year():
    assert default_season_for_sport("UFC", reference_date=date(2026, 3, 31)) == 2026


def test_stats_query_service_returns_nba_metric_map():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("What is Jalen Brunson's stats in the last 2 games?", sport_key="NBA", season=2026)

    assert result["sport_key"] == "NBA"
    assert result["games_analyzed"] == 2
    assert result["summary"]["metrics"]["points"] == 29.5
    assert result["summary"]["metrics"]["assists"] == 8.5
    assert result["metric_labels"]["field_goal_pct"] == "FG%"
    assert result["game_logs"][0]["metrics"]["field_goal_pct"] == 57.9
    assert result["summary"]["stat_line"] == "29.5 points, 8.5 assists, 4.5 rebounds, 35.5 minutes"
    assert result["game_logs"][0]["stat_line"] == "32 points, 9 assists, 5 rebounds, 36.0 minutes"


def test_stats_query_service_returns_nfl_metric_map():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Patrick Mahomes last 2 games", sport_key="NFL", season=2025)

    assert result["sport_key"] == "NFL"
    assert result["summary"]["metrics"]["passing_yards"] == 260.0
    assert result["summary"]["metrics"]["completion_pct"] == 67.7
    assert result["summary"]["metrics"]["qbr"] == 67.5
    assert result["game_logs"][0]["metrics"]["passing_touchdowns"] == 2.0
    assert result["summary"]["stat_line"] == "260 pass yards, 1.5 pass TD, 20 rush yards, 67.5 QBR"


def test_stats_query_service_returns_mlb_totals_and_rates():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Aaron Judge this season", sport_key="MLB", season=2026)

    assert result["sport_key"] == "MLB"
    assert result["summary"]["metrics"]["hits"] == 3.0
    assert result["summary"]["metrics"]["home_runs"] == 1.0
    assert result["summary"]["metrics"]["batting_avg"] == 0.375
    assert result["summary"]["metrics"]["ops"] == 1.319
    assert result["game_logs"][0]["metrics"]["ops"] == 2.1
    assert result["summary"]["stat_line"] == "3 hits, 1 HR, 4 RBI, 1.319 OPS"


def test_stats_query_service_returns_soccer_recent_match_stats():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Lionel Messi last 3 matches", sport_key="SOCCER", season=2026)

    assert result["sport_key"] == "SOCCER"
    assert result["games_analyzed"] == 3
    assert result["summary"]["draws"] == 1
    assert result["summary"]["metrics"]["goals"] == 4.0
    assert result["summary"]["metrics"]["goal_contributions"] == 6.0
    assert result["summary"]["metrics"]["shots_on_target"] == 6.0
    assert result["game_logs"][0]["competition"] == "MLS"
    assert result["game_logs"][0]["team_name"] == "MIA"
    assert result["summary"]["stat_line"] == "4 goals, 2 assists, 13 shots, 6 shots on target"
    assert result["game_logs"][0]["stat_line"] == "1 goal, 1 assist, 5 shots, 2 shots on target"
    assert result["coverage_note"].startswith("Soccer beta uses ESPN's public player overview")
    assert result["source"] == "espn_public_player_page"


def test_stats_query_service_returns_soccer_season_summary():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Lionel Messi this season", sport_key="SOCCER", season=2026)

    assert result["sport_key"] == "SOCCER"
    assert result["summary"]["games"] == 4
    assert result["summary"]["wins"] == 2
    assert result["summary"]["losses"] == 1
    assert result["summary"]["draws"] == 1
    assert result["summary"]["metrics"]["starts"] == 4.0
    assert result["summary"]["metrics"]["sub_appearances"] == 0.0
    assert result["summary"]["metrics"]["goals"] == 4.0
    assert result["summary"]["metrics"]["assists"] == 2.0
    assert result["summary"]["stat_line"] == "4 goals, 2 assists, 15 shots, 7 shots on target"
    assert "2026 MLS Stats" in result["explanation"]


def test_stats_query_service_returns_tennis_recent_match_stats():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Novak Djokovic last 3 matches", sport_key="TENNIS", season=2026)

    assert result["sport_key"] == "TENNIS"
    assert result["games_analyzed"] == 3
    assert result["summary"]["wins"] == 2
    assert result["summary"]["losses"] == 1
    assert result["summary"]["metrics"]["sets_won"] == 5.0
    assert result["summary"]["metrics"]["games_won"] == 43.0
    assert result["summary"]["metrics"]["win_pct"] == 66.7
    assert result["summary"]["stat_line"] == "2-1 record, 5-3 in sets, 43-39 in games"
    assert result["game_logs"][0]["competition"] == "Miami Open"
    assert result["game_logs"][0]["stat_line"] == "W vs Carlos Alcaraz, 6-4 7-6 (7-5) (Final, Miami Open)"
    assert result["game_logs"][2]["stat_line"] == "L vs Jannik Sinner, 3-6 6-3 5-7 (Semifinal, BNP Paribas Open)"
    assert result["source"] == "espn_public_tennis_core"


def test_stats_query_service_returns_tennis_season_summary():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Novak Djokovic this season", sport_key="TENNIS", season=2026)

    assert result["sport_key"] == "TENNIS"
    assert result["summary"]["games"] == 4
    assert result["summary"]["wins"] == 3
    assert result["summary"]["losses"] == 1
    assert result["summary"]["metrics"]["sets_won"] == 7.0
    assert result["summary"]["metrics"]["games_lost"] == 45.0
    assert result["summary"]["metrics"]["straight_sets_wins"] == 2.0
    assert result["summary"]["metrics"]["titles"] == 1.0
    assert result["summary"]["metrics"]["prize_money_usd"] == 987654.0
    assert result["summary"]["stat_line"] == "3-1 record, 7-3 in sets, 55-45 in games, 1 title"
    assert result["coverage_note"] == "Tennis beta uses ESPN's public core tennis refs for singles season totals and match logs."
    assert "won 1 title" in result["explanation"]


def test_stats_query_service_returns_ufc_recent_fight_stats():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Alex Pereira last 3 fights", sport_key="UFC", season=2025)

    assert result["sport_key"] == "UFC"
    assert result["games_analyzed"] == 3
    assert result["summary"]["wins"] == 2
    assert result["summary"]["losses"] == 1
    assert result["summary"]["metrics"]["ko_tko_wins"] == 2.0
    assert result["summary"]["metrics"]["finish_rate"] == 66.7
    assert result["summary"]["metrics"]["avg_round"] == 3.3
    assert result["summary"]["metrics"]["avg_fight_minutes"] == 3.6
    assert result["summary"]["metrics"]["title_fights"] == 3.0
    assert result["summary"]["stat_line"] == "2-1 record, 2 KO/TKO wins, 66.7% finish rate, 3.3 avg rounds"
    assert result["game_logs"][0]["stat_line"] == "W vs Magomed Ankalaev, KO/TKO, R1 1:20 (UFC 320: Ankalaev vs. Pereira 2)"
    assert result["coverage_note"].startswith("UFC beta uses ESPN's public fighter history page")
    assert result["source"] == "espn_public_mma_history_page"


def test_stats_query_service_returns_ufc_season_summary():
    service = StatsQueryService(espn_client=FakeEspnClient())

    result = service.query("Alex Pereira this season", sport_key="UFC", season=2025)

    assert result["sport_key"] == "UFC"
    assert result["summary"]["games"] == 2
    assert result["summary"]["wins"] == 1
    assert result["summary"]["losses"] == 1
    assert result["summary"]["metrics"]["ko_tko_wins"] == 1.0
    assert result["summary"]["metrics"]["decision_losses"] == 1.0
    assert result["summary"]["metrics"]["finish_rate"] == 50.0
    assert result["summary"]["metrics"]["avg_round"] == 3.0
    assert result["summary"]["metrics"]["avg_fight_minutes"] == 3.2
    assert result["summary"]["stat_line"] == "1-1 record, 1 KO/TKO wins, 50.0% finish rate, 3.0 avg rounds"
    assert "2025 calendar year" in result["explanation"]


def test_stats_query_endpoint(client):
    class FakeService:
        def query(self, question: str, sport_key: str = "NBA", season: int | None = None, *, db=None):
            assert question == "Patrick Mahomes last 2 games"
            assert sport_key == "NFL"
            assert season == 2025
            return {
                "question": question,
                "sport_key": "NFL",
                "entity_name": "Patrick Mahomes",
                "entity_id": "3139477",
                "team_name": "Kansas City Chiefs",
                "query_type": "last_n_games",
                "season": 2025,
                "games_requested": 2,
                "games_analyzed": 2,
                "split": None,
                "opponent": None,
                "metric_labels": {"passing_yards": "Pass Yards"},
                "summary": {
                    "games": 2,
                    "wins": 1,
                    "losses": 1,
                    "metrics": {"passing_yards": 260.0},
                    "stat_line": "260 pass yards, 1.5 pass TD, 20 rush yards, 67.5 QBR",
                },
                "game_logs": [
                    {
                        "game_id": "nfl2",
                        "game_date": "2025-12-14T18:00:00Z",
                        "location": "home",
                        "opponent": "Los Angeles Chargers",
                        "opponent_abbreviation": "LAC",
                        "result": "W",
                        "team_score": 31.0,
                        "opponent_score": 24.0,
                        "metrics": {"passing_yards": 280.0},
                        "stat_line": "280 pass yards, 2 pass TD, 22 rush yards, 70.5 QBR",
                    }
                ],
                "explanation": "Patrick Mahomes averaged 260.0 passing yards over the last 2 games.",
                "source": "espn_public",
            }

    app.dependency_overrides[get_stats_query_service] = lambda: FakeService()
    try:
        response = client.post("/research/stats/query", json={"question": "Patrick Mahomes last 2 games", "sport_key": "NFL", "season": 2025})
    finally:
        app.dependency_overrides.pop(get_stats_query_service, None)

    assert response.status_code == 200
    assert response.json()["sport_key"] == "NFL"
    assert response.json()["summary"]["metrics"]["passing_yards"] == 260.0
    assert response.json()["summary"]["stat_line"] == "260 pass yards, 1.5 pass TD, 20 rush yards, 67.5 QBR"

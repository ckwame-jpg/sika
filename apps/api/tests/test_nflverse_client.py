"""Smarter NFL PR 3 — nflverse-data client parsing regression.

Fixture CSVs mirror the live column layout (verified 2026-07-09):
``stats_player_week_{season}.csv`` under the ``stats_player`` release
(the old ``player_stats`` tag is dead), snap counts keyed by PFR id,
depth charts carrying espn_id + gsis_id per row, and nfldata's
``games.csv`` with closing spread/total/moneyline columns.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.clients.nflverse import (
    NFLDATA_GAMES_URL,
    NflverseClient,
)


class _StubHttpClient:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(url)
        for fragment, body in self.responses.items():
            if fragment in url:
                return httpx.Response(200, text=body, request=httpx.Request("GET", url))
        return httpx.Response(404, text="not found", request=httpx.Request("GET", url))


def test_weekly_player_stats_uses_stats_player_release_and_parses_rows() -> None:
    csv_body = (
        "player_id,player_display_name,position,team,season,week,season_type,"
        "passing_yards,rushing_yards,receptions,targets,receiving_yards,target_share\n"
        "00-0033873,Patrick Mahomes,QB,KC,2025,1,REG,312,18,0,0,0,\n"
        "00-0036322,Justin Jefferson,WR,MIN,2025,1,REG,0,3,8,11,101,0.31\n"
    )
    stub = _StubHttpClient({"stats_player/stats_player_week_2025.csv": csv_body})
    rows = NflverseClient(http_client=stub).fetch_weekly_player_stats(2025)
    assert len(rows) == 2
    assert rows[0]["player_display_name"] == "Patrick Mahomes"
    assert rows[1]["receiving_yards"] == "101"
    assert "stats_player/stats_player_week_2025.csv" in stub.calls[0]


def test_latest_depth_charts_filters_to_newest_snapshot_per_team() -> None:
    csv_body = (
        "dt,team,player_name,espn_id,gsis_id,pos_abb,pos_slot,pos_rank\n"
        "2025-09-01,KC,Old Starter,111,00-1,QB,1,1\n"
        "2025-11-05,KC,Patrick Mahomes,3139477,00-0033873,QB,1,1\n"
        "2025-11-05,KC,Backup QB,222,00-2,QB,1,2\n"
        "2025-11-04,PHI,Jalen Hurts,4040715,00-0036389,QB,1,1\n"
    )
    stub = _StubHttpClient({"depth_charts/depth_charts_2025.csv": csv_body})
    rows = NflverseClient(http_client=stub).fetch_latest_depth_charts(2025)
    kc_rows = [row for row in rows if row["team"] == "KC"]
    assert {row["player_name"] for row in kc_rows} == {"Patrick Mahomes", "Backup QB"}
    assert all(row["dt"] == "2025-11-05" for row in kc_rows)
    phi_rows = [row for row in rows if row["team"] == "PHI"]
    assert len(phi_rows) == 1  # a team's own latest snapshot, not the global max dt


def test_fetch_games_filters_seasons() -> None:
    csv_body = (
        "game_id,season,game_type,week,away_team,home_team,away_score,home_score,"
        "result,total,spread_line,total_line,home_moneyline,away_moneyline,home_rest,away_rest\n"
        "2024_01_BAL_KC,2024,REG,1,BAL,KC,20,27,7,47,-3.0,46.5,-160,135,7,7\n"
        "2025_01_DAL_PHI,2025,REG,1,DAL,PHI,20,24,4,44,-6.5,48.5,-280,230,7,7\n"
    )
    stub = _StubHttpClient({"games.csv": csv_body})
    client = NflverseClient(http_client=stub)
    rows = client.fetch_games([2025])
    assert len(rows) == 1
    assert rows[0]["home_team"] == "PHI"
    assert rows[0]["spread_line"] == "-6.5"
    assert stub.calls[0] == NFLDATA_GAMES_URL
    assert len(client.fetch_games()) == 2


def test_official_injuries_and_snap_counts_parse() -> None:
    injuries_body = (
        "season,season_type,team,week,gsis_id,position,full_name,report_status,practice_status\n"
        "2025,REG,KC,10,00-0033873,QB,Patrick Mahomes,Questionable,Limited Participation in Practice\n"
    )
    snaps_body = (
        "game_id,season,week,player,pfr_player_id,position,team,offense_snaps,offense_pct\n"
        "2025_10_KC_BUF,2025,10,Patrick Mahomes,MahoPa00,QB,KC,68,100.0\n"
    )
    stub = _StubHttpClient({
        "injuries/injuries_2025.csv": injuries_body,
        "snap_counts/snap_counts_2025.csv": snaps_body,
    })
    client = NflverseClient(http_client=stub)
    injuries = client.fetch_official_injuries(2025)
    assert injuries[0]["report_status"] == "Questionable"
    snaps = client.fetch_snap_counts(2025)
    assert snaps[0]["offense_pct"] == "100.0"

"""nflverse-data client (Smarter NFL PR 3).

Pulls the free, nightly-updated CSV release assets published by the
nflverse project — the de-facto open NFL data standard — over plain
HTTPS (no API key). Asset names + columns verified live 2026-07-09:

- ``stats_player/stats_player_week_{season}.csv`` — weekly player box
  stats incl. EPA splits, target share (~7 MB / season). NOTE: the old
  ``player_stats/player_stats_{season}.csv`` tag is dead (404) — the
  project renamed the release to ``stats_player`` in 2024.
- ``snap_counts/snap_counts_{season}.csv`` — per-game snap counts +
  offense snap %, keyed by PFR player id + player name (~2.5 MB).
- ``depth_charts/depth_charts_{season}.csv`` — daily depth-chart
  snapshots. Big (~50 MB / season, ~550k rows) because it carries every
  historical snapshot; the client filters to the LATEST snapshot per
  team during parse so callers never hold the full history. Carries
  ``espn_id`` + ``gsis_id`` per player — the identity bridge between
  ESPN athletes and nflverse stat rows.
- ``injuries/injuries_{season}.csv`` — the OFFICIAL club injury reports
  (report_status Out / Doubtful / Questionable + practice status).
- ``stats_team/stats_team_week_{season}.csv`` — team-week offense
  aggregates incl. passing/rushing EPA (defense derived by joining the
  opponent's offense row on ``game_id``).
- ``nfldata`` ``games.csv`` — full schedule/results back to 1999 with
  rest days, division flags, QB starters, stadium/roof, and the CLOSING
  ``spread_line`` / ``total_line`` / moneylines. Powers both the
  schedule cache and the 2025-replay backtest (Smarter NFL PR 9).

Design: pure fetch/parse — no DB access. Cache-or-fetch orchestration
lives in ``services/nfl_advanced.py`` (mirrors the mlb_advanced split).
GitHub release downloads redirect to ``objects.githubusercontent.com``,
so ``follow_redirects`` is required. Rate limiting via the shared
process-level token bucket; the budget is generous (GitHub CDN) but a
bucket keeps a misbehaving retry loop from hammering it.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

import httpx

from app.clients._rate_limit import shared_bucket


logger = logging.getLogger(__name__)

NFLVERSE_RELEASE_BASE_URL = "https://github.com/nflverse/nflverse-data/releases/download"
NFLDATA_GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

_RATE_LIMIT_NAME = "nflverse"
_RATE_LIMIT_RPS = 1.0
_RATE_LIMIT_BURST = 2.0

# The depth-chart asset is ~50 MB; everything else is single-digit MB.
_REQUEST_TIMEOUT_SECONDS = 120.0


class NflverseClient:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http_client = http_client

    # -- transport -----------------------------------------------------------

    def _get_text(self, url: str) -> str:
        shared_bucket(_RATE_LIMIT_NAME, rps=_RATE_LIMIT_RPS, burst=_RATE_LIMIT_BURST).acquire()
        if self._http_client is not None:
            response = self._http_client.get(url, follow_redirects=True, timeout=_REQUEST_TIMEOUT_SECONDS)
        else:
            response = httpx.get(url, follow_redirects=True, timeout=_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text

    def _fetch_csv_rows(self, url: str) -> list[dict[str, Any]]:
        text = self._get_text(url)
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]

    @staticmethod
    def _release_url(release: str, asset: str) -> str:
        return f"{NFLVERSE_RELEASE_BASE_URL}/{release}/{asset}"

    # -- datasets ------------------------------------------------------------

    def fetch_weekly_player_stats(self, season: int) -> list[dict[str, Any]]:
        """Weekly player box stats (one row per player-game). Key columns:
        ``player_id`` (GSIS), ``player_display_name``, ``position``,
        ``team``, ``week``, ``season_type``, passing/rushing/receiving
        yardage + TD + EPA columns, ``targets``, ``target_share``."""
        return self._fetch_csv_rows(
            self._release_url("stats_player", f"stats_player_week_{season}.csv")
        )

    def fetch_snap_counts(self, season: int) -> list[dict[str, Any]]:
        """Per-game snap counts. Key columns: ``player`` (display name),
        ``pfr_player_id``, ``position``, ``team``, ``week``,
        ``offense_snaps``, ``offense_pct`` (0-100)."""
        return self._fetch_csv_rows(
            self._release_url("snap_counts", f"snap_counts_{season}.csv")
        )

    def fetch_latest_depth_charts(self, season: int) -> list[dict[str, Any]]:
        """Depth-chart rows for each team's LATEST snapshot only.

        The raw asset carries every historical daily snapshot (~550k
        rows / 50 MB); holding onto all of it would bloat both memory
        and the cache table, and the scoring path only ever needs the
        current chart. Single pass keeps, per team, only rows tagged
        with the maximum ``dt`` seen so far. Key columns per row:
        ``team``, ``player_name``, ``espn_id``, ``gsis_id``,
        ``pos_abb``, ``pos_slot``, ``pos_rank``, ``dt``.
        """
        text = self._get_text(self._release_url("depth_charts", f"depth_charts_{season}.csv"))
        reader = csv.DictReader(io.StringIO(text))
        latest_dt_by_team: dict[str, str] = {}
        rows_by_team: dict[str, list[dict[str, Any]]] = {}
        for row in reader:
            team = str(row.get("team") or "").strip()
            snapshot_dt = str(row.get("dt") or "").strip()
            if not team or not snapshot_dt:
                continue
            current = latest_dt_by_team.get(team)
            if current is None or snapshot_dt > current:
                latest_dt_by_team[team] = snapshot_dt
                rows_by_team[team] = [dict(row)]
            elif snapshot_dt == current:
                rows_by_team[team].append(dict(row))
        out: list[dict[str, Any]] = []
        for team in sorted(rows_by_team):
            out.extend(rows_by_team[team])
        return out

    def fetch_official_injuries(self, season: int) -> list[dict[str, Any]]:
        """Official club injury reports. Key columns: ``season``,
        ``week``, ``team``, ``gsis_id``, ``full_name``, ``position``,
        ``report_status`` (Out / Doubtful / Questionable / empty),
        ``practice_status``, ``report_primary_injury``."""
        return self._fetch_csv_rows(
            self._release_url("injuries", f"injuries_{season}.csv")
        )

    def fetch_team_week_stats(self, season: int) -> list[dict[str, Any]]:
        """Team-week OFFENSE aggregates (one row per team-game). Key
        columns: ``team``, ``opponent_team``, ``game_id``, ``week``,
        ``season_type``, ``attempts``, ``carries``, ``sacks_suffered``,
        ``passing_epa``, ``rushing_epa``. A team's DEFENSIVE numbers are
        the opponent's offense row for the same ``game_id``."""
        return self._fetch_csv_rows(
            self._release_url("stats_team", f"stats_team_week_{season}.csv")
        )

    def fetch_games(self, seasons: list[int] | None = None) -> list[dict[str, Any]]:
        """Schedule/results rows from Lee Sharpe's nfldata ``games.csv``
        (the canonical nflverse schedule source — one file, all seasons
        1999+). Optional ``seasons`` filter trims the parse output. Key
        columns: ``season``, ``game_type``, ``week``, ``gameday``,
        ``gametime``, ``away_team`` / ``home_team`` (+ scores),
        ``result`` (home margin), ``total``, ``away_rest`` /
        ``home_rest``, ``away_moneyline`` / ``home_moneyline``,
        ``spread_line`` (home-oriented closing spread), ``total_line``,
        ``div_game``, ``roof``, ``home_qb_name`` / ``away_qb_name``."""
        rows = self._fetch_csv_rows(NFLDATA_GAMES_URL)
        if seasons is None:
            return rows
        wanted = {str(season) for season in seasons}
        return [row for row in rows if str(row.get("season") or "") in wanted]

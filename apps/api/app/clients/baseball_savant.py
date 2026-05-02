"""Client for baseballsavant.mlb.com — MLB Statcast data.

Two surfaces:
  - statcast_search/csv — per-batted-ball events for a player/season.
    Returns CSV; we parse to dicts for downstream aggregation.
  - leaderboards (percentile rankings, expected stats, sprint speed,
    catcher framing, OAA, pitch arsenal) — return CSV or JSON.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from typing import Any

import httpx

from app.clients._rate_limit import parse_retry_after, shared_bucket


logger = logging.getLogger(__name__)


_BASE_URL = "https://baseballsavant.mlb.com"
_RATE_LIMIT_RPS = 2.0
_RATE_LIMIT_BURST = 4.0


def parse_csv_rows(payload: str) -> list[dict[str, str]]:
    """Parse a Savant CSV string into a list of column-keyed dicts."""
    if not payload:
        return []
    reader = csv.DictReader(io.StringIO(payload))
    return [dict(row) for row in reader]


class BaseballSavantClient:
    _MAX_ATTEMPTS = 3
    _BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0, 15.0)

    def __init__(self, http_client: httpx.Client | None = None, base_url: str | None = None) -> None:
        self._base_url = (base_url or _BASE_URL).rstrip("/")
        self._http_client = http_client
        self._bucket = shared_bucket("baseball_savant", _RATE_LIMIT_RPS, _RATE_LIMIT_BURST)

    # ------------------------------------------------------------------
    # Public methods

    def fetch_batter_statcast(self, mlb_player_id: str, season: int) -> str:
        """Per-batted-ball events for a batter for one season. Returns CSV text."""
        params = self._statcast_search_params(
            mlb_player_id=mlb_player_id,
            season=season,
            player_type="batter",
        )
        return self._get_csv("/statcast_search/csv", params)

    def fetch_pitcher_statcast(self, mlb_player_id: str, season: int) -> str:
        """Per-pitch events for a pitcher for one season. Returns CSV text."""
        params = self._statcast_search_params(
            mlb_player_id=mlb_player_id,
            season=season,
            player_type="pitcher",
        )
        return self._get_csv("/statcast_search/csv", params)

    def fetch_batter_percentile_rankings(self, season: int) -> str:
        """League-wide batter percentile rankings. CSV."""
        return self._get_csv(
            "/leaderboard/expected_statistics",
            {"type": "batter", "year": str(season), "filter": "", "csv": "true"},
        )

    def fetch_pitcher_percentile_rankings(self, season: int) -> str:
        """League-wide pitcher percentile rankings. CSV."""
        return self._get_csv(
            "/leaderboard/expected_statistics",
            {"type": "pitcher", "year": str(season), "filter": "", "csv": "true"},
        )

    def fetch_sprint_speed(self, season: int) -> str:
        return self._get_csv(
            "/leaderboard/sprint_speed",
            {"year": str(season), "min_drives": "0", "csv": "true"},
        )

    def fetch_outs_above_average(self, season: int) -> str:
        return self._get_csv(
            "/leaderboard/outs_above_average",
            {"year": str(season), "min_attempts": "0", "csv": "true"},
        )

    def fetch_pitch_arsenal_stats(self, season: int) -> str:
        """Per-pitcher per-pitch-type results: opp wOBA, putaway%, usage%, etc."""
        return self._get_csv(
            "/leaderboard/pitch-arsenal-stats",
            {"year": str(season), "min_pitches": "0", "csv": "true"},
        )

    # ------------------------------------------------------------------
    # Internal

    @staticmethod
    def _statcast_search_params(
        *, mlb_player_id: str, season: int, player_type: str
    ) -> dict[str, Any]:
        return {
            "all": "true",
            "hfPT": "",
            "hfAB": "",
            "hfBBT": "",
            "hfPR": "",
            "hfZ": "",
            "stadium": "",
            "hfBBL": "",
            "hfNewZones": "",
            "hfGT": "R|",
            "hfC": "",
            "hfSea": f"{season}|",
            "hfSit": "",
            "player_type": player_type,
            "hfOuts": "",
            "opponent": "",
            "pitcher_throws": "",
            "batter_stands": "",
            "hfSA": "",
            "game_date_gt": "",
            "game_date_lt": "",
            "hfInfield": "",
            "team": "",
            "position": "",
            "hfOutfield": "",
            "hfRO": "",
            "home_road": "",
            "hfFlag": "",
            "hfPull": "",
            "metric_1": "",
            "hfInn": "",
            "min_pitches": "0",
            "min_results": "0",
            "group_by": "name",
            "sort_col": "pitches",
            "player_event_sort": "api_p_release_speed",
            "sort_order": "desc",
            "min_pas": "0",
            "type": "details",
            "batters_lookup[]": mlb_player_id if player_type == "batter" else "",
            "pitchers_lookup[]": mlb_player_id if player_type == "pitcher" else "",
        }

    def _do_get(self, url: str, params: dict[str, Any], timeout: float) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.get(url, params=params, timeout=timeout)
        return httpx.get(url, params=params, timeout=timeout)

    def _get_csv(self, path: str, params: dict[str, Any]) -> str:
        url = f"{self._base_url}{path}"
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            self._bucket.acquire()
            try:
                response = self._do_get(url, params, timeout=30.0)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self._MAX_ATTEMPTS:
                    raise
                time.sleep(1.0 * attempt)
                continue

            if response.status_code == 429 and attempt < self._MAX_ATTEMPTS:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is None:
                    retry_after = self._BACKOFF_SECONDS[
                        min(attempt - 1, len(self._BACKOFF_SECONDS) - 1)
                    ]
                logger.warning("Savant 429 on %s (attempt %d); sleeping %.1fs", path, attempt, retry_after)
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            return response.text

        assert last_error is not None
        raise last_error

"""Client for stats.nba.com — NBA's free advanced-stats API.

The endpoint is undocumented and aggressively rate-limited. To avoid 429 bans:
  - Strict process-level token bucket (rps=0.6 default).
  - Exponential backoff with ``Retry-After`` honored on 429.
  - User-Agent rotation across a small allowlist of modern browsers.
  - Required NBA Stats headers (Origin, Referer, x-nba-stats-token, etc.).

Daily IP budget enforcement and the circuit breaker live in the orchestrator
(``app.services.advanced_stats``) — this client only does per-request fairness
and transient-failure retries.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from app.clients._rate_limit import parse_retry_after, shared_bucket
from app.config import get_settings


logger = logging.getLogger(__name__)


_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
)


def season_param(year: int) -> str:
    """Format an integer season-start year as the NBA Stats ``2024-25`` form."""
    suffix = (year + 1) % 100
    return f"{year}-{suffix:02d}"


def parse_result_set(payload: dict[str, Any], name: str | None = None) -> list[dict[str, Any]]:
    """Convert an NBA Stats ``resultSets[...]`` block into a list of dicts.

    NBA Stats responses are shaped as ``{"resultSets": [{"name": ..., "headers": [...],
    "rowSet": [[...], ...]}]}``. This helper picks the first matching result set
    (by ``name`` if supplied, else the first one) and zips headers with each row.
    """
    sets = payload.get("resultSets") or payload.get("resultSet") or []
    if isinstance(sets, dict):
        sets = [sets]
    chosen: dict[str, Any] | None = None
    for entry in sets:
        if not isinstance(entry, dict):
            continue
        if name is None or entry.get("name") == name:
            chosen = entry
            break
    if chosen is None and sets and isinstance(sets[0], dict):
        chosen = sets[0]
    if chosen is None:
        return []
    headers: list[str] = list(chosen.get("headers") or [])
    rows: list[list[Any]] = list(chosen.get("rowSet") or [])
    return [dict(zip(headers, row, strict=False)) for row in rows]


class NbaStatsRateLimitError(RuntimeError):
    """Raised when retries are exhausted on 429 responses from NBA Stats."""


class NbaStatsClient:
    _MAX_ATTEMPTS = 4
    _BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (10.0, 30.0, 90.0, 300.0)
    _MAX_BACKOFF_SECONDS = 300.0

    def __init__(self, http_client: httpx.Client | None = None, base_url: str | None = None) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.nba_stats_base_url).rstrip("/")
        self._http_client = http_client
        self._bucket = shared_bucket(
            "nba_stats",
            settings.nba_stats_rate_limit_rps,
            settings.nba_stats_rate_limit_burst,
        )

    # ------------------------------------------------------------------
    # Public methods

    def fetch_player_advanced_gamelog(
        self,
        player_id: str,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        """Fetch one player's game-by-game Advanced measure-type log."""
        return self._get(
            "/playergamelogs",
            {
                "PlayerID": str(player_id),
                "Season": season_param(season),
                "SeasonType": season_type,
                "MeasureType": "Advanced",
                "PerMode": "PerGame",
                "LastNGames": "0",
                "Month": "0",
                "OpponentTeamID": "0",
                "Period": "0",
                "LeagueID": "00",
            },
        )

    def fetch_team_advanced(
        self,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        """Fetch league-wide team Advanced stats for one season."""
        return self._get(
            "/leaguedashteamstats",
            {
                "MeasureType": "Advanced",
                "Season": season_param(season),
                "SeasonType": season_type,
                "PerMode": "PerGame",
                "LastNGames": "0",
                "Month": "0",
                "OpponentTeamID": "0",
                "Period": "0",
                "LeagueID": "00",
                "PaceAdjust": "N",
                "PlusMinus": "N",
                "Rank": "N",
                "PORound": "0",
                "Conference": "",
                "Division": "",
                "GameScope": "",
                "GameSegment": "",
                "Location": "",
                "Outcome": "",
                "PlayerExperience": "",
                "PlayerPosition": "",
                "SeasonSegment": "",
                "ShotClockRange": "",
                "StarterBench": "",
                "TeamID": "0",
                "TwoWay": "0",
                "VsConference": "",
                "VsDivision": "",
            },
        )

    def fetch_league_player_advanced(
        self,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        """Fetch league-wide per-player Advanced season averages.

        Used to compute league percentile breakpoints for advanced metrics.
        """
        return self._get(
            "/leaguedashplayerstats",
            {
                "MeasureType": "Advanced",
                "Season": season_param(season),
                "SeasonType": season_type,
                "PerMode": "PerGame",
                "LastNGames": "0",
                "Month": "0",
                "OpponentTeamID": "0",
                "Period": "0",
                "LeagueID": "00",
                "PaceAdjust": "N",
                "PlusMinus": "N",
                "Rank": "N",
                "PORound": "0",
                "College": "",
                "Conference": "",
                "Country": "",
                "DateFrom": "",
                "DateTo": "",
                "Division": "",
                "DraftPick": "",
                "DraftYear": "",
                "GameScope": "",
                "GameSegment": "",
                "Height": "",
                "Location": "",
                "Outcome": "",
                "PlayerExperience": "",
                "PlayerPosition": "",
                "SeasonSegment": "",
                "ShotClockRange": "",
                "StarterBench": "",
                "TeamID": "0",
                "TwoWay": "0",
                "VsConference": "",
                "VsDivision": "",
                "Weight": "",
            },
        )

    # ------------------------------------------------------------------
    # Internal

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Origin": "https://www.nba.com",
            "Referer": "https://www.nba.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "x-nba-stats-token": "true",
            "x-nba-stats-origin": "stats",
            "Connection": "keep-alive",
        }

    def _do_get(self, url: str, params: dict[str, Any], headers: dict[str, str], timeout: float) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.get(url, params=params, headers=headers, timeout=timeout)
        return httpx.get(url, params=params, headers=headers, timeout=timeout)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            self._bucket.acquire()
            try:
                response = self._do_get(url, params, self._headers(), timeout=20.0)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self._MAX_ATTEMPTS:
                    raise
                time.sleep(0.5 * attempt)
                continue

            if response.status_code == 429 and attempt < self._MAX_ATTEMPTS:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is None:
                    retry_after = self._BACKOFF_SCHEDULE_SECONDS[
                        min(attempt - 1, len(self._BACKOFF_SCHEDULE_SECONDS) - 1)
                    ]
                logger.warning("NBA Stats 429 on %s (attempt %d); sleeping %.1fs", path, attempt, retry_after)
                time.sleep(min(retry_after, self._MAX_BACKOFF_SECONDS))
                continue

            if response.status_code == 429:
                raise NbaStatsRateLimitError(f"NBA Stats rate-limited on {path} after {self._MAX_ATTEMPTS} attempts")

            response.raise_for_status()
            return response.json()

        assert last_error is not None
        raise last_error

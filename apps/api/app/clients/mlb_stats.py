"""Client for statsapi.mlb.com — MLB's free public stats API.

Open access (no auth required) but we apply a token bucket to be polite —
the endpoint is shared infrastructure and bursting can trigger 429s.

Public-API conventions on this host:
  - JSON throughout (no XML / CSV)
  - ``hydrate`` query string composes related resources (lineups,
    probablePitcher, weather, broadcasts, ...)
  - ``stats`` query string selects between sabermetrics, advanced,
    gameLog, byMonth, byDayOfWeek, vsTeam, vsPlayer, splits, etc.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import httpx

from app.clients._rate_limit import parse_retry_after, shared_bucket


logger = logging.getLogger(__name__)


_BASE_URL = "https://statsapi.mlb.com/api/v1"
_RATE_LIMIT_RPS = 5.0
_RATE_LIMIT_BURST = 10.0


class MlbStatsClient:
    _MAX_ATTEMPTS = 4
    _BACKOFF_SECONDS: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0)

    def __init__(self, http_client: httpx.Client | None = None, base_url: str | None = None) -> None:
        self._base_url = (base_url or _BASE_URL).rstrip("/")
        self._http_client = http_client
        self._bucket = shared_bucket("mlb_stats", _RATE_LIMIT_RPS, _RATE_LIMIT_BURST)

    # ------------------------------------------------------------------
    # Public methods — players

    def fetch_player_sabermetrics(self, person_id: str, season: int) -> dict[str, Any]:
        """Sabermetrics: wOBA, wRC+, ISO, BABIP, OPS+, etc."""
        return self._get(
            f"/people/{person_id}/stats",
            {"stats": "sabermetrics", "season": str(season), "group": "hitting,pitching"},
        )

    def fetch_player_hitting_advanced(self, person_id: str, season: int) -> dict[str, Any]:
        """Advanced batting: OBP, slugging, OPS, plate appearances."""
        return self._get(
            f"/people/{person_id}/stats",
            {"stats": "season", "season": str(season), "group": "hitting", "sportId": "1"},
        )

    def fetch_pitcher_sabermetrics(self, person_id: str, season: int) -> dict[str, Any]:
        """Pitcher sabermetrics: FIP, K/9, BB/9, HR/9, WHIP."""
        return self._get(
            f"/people/{person_id}/stats",
            {"stats": "sabermetrics,season", "season": str(season), "group": "pitching"},
        )

    def fetch_player_gamelog(self, person_id: str, season: int, group: str = "hitting") -> dict[str, Any]:
        """Per-game stats for a player (group=hitting|pitching)."""
        return self._get(
            f"/people/{person_id}/stats",
            {"stats": "gameLog", "season": str(season), "group": group, "sportId": "1"},
        )

    def fetch_player_splits(
        self,
        person_id: str,
        season: int,
        *,
        split_kind: str = "vsLeftRight",
        group: str = "hitting",
    ) -> dict[str, Any]:
        """Splits: vsLeftRight, vsTeamSplits, byMonth, byDayOfWeek, homeAndAway, dayNight."""
        return self._get(
            f"/people/{person_id}/stats",
            {
                "stats": split_kind,
                "season": str(season),
                "group": group,
                "sportId": "1",
            },
        )

    def search_player(self, full_name: str, *, sport_ids: str = "1") -> dict[str, Any]:
        """Resolve a player name to MLB Stats PERSON_ID."""
        return self._get(
            "/people/search",
            {"names": full_name, "sportIds": sport_ids},
        )

    # ------------------------------------------------------------------
    # Public methods — teams

    def fetch_team_gamelog(self, team_id: str, season: int) -> dict[str, Any]:
        """Per-game team batting + pitching context."""
        return self._get(
            f"/teams/{team_id}/stats",
            {"stats": "gameLog", "season": str(season), "group": "hitting,pitching", "sportId": "1"},
        )

    def fetch_team_roster(self, team_id: str, season: int | None = None) -> dict[str, Any]:
        """Active roster for a team."""
        params: dict[str, Any] = {"rosterType": "active"}
        if season is not None:
            params["season"] = str(season)
        return self._get(f"/teams/{team_id}/roster", params)

    def fetch_team_injury_report(self, team_id: str) -> dict[str, Any]:
        """10-day, 60-day IL + day-to-day."""
        return self._get(
            f"/teams/{team_id}/roster",
            {"rosterType": "injuryReport"},
        )

    def fetch_all_teams(self, season: int, sport_id: str = "1") -> dict[str, Any]:
        return self._get("/teams", {"season": str(season), "sportId": sport_id})

    # ------------------------------------------------------------------
    # Public methods — schedule + venues + lineups + weather

    def fetch_schedule(
        self,
        target_date: date,
        *,
        hydrate: str = "lineups,probablePitcher,weather,broadcasts",
        sport_id: str = "1",
    ) -> dict[str, Any]:
        return self._get(
            "/schedule",
            {
                "sportId": sport_id,
                "date": target_date.strftime("%Y-%m-%d"),
                "hydrate": hydrate,
            },
        )

    def fetch_venue_metadata(self, venue_id: str) -> dict[str, Any]:
        """Venue details: dimensions, surface, roof type, location."""
        return self._get(f"/venues/{venue_id}", {"hydrate": "fieldInfo,location"})

    # ------------------------------------------------------------------
    # Internal

    def _do_get(self, url: str, params: dict[str, Any], timeout: float) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.get(url, params=params, timeout=timeout)
        return httpx.get(url, params=params, timeout=timeout)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            self._bucket.acquire()
            try:
                response = self._do_get(url, params, timeout=20.0)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self._MAX_ATTEMPTS:
                    raise
                time.sleep(0.5 * attempt)
                continue

            if response.status_code == 429 and attempt < self._MAX_ATTEMPTS:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is None:
                    retry_after = self._BACKOFF_SECONDS[
                        min(attempt - 1, len(self._BACKOFF_SECONDS) - 1)
                    ]
                # Clamp the server-controlled Retry-After: an upstream/CDN
                # incident can emit a huge value that would freeze the refresh
                # worker for hours (kalshi/nba_stats clamp the same way).
                retry_after = min(retry_after, 30.0)
                logger.warning("MLB Stats 429 on %s (attempt %d); sleeping %.1fs", path, attempt, retry_after)
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            return response.json()

        assert last_error is not None
        raise last_error

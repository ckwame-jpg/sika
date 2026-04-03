from datetime import date, timedelta
import json
import logging
import re
from typing import Any

import httpx


ESPN_SEARCH_URL = "https://site.api.espn.com/apis/search/v2"
ESPN_SEARCH_SLUGS = {
    "NBA": "nba",
    "NFL": "nfl",
    "MLB": "mlb",
    "SOCCER": "soccer",
    "TENNIS": "tennis",
    "UFC": "mma",
}
ESPN_SCOREBOARD_URLS = {
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
}
ESPN_GAMELOG_URLS = {
    "NBA": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{athlete_id}/gamelog",
    "NFL": "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/gamelog",
    "MLB": "https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{athlete_id}/gamelog",
}

ESPN_LEAGUE_NAMES = {
    "NBA": "NBA",
    "NFL": "NFL",
    "MLB": "MLB",
}

ESPN_SOCCER_PLAYER_PAGE_URL = "https://www.espn.com/soccer/player/_/id/{athlete_id}/{slug}"
ESPN_MMA_FIGHTER_HISTORY_PAGE_URL = "https://www.espn.com/mma/fighter/history/_/id/{athlete_id}/{slug}"
ESPN_TENNIS_ATHLETE_URL = "https://sports.core.api.espn.com/v2/sports/tennis/athletes/{athlete_id}?lang=en&region=us"
_ESPN_FITT_STATE_RE = re.compile(r"window\['__espnfitt__'\]=(\{.*?\});</script>", re.DOTALL)
logger = logging.getLogger(__name__)


class EspnPublicClient:
    def search_player(self, query: str, sport_key: str = "NBA") -> dict[str, Any]:
        normalized_sport = sport_key.upper()
        if normalized_sport not in ESPN_SEARCH_SLUGS:
            raise ValueError(f"ESPN player search is not configured for {sport_key}")

        response = httpx.get(ESPN_SEARCH_URL, params={"query": query}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        for result in payload.get("results") or []:
            if result.get("type") != "player":
                continue
            for player in result.get("contents") or []:
                if not self._matches_sport(player, normalized_sport):
                    continue
                athlete_id = self._athlete_id_from_player_result(player)
                if athlete_id:
                    web_link = ((player.get("link") or {}).get("web")) or ""
                    return {
                        "athlete_id": athlete_id,
                        "sport_key": normalized_sport,
                        "display_name": player.get("displayName") or query,
                        "team_name": player.get("subtitle"),
                        "headshot_url": ((player.get("image") or {}).get("default")),
                        "default_league_slug": player.get("defaultLeagueSlug"),
                        "page_slug": self._player_slug_from_web_link(web_link),
                        "raw": player,
                    }

        raise LookupError(f"No {normalized_sport} player found for query: {query}")

    def fetch_player_gamelog(self, sport_key: str, athlete_id: str, season: int) -> dict[str, Any]:
        if sport_key.upper() not in ESPN_GAMELOG_URLS:
            raise ValueError(f"ESPN game log is not configured for {sport_key}")

        response = httpx.get(
            ESPN_GAMELOG_URLS[sport_key.upper()].format(athlete_id=athlete_id),
            params={"season": season},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def fetch_soccer_player_overview(self, athlete_id: str, page_slug: str | None = None) -> dict[str, Any]:
        slug = page_slug or athlete_id
        return self._fetch_fitt_page(
            ESPN_SOCCER_PLAYER_PAGE_URL.format(athlete_id=athlete_id, slug=slug),
            f"Could not extract soccer overview payload for athlete {athlete_id}",
        )

    def fetch_mma_fighter_history(self, athlete_id: str, page_slug: str | None = None) -> dict[str, Any]:
        slug = page_slug or athlete_id
        return self._fetch_fitt_page(
            ESPN_MMA_FIGHTER_HISTORY_PAGE_URL.format(athlete_id=athlete_id, slug=slug),
            f"Could not extract MMA history payload for athlete {athlete_id}",
        )

    def fetch_tennis_athlete_profile(self, athlete_id: str) -> dict[str, Any]:
        return self.fetch_json_ref(ESPN_TENNIS_ATHLETE_URL.format(athlete_id=athlete_id))

    def fetch_json_ref(self, ref_url: str) -> dict[str, Any]:
        normalized_url = ref_url.replace("http://", "https://")
        response = httpx.get(normalized_url, timeout=20)
        response.raise_for_status()
        return response.json()

    def _fetch_fitt_page(self, url: str, error_message: str) -> dict[str, Any]:
        response = httpx.get(url, timeout=20)
        response.raise_for_status()
        match = _ESPN_FITT_STATE_RE.search(response.text)
        if not match:
            raise LookupError(error_message)
        return json.loads(match.group(1))

    def fetch_events_for_day(self, sport_key: str, target_day: date) -> list[dict[str, Any]]:
        base_url = ESPN_SCOREBOARD_URLS[sport_key]
        response = httpx.get(
            base_url,
            params={"dates": target_day.strftime("%Y%m%d")},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return [self._normalize_event(sport_key, raw_event) for raw_event in payload.get("events") or []]

    def fetch_events_window_with_diagnostics(
        self,
        sport_key: str,
        start_day: date,
        end_day: date,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        current = start_day
        while current <= end_day:
            try:
                events.extend(self.fetch_events_for_day(sport_key, current))
            except httpx.HTTPError as exc:
                message = str(exc).strip() or exc.__class__.__name__
                errors.append(f"{current.isoformat()}: {exc.__class__.__name__}: {message}")
                logger.warning("ESPN fetch failed for %s on %s: %s", sport_key, current.isoformat(), message)
            current += timedelta(days=1)
        return events, errors

    def fetch_events_window(self, sport_key: str, start_day: date, end_day: date) -> list[dict[str, Any]]:
        events, _ = self.fetch_events_window_with_diagnostics(sport_key, start_day, end_day)
        return events

    def _normalize_event(self, sport_key: str, raw_event: dict[str, Any]) -> dict[str, Any]:
        competition = (raw_event.get("competitions") or [{}])[0]
        competitors = competition.get("competitors") or []
        home = next((item for item in competitors if item.get("homeAway") == "home"), competitors[0] if competitors else {})
        away = next((item for item in competitors if item.get("homeAway") == "away"), competitors[1] if len(competitors) > 1 else {})
        status = raw_event.get("status", {})
        status_type = status.get("type", {})
        normalized_status = self._normalize_status_type(status_type)

        def team_name(competitor: dict[str, Any]) -> str:
            team = competitor.get("team") or competitor.get("athlete") or {}
            return team.get("displayName") or team.get("shortDisplayName") or team.get("abbreviation") or ""

        def team_short_name(competitor: dict[str, Any]) -> str | None:
            team = competitor.get("team") or competitor.get("athlete") or {}
            return team.get("shortDisplayName") or team.get("abbreviation")

        def score_value(competitor: dict[str, Any]) -> str | None:
            score = competitor.get("score")
            return str(score) if score not in {None, ""} else None

        return {
            "idEvent": str(raw_event.get("id") or ""),
            "idLeague": str(((raw_event.get("leagues") or [{}])[0]).get("id") or sport_key),
            "strLeague": ESPN_LEAGUE_NAMES[sport_key],
            "strHomeTeam": team_name(home),
            "strAwayTeam": team_name(away),
            "strHomeTeamShort": team_short_name(home),
            "strAwayTeamShort": team_short_name(away),
            "idHomeTeam": str(((home.get("team") or {}).get("id")) or f"{raw_event.get('id')}:home"),
            "idAwayTeam": str(((away.get("team") or {}).get("id")) or f"{raw_event.get('id')}:away"),
            "strEvent": raw_event.get("name") or raw_event.get("shortName") or f"{team_name(away)} at {team_name(home)}",
            "strTimestamp": raw_event.get("date"),
            "dateEvent": (raw_event.get("date") or "").split("T", 1)[0] or None,
            "intHomeScore": score_value(home),
            "intAwayScore": score_value(away),
            "strStatus": normalized_status,
            "strStatusDetail": status_type.get("description") or status.get("detail"),
            "source": "espn_public",
            "raw": raw_event,
        }

    @staticmethod
    def _normalize_status_type(status_type: dict[str, Any]) -> str:
        state = str(status_type.get("state") or "").strip().lower()
        name = str(status_type.get("name") or "").strip().lower()
        description = " ".join(
            str(part or "").strip().lower()
            for part in (status_type.get("description"), status_type.get("detail"), status_type.get("shortDetail"))
            if part
        )
        tokens = " ".join(part for part in (state, name, description) if part)

        if "postpon" in tokens:
            return "postponed"
        if "cancel" in tokens:
            return "cancelled"
        if status_type.get("completed") is True or state == "post" or any(term in tokens for term in ("final", "completed", "full time", "full-time")):
            return "completed"
        if state == "in" or any(
            term in tokens
            for term in ("status_in", "live", "progress", "halftime", "half-time", "intermission", "quarter", "period", "inning", "extra time", "overtime")
        ):
            return "in_progress"
        return "scheduled"

    @staticmethod
    def _athlete_id_from_player_result(player: dict[str, Any]) -> str | None:
        uid = player.get("uid") or ""
        match = re.search(r"~a:(\d+)$", uid)
        if match:
            return match.group(1)
        web_link = ((player.get("link") or {}).get("web")) or ""
        match = re.search(r"/id/(\d+)/", web_link)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _player_slug_from_web_link(web_link: str) -> str | None:
        match = re.search(r"/id/\d+/([^/?#]+)", web_link)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _matches_sport(player: dict[str, Any], sport_key: str) -> bool:
        if sport_key in {"SOCCER", "TENNIS"}:
            return (player.get("sport") or "").lower() == sport_key.lower()
        if sport_key == "UFC":
            return (player.get("sport") or "").lower() == "mma"
        expected_slug = ESPN_SEARCH_SLUGS[sport_key]
        return (player.get("defaultLeagueSlug") or "").lower() == expected_slug

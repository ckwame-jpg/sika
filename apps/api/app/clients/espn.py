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
    "WNBA": "wnba",
    "TENNIS": "tennis",
}
ESPN_SCOREBOARD_URLS = {
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "WNBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
}
ESPN_GAMELOG_URLS = {
    "NBA": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{athlete_id}/gamelog",
    "NFL": "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/gamelog",
    "MLB": "https://site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{athlete_id}/gamelog",
    "WNBA": "https://site.web.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{athlete_id}/gamelog",
}
ESPN_TEAM_SCHEDULE_URLS = {
    "NBA": "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/schedule",
    "NFL": "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule",
    "MLB": "https://site.web.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{team_id}/schedule",
    "WNBA": "https://site.web.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team_id}/schedule",
}


# Bug #13: prop metadata sends ``team_hint`` as a three-letter ticker
# abbreviation (e.g. ``NYK``, ``BOS``) while ESPN's player search payload
# only emits the team display name in ``subtitle`` (e.g. ``New York Knicks``,
# ``Boston Celtics``). A naïve substring match fails on every abbreviation
# hint we send in production. These mappings let us resolve the hint to its
# full team name before matching. Spring training / international team
# variants without entries here fall through to a substring check on the
# raw hint, which still catches "Celtics" / "Knicks" forms.
ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME: dict[str, dict[str, str]] = {
    "NBA": {
        "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
        "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
        "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
        "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
        "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
        "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
        "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
        "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
        "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
        "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
    },
    "MLB": {
        "ARI": "Arizona Diamondbacks", "AZ": "Arizona Diamondbacks", "ATL": "Atlanta Braves", "BAL": "Baltimore Orioles",
        "BOS": "Boston Red Sox", "CHC": "Chicago Cubs", "CHW": "Chicago White Sox", "CWS": "Chicago White Sox",
        "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians", "COL": "Colorado Rockies",
        "DET": "Detroit Tigers", "HOU": "Houston Astros", "KC": "Kansas City Royals",
        "KCR": "Kansas City Royals", "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
        "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins",
        "NYM": "New York Mets", "NYY": "New York Yankees", "OAK": "Oakland Athletics",
        "ATH": "Oakland Athletics", "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
        "SD": "San Diego Padres", "SDP": "San Diego Padres", "SF": "San Francisco Giants",
        "SFG": "San Francisco Giants", "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals",
        "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays", "TEX": "Texas Rangers",
        "TOR": "Toronto Blue Jays", "WSH": "Washington Nationals", "WSN": "Washington Nationals",
    },
    # WNBA — 2026 season (15 teams, including expansion Toronto Tempo +
    # Portland Fire). ESPN uses 2-letter codes for NY / LV / LA / GS;
    # Kalshi ticker conventions for those four aren't fully confirmed at
    # the time of MVP scaffolding (per SMARTER_WNBA_PREP.md §2). The
    # substring fallback in ``_team_hint_matches_subtitle`` handles
    # remaining mismatch surface area at the resolver level.
    "WNBA": {
        "ATL": "Atlanta Dream", "CHI": "Chicago Sky", "CON": "Connecticut Sun",
        "IND": "Indiana Fever", "NY": "New York Liberty", "NYL": "New York Liberty",
        "TOR": "Toronto Tempo", "WSH": "Washington Mystics", "DAL": "Dallas Wings",
        "GS": "Golden State Valkyries", "GSV": "Golden State Valkyries",
        "LV": "Las Vegas Aces", "LVA": "Las Vegas Aces",
        "LA": "Los Angeles Sparks", "LAS": "Los Angeles Sparks",
        "MIN": "Minnesota Lynx", "PHX": "Phoenix Mercury",
        "POR": "Portland Fire", "SEA": "Seattle Storm",
    },
}


def _team_hint_matches_subtitle(team_hint: str, subtitle: str, sport_key: str) -> bool:
    """Return True when ``team_hint`` plausibly identifies the team whose
    display name lives in ``subtitle``. Handles three cases:

    1. Direct ticker abbreviation (``"NYK"``, ``"BOS"``, ``"AZ"``) — looked
       up in ``ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME`` and matched against
       the subtitle.
    2. Kalshi prop-ticker codes like ``"AZN"``, ``"KCB"``, ``"SDM"`` — the
       first two characters encode the team and the third is a per-player
       discriminator. Sika's ``copilot_subject_team`` metadata is captured
       straight from these tickers, so the 2-char prefix is what we have
       to match on.
    3. Substring fallback for hints already given as full / partial names
       (``"Celtics"`` matches ``"Boston Celtics"``).
    """
    if not team_hint or not subtitle:
        return False
    normalized_hint = team_hint.strip().upper()
    normalized_subtitle = subtitle.strip().lower()
    abbreviation_map = ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME.get(sport_key.upper(), {})
    # 1. Direct abbreviation
    full_name = abbreviation_map.get(normalized_hint)
    if full_name and full_name.lower() in normalized_subtitle:
        return True
    # 2. Two-character prefix (Kalshi prop-ticker codes)
    if len(normalized_hint) >= 2:
        prefix_name = abbreviation_map.get(normalized_hint[:2])
        if prefix_name and prefix_name.lower() in normalized_subtitle:
            return True
    # 3. Substring fallback (handles already-full hints like "Celtics")
    lowered_hint = normalized_hint.lower()
    return lowered_hint in normalized_subtitle or normalized_subtitle in lowered_hint

ESPN_LEAGUE_NAMES = {
    "NBA": "NBA",
    "NFL": "NFL",
    "MLB": "MLB",
    "WNBA": "WNBA",
}

# ESPN's basketball-league URL slug per sport (used by the
# generalized ``fetch_injury_report``). NBA was the only entry until
# Smarter WNBA PR 7 added WNBA; ESPN's per-team injury response schema
# is identical across both leagues, so the parser is shared in
# ``services/nba_injury_report.py``.
_INJURY_REPORT_LEAGUE_SLUGS: dict[str, str] = {
    "NBA": "nba",
    "WNBA": "wnba",
}

ESPN_TENNIS_ATHLETE_URL = "https://sports.core.api.espn.com/v2/sports/tennis/athletes/{athlete_id}?lang=en&region=us"
_ESPN_FITT_STATE_RE = re.compile(r"window\['__espnfitt__'\]=(\{.*?\});</script>", re.DOTALL)
logger = logging.getLogger(__name__)


class EspnPublicClient:
    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http_client = http_client

    def _get(self, url: str, **kwargs):
        if self._http_client is not None:
            return self._http_client.get(url, **kwargs)
        return httpx.get(url, **kwargs)

    def fetch_injury_report(self, sport_key: str = "NBA") -> dict[str, Any]:
        """Fetch ESPN's current injury report for ``sport_key``.

        Generalizes the Smarter #17 NBA fetcher to also cover WNBA
        (Smarter WNBA PR 7). ESPN's per-team response shape is
        identical across NBA / WNBA (verified in
        ``SMARTER_WNBA_PREP.md`` §3) so the parser in
        ``services/nba_injury_report.py`` is shared.

        Raises ``httpx.HTTPStatusError`` on 4xx/5xx so the loader's
        exception path can fall back to the cached payload and record
        the failure on the upstream-health board.
        """
        normalized = sport_key.upper()
        if normalized not in _INJURY_REPORT_LEAGUE_SLUGS:
            raise ValueError(
                f"ESPN injury report is not configured for sport_key={sport_key!r}"
            )
        league_slug = _INJURY_REPORT_LEAGUE_SLUGS[normalized]
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
            f"{league_slug}/injuries"
        )
        response = self._get(url, timeout=20)
        response.raise_for_status()
        return response.json()

    def fetch_nba_injury_report(self) -> dict[str, Any]:
        """Smarter #17 phase 2 — thin wrapper around
        :meth:`fetch_injury_report` preserved for backwards compat with
        existing callers (Smarter #17's loader, upstream-health tests)."""
        return self.fetch_injury_report("NBA")

    def fetch_wnba_injury_report(self) -> dict[str, Any]:
        """Smarter WNBA PR 7 — thin wrapper around
        :meth:`fetch_injury_report` for the WNBA loader."""
        return self.fetch_injury_report("WNBA")

    def search_player(
        self,
        query: str,
        sport_key: str = "NBA",
        *,
        team_hint: str | None = None,
    ) -> dict[str, Any]:
        """Return the best player match for the query.

        Bug #13: when ``team_hint`` is provided and ESPN returns multiple
        candidates (same name, different teams), prefer the candidate
        whose ``subtitle`` (team display name) contains the hint
        case-insensitively. Falls back to the first candidate and logs a
        warning when no team match is found, so silent wrong-athlete
        attribution is observable.
        """
        normalized_sport = sport_key.upper()
        if normalized_sport not in ESPN_SEARCH_SLUGS:
            raise ValueError(f"ESPN player search is not configured for {sport_key}")

        response = self._get(ESPN_SEARCH_URL, params={"query": query}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        candidates: list[dict[str, Any]] = []
        for result in payload.get("results") or []:
            if result.get("type") != "player":
                continue
            for player in result.get("contents") or []:
                if not self._matches_sport(player, normalized_sport):
                    continue
                athlete_id = self._athlete_id_from_player_result(player)
                if not athlete_id:
                    continue
                web_link = ((player.get("link") or {}).get("web")) or ""
                candidates.append(
                    {
                        "athlete_id": athlete_id,
                        "sport_key": normalized_sport,
                        "display_name": player.get("displayName") or query,
                        "team_name": player.get("subtitle"),
                        "headshot_url": ((player.get("image") or {}).get("default")),
                        "default_league_slug": player.get("defaultLeagueSlug"),
                        "page_slug": self._player_slug_from_web_link(web_link),
                        "raw": player,
                    }
                )

        if not candidates:
            raise LookupError(f"No {normalized_sport} player found for query: {query}")

        if team_hint:
            for candidate in candidates:
                if _team_hint_matches_subtitle(
                    str(team_hint),
                    str(candidate.get("team_name") or ""),
                    normalized_sport,
                ):
                    return candidate
            logger.warning(
                "ESPN %s player search for %r did not find a team_hint=%r match across %d candidates; falling back to first",
                normalized_sport,
                query,
                team_hint,
                len(candidates),
            )

        return candidates[0]

    def search_team(self, query: str, sport_key: str = "NBA") -> dict[str, Any]:
        """Mirror of ``search_player`` for team-level lookups.

        ESPN's ``/apis/search/v2`` endpoint returns ``type: "team"`` results
        alongside player results. We filter to the matching sport slug and
        return the first viable match, normalized to the same shape the
        callers expect.
        """
        normalized_sport = sport_key.upper()
        if normalized_sport not in ESPN_SEARCH_SLUGS:
            raise ValueError(f"ESPN team search is not configured for {sport_key}")

        response = self._get(ESPN_SEARCH_URL, params={"query": query}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        for result in payload.get("results") or []:
            if result.get("type") != "team":
                continue
            for team in result.get("contents") or []:
                if not self._matches_sport(team, normalized_sport):
                    continue
                team_id = self._team_id_from_result(team)
                if team_id:
                    return {
                        "team_id": team_id,
                        "sport_key": normalized_sport,
                        "display_name": team.get("displayName") or query,
                        "abbreviation": team.get("abbreviation"),
                        "logo_url": ((team.get("image") or {}).get("default")),
                        "raw": team,
                    }

        raise LookupError(f"No {normalized_sport} team found for query: {query}")

    def fetch_team_schedule(self, sport_key: str, team_id: str, season: int | None = None) -> dict[str, Any]:
        """Fetch ESPN's team-schedule payload (includes past + upcoming events)."""
        normalized_sport = sport_key.upper()
        if normalized_sport not in ESPN_TEAM_SCHEDULE_URLS:
            raise ValueError(f"ESPN team schedule is not configured for {sport_key}")
        params: dict[str, Any] = {}
        if season is not None:
            params["season"] = season
        response = self._get(
            ESPN_TEAM_SCHEDULE_URLS[normalized_sport].format(team_id=team_id),
            params=params or None,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def fetch_player_gamelog(self, sport_key: str, athlete_id: str, season: int) -> dict[str, Any]:
        if sport_key.upper() not in ESPN_GAMELOG_URLS:
            raise ValueError(f"ESPN game log is not configured for {sport_key}")

        response = self._get(
            ESPN_GAMELOG_URLS[sport_key.upper()].format(athlete_id=athlete_id),
            params={"season": season},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def fetch_tennis_athlete_profile(self, athlete_id: str) -> dict[str, Any]:
        return self.fetch_json_ref(ESPN_TENNIS_ATHLETE_URL.format(athlete_id=athlete_id))

    def fetch_json_ref(self, ref_url: str) -> dict[str, Any]:
        normalized_url = ref_url.replace("http://", "https://")
        response = self._get(normalized_url, timeout=20)
        response.raise_for_status()
        return response.json()

    def _fetch_fitt_page(self, url: str, error_message: str) -> dict[str, Any]:
        response = self._get(url, timeout=20)
        response.raise_for_status()
        match = _ESPN_FITT_STATE_RE.search(response.text)
        if not match:
            raise LookupError(error_message)
        return json.loads(match.group(1))

    def fetch_events_for_day(self, sport_key: str, target_day: date) -> list[dict[str, Any]]:
        base_url = ESPN_SCOREBOARD_URLS[sport_key]
        response = self._get(
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
    def _team_id_from_result(team: dict[str, Any]) -> str | None:
        """Extract the team id from a ``type: "team"`` search result.

        ESPN packs the id into the ``uid`` (``s:40~l:46~t:5``) or the
        ``link.web`` URL (``/team/_/name/cle/...``). The uid path is more
        stable; fall back to the link parse only if the uid is missing.
        """
        uid = team.get("uid") or ""
        match = re.search(r"~t:(\d+)$", uid)
        if match:
            return match.group(1)
        identity = team.get("id")
        if identity:
            return str(identity)
        return None

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
        if sport_key == "TENNIS":
            return (player.get("sport") or "").lower() == "tennis"
        expected_slug = ESPN_SEARCH_SLUGS[sport_key]
        return (player.get("defaultLeagueSlug") or "").lower() == expected_slug

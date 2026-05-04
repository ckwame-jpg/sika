"""Drop-in replacement for ``NbaStatsClient`` backed by basketball-reference.com.

stats.nba.com is unreachable from many home / cloud egresses. Basketball
Reference scrapes the same advanced metrics from public HTML pages and
returns them in the same NBA-Stats-shaped envelope so the cached loaders
in :mod:`app.services.advanced_stats` keep working unchanged.

Each fetch method returns a dict shaped like
``{"resultSets": [{"name": ..., "headers": [...], "rowSet": [[...], ...]}]}``
with NBA-Stats-style snake-uppercase keys (``TS_PCT``, ``OFF_RATING``, ...).
Long-tail endpoints without a clean BBR equivalent (hustle, tracking,
clutch, defense, lineups) return successful empty result sets so the
upstream breaker stays closed and the proxy-fallback path takes over at
scoring time.

PERSON_ID semantics: when the source is BBR, each player's ``PERSON_ID``
is the BBR slug (e.g., ``jamesle01``). The roster snapshot in
``fetch_common_all_players`` populates the same slugs that the per-player
gamelog accepts, keeping the resolver chain consistent end-to-end.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from lxml import html

from app.clients._rate_limit import shared_bucket
from app.config import get_settings


logger = logging.getLogger(__name__)


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Bounded so a black-holed network can't outlive the worker watchdog.
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)
_MAX_ATTEMPTS = 2


class BasketballReferenceClient:
    """Public methods mirror :class:`NbaStatsClient`'s 11-method shape."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        base_url: str | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.basketball_reference_base_url).rstrip("/")
        self._http_client = http_client
        self._bucket = shared_bucket(
            "basketball_reference",
            settings.basketball_reference_rate_limit_rps,
            settings.basketball_reference_rate_limit_burst,
        )

    # ------------------------------------------------------------------
    # Public methods — implemented (6)

    def fetch_player_advanced_gamelog(
        self,
        player_id: str,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        """Per-game advanced metrics log for one player.

        ``player_id`` is interpreted as a BBR slug (e.g., ``jamesle01``).
        Empty ``player_id`` or 404 returns an empty envelope rather than
        raising — the upstream loader writes an empty cache row and the
        breaker stays closed.
        """
        slug = (player_id or "").strip().lower()
        if not slug:
            return _empty_envelope("PlayerGameLogs")
        end_year = int(season) + 1
        # BBR sometimes serves an HTTP 200 with an empty page for
        # not-yet-started seasons, so we have to fall back based on
        # parsed-row count rather than just status code.
        rows: list[dict[str, Any]] = []
        for offset in (0, -1):
            path = f"/players/{slug[:1]}/{slug}/gamelog-advanced/{end_year + offset}"
            doc = self._fetch_html_or_empty(path)
            if doc is None:
                continue
            for table_id in ("player_game_log_adv_reg", "pgl_advanced", "advanced_pgl"):
                _, parsed = _parse_table(doc, table_id)
                if parsed:
                    rows = parsed
                    break
            if rows:
                break
        if not rows:
            return _empty_envelope("PlayerGameLogs")

        out_headers = [
            "GAME_DATE",
            "MATCHUP",
            "MIN",
            "TS_PCT",
            "EFG_PCT",
            "USG_PCT",
            "OFF_RATING",
            "DEF_RATING",
            "NET_RATING",
            "AST_PCT",
            "OREB_PCT",
            "DREB_PCT",
            "REB_PCT",
            "PACE",
            "PIE",
        ]
        out_rows: list[list[Any]] = []
        for row in rows:
            off_rtg = _safe_float_str(row.get("off_rtg"))
            def_rtg = _safe_float_str(row.get("def_rtg"))
            opp = row.get("opp_name_abbr") or row.get("opp_id")
            out_rows.append([
                row.get("date") or row.get("date_game"),
                _build_matchup(opp, row.get("game_location")),
                _parse_minutes(row.get("mp")),
                _safe_float_str(row.get("ts_pct")),
                _safe_float_str(row.get("efg_pct")),
                _safe_float_str(row.get("usg_pct")),
                off_rtg,
                def_rtg,
                _diff(off_rtg, def_rtg),
                _safe_float_str(row.get("ast_pct")),
                _safe_float_str(row.get("orb_pct")),
                _safe_float_str(row.get("drb_pct")),
                _safe_float_str(row.get("trb_pct")),
                _safe_float_str(row.get("pace")),
                # PIE has no clean BBR equivalent — substitute BPM.
                _safe_float_str(row.get("bpm")),
            ])
        return {
            "resultSets": [
                {"name": "PlayerGameLogs", "headers": out_headers, "rowSet": out_rows}
            ]
        }

    def fetch_team_advanced(
        self,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        """League-wide team advanced stats for one season."""
        end_year = int(season) + 1
        doc, _offset = self._fetch_with_season_fallback(
            lambda offset: f"/leagues/NBA_{end_year + offset}.html"
        )
        if doc is None:
            return _empty_envelope("LeagueDashTeamStats")

        rows: list[dict[str, Any]] = []
        for table_id in ("advanced-team", "advanced_team", "advanced"):
            _, parsed = _parse_table(doc, table_id)
            if parsed:
                rows = parsed
                break
        if not rows:
            return _empty_envelope("LeagueDashTeamStats")

        # The advanced-team table only carries the full team name. Build a
        # name → abbreviation map from per_game-team on the same page so
        # ``TEAM_ID`` can be the abbreviation that team-gamelog URLs need.
        abbr_by_name: dict[str, str] = {}
        for tr in doc.xpath("//table[@id='per_game-team']//tbody//tr"):
            cell_text = "".join(tr.xpath(".//*[@data-stat='team']")[0].itertext()).strip() if tr.xpath(".//*[@data-stat='team']") else ""
            name = cell_text.rstrip("*").strip()
            link = tr.xpath(".//*[@data-stat='team']/a/@href")
            if name and link:
                # /teams/DEN/2026.html → DEN
                parts = link[0].strip("/").split("/")
                if len(parts) >= 2:
                    abbr_by_name[name] = parts[1].upper()

        out_headers = [
            "TEAM_ID",
            "TEAM_NAME",
            "OFF_RATING",
            "DEF_RATING",
            "NET_RATING",
            "PACE",
            "TS_PCT",
            "EFG_PCT",
        ]
        out_rows: list[list[Any]] = []
        for row in rows:
            # BBR appends ``*`` to playoff teams in this column — strip it.
            team_name = (row.get("team") or row.get("team_name") or "").strip().rstrip("*").strip()
            if not team_name:
                continue
            # Prefer abbreviation as TEAM_ID — that's what
            # ``fetch_team_advanced_gamelog`` expects for the URL path.
            team_id = abbr_by_name.get(team_name) or team_name
            ortg = _safe_float_str(row.get("off_rtg") or row.get("ortg"))
            drtg = _safe_float_str(row.get("def_rtg") or row.get("drtg"))
            nrtg_raw = row.get("net_rtg") or row.get("nrtg")
            net = _safe_float_str(nrtg_raw) if nrtg_raw not in (None, "") else _diff(ortg, drtg)
            out_rows.append([
                team_id,
                team_name,
                ortg,
                drtg,
                net,
                _safe_float_str(row.get("pace")),
                _safe_float_str(row.get("ts_pct")),
                _safe_float_str(row.get("efg_pct")),
            ])
        return {
            "resultSets": [
                {"name": "LeagueDashTeamStats", "headers": out_headers, "rowSet": out_rows}
            ]
        }

    def fetch_league_player_advanced(
        self,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        """League-wide per-player advanced season averages (percentile feed)."""
        end_year = int(season) + 1
        doc, _offset = self._fetch_with_season_fallback(
            lambda offset: f"/leagues/NBA_{end_year + offset}_advanced.html"
        )
        if doc is None:
            return _empty_envelope("LeagueDashPlayerStats")

        rows: list[dict[str, Any]] = []
        for table_id in ("advanced", "advanced_stats", "totals"):
            _, parsed = _parse_table(doc, table_id)
            if parsed:
                rows = parsed
                break
        if not rows:
            return _empty_envelope("LeagueDashPlayerStats")

        out_headers = [
            "PLAYER_ID",
            "PLAYER_NAME",
            "TEAM_ID",
            "TEAM_ABBREVIATION",
            "TS_PCT",
            "EFG_PCT",
            "USG_PCT",
            "OFF_RATING",
            "DEF_RATING",
            "NET_RATING",
            "PIE",
            "PACE",
            "AST_PCT",
            "OREB_PCT",
            "DREB_PCT",
            "REB_PCT",
        ]
        out_rows: list[list[Any]] = []
        for row in rows:
            # BBR's per-player advanced page uses ``name_display`` (post-2024
            # schema) but older snapshots use ``player``. Try both.
            player_name = (row.get("name_display") or row.get("player") or "").strip()
            if not player_name:
                continue
            team_abbr = (row.get("team_name_abbr") or row.get("team_id") or "").strip().upper()
            # OFF_RATING / DEF_RATING are NOT on the league_advanced page
            # (they live on per-game logs). Leave them None — downstream
            # treats missing as "not applicable".
            ortg = _safe_float_str(row.get("off_rtg"))
            drtg = _safe_float_str(row.get("def_rtg"))
            net = _diff(ortg, drtg)
            out_rows.append([
                player_name,
                player_name,
                team_abbr,
                team_abbr,
                _safe_float_str(row.get("ts_pct")),
                _safe_float_str(row.get("efg_pct")),
                _safe_float_str(row.get("usg_pct")),
                ortg,
                drtg,
                net,
                # PIE has no clean BBR equivalent — substitute BPM.
                _safe_float_str(row.get("bpm")),
                _safe_float_str(row.get("pace")),
                _safe_float_str(row.get("ast_pct")),
                _safe_float_str(row.get("orb_pct")),
                _safe_float_str(row.get("drb_pct")),
                _safe_float_str(row.get("trb_pct")),
            ])
        return {
            "resultSets": [
                {"name": "LeagueDashPlayerStats", "headers": out_headers, "rowSet": out_rows}
            ]
        }

    def fetch_team_advanced_gamelog(
        self,
        team_id: str,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        """Per-game advanced log for one team. ``team_id`` is a BBR abbreviation."""
        abbr = (team_id or "").strip().upper()
        if not abbr:
            return _empty_envelope("TeamGameLogs")
        end_year = int(season) + 1
        doc, _offset = self._fetch_with_season_fallback(
            lambda offset: f"/teams/{abbr}/{end_year + offset}/gamelog-advanced"
        )
        if doc is None:
            return _empty_envelope("TeamGameLogs")

        rows: list[dict[str, Any]] = []
        # Modern BBR: ``team_game_log_adv_reg`` (regular season) +
        # ``team_game_log_adv_post`` (postseason). Older snapshots used
        # ``tgl_advanced`` — try both shapes.
        for table_id in ("team_game_log_adv_reg", "tgl_advanced",
                         "team_game_log_adv", "advanced_team_game_log"):
            _, parsed = _parse_table(doc, table_id)
            if parsed:
                rows = parsed
                break
        if not rows:
            return _empty_envelope("TeamGameLogs")

        out_headers = [
            "GAME_ID",
            "GAME_DATE",
            "MATCHUP",
            "OFF_RATING",
            "DEF_RATING",
            "NET_RATING",
            "PACE",
            "TS_PCT",
            "EFG_PCT",
            "AST_PCT",
            "OREB_PCT",
            "DREB_PCT",
            "TM_TOV_PCT",
        ]
        out_rows: list[list[Any]] = []
        for row in rows:
            # Modern BBR uses ``team_off_rtg`` / ``team_def_rtg`` prefixes
            # on team-level gamelogs; older snapshots use plain ``off_rtg``.
            ortg = _safe_float_str(
                row.get("team_off_rtg") or row.get("off_rtg") or row.get("ortg")
            )
            drtg = _safe_float_str(
                row.get("team_def_rtg") or row.get("def_rtg") or row.get("drtg")
            )
            opp = row.get("opp_name_abbr") or row.get("opp_name") or row.get("opp_id")
            out_rows.append([
                row.get("game_id") or row.get("box_score_text") or "",
                row.get("date") or row.get("date_game"),
                _build_matchup(opp, row.get("game_location")),
                ortg,
                drtg,
                _diff(ortg, drtg),
                _safe_float_str(row.get("pace")),
                _safe_float_str(row.get("ts_pct")),
                _safe_float_str(row.get("efg_pct")),
                _safe_float_str(row.get("team_ast_pct") or row.get("ast_pct")),
                _safe_float_str(row.get("team_orb_pct") or row.get("orb_pct")),
                _safe_float_str(row.get("team_drb_pct") or row.get("drb_pct")),
                _safe_float_str(row.get("team_tov_pct") or row.get("tov_pct")),
            ])
        return {
            "resultSets": [
                {"name": "TeamGameLogs", "headers": out_headers, "rowSet": out_rows}
            ]
        }

    def fetch_boxscore_advanced(self, game_id: str) -> dict[str, Any]:
        """Per-game advanced box score. ``game_id`` is a BBR ID (``YYYYMMDD0HOME``)."""
        bbr_id = (game_id or "").strip()
        if not bbr_id:
            return _empty_envelope("BoxScoreAdvanced")
        path = f"/boxscores/{bbr_id}.html"
        doc = self._fetch_html_or_empty(path)
        if doc is None:
            return _empty_envelope("BoxScoreAdvanced")

        # Tables on the boxscore page are named ``box-{ABBR}-game-advanced``.
        # We can't know the team abbreviations without parsing, so iterate
        # over every advanced table.
        out_headers = [
            "GAME_ID",
            "TEAM_ABBREVIATION",
            "PLAYER_ID",
            "PLAYER_NAME",
            "MIN",
            "TS_PCT",
            "EFG_PCT",
            "USG_PCT",
            "OFF_RATING",
            "DEF_RATING",
        ]
        out_rows: list[list[Any]] = []
        for table in doc.xpath("//table[contains(@id, '-game-advanced')]"):
            tid = table.get("id") or ""
            # ``box-LAL-game-advanced`` → ``LAL``
            parts = tid.split("-")
            team_abbr = parts[1].upper() if len(parts) >= 3 else ""
            for tr in table.xpath(".//tbody//tr"):
                if "thead" in (tr.get("class") or "").split():
                    continue
                cells = {
                    cell.get("data-stat"): "".join(cell.itertext()).strip() or None
                    for cell in tr.xpath("./*")
                    if cell.get("data-stat")
                }
                player_link = tr.xpath("./th[@data-stat='player']/a/@href")
                slug = ""
                if player_link:
                    href = player_link[0]
                    if href.endswith(".html"):
                        slug = href.split("/")[-1].removesuffix(".html")
                player_name = cells.get("player") or ""
                if not player_name:
                    continue
                out_rows.append([
                    bbr_id,
                    team_abbr,
                    slug,
                    player_name,
                    _safe_float_str(cells.get("mp")),
                    _safe_float_str(cells.get("ts_pct")),
                    _safe_float_str(cells.get("efg_pct")),
                    _safe_float_str(cells.get("usg_pct")),
                    _safe_float_str(cells.get("off_rtg")),
                    _safe_float_str(cells.get("def_rtg")),
                ])
        return {
            "resultSets": [
                {"name": "BoxScoreAdvanced", "headers": out_headers, "rowSet": out_rows}
            ]
        }

    def fetch_common_all_players(
        self,
        season: int,
        is_only_current_season: int = 1,
    ) -> dict[str, Any]:
        """Roster snapshot. ``PERSON_ID`` is the BBR slug for each player."""
        end_year = int(season) + 1
        doc, _offset = self._fetch_with_season_fallback(
            lambda offset: f"/leagues/NBA_{end_year + offset}_per_game.html"
        )
        if doc is None:
            return _empty_envelope("CommonAllPlayers")

        # Identify the per_game_stats table (or fall back).
        table = _get_element_by_id(doc, "per_game_stats")
        if table is None:
            for tid in ("totals_stats", "advanced_stats"):
                table = _get_element_by_id(doc, tid)
                if table is not None:
                    break
        if table is None:
            return _empty_envelope("CommonAllPlayers")

        out_headers = [
            "PERSON_ID",
            "DISPLAY_FIRST_LAST",
            "TEAM_ID",
            "TEAM_ABBREVIATION",
            "ROSTERSTATUS",
        ]
        out_rows: list[list[Any]] = []
        seen: set[str] = set()
        for tr in table.xpath(".//tbody//tr"):
            if "thead" in (tr.get("class") or "").split():
                continue
            # The player anchor lives on whichever cell carries the player
            # column — modern BBR uses ``name_display`` on a <td>, older
            # snapshots used ``player`` on a <th>.
            player_link = tr.xpath(
                "./*[@data-stat='name_display']/a/@href"
                " | ./*[@data-stat='player']/a/@href"
            )
            slug = ""
            if player_link:
                href = player_link[0]
                if href.endswith(".html"):
                    slug = href.split("/")[-1].removesuffix(".html")
            cells = {
                cell.get("data-stat"): "".join(cell.itertext()).strip() or None
                for cell in tr.xpath("./*")
                if cell.get("data-stat")
            }
            player_name = (cells.get("name_display") or cells.get("player") or "").strip()
            if not slug or not player_name or slug in seen:
                continue
            seen.add(slug)
            team_abbr = (cells.get("team_name_abbr") or cells.get("team_id") or "").strip().upper()
            out_rows.append([
                slug,
                player_name,
                team_abbr,
                team_abbr,
                1.0,  # BBR's roster page lists active players for the season.
            ])
        return {
            "resultSets": [
                {"name": "CommonAllPlayers", "headers": out_headers, "rowSet": out_rows}
            ]
        }

    # ------------------------------------------------------------------
    # Public methods — stubs (5)
    #
    # No clean BBR equivalent for hustle / tracking / clutch / defense /
    # lineups. Each returns a successful empty envelope so the loader caches
    # an empty payload, the breaker stays closed, and the proxy-fallback
    # path takes over at scoring time.

    def fetch_lineup_advanced(
        self,
        season: int,
        season_type: str = "Regular Season",
        group_quantity: int = 5,
    ) -> dict[str, Any]:
        return _empty_envelope("Lineups")

    def fetch_hustle_stats_player(
        self,
        season: int,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        return _empty_envelope("HustleStatsPlayer")

    def fetch_player_tracking(
        self,
        season: int,
        pt_measure_type: str,
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        return _empty_envelope("PlayerTracking")

    def fetch_player_clutch(
        self,
        season: int,
        season_type: str = "Regular Season",
        clutch_time: str = "Last 5 Minutes",
        ahead_behind: str = "Ahead or Behind",
        point_diff: int = 5,
    ) -> dict[str, Any]:
        return _empty_envelope("PlayerClutch")

    def fetch_player_defense_dashboard(
        self,
        season: int,
        defense_category: str = "Overall",
        season_type: str = "Regular Season",
    ) -> dict[str, Any]:
        return _empty_envelope("PlayerDefense")

    # ------------------------------------------------------------------
    # Internal

    def _fetch_html_or_empty(self, path: str):
        """Fetch + parse HTML. Returns ``None`` on 404 / non-recoverable error."""
        url = f"{self._base_url}{path}"
        headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._bucket.acquire()
            try:
                if self._http_client is not None:
                    response = self._http_client.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
                else:
                    response = httpx.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= _MAX_ATTEMPTS:
                    raise
                continue
            if response.status_code in (404, 500):
                # BBR returns 500 for not-yet-started seasons (e.g., the
                # 2026-27 season's pages don't exist yet during the 2026
                # off-season). Treat as "no data available".
                logger.info("BBR %s (treated as empty): %s", response.status_code, path)
                return None
            response.raise_for_status()
            text = _strip_html_comments(response.text)
            return html.fromstring(text)
        if last_error is not None:
            raise last_error
        return None

    def _fetch_with_season_fallback(self, build_path) -> tuple[Any, int] | tuple[None, None]:
        """Fetch a season-keyed URL, falling back to ``season - 1`` on 404.

        ``build_path`` is a callable ``int -> str`` that produces the URL path
        for a given end-year. This is the pragmatic workaround for the
        upstream season-convention mismatch where the orchestrator may pass
        a season whose page hasn't materialized yet (off-season). Returning
        the most recent available data is more useful than an empty cache.
        """
        for end_year_offset in (0, -1):
            doc = self._fetch_html_or_empty(build_path(end_year_offset))
            if doc is not None:
                return doc, end_year_offset
        return None, None


# ---------------------------------------------------------------------------
# Helpers


def _empty_envelope(name: str) -> dict[str, Any]:
    """Return a successful but empty NBA-Stats result-set envelope."""
    return {"resultSets": [{"name": name, "headers": [], "rowSet": []}]}


def _strip_html_comments(text: str) -> str:
    """Unwrap commented-out tables that BBR uses to defer rendering.

    Many BBR data tables live inside ``<!-- ... -->`` blocks that browsers
    inject at JS execution time. Strip the comment markers so lxml sees the
    tables as part of the document tree.
    """
    return text.replace("<!--", "").replace("-->", "")


def _get_element_by_id(doc: Any, table_id: str) -> Any | None:
    """``HtmlElement.get_element_by_id`` raises on miss; this returns ``None`` instead."""
    matches = doc.xpath(f"//*[@id='{table_id}']")
    return matches[0] if matches else None


def _parse_table(doc: Any, table_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Return ``(data_stats, rows)`` from the BBR table with the given id.

    ``data_stats`` is the list of column ``data-stat`` attributes (BBR's
    machine-readable column keys). Each row is a dict mapping each
    ``data-stat`` to its cell text.

    Returns ``([], [])`` when the table is missing — a well-formed empty
    response that the orchestrator persists as an empty cache row rather
    than tripping the breaker.
    """
    table = _get_element_by_id(doc, table_id)
    if table is None:
        return ([], [])
    headers: list[str] = []
    for cell in table.xpath(".//thead//th"):
        stat = cell.get("data-stat")
        if stat and stat not in headers:
            headers.append(stat)
    rows: list[dict[str, Any]] = []
    for tr in table.xpath(".//tbody//tr"):
        # Skip BBR's interstitial header rows.
        if "thead" in (tr.get("class") or "").split():
            continue
        row: dict[str, Any] = {}
        for cell in tr.xpath("./*"):
            stat = cell.get("data-stat")
            if not stat:
                continue
            text = "".join(cell.itertext()).strip()
            row[stat] = text or None
        if row:
            rows.append(row)
    return headers, rows


def _safe_float_str(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_minutes(value: Any) -> float | None:
    """Parse BBR's ``MP`` cell (``"34:23"`` mm:ss form) into a float minutes value.

    Per-game logs use ``MM:SS`` while season aggregates use plain decimals —
    handle both transparently.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        try:
            mm, ss = text.split(":", 1)
            return float(mm) + float(ss) / 60.0
        except (TypeError, ValueError):
            return None
    return _safe_float_str(text)


def _diff(a: float | None, b: float | None) -> float | None:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) - float(b)
    return None


def _build_matchup(opponent: str | None, location: str | None) -> str | None:
    """Compose an NBA-Stats-style ``MATCHUP`` string (``LAL @ BOS`` / ``LAL vs. BOS``)."""
    if not opponent:
        return None
    opp = str(opponent).strip().upper()
    if not opp:
        return None
    away = (location or "").strip() == "@"
    return f"@ {opp}" if away else f"vs. {opp}"

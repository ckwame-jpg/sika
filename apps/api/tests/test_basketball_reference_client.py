from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.clients import _rate_limit
from app.clients.basketball_reference import BasketballReferenceClient
from app.clients.nba_stats import parse_result_set


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bbr"


@pytest.fixture(autouse=True)
def _reset_registry():
    _rate_limit.reset_for_tests()
    yield
    _rate_limit.reset_for_tests()


def _fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def _ok_html(text: str, url: str = "https://www.basketball-reference.com/") -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("GET", url), text=text)


def _install_get(monkeypatch, *, text: str | None = None, status: int = 200) -> dict:
    """Install a fake ``httpx.get`` and capture the URL it was called with."""
    seen: dict = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        request = httpx.Request("GET", url)
        if status == 404:
            return httpx.Response(404, request=request, text="")
        return httpx.Response(status, request=request, text=text or "")

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


# ---------------------------------------------------------------------------
# Implemented endpoints (6) — verify NBA-Stats-shaped envelope round-trips


def test_fetch_player_advanced_gamelog_renames_to_nba_stats_keys(monkeypatch):
    seen = _install_get(monkeypatch, text=_fixture("player_advanced_gamelog.html"))
    client = BasketballReferenceClient()
    payload = client.fetch_player_advanced_gamelog("jamesle01", 2025)
    rows = parse_result_set(payload, name="PlayerGameLogs")

    assert seen["url"].endswith("/players/j/jamesle01/gamelog-advanced/2026")
    assert isinstance(seen["timeout"], httpx.Timeout)
    assert len(rows) == 2
    first = rows[0]
    assert first["GAME_DATE"] == "2026-01-12"
    assert first["MATCHUP"] == "@ BOS"
    assert first["TS_PCT"] == pytest.approx(0.612)
    assert first["EFG_PCT"] == pytest.approx(0.555)
    assert first["USG_PCT"] == pytest.approx(0.305)
    assert first["OFF_RATING"] == pytest.approx(118.0)
    assert first["DEF_RATING"] == pytest.approx(110.0)
    assert first["NET_RATING"] == pytest.approx(8.0)
    assert first["PACE"] == pytest.approx(99.5)
    assert first["PIE"] == pytest.approx(7.2)  # BPM substitute
    # Second row was a home game — no "@" prefix in fixture cell.
    assert rows[1]["MATCHUP"] == "vs. PHI"


def test_fetch_player_advanced_gamelog_returns_empty_on_blank_id():
    payload = BasketballReferenceClient().fetch_player_advanced_gamelog("", 2025)
    rows = parse_result_set(payload, name="PlayerGameLogs")
    assert rows == []


def test_fetch_player_advanced_gamelog_treats_404_as_empty(monkeypatch):
    _install_get(monkeypatch, status=404)
    payload = BasketballReferenceClient().fetch_player_advanced_gamelog("nobody01", 2025)
    rows = parse_result_set(payload, name="PlayerGameLogs")
    assert rows == []


def test_fetch_team_advanced_extracts_league_table(monkeypatch):
    seen = _install_get(monkeypatch, text=_fixture("team_advanced.html"))
    client = BasketballReferenceClient()
    payload = client.fetch_team_advanced(2025)
    rows = parse_result_set(payload, name="LeagueDashTeamStats")

    assert seen["url"].endswith("/leagues/NBA_2026.html")
    assert len(rows) == 2
    by_name = {r["TEAM_NAME"]: r for r in rows}
    assert by_name["Boston Celtics"]["OFF_RATING"] == pytest.approx(122.1)
    assert by_name["Boston Celtics"]["DEF_RATING"] == pytest.approx(110.5)
    assert by_name["Boston Celtics"]["NET_RATING"] == pytest.approx(11.6)
    assert by_name["Los Angeles Lakers"]["PACE"] == pytest.approx(100.4)
    # TEAM_ID falls back to team name (BBR has no NBA-Stats numeric IDs).
    assert by_name["Boston Celtics"]["TEAM_ID"] == "Boston Celtics"


def test_fetch_league_player_advanced_emits_percentile_inputs(monkeypatch):
    _install_get(monkeypatch, text=_fixture("league_player_advanced.html"))
    payload = BasketballReferenceClient().fetch_league_player_advanced(2025)
    rows = parse_result_set(payload, name="LeagueDashPlayerStats")

    by_player = {r["PLAYER_NAME"]: r for r in rows}
    assert by_player["Jalen Brunson"]["TS_PCT"] == pytest.approx(0.598)
    assert by_player["Jalen Brunson"]["TEAM_ABBREVIATION"] == "NYK"
    assert by_player["Nikola Jokic"]["PIE"] == pytest.approx(11.2)


def test_fetch_team_advanced_gamelog_uses_team_abbr(monkeypatch):
    seen = _install_get(monkeypatch, text=_fixture("team_gamelog_advanced.html"))
    payload = BasketballReferenceClient().fetch_team_advanced_gamelog("LAL", 2025)
    rows = parse_result_set(payload, name="TeamGameLogs")

    assert seen["url"].endswith("/teams/LAL/2026/gamelog-advanced")
    assert rows[0]["MATCHUP"] == "@ BOS"
    assert rows[0]["OFF_RATING"] == pytest.approx(118.0)
    assert rows[0]["TM_TOV_PCT"] == pytest.approx(0.13)


def test_fetch_boxscore_advanced_extracts_per_team_tables(monkeypatch):
    _install_get(monkeypatch, text=_fixture("boxscore.html"))
    payload = BasketballReferenceClient().fetch_boxscore_advanced("202601120LAL")
    rows = parse_result_set(payload, name="BoxScoreAdvanced")

    teams = {r["TEAM_ABBREVIATION"] for r in rows}
    assert teams == {"LAL", "BOS"}
    by_player = {r["PLAYER_NAME"]: r for r in rows}
    assert by_player["LeBron James"]["PLAYER_ID"] == "jamesle01"
    assert by_player["LeBron James"]["TS_PCT"] == pytest.approx(0.612)


def test_fetch_common_all_players_returns_bbr_slugs(monkeypatch):
    _install_get(monkeypatch, text=_fixture("per_game_roster.html"))
    payload = BasketballReferenceClient().fetch_common_all_players(2025)
    rows = parse_result_set(payload, name="CommonAllPlayers")

    assert {r["PERSON_ID"] for r in rows} == {"jamesle01", "brunsja01", "jokicni01"}
    by_slug = {r["PERSON_ID"]: r for r in rows}
    assert by_slug["jamesle01"]["DISPLAY_FIRST_LAST"] == "LeBron James"
    assert by_slug["jamesle01"]["TEAM_ABBREVIATION"] == "LAL"
    assert by_slug["jamesle01"]["ROSTERSTATUS"] == 1.0


# ---------------------------------------------------------------------------
# Robustness — missing columns, malformed tables


def test_parse_robust_to_missing_column(monkeypatch):
    """When a BBR column is absent, _safe_float_str returns None rather than KeyError."""
    fixture = """<html><body><!--
    <table id="advanced-team">
      <thead><tr><th data-stat="team">Team</th><th data-stat="ortg">ORtg</th></tr></thead>
      <tbody><tr><td data-stat="team">Indiana Pacers</td><td data-stat="ortg">119</td></tr></tbody>
    </table>
    --></body></html>"""
    _install_get(monkeypatch, text=fixture)
    payload = BasketballReferenceClient().fetch_team_advanced(2025)
    rows = parse_result_set(payload, name="LeagueDashTeamStats")
    assert rows[0]["OFF_RATING"] == pytest.approx(119.0)
    # Missing pace / drtg cells must not raise; they come back as None.
    assert rows[0]["DEF_RATING"] is None
    assert rows[0]["PACE"] is None


def test_returns_empty_envelope_when_table_missing(monkeypatch):
    _install_get(monkeypatch, text="<html><body><p>nope</p></body></html>")
    payload = BasketballReferenceClient().fetch_team_advanced(2025)
    rows = parse_result_set(payload, name="LeagueDashTeamStats")
    assert rows == []


# ---------------------------------------------------------------------------
# Stub endpoints (5) — must succeed with empty result sets, never raise


@pytest.mark.parametrize(
    "method_call,name",
    [
        (lambda c: c.fetch_lineup_advanced(2025), "Lineups"),
        (lambda c: c.fetch_hustle_stats_player(2025), "HustleStatsPlayer"),
        (lambda c: c.fetch_player_tracking(2025, "Drives"), "PlayerTracking"),
        (lambda c: c.fetch_player_clutch(2025), "PlayerClutch"),
        (lambda c: c.fetch_player_defense_dashboard(2025), "PlayerDefense"),
    ],
)
def test_stubbed_endpoints_return_empty_envelope_without_network(method_call, name, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("stubbed endpoint should not hit the network")

    monkeypatch.setattr(httpx, "get", boom)
    client = BasketballReferenceClient()
    payload = method_call(client)
    rows = parse_result_set(payload, name=name)
    assert rows == []
    assert payload["resultSets"][0]["headers"] == []


# ---------------------------------------------------------------------------
# Referee tendency (Smarter #13 phase 2b-2)


def test_fetch_referee_season_stats_parses_rs_raw_table(monkeypatch):
    """Happy path: scraper reads the rs_raw table at /referees/{season}_register.html
    and returns BR-shaped rows with the canonical column keys the parser
    (apps/api/app/services/nba_referee_tendencies.py:parse_referee_tendency_rows)
    expects: Referee / G / FTA / PF.

    Fixture mirrors the live 2025-26 page screenshot verified on
    2026-05-16 — 31-column layout across 6 group headers; scraper
    positionally indexes the first 7 cells (Referee, Lg, G, then the
    Per Game group: FGA, FTA, PF, PTS).
    """
    seen = _install_get(monkeypatch, text=_fixture("referee_register.html"))
    client = BasketballReferenceClient()
    rows = client.fetch_referee_season_stats(2026)

    assert seen["url"].endswith("/referees/2026_register.html")
    # 3 valid rows + 1 row with empty referee name (skipped) + 1 mid-table
    # header row (< 7 cells, skipped). Expect 3 surviving rows.
    assert len(rows) == 3
    by_name = {r["Referee"]: r for r in rows}
    assert set(by_name.keys()) == {"Tony Brothers", "Scott Foster", "Brent Haskill"}
    foster = by_name["Scott Foster"]
    assert foster["G"] == "62"
    assert foster["PF"] == "41.3"  # per-game personal fouls (Per Game group, col 5)
    assert foster["FTA"] == "48.9"  # per-game free-throw attempts (col 4)


def test_fetch_referee_season_stats_returns_empty_on_403(monkeypatch):
    """BR returns 403 to fresh IPs that aren't the operator's
    configured proxy. The scraper's empty-return path matches the
    loader's tolerance for missing data — the operator UI surfaces
    'no tendency data cached' rather than the scoring kernel
    crashing."""
    def fake_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(403, request=httpx.Request("GET", url), text="")
    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(httpx.HTTPStatusError):
        BasketballReferenceClient().fetch_referee_season_stats(2026)


def test_fetch_referee_season_stats_returns_empty_when_table_missing(monkeypatch):
    """Off-season or before-season-tips state: page exists but no
    rs_raw table yet. Scraper returns []."""
    _install_get(monkeypatch, text="<html><body><p>no data</p></body></html>")
    rows = BasketballReferenceClient().fetch_referee_season_stats(2026)
    assert rows == []


def test_fetch_referee_season_stats_returns_empty_on_404(monkeypatch):
    """Far-future season pages don't exist yet — 404 path returns []
    via _fetch_html_or_empty's None handling."""
    _install_get(monkeypatch, status=404)
    rows = BasketballReferenceClient().fetch_referee_season_stats(2099)
    assert rows == []

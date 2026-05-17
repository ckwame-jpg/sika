"""WNBA PR 1 scaffolding pin — apps/api side.

PR 1 wires WNBA as a first-class sport at the config / client / type
layer. The actual scoring + market mapping land in PRs 2-4. These
tests pin the PR 1 surface:

- ESPN URL constants (scoreboard / gamelog / schedule / search slug /
  league name) include a WNBA entry pointing at ``/wnba/`` endpoints.
- ``ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME`` carries the 15 WNBA team
  display names (including 2026 expansion Toronto Tempo + Portland
  Fire) so the resolver's team-hint matching works for WNBA props.
- ``Settings.enabled_sports`` + ``Settings.parlay_enabled_sports``
  include WNBA so refresh jobs and parlay generation pick it up.
- ``default_season_for_sport`` returns the in-season calendar year
  for a mid-season WNBA reference date (pinned in ``test_stats_query``;
  this test focuses on the scaffolding surface).
"""

from __future__ import annotations

from app.clients.espn import (
    ESPN_GAMELOG_URLS,
    ESPN_LEAGUE_NAMES,
    ESPN_SCOREBOARD_URLS,
    ESPN_SEARCH_SLUGS,
    ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME,
    ESPN_TEAM_SCHEDULE_URLS,
)
from app.config import Settings


def test_espn_url_constants_include_wnba() -> None:
    assert "WNBA" in ESPN_SEARCH_SLUGS
    assert ESPN_SEARCH_SLUGS["WNBA"] == "wnba"
    assert "WNBA" in ESPN_SCOREBOARD_URLS
    assert "/wnba/" in ESPN_SCOREBOARD_URLS["WNBA"]
    assert "WNBA" in ESPN_GAMELOG_URLS
    assert "/wnba/" in ESPN_GAMELOG_URLS["WNBA"]
    assert "{athlete_id}" in ESPN_GAMELOG_URLS["WNBA"]
    assert "WNBA" in ESPN_TEAM_SCHEDULE_URLS
    assert "/wnba/" in ESPN_TEAM_SCHEDULE_URLS["WNBA"]
    assert "{team_id}" in ESPN_TEAM_SCHEDULE_URLS["WNBA"]
    assert "WNBA" in ESPN_LEAGUE_NAMES


def test_wnba_team_map_includes_all_15_teams() -> None:
    wnba_map = ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME.get("WNBA")
    assert wnba_map is not None, "WNBA team abbreviation map missing"
    # All 15 distinct team display names (2026 season). The map may
    # carry multiple aliases per team (e.g. ``NY`` + ``NYL`` for the
    # Liberty) — assert on the canonical names, not the alias count.
    expected_teams = {
        "Atlanta Dream", "Chicago Sky", "Connecticut Sun", "Indiana Fever",
        "New York Liberty", "Toronto Tempo", "Washington Mystics",
        "Dallas Wings", "Golden State Valkyries", "Las Vegas Aces",
        "Los Angeles Sparks", "Minnesota Lynx", "Phoenix Mercury",
        "Portland Fire", "Seattle Storm",
    }
    actual_teams = set(wnba_map.values())
    assert expected_teams.issubset(actual_teams), (
        f"WNBA team map missing teams: {expected_teams - actual_teams}"
    )


def test_settings_include_wnba_in_enabled_sports_lists() -> None:
    settings = Settings()
    assert "WNBA" in settings.enabled_sports
    assert "WNBA" in settings.parlay_enabled_sports


def test_settings_include_wnba_cache_ttls() -> None:
    """WNBA shares NBA's payload shape; the TTLs mirror NBA defaults
    as a starting point. PR 4 may diverge them once sport-specific
    advanced data lands."""
    settings = Settings()
    assert settings.wnba_prop_gamelog_cache_minutes > 0
    assert settings.wnba_advanced_cache_minutes > 0
    assert settings.wnba_team_advanced_cache_minutes > 0
    assert settings.wnba_referee_assignments_cache_minutes > 0

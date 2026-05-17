"""WNBA PR 1 + PR 6 scaffolding pin — apps/api side.

PR 1 wired WNBA as a first-class sport at the config / client / type
layer; PR 6 flipped the ``enabled_sports`` default and registered the
sport adapter / Kalshi constants / Odds API mapping that the refresh
job + scoring + trade-desk surfaces require.

These tests pin the combined surface:

- ESPN URL constants (scoreboard / gamelog / schedule / search slug /
  league name) include a WNBA entry pointing at ``/wnba/`` endpoints.
- ``ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME`` carries the 15 WNBA team
  display names (including 2026 expansion Toronto Tempo + Portland
  Fire) so the resolver's team-hint matching works for WNBA props.
- ``Settings.enabled_sports`` includes WNBA so the default refresh
  cycle persists + scores KXWNBA markets.
  ``Settings.parlay_enabled_sports`` intentionally still EXCLUDES WNBA
  — there is no ``wnba_parlay_*`` family yet (would pollute mixed
  calibration); a separate PR adds WNBA parlay families after Smarter
  #28 backtest data justifies one.
- ``ADAPTERS`` registers ``"WNBA": TeamSportAdapter("WNBA", "Basketball")``
  so the refresh job can normalize ESPN WNBA scoreboard payloads.
- Kalshi sport-category / event-series / prop-slug constants — in BOTH
  ``app/api/routes.py`` AND ``app/services/trade_desk.py`` (Bug #30
  duplication) — include the ``kxwnbagame`` series and the WNBA root.
- The Odds API mapping translates sika's ``WNBA`` sport key to the
  upstream ``basketball_wnba`` slug for Smarter #18 sportsbook consensus.
- ``CURRENT_WATCHLIST_SPORTS`` includes WNBA so trade-desk responses
  and ``/product/freshness`` enumerate it alongside NBA + MLB.
- A smoke call to ``refresh_sports_data`` with WNBA in the sports
  list completes without ``KeyError`` (pre-PR 6 the registry lookup
  raised because the adapter wasn't registered).
- ``default_season_for_sport`` returns the in-season calendar year
  for a mid-season WNBA reference date (pinned in ``test_stats_query``;
  this test focuses on the scaffolding surface).
"""

from __future__ import annotations

from datetime import date

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


def test_settings_default_enabled_sports_include_wnba() -> None:
    """PR 6 flipped the default: WNBA is now in ``enabled_sports``
    so the default refresh cycle persists + scores KXWNBA markets.
    The 2026 WNBA season is live; deferring the flip past PR 5 was a
    deliberate guard against the adapter / Kalshi-constant gap, but
    PR 6 fills that gap and turns WNBA on by default for new
    deployments. Operators upgrading sika get WNBA automatically.
    """
    settings = Settings()
    assert "WNBA" in settings.enabled_sports


def test_settings_default_parlay_enabled_sports_excludes_wnba() -> None:
    """Intentional MVP+1 deferral: ``parlay_family_key`` has no
    ``wnba_parlay_*`` definition yet. Adding WNBA to
    ``parlay_enabled_sports`` without a matching family would push
    WNBA combos into ``mixed_parlay_*`` and pollute mixed-family
    calibration. A separate PR introduces WNBA parlay families once
    Smarter #28 backtest data justifies the new heads.
    """
    settings = Settings()
    assert "WNBA" not in settings.parlay_enabled_sports


def test_sport_adapter_registry_includes_wnba() -> None:
    """PR 6 registers ``"WNBA": TeamSportAdapter("WNBA", "Basketball")``
    in ``ADAPTERS``. Without this entry the refresh job's
    ``ADAPTERS[sport_key]`` lookup raises ``KeyError`` the first time
    WNBA appears in the active-sports list.
    """
    from app.sports.registry import ADAPTERS
    from app.sports.team import TeamSportAdapter

    adapter = ADAPTERS.get("WNBA")
    assert adapter is not None, "WNBA missing from sport adapter registry"
    assert isinstance(adapter, TeamSportAdapter)
    assert adapter.sport_key == "WNBA"
    assert adapter.provider_name == "Basketball"


def test_kalshi_constants_include_wnba_in_routes_and_trade_desk() -> None:
    """The Kalshi sport-category / event-series / prop-slug constants
    are duplicated across ``app/api/routes.py`` and
    ``app/services/trade_desk.py`` (Bug #30 design smell). PR 6 must
    add WNBA entries to BOTH copies so the dedupe gap doesn't quietly
    break the trade-desk URL builder.
    """
    from app.api.routes import (
        KALSHI_EVENT_SERIES as ROUTES_KALSHI_EVENT_SERIES,
        KALSHI_PROP_CATEGORY_SLUGS as ROUTES_KALSHI_PROP_CATEGORY_SLUGS,
        KALSHI_SPORT_CATEGORY_ROOTS as ROUTES_KALSHI_SPORT_CATEGORY_ROOTS,
    )
    from app.services.trade_desk import (
        KALSHI_EVENT_SERIES as TD_KALSHI_EVENT_SERIES,
        KALSHI_PROP_CATEGORY_SLUGS as TD_KALSHI_PROP_CATEGORY_SLUGS,
        KALSHI_SPORT_CATEGORY_ROOTS as TD_KALSHI_SPORT_CATEGORY_ROOTS,
    )

    for category_root in (ROUTES_KALSHI_SPORT_CATEGORY_ROOTS, TD_KALSHI_SPORT_CATEGORY_ROOTS):
        wnba_root = category_root.get("WNBA")
        assert wnba_root, "WNBA missing from KALSHI_SPORT_CATEGORY_ROOTS"
        # WNBA lives under Kalshi's pro-basketball-w (women's) slug; the NBA
        # root is pro-basketball-m. Pin the substring so a regression to the
        # NBA URL is caught.
        assert "basketball" in wnba_root
        assert "pro-basketball-w" in wnba_root

    for event_series in (ROUTES_KALSHI_EVENT_SERIES, TD_KALSHI_EVENT_SERIES):
        wnba_series = event_series.get("WNBA")
        assert wnba_series, "WNBA missing from KALSHI_EVENT_SERIES"
        series_ticker, series_slug = wnba_series
        # Pin the lowercase ticker prefix observed in Kalshi's live URLs
        # (kxnbagame / kxmlbgame → kxwnbagame).
        assert series_ticker == "kxwnbagame"
        assert "basketball" in series_slug

    for prop_slugs in (ROUTES_KALSHI_PROP_CATEGORY_SLUGS, TD_KALSHI_PROP_CATEGORY_SLUGS):
        wnba_slugs = prop_slugs.get("WNBA")
        assert wnba_slugs is not None, "WNBA missing from KALSHI_PROP_CATEGORY_SLUGS"
        # Mirror NBA's stat keys — WNBA props share the same stat
        # vocabulary (PR 2 wired the alias mapping).
        for stat_key in ("points", "rebounds", "assists", "made_threes", "steals", "blocks", "turnovers"):
            assert stat_key in wnba_slugs, f"Missing WNBA stat slug for {stat_key}"


def test_odds_api_mapping_includes_wnba() -> None:
    """Smarter #18's sportsbook consensus consumes The Odds API; the
    sport-key dict must translate sika's ``WNBA`` to the upstream
    ``basketball_wnba`` slug. The prep doc verified Odds API supports
    WNBA h2h / spreads / totals / props.
    """
    from app.clients.the_odds_api import odds_api_sport_key

    assert odds_api_sport_key("WNBA") == "basketball_wnba"


def test_current_watchlist_sports_include_wnba() -> None:
    """``CURRENT_WATCHLIST_SPORTS`` gates the trade-desk's current-slate
    query and the ``/product/freshness`` scope enumeration. WNBA must
    join NBA + MLB so the scored WNBA markets actually surface in the
    operator UI alongside NBA / MLB.
    """
    from app.services.watchlist_coverage import CURRENT_WATCHLIST_SPORTS

    assert "WNBA" in CURRENT_WATCHLIST_SPORTS


def test_refresh_sports_data_smoke_handles_wnba(db_session) -> None:
    """Pre-PR 6 the refresh-job path raised ``KeyError: 'WNBA'`` at
    ``ADAPTERS[sport_key]`` because the adapter wasn't registered.
    PR 6 registers the adapter; ``refresh_sports_data`` should now
    accept WNBA in its sports list and return cleanly (zero events
    because we use stub providers, but no exception).
    """
    from app.services import ingestion

    class _StubEspn:
        def fetch_events_window_with_diagnostics(self, sport_key, start_day, end_day):
            return [], []

    class _StubNiche:
        def fetch_events_window(self, *args, **kwargs):
            return []

    summary = ingestion.refresh_sports_data(
        db_session,
        major_provider=_StubEspn(),
        niche_provider=_StubNiche(),
        sports=["WNBA"],
        anchor_day=date(2026, 5, 15),
    )
    assert "WNBA" in summary["sports_records_ingested"]
    assert summary["sports_records_ingested"]["WNBA"] == 0


def test_settings_include_wnba_cache_ttls() -> None:
    """WNBA shares NBA's payload shape; the TTLs mirror NBA defaults
    as a starting point. PR 4 may diverge them once sport-specific
    advanced data lands."""
    settings = Settings()
    assert settings.wnba_prop_gamelog_cache_minutes > 0
    assert settings.wnba_advanced_cache_minutes > 0
    assert settings.wnba_team_advanced_cache_minutes > 0
    assert settings.wnba_referee_assignments_cache_minutes > 0

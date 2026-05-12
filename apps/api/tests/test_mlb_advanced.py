from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import get_settings
from app.models import (
    EspnPlayerSearchCache,
    MlbBatterAdvancedCache,
    MlbPitcherAdvancedCache,
    MlbPlayerRosterCache,
    MlbStatcastBatterCache,
    MlbStatcastPitcherCache,
    MlbWeatherCache,
    utcnow,
)
from app.services import mlb_advanced


class _StubMlbStatsClient:
    def __init__(
        self,
        *,
        sabermetrics: dict[str, Any] | None = None,
        season_stats: dict[str, Any] | None = None,
        pitcher_saber: dict[str, Any] | None = None,
        teams: dict[str, Any] | None = None,
        rosters: dict[str, dict[str, Any]] | None = None,
        raise_with: Exception | None = None,
    ) -> None:
        self.sabermetrics = sabermetrics
        self.season_stats = season_stats
        self.pitcher_saber = pitcher_saber
        self.teams = teams
        self.rosters = rosters or {}
        self.raise_with = raise_with
        self.fetch_calls: list[tuple[str, str, int]] = []

    def fetch_player_sabermetrics(self, person_id, season):
        self.fetch_calls.append(("sabermetrics", str(person_id), int(season)))
        if self.raise_with is not None:
            raise self.raise_with
        return self.sabermetrics or {"stats": []}

    def fetch_player_hitting_advanced(self, person_id, season):
        self.fetch_calls.append(("hitting", str(person_id), int(season)))
        if self.raise_with is not None:
            raise self.raise_with
        return self.season_stats or {"stats": []}

    def fetch_pitcher_sabermetrics(self, person_id, season):
        self.fetch_calls.append(("pitcher", str(person_id), int(season)))
        if self.raise_with is not None:
            raise self.raise_with
        return self.pitcher_saber or {"stats": []}

    def fetch_player_splits(self, person_id, season, *, split_kind, group="hitting"):
        return {"stats": []}

    def fetch_all_teams(self, season, sport_id="1"):
        return self.teams or {"teams": []}

    def fetch_team_roster(self, team_id, season=None):
        return self.rosters.get(str(team_id), {"roster": []})


class _StubSavantClient:
    def __init__(self, *, batter_csv: str = "", pitcher_csv: str = "", raise_with: Exception | None = None) -> None:
        self.batter_csv = batter_csv
        self.pitcher_csv = pitcher_csv
        self.raise_with = raise_with

    def fetch_batter_statcast(self, mlb_player_id, season):
        if self.raise_with is not None:
            raise self.raise_with
        return self.batter_csv

    def fetch_pitcher_statcast(self, mlb_player_id, season):
        if self.raise_with is not None:
            raise self.raise_with
        return self.pitcher_csv


def _saber_payload(woba: float, iso: float, wrc_plus: float = 120.0) -> dict[str, Any]:
    return {
        "stats": [
            {
                "group": {"displayName": "hitting"},
                "splits": [
                    {"stat": {"woba": str(woba), "iso": str(iso), "wRcPlus": wrc_plus}},
                ],
            }
        ]
    }


def _hitting_payload(ops: float = 0.850, obp: float = 0.350, slg: float = 0.500, avg: float = 0.280,
                      pa: int = 600, walks: int = 60, strikeouts: int = 120) -> dict[str, Any]:
    return {
        "stats": [
            {
                "group": {"displayName": "hitting"},
                "splits": [
                    {"stat": {"ops": str(ops), "obp": str(obp), "slg": str(slg), "avg": str(avg),
                              "plateAppearances": pa, "baseOnBalls": walks, "strikeOuts": strikeouts}},
                ],
            }
        ]
    }


def _pitcher_payload(fip: float = 3.50, xfip: float = 3.60, era: float = 3.20,
                     whip: float = 1.10, k9: float = 9.5, bb9: float = 2.5, hr9: float = 0.9) -> dict[str, Any]:
    return {
        "stats": [
            {
                "group": {"displayName": "pitching"},
                "splits": [
                    {"stat": {"fip": str(fip), "xfip": str(xfip), "era": str(era),
                              "whip": str(whip), "strikeoutsPer9Inn": str(k9),
                              "walksPer9Inn": str(bb9), "homeRunsPer9": str(hr9)}},
                ],
            }
        ]
    }


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_load_mlb_batter_advanced_persists_sabermetrics(db_session):
    stub = _StubMlbStatsClient(
        sabermetrics=_saber_payload(0.380, 0.225, wrc_plus=140.0),
        season_stats=_hitting_payload(),
    )
    result = mlb_advanced.load_mlb_batter_advanced(
        db_session, mlb_player_id="660271", season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(MlbBatterAdvancedCache).one()
    assert cached.payload["season_avg"]["woba"] == pytest.approx(0.380)
    assert cached.payload["season_avg"]["iso"] == pytest.approx(0.225)
    assert cached.payload["season_avg"]["walk_rate"] == pytest.approx(60 / 600)
    assert cached.payload["season_avg"]["strikeout_rate"] == pytest.approx(120 / 600)


def test_load_mlb_pitcher_advanced_persists_metrics(db_session):
    stub = _StubMlbStatsClient(pitcher_saber=_pitcher_payload(fip=3.60, xfip=3.45, k9=10.5))
    result = mlb_advanced.load_mlb_pitcher_advanced(
        db_session, mlb_player_id="592450", season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(MlbPitcherAdvancedCache).one()
    assert cached.payload["season_avg"]["fip"] == pytest.approx(3.60)
    assert cached.payload["season_avg"]["xfip"] == pytest.approx(3.45)
    assert cached.payload["season_avg"]["k_per_9"] == pytest.approx(10.5)


def test_load_mlb_batter_advanced_falls_back_to_stale_on_failure(db_session):
    payload = {"season_avg": {"woba": 0.330}}
    db_session.add(
        MlbBatterAdvancedCache(
            athlete_id="660271",
            season=2024,
            payload=payload,
            cached_at=utcnow() - timedelta(days=1),
            expires_at=utcnow() - timedelta(hours=1),
        )
    )
    db_session.flush()
    stub = _StubMlbStatsClient(raise_with=RuntimeError("upstream boom"))
    result = mlb_advanced.load_mlb_batter_advanced(
        db_session, mlb_player_id="660271", season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "stale"
    assert result.payload == payload


def test_load_mlb_statcast_batter_aggregates_csv(db_session):
    csv_text = (
        "launch_speed,launch_angle,launch_speed_angle,estimated_ba_using_speedangle,"
        "estimated_slg_using_speedangle,estimated_woba_using_speedangle\n"
        "98.0,15.0,6,0.700,1.200,0.480\n"
        "85.0,5.0,4,0.300,0.450,0.210\n"
        "104.0,28.0,6,0.800,1.500,0.600\n"
    )
    stub = _StubSavantClient(batter_csv=csv_text)
    result = mlb_advanced.load_mlb_statcast_batter(
        db_session, mlb_player_id="660271", season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(MlbStatcastBatterCache).one()
    payload = cached.payload
    assert payload["events"] == 3
    assert payload["season_avg"]["barrel_rate"] == pytest.approx(2 / 3)
    assert payload["season_avg"]["hard_hit_rate"] == pytest.approx(2 / 3)
    assert payload["season_avg"]["xwoba"] == pytest.approx((0.480 + 0.210 + 0.600) / 3)
    assert payload["season_avg"]["exit_velocity_avg"] == pytest.approx((98 + 85 + 104) / 3)


def test_load_mlb_statcast_pitcher_aggregates_csv(db_session):
    csv_text = (
        "pitch_type,release_speed,description,strikes,events\n"
        "FF,96.5,swinging_strike,2,strikeout\n"
        "FF,95.0,called_strike,1,\n"
        "SL,87.5,foul,0,\n"
        "FF,97.0,swinging_strike,2,strikeout\n"
    )
    stub = _StubSavantClient(pitcher_csv=csv_text)
    result = mlb_advanced.load_mlb_statcast_pitcher(
        db_session, mlb_player_id="592450", season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(MlbStatcastPitcherCache).one()
    payload = cached.payload
    assert payload["pitches"] == 4
    assert payload["season_avg"]["avg_fastball_velo"] == pytest.approx((96.5 + 95.0 + 97.0) / 3)
    assert payload["season_avg"]["putaway_pct"] == pytest.approx(2 / 4)


def test_load_park_factors_returns_neutral_for_unknown_venue():
    factors = mlb_advanced.load_park_factors("999999")
    assert factors["hr"] == 1.0
    assert factors["_data_complete"] == 0.0


def test_load_park_factors_returns_curated_for_known_venue():
    factors = mlb_advanced.load_park_factors("12")  # Coors Field
    assert factors["hr"] > 1.10
    assert factors["_data_complete"] == 1.0


def test_load_park_factors_for_team_returns_curated_for_known_abbreviation():
    """Bug #4: ESPN's venue.id (e.g., 230 for CoolToday Park) doesn't match
    park_factors.json's numeric keys (1-33, a FanGraphs schema). The
    reliable join key is the home team's three-letter abbreviation, which
    ESPN provides on every event. This helper makes that lookup explicit."""
    factors = mlb_advanced.load_park_factors_for_team("COL")  # Colorado Rockies → Coors Field
    assert factors["hr"] > 1.10
    assert factors["_data_complete"] == 1.0


def test_load_park_factors_for_team_returns_neutral_for_unknown_team():
    factors = mlb_advanced.load_park_factors_for_team("ZZZ")
    assert factors["hr"] == 1.0
    assert factors["_data_complete"] == 0.0


def test_load_park_factors_for_team_is_case_insensitive():
    """ESPN abbreviations are uppercase but be defensive about casing."""
    upper = mlb_advanced.load_park_factors_for_team("COL")
    lower = mlb_advanced.load_park_factors_for_team("col")
    assert upper == lower
    assert upper["_data_complete"] == 1.0


def test_load_park_factors_for_team_aliases_espn_abbreviations_to_park_factors_keys():
    """Codex PR #30 P2: ESPN uses two-letter codes for some teams while
    park_factors.json uses three-letter (FanGraphs) codes. Without an
    alias, six real ESPN abbreviations silently fall back to neutral
    factors — losing park signal on six home venues.

    Confirmed via the user's DB: ESPN emits SF/SD/TB/KC/WSH/ATH; the
    matching park_factors keys are SFG/SDP/TBR/KCR/WSN/OAK.
    """
    espn_to_pf = {
        "SF": "SFG",   # San Francisco Giants
        "SD": "SDP",   # San Diego Padres
        "TB": "TBR",   # Tampa Bay Rays
        "KC": "KCR",   # Kansas City Royals
        "WSH": "WSN",  # Washington Nationals
        "ATH": "OAK",  # Oakland Athletics (ESPN rebrand)
    }
    for espn, pf in espn_to_pf.items():
        from_espn = mlb_advanced.load_park_factors_for_team(espn)
        from_pf = mlb_advanced.load_park_factors_for_team(pf)
        assert from_espn["_data_complete"] == 1.0, (
            f"ESPN abbreviation {espn!r} should resolve to {pf!r} but returned neutral"
        )
        assert from_espn == from_pf, (
            f"ESPN {espn!r} and park-factors {pf!r} should yield identical factors"
        )


def test_emit_mlb_batter_features_combines_sabermetrics_and_statcast():
    saber = {"season_avg": {"woba": 0.385, "iso": 0.230, "ops": 0.910, "obp": 0.380,
                             "slg": 0.530, "avg": 0.295, "wrc_plus": 145.0,
                             "walk_rate": 0.10, "strikeout_rate": 0.20, "babip": 0.310}}
    statcast = {"season_avg": {"xwoba": 0.395, "xba": 0.300, "xslg": 0.560,
                                "barrel_rate": 0.16, "hard_hit_rate": 0.55,
                                "exit_velocity_avg": 92.0, "launch_angle_avg": 14.0,
                                "sweet_spot_rate": 0.40}}
    out = mlb_advanced.emit_mlb_batter_features(saber, statcast)
    assert out["season_woba"] == 0.385
    assert out["season_xwoba"] == 0.395
    assert out["season_barrel_rate"] == 0.16
    assert out["season_iso"] == 0.230
    assert out["mlb_batter_data_complete"] == 1.0


def test_emit_mlb_batter_features_empty_for_missing_payload():
    assert mlb_advanced.emit_mlb_batter_features(None, None) == {}


def test_emit_mlb_pitcher_features_combines_sabermetrics_and_statcast():
    saber = {"season_avg": {"fip": 3.45, "xfip": 3.55, "xera": 3.30, "era": 3.20,
                             "whip": 1.05, "k_per_9": 11.0, "bb_per_9": 2.0, "hr_per_9": 0.9}}
    statcast = {"season_avg": {"avg_fastball_velo": 96.5, "whiff_pct": 0.30,
                                "csw_pct": 0.34, "putaway_pct": 0.20}}
    out = mlb_advanced.emit_mlb_pitcher_features(saber, statcast)
    assert out["opposing_starter_xfip"] == 3.55
    assert out["opposing_starter_avg_fastball_velo"] == 96.5
    assert out["opposing_starter_csw_pct"] == 0.34
    assert out["pitcher_data_complete"] == 1.0


def test_emit_park_features_passes_through_curated_factors():
    park = mlb_advanced.load_park_factors("22")  # Yankee Stadium
    out = mlb_advanced.emit_park_features(park)
    assert out["park_factor_hr"] > 1.10
    assert out["park_data_complete"] == 1.0


def test_emit_weather_features_normalizes_payload():
    weather = {"temp_f": 78.0, "wind_speed_mph": 8.5, "wind_dir_deg": 90.0,
               "precip_pct": 5.0, "humidity_pct": 60.0, "is_dome": False, "source": "openweather"}
    out = mlb_advanced.emit_weather_features(weather)
    assert out["weather_temp_f"] == 78.0
    assert out["weather_wind_speed_mph"] == 8.5
    assert out["weather_is_dome"] == 0.0
    assert out["weather_data_complete"] == 1.0


def test_load_weather_dome_short_circuit(db_session):
    result = mlb_advanced.load_weather(
        db_session,
        event_id="evt-1",
        lat=None,
        lon=None,
        game_time_utc=None,
        is_dome=True,
        allow_network=False,
    )
    assert result.cache_status == "dome"
    assert result.payload["is_dome"] is True


def test_load_weather_returns_miss_without_lat_lon(db_session):
    result = mlb_advanced.load_weather(
        db_session,
        event_id="evt-2",
        lat=None,
        lon=None,
        game_time_utc=None,
        is_dome=False,
        allow_network=True,
    )
    assert result.cache_status == "miss"


def test_load_weather_caches_persisted_payload(db_session):
    payload = {"temp_f": 75.0, "wind_speed_mph": 5.0, "wind_dir_deg": 180.0,
               "precip_pct": 10.0, "humidity_pct": 55.0, "is_dome": False, "source": "openweather"}
    db_session.add(
        MlbWeatherCache(
            event_id="evt-3",
            payload=payload,
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    db_session.flush()
    result = mlb_advanced.load_weather(
        db_session,
        event_id="evt-3",
        lat=None,
        lon=None,
        game_time_utc=None,
        is_dome=False,
        allow_network=False,
    )
    assert result.cache_status == "hit"
    assert result.payload == payload


def test_resolve_mlb_stats_player_id_uses_roster(db_session):
    teams_payload = {
        "teams": [
            {"id": 147, "name": "New York Yankees", "abbreviation": "NYY"},
            {"id": 119, "name": "Los Angeles Dodgers", "abbreviation": "LAD"},
        ]
    }
    rosters = {
        "147": {"roster": [{"person": {"id": 592450, "fullName": "Aaron Judge"}}]},
        "119": {"roster": [{"person": {"id": 660271, "fullName": "Shohei Ohtani"}}]},
    }
    stub = _StubMlbStatsClient(teams=teams_payload, rosters=rosters)
    resolved = mlb_advanced.resolve_mlb_stats_player_id(
        db_session,
        espn_athlete_id=None,
        full_name="Aaron Judge",
        team_abbreviation="NYY",
        season=2024,
        client=stub,
        allow_network=True,
    )
    assert resolved == "592450"
    assert db_session.query(MlbPlayerRosterCache).count() == 1


def test_resolve_mlb_stats_player_id_writes_back_to_search_cache(db_session):
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="MLB",
            query_normalized="aaron judge",
            payload={"athlete_id": "33192", "display_name": "Aaron Judge", "team_name": "New York Yankees"},
            cached_at=utcnow(),
            expires_at=utcnow() + timedelta(days=7),
        )
    )
    db_session.flush()
    teams = {"teams": [{"id": 147, "name": "New York Yankees", "abbreviation": "NYY"}]}
    rosters = {"147": {"roster": [{"person": {"id": 592450, "fullName": "Aaron Judge"}}]}}
    stub = _StubMlbStatsClient(teams=teams, rosters=rosters)
    resolved = mlb_advanced.resolve_mlb_stats_player_id(
        db_session,
        espn_athlete_id="33192",
        full_name="Aaron Judge",
        team_abbreviation="NYY",
        season=2024,
        client=stub,
        allow_network=True,
    )
    assert resolved == "592450"
    cache_row = db_session.query(EspnPlayerSearchCache).one()
    assert cache_row.payload["mlb_stats_id"] == "592450"


def test_warm_mlb_advanced_returns_summary(db_session):
    stub = _StubMlbStatsClient(
        sabermetrics=_saber_payload(0.380, 0.225),
        season_stats=_hitting_payload(),
        teams={"teams": [{"id": 147, "name": "Yankees", "abbreviation": "NYY"}]},
        rosters={"147": {"roster": [{"person": {"id": 592450, "fullName": "Aaron Judge"}}]}},
    )
    summary = mlb_advanced.warm_mlb_advanced_for_athletes(
        db_session, mlb_stats_player_ids=["592450"], season=2024, client=stub
    )
    assert summary["mlb_batters_attempted"] == 1
    assert summary["mlb_batters_succeeded"] == 1
    assert summary["mlb_roster_loaded"] == 1

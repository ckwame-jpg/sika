from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Kalshi Sports Copilot API"
    environment: str = "development"
    database_url: str = "sqlite:///./kalshi_sports_copilot.db"
    sports_api_base_url: str = "https://www.thesportsdb.com/api/v1/json"
    sports_api_key: str = "123"
    kalshi_public_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_demo_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"
    kalshi_key_id: str = ""
    kalshi_private_key_path: Path = Path.home() / ".config" / "kalshi" / "kalshi-demo.key"
    default_timezone: str = "America/Chicago"
    web_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3001",
        ]
    )
    scheduler_enabled: bool = True
    refresh_interval_minutes: int = 5
    prop_refresh_interval_minutes: int = 5
    queue_poll_interval_seconds: int = 5
    maintenance_claim_budget_seconds: int = 25
    cleanup_interval_hours: int = 6
    startup_refresh_stale_after_minutes: int = 15
    espn_player_search_cache_hours: int = 168
    nba_prop_gamelog_cache_minutes: int = 30
    mlb_prop_gamelog_cache_minutes: int = 60
    current_slate_lookback_days: int = 0
    current_slate_lookahead_days: int = 1
    watchlist_min_edge: float = 0.03
    watchlist_min_confidence: float = 0.35
    watchlist_min_selected_prob_heuristic_winner: float = 0.20
    ml_serving_mode: Literal["heuristic", "shadow", "ml"] = "heuristic"
    ml_manifest_path: str = ""
    ml_family_modes_json: str = ""
    parlay_min_legs: int = 2
    parlay_max_legs: int = 6
    parlay_candidate_pool_size: int = 10
    parlay_max_output: int = 15
    parlay_enabled_sports: list[str] = Field(default_factory=lambda: ["NBA", "MLB"])
    lookback_days: int = 14
    lookahead_days: int = 2
    free_provider_lookback_days: int = 5
    free_provider_lookahead_days: int = 2
    market_snapshot_retention_days: int = 3
    signal_snapshot_retention_days: int = 2
    shadow_inference_retention_days: int = 30
    run_retention_days: int = 14
    refresh_job_retention_days: int = 14
    prediction_retention_days: int = 7
    refresh_job_stale_minutes: int = 30
    market_snapshot_heartbeat_minutes: int = 30
    prefer_yes_side_props: bool = True
    enabled_sports: list[str] = Field(default_factory=lambda: ["NBA", "NFL", "MLB", "SOCCER", "TENNIS"])
    soccer_leagues: list[str] = Field(
        default_factory=lambda: [
            "English Premier League",
            "UEFA Champions League",
            "Major League Soccer",
            "FIFA World Cup",
            "UEFA European Championship",
        ]
    )

    advanced_stats_enabled: bool = True
    nba_stats_source: Literal["nba_stats", "basketball_reference"] = "basketball_reference"
    nba_stats_base_url: str = "https://stats.nba.com/stats"
    nba_stats_rate_limit_rps: float = 0.6
    nba_stats_rate_limit_burst: float = 2.0
    nba_stats_daily_request_cap: int = 500
    basketball_reference_base_url: str = "https://www.basketball-reference.com"
    basketball_reference_rate_limit_rps: float = 0.3
    basketball_reference_rate_limit_burst: float = 1.0
    nba_advanced_cache_minutes: int = 240
    nba_team_advanced_cache_minutes: int = 1440
    nba_team_gamelog_cache_minutes: int = 360
    nba_lineup_advanced_cache_minutes: int = 1440
    nba_boxscore_advanced_cache_minutes: int = 10080  # historical games — 1 week
    nba_player_roster_cache_minutes: int = 1440
    nba_league_percentiles_cache_minutes: int = 1440
    nba_hustle_player_cache_minutes: int = 1440
    nba_tracking_cache_minutes: int = 720
    nba_clutch_cache_minutes: int = 1440
    nba_player_defense_cache_minutes: int = 1440
    nba_injury_report_cache_minutes: int = 60

    # MLB advanced stats
    mlb_batter_advanced_cache_minutes: int = 360
    mlb_pitcher_advanced_cache_minutes: int = 360
    mlb_statcast_batter_cache_minutes: int = 720
    mlb_statcast_pitcher_cache_minutes: int = 720
    mlb_player_splits_cache_minutes: int = 1440
    mlb_team_gamelog_cache_minutes: int = 360
    mlb_bullpen_state_cache_minutes: int = 60
    mlb_lineup_cache_minutes: int = 60
    mlb_weather_cache_minutes: int = 30
    mlb_player_roster_cache_minutes: int = 1440
    mlb_league_percentiles_cache_minutes: int = 1440
    mlb_injury_report_cache_minutes: int = 240

    # Weather
    openweather_api_key: str = ""
    nws_user_agent: str = "sika-sports-copilot"


@lru_cache
def get_settings() -> Settings:
    return Settings()

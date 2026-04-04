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
    kalshi_private_key_path: Path = Path("/Users/chris/.config/kalshi/kalshi-demo.key")
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
    app_role: str = "web"
    refresh_interval_minutes: int = 5
    prop_refresh_interval_minutes: int = 20
    queue_poll_interval_seconds: int = 5
    cleanup_interval_hours: int = 24
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
    market_snapshot_retention_days: int = 14
    signal_snapshot_retention_days: int = 14
    shadow_inference_retention_days: int = 14
    run_retention_days: int = 30
    enabled_sports: list[str] = Field(default_factory=lambda: ["NBA", "NFL", "MLB", "SOCCER", "TENNIS", "UFC"])
    soccer_leagues: list[str] = Field(
        default_factory=lambda: [
            "English Premier League",
            "UEFA Champions League",
            "Major League Soccer",
            "FIFA World Cup",
            "UEFA European Championship",
        ]
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

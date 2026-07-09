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
    # Bug #51: cap individual DB statements issued by the refresh worker
    # on Postgres so a slow query (lock contention, missing index, table
    # bloat) can't pin a worker thread indefinitely and leak its pool
    # connection. SQLite has no equivalent — the setting is a no-op there.
    # 30s is well under the smallest per-kind worker timeout (60s for
    # injury/referee refresh) so the worker's exception path still has
    # time to fail the job cleanly when the statement_timeout fires.
    refresh_worker_statement_timeout_seconds: int = 30
    # Smarter #14 — event-aware scheduler bursts. When ANY event has a
    # tip-off within ``near_tip_off_window_minutes`` in the future, OR
    # started within ``live_game_window_hours`` in the past (i.e. is
    # likely still in progress), the current-slate refresh cadence
    # shortens from ``refresh_interval_minutes`` to
    # ``near_tip_off_refresh_interval_minutes``. Defaults: 30min
    # pre-tip / 4h post-tip / 1min burst cadence. 4h is generous for
    # MLB extras; tighten if it costs too many DB reads.
    near_tip_off_window_minutes: int = 30
    live_game_window_hours: int = 4
    near_tip_off_refresh_interval_minutes: int = 1
    espn_player_search_cache_hours: int = 168
    nba_prop_gamelog_cache_minutes: int = 30
    mlb_prop_gamelog_cache_minutes: int = 60
    # WNBA shares NBA's payload shape + cadence; default to the NBA TTL.
    wnba_prop_gamelog_cache_minutes: int = 30
    # Smarter NFL PR 1 — NFL players log one game a week, so a 6-hour
    # gamelog TTL keeps game-day freshness without the daily-sport churn
    # the 30-minute NBA TTL is sized for.
    nfl_prop_gamelog_cache_minutes: int = 360
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
    # Bug #19: two-tier retention for ML-relevant rows. The short
    # ``*_retention_days`` settings above are the UI / runtime TTL —
    # they reap UNSETTLED predictions and shadows that never paired
    # with a real outcome. The ``*_archive_retention_days`` settings
    # below extend that TTL for rows that DID settle, so training,
    # calibration, and promotion can read the historical outcomes
    # they need without the runtime cleanup eating its own data.
    prediction_archive_retention_days: int = 365
    shadow_inference_archive_retention_days: int = 365
    refresh_job_stale_minutes: int = 30
    market_snapshot_heartbeat_minutes: int = 30
    prefer_yes_side_props: bool = True
    # Active ship target: NBA + MLB + WNBA (Smarter WNBA PR 6 added
    # WNBA). NFL stays in the list as the next-up sport (research_only
    # mode until the per-sport pipeline ships). Tennis remains
    # research_only. ``parlay_enabled_sports`` above stays NBA / MLB
    # only; ``parlay_family_key`` has no WNBA-specific family yet, and
    # adding WNBA without one would silently route WNBA combos into
    # ``mixed_parlay_*`` and pollute mixed-family calibration.
    #
    # Soccer + UFC were removed from scope on 2026-05-17 — their
    # adapters, ESPN slugs, Odds API mappings, stats-query branches,
    # and front-end UI surfaces are all deleted in the same change.
    enabled_sports: list[str] = Field(default_factory=lambda: ["NBA", "NFL", "MLB", "TENNIS", "WNBA"])

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

    # WNBA advanced stats — WNBA's ESPN payloads mirror NBA's shape, so
    # the cache TTLs mirror NBA defaults as the starting point. Once
    # WNBA-specific data sources land (Smarter follow-up — generalized
    # stats client, RefMetrics for referees), the team_advanced /
    # referee TTLs can diverge from NBA's.
    wnba_advanced_cache_minutes: int = 240
    wnba_team_advanced_cache_minutes: int = 1440
    wnba_referee_assignments_cache_minutes: int = 240

    # NFL data layer (Smarter NFL PR 1; consumed by the nflverse client
    # + nfl_advanced loaders from PR 3 onward). nflverse release assets
    # update nightly during the season, so the bulk datasets default to
    # 24h TTLs; the injury report and weather run tighter because they
    # move on game day.
    nfl_injury_report_cache_minutes: int = 240
    nfl_weekly_stats_cache_minutes: int = 1440
    nfl_snap_counts_cache_minutes: int = 1440
    nfl_depth_chart_cache_minutes: int = 1440
    nfl_team_rating_cache_minutes: int = 1440
    nfl_schedule_cache_minutes: int = 10080  # weekly — slate + rest days shift rarely
    nfl_weather_cache_minutes: int = 60
    # Margin adjustment (points) applied against a team whose depth-chart
    # QB1 is OUT / doubtful on a fresh injury report. Literature range is
    # 3–7 depending on the backup; 4.5 is the conservative midpoint and
    # the 2025 backtest (Smarter NFL PR 9) re-tunes it.
    nfl_qb_out_margin_penalty: float = 4.5

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

    # Smarter #18 — The Odds API (sportsbook implied-probability prior).
    # Free tier with 500 monthly requests; empty key disables fetching
    # gracefully (no error, all consumer paths skip the prior).
    the_odds_api_key: str = ""
    the_odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    the_odds_api_request_timeout_seconds: float = 15.0
    # Smarter #18 phase 2 — H2H quote cache TTL. Free-tier monthly cap
    # (500 reqs) is the binding constraint; with NBA + MLB simultaneously
    # active, ~30 minutes between fetches gives headroom for ~2 fetches
    # per sport per hour over a 12-hour active window (~48/day,
    # ~1500/month — well under the cap with 2+ sports).
    the_odds_api_cache_ttl_minutes: int = 30
    # Smarter NFL PR 1 — per-sport TTL overrides so a 4th active sport
    # doesn't blow the free-tier budget. NFL lines are weekly-cadence:
    # 6 hours between fetches is plenty outside the pre-kick window, and
    # the PR 4 event-window gate stops fetches entirely when no NFL game
    # is near. Sports without an entry fall back to the global TTL.
    the_odds_api_cache_ttl_minutes_by_sport: dict[str, int] = Field(
        default_factory=lambda: {"NFL": 360}
    )

    # Smarter #13 phase 2 — NBA referee-assignments cache TTL. NBA
    # posts assignments the afternoon-of for that night's games
    # (typically around 5pm ET); 4 hours refreshes ~6x/day so the
    # publication window is captured without hammering the upstream.
    nba_referee_assignments_cache_minutes: int = 240

    # Smarter #31 — LLM narrator (OpenAI).
    # Off by default. Operators toggle via the model-readiness settings
    # endpoint (persisted in ``OperatorSetting``) so flipping the
    # feature on/off doesn't require a redeploy. The OpenAI key is
    # read from ``OPENAI_API_KEY`` env; empty key → narrator service
    # short-circuits with a clean "not configured" status.
    openai_api_key: str = ""
    narrator_openai_model: str = "gpt-4o-mini"
    narrator_openai_base_url: str = "https://api.openai.com/v1"
    narrator_request_timeout_seconds: float = 25.0
    narrator_max_output_tokens: int = 220

    # Smarter #9 phase 2 — fractional Kelly position sizing inputs.
    # ``kelly_sizing_bankroll_dollars`` is the operator's fallback
    # bankroll when the Kalshi-balance opt-in is off (or the
    # account isn't connected). A small default sized for paper /
    # demo testing — operators with real money override via env.
    kelly_sizing_bankroll_dollars: float = 1000.0
    # Opt-in: when True AND the Kalshi account is connected, the
    # bankroll resolver returns the live account total instead of
    # the static ``kelly_sizing_bankroll_dollars``. Defaults off so
    # an account-connection blip can't silently change position
    # sizes mid-session.
    kelly_sizing_use_kalshi_balance: bool = False
    # Hypothetical notional used by ``compute_rolling_pnl_fraction``
    # to convert per-share realized PnL (which is what the
    # Prediction table tracks today, ahead of per-prediction sizing
    # persistence) into a bankroll-relative drawdown signal. A
    # reasonable proxy when actual sizes aren't recorded; phase 2b
    # will replace this with persisted ``suggested_size_dollars``
    # per prediction once sizing lands in the schema.
    kelly_sizing_assumed_notional_dollars: float = 100.0

    # Multi-user identity (PAPER_PARLAY_SCOPE.md / multi-user batch step 1).
    # SIKA_USERS is a comma-separated list of usernames that get seeded
    # into the ``users`` table on API startup. New user = edit .env,
    # restart. SIKA_KALSHI_OWNER names the user whose user_id should
    # be attached to the existing env-var Kalshi credentials when the
    # per-user Kalshi migration (PR 4) runs. Both default to empty;
    # an empty users list means single-tenant (no auth, legacy
    # behavior).
    users_csv: str = Field(default="", validation_alias="SIKA_USERS")
    kalshi_owner: str = Field(default="", validation_alias="SIKA_KALSHI_OWNER")

    @property
    def users(self) -> list[str]:
        """Parsed list of usernames from SIKA_USERS=chris,canaan."""
        return [name.strip() for name in self.users_csv.split(",") if name.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

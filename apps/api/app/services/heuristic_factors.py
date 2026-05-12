"""Heuristic equation factors driven by advanced stats.

The base scoring kernel in ``app.services.scoring`` already applies a few
hand-crafted multiplicative factors built from box-score proxies (rest,
location, opponent, NBA usage proxy, MLB ERA proxy). This module adds a
second pass of multipliers that fire when *real* advanced stats are
present in the features dict — replacing the proxies where they overlap
and adding net-new factors (efficiency, quality-of-contact, pitcher
dominance, park, weather, lineup).

Each factor:
  - Defaults to ``1.0`` when its source data is absent.
  - Clamps to ``[0.85, 1.15]`` (existing convention) to avoid runaway
    swings on anomalous inputs.
  - Maps to a list of stat keys via the ``_*_FACTORS_BY_STAT`` dicts so
    each prop only sees the factors that are physically meaningful (a
    "drives_per_game" feature does nothing for a strikeout prop).
"""

from __future__ import annotations

from typing import Any


_FACTOR_CLAMP_LOW = 0.85
_FACTOR_CLAMP_HIGH = 1.15


def _clamp(value: float, lo: float = _FACTOR_CLAMP_LOW, hi: float = _FACTOR_CLAMP_HIGH) -> float:
    return max(lo, min(hi, value))


def _safe_ratio(numerator: Any, denominator: Any, *, default: float = 1.0) -> float:
    if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)):
        return default
    if denominator == 0:
        return default
    return float(numerator) / float(denominator)


# -----------------------------------------------------------------------------
# Stat-keyed factor gating

_NBA_FACTORS_BY_STAT: dict[str, tuple[str, ...]] = {
    "points": ("efficiency_factor", "opp_def_factor", "opp_recent_form_factor",
               "pace_factor_advanced", "usage_factor_advanced"),
    # ``made_threes`` is the canonical stat_key produced by market_support's
    # alias normalization; ``three_points_made`` is the raw_metrics column
    # name. Both must map to the same gating tuple so props arriving as
    # either key receive the advanced factors.
    "made_threes": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                     "usage_factor_advanced"),
    "three_points_made": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                           "usage_factor_advanced"),
    "field_goals_made": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                          "usage_factor_advanced"),
    "rebounds": ("pace_factor_advanced", "opp_recent_form_factor"),
    "assists": ("opp_def_factor", "pace_factor_advanced", "usage_factor_advanced"),
    "steals": ("pace_factor_advanced",),
    "blocks": ("pace_factor_advanced",),
    "turnovers": ("usage_factor_advanced",),
    "points_assists": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                       "usage_factor_advanced"),
    "points_rebounds": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                         "usage_factor_advanced"),
    "rebounds_assists": ("opp_def_factor", "pace_factor_advanced", "opp_recent_form_factor"),
    "points_rebounds_assists": ("efficiency_factor", "opp_def_factor",
                                  "pace_factor_advanced", "usage_factor_advanced"),
}


_MLB_FACTORS_BY_STAT: dict[str, tuple[str, ...]] = {
    "hits": ("xstats_anchor_factor", "starter_factor_advanced",
             "pitcher_dominance_factor", "park_factor_singles"),
    "home_runs": ("quality_of_contact_factor", "starter_factor_advanced",
                   "park_factor_hr_mult", "weather_factor", "pitcher_dominance_factor"),
    "rbis": ("lineup_factor", "park_factor_runs_mult", "starter_factor_advanced"),
    "runs": ("lineup_factor", "park_factor_runs_mult"),
    "total_bases": ("quality_of_contact_factor", "starter_factor_advanced",
                     "park_factor_singles", "weather_factor"),
    # Bug #3: pitcher_dominance_factor returns < 1.0 for dominant pitchers
    # (correct for batter hits/HR/walks where their output drops). For batter
    # strikeouts a dominant pitcher should AMPLIFY expected count.
    # k_rate_factor (k9/8.5) covers the common case; strikeout_dominance_factor
    # is the partial-cache fallback that fires when K/9 is missing but CSW
    # or whiff is available (codex PR #27 P2 — caches can be out of sync).
    "strikeouts": ("k_rate_factor", "strikeout_dominance_factor"),
    "walks": ("pitcher_dominance_factor",),
    "doubles": ("park_factor_singles", "starter_factor_advanced"),
    "triples": ("park_factor_singles",),
}


# -----------------------------------------------------------------------------
# NBA factors

def _nba_efficiency_factor(features: dict[str, Any]) -> float:
    return _clamp(_safe_ratio(features.get("recent_true_shooting_pct"),
                                features.get("season_true_shooting_pct")))


def _nba_opp_def_factor(features: dict[str, Any]) -> float:
    drtg = features.get("opponent_defensive_rating_season")
    if not isinstance(drtg, (int, float)) or drtg <= 0:
        return 1.0
    # Lower DRtg → harder matchup → expected DOWN. 110 ≈ league avg, so a
    # DRtg of 100 yields 100/110 ≈ 0.91 (suppressed) and 120 yields ~1.09.
    return _clamp(float(drtg) / 110.0)


def _nba_opp_recent_form_factor(features: dict[str, Any]) -> float:
    drtg = features.get("opponent_def_rating_recent_5")
    if not isinstance(drtg, (int, float)) or drtg <= 0:
        return 1.0
    return _clamp(float(drtg) / 110.0)


def _nba_pace_factor_advanced(features: dict[str, Any]) -> float:
    pace = features.get("opponent_pace_recent_5") or features.get("opponent_pace_season")
    if not isinstance(pace, (int, float)) or pace <= 0:
        return 1.0
    return _clamp(float(pace) / 100.0)


def _nba_usage_factor_advanced(features: dict[str, Any]) -> float:
    return _clamp(_safe_ratio(features.get("recent_usage_pct"),
                                features.get("season_usage_pct")))


_NBA_FACTOR_FNS = {
    "efficiency_factor": _nba_efficiency_factor,
    "opp_def_factor": _nba_opp_def_factor,
    "opp_recent_form_factor": _nba_opp_recent_form_factor,
    "pace_factor_advanced": _nba_pace_factor_advanced,
    "usage_factor_advanced": _nba_usage_factor_advanced,
}


# -----------------------------------------------------------------------------
# MLB factors

def _mlb_xstats_anchor_factor(features: dict[str, Any]) -> float:
    """Hits-style props: regress recent average toward season-level expected
    stats (xBA / xwOBA) to capture luck.

    The blend is between two ratios:
      - actual: how the player has performed recently vs season AVG.
      - expected: where the player's season-level xBA sits vs their season AVG.

    A future enhancement (called out in CODEX_REVIEW_NOTES.md) is to cache
    *rolling-window* xBA so we can compare recent-xBA → season-xBA for a
    proper luck-regression view. Until that cache exists, this factor only
    nudges the prediction toward season-level expected stats — useful when
    a player's actual AVG is well below their xBA (positive regression),
    or above (negative regression).
    """
    recent_avg = features.get("recent_3_average") or features.get("recent_10_average")
    season_avg = features.get("season_average")
    season_xba = features.get("season_xba")
    if not all(isinstance(v, (int, float)) for v in (recent_avg, season_avg, season_xba)):
        return 1.0
    if season_avg in (None, 0):
        return 1.0
    actual_ratio = float(recent_avg) / float(season_avg)
    # season_xba / season_avg captures luck-regression direction for the
    # season as a whole; ~1.0 when actual matches expected.
    expected_ratio = float(season_xba) / float(season_avg)
    return _clamp(0.5 * actual_ratio + 0.5 * expected_ratio)


def _mlb_quality_of_contact_factor(features: dict[str, Any]) -> float:
    barrel = features.get("season_barrel_rate")
    hard_hit = features.get("season_hard_hit_rate")
    if isinstance(barrel, (int, float)) and barrel > 0:
        # 7% barrel rate is roughly league average; scale around that.
        return _clamp(float(barrel) / 0.07)
    if isinstance(hard_hit, (int, float)) and hard_hit > 0:
        return _clamp(float(hard_hit) / 0.40)
    return 1.0


def _mlb_starter_factor_advanced(features: dict[str, Any]) -> float:
    xfip = features.get("opposing_starter_xfip")
    if isinstance(xfip, (int, float)) and xfip > 0:
        return _clamp(float(xfip) / 4.0)
    fip = features.get("opposing_starter_fip")
    if isinstance(fip, (int, float)) and fip > 0:
        return _clamp(float(fip) / 4.0)
    return 1.0


def _mlb_k_rate_factor(features: dict[str, Any]) -> float:
    k9 = features.get("opposing_starter_k_per_9")
    if isinstance(k9, (int, float)) and k9 > 0:
        return _clamp(float(k9) / 8.5)
    return 1.0


def _mlb_pitcher_dominance_factor(features: dict[str, Any]) -> float:
    csw = features.get("opposing_starter_csw_pct")
    if isinstance(csw, (int, float)) and csw > 0:
        # Higher CSW% → more dominant → expected DOWN for hits/HR; invert ratio.
        return _clamp(0.30 / float(csw))
    whiff = features.get("opposing_starter_whiff_pct")
    if isinstance(whiff, (int, float)) and whiff > 0:
        return _clamp(0.25 / float(whiff))
    return 1.0


def _mlb_strikeout_dominance_factor(features: dict[str, Any]) -> float:
    """Strikeouts-only amplifier — mirror of ``_mlb_pitcher_dominance_factor``.

    Bug #3 + codex PR #27 P2 fallback: a dominant pitcher should AMPLIFY
    expected batter strikeouts. When ``opposing_starter_k_per_9`` is
    present the ``k_rate_factor`` covers that signal, so this returns 1.0
    to avoid double-amplification. When K/9 is missing (Statcast cache
    is warm but sabermetrics aren't), fall back to CSW or whiff as the
    amplifier source so the strikeouts gate isn't left neutral.
    """
    k9 = features.get("opposing_starter_k_per_9")
    if isinstance(k9, (int, float)) and k9 > 0:
        return 1.0
    csw = features.get("opposing_starter_csw_pct")
    if isinstance(csw, (int, float)) and csw > 0:
        return _clamp(float(csw) / 0.30)
    whiff = features.get("opposing_starter_whiff_pct")
    if isinstance(whiff, (int, float)) and whiff > 0:
        return _clamp(float(whiff) / 0.25)
    return 1.0


def _mlb_park_factor_hr_mult(features: dict[str, Any]) -> float:
    return _clamp(float(features.get("park_factor_hr") or 1.0))


def _mlb_park_factor_runs_mult(features: dict[str, Any]) -> float:
    return _clamp(float(features.get("park_factor_runs") or 1.0))


def _mlb_park_factor_singles(features: dict[str, Any]) -> float:
    return _clamp(float(features.get("park_factor_singles") or 1.0))


def _mlb_weather_factor(features: dict[str, Any]) -> float:
    """Composite for fly-ball props: temperature + wind out to CF.

    - Cold suppresses HR ~10% per 30°F drop from the 75°F baseline.
    - Wind blowing out (azimuth ~0°-30° toward CF) adds boost; in suppresses.
    """
    if features.get("weather_is_dome") == 1.0:
        return 1.0
    temp = features.get("weather_temp_f")
    wind_speed = features.get("weather_wind_speed_mph")
    wind_dir = features.get("weather_wind_dir_deg")
    factor = 1.0
    if isinstance(temp, (int, float)):
        factor *= 1.0 + ((float(temp) - 75.0) * 0.10 / 30.0)
    if isinstance(wind_speed, (int, float)) and isinstance(wind_dir, (int, float)):
        # Wind blowing out to CF helps — angle near 0° (north). 180° suppresses.
        # Very rough projection: cos(angle) gives the out-to-in component.
        import math
        component = math.cos(math.radians(float(wind_dir)))
        factor *= 1.0 + (component * float(wind_speed) * 0.005)
    return _clamp(factor)


def _mlb_lineup_factor(features: dict[str, Any]) -> float:
    """Batting-order position multiplier: leadoff/2-hole get ~5% PA boost."""
    order = features.get("batting_order_position")
    if not isinstance(order, (int, float)):
        return 1.0
    order = int(order)
    if order <= 2:
        return 1.05
    if order in (3, 4):
        return 1.02
    if order >= 8:
        return 0.96
    return 1.0


_MLB_FACTOR_FNS = {
    "xstats_anchor_factor": _mlb_xstats_anchor_factor,
    "quality_of_contact_factor": _mlb_quality_of_contact_factor,
    "starter_factor_advanced": _mlb_starter_factor_advanced,
    "k_rate_factor": _mlb_k_rate_factor,
    "pitcher_dominance_factor": _mlb_pitcher_dominance_factor,
    "strikeout_dominance_factor": _mlb_strikeout_dominance_factor,
    "park_factor_hr_mult": _mlb_park_factor_hr_mult,
    "park_factor_runs_mult": _mlb_park_factor_runs_mult,
    "park_factor_singles": _mlb_park_factor_singles,
    "weather_factor": _mlb_weather_factor,
    "lineup_factor": _mlb_lineup_factor,
}


# -----------------------------------------------------------------------------
# Public API

def compute_advanced_factors(
    sport_key: str,
    stat_key: str,
    features: dict[str, Any],
) -> dict[str, float]:
    """Return a dict of factor_name → multiplier for the (sport, stat) pair.

    Only factors gated for ``stat_key`` and whose source data is present
    will appear in the output. Caller multiplies ``expected`` by each value
    and is responsible for writing the chosen factors into the features
    dict for downstream attribution.
    """
    sport = sport_key.upper()
    if sport == "NBA":
        gating = _NBA_FACTORS_BY_STAT.get(stat_key) or _NBA_FACTORS_BY_STAT.get(stat_key.lower())
        fns = _NBA_FACTOR_FNS
    elif sport == "MLB":
        gating = _MLB_FACTORS_BY_STAT.get(stat_key) or _MLB_FACTORS_BY_STAT.get(stat_key.lower())
        fns = _MLB_FACTOR_FNS
    else:
        return {}

    if not gating:
        return {}

    out: dict[str, float] = {}
    for name in gating:
        fn = fns.get(name)
        if fn is None:
            continue
        value = fn(features)
        if abs(value - 1.0) >= 1e-4:  # Drop no-op factors so the dict stays clean
            out[name] = round(value, 4)
    return out


def factor_applies(sport_key: str, stat_key: str, factor_name: str) -> bool:
    """Return True when ``factor_name`` is wired into the per-stat gating
    tuple for the given (sport, stat) pair.

    Used by the scoring kernel to decide whether suppressing the
    corresponding box-score proxy is safe: if the advanced replacement is
    NOT in the tuple, ``compute_advanced_factors`` won't emit it, so the
    proxy must continue to apply (otherwise both drop out and the
    prediction loses signal entirely).
    """
    sport = sport_key.upper()
    if sport == "NBA":
        gating = _NBA_FACTORS_BY_STAT.get(stat_key) or _NBA_FACTORS_BY_STAT.get(stat_key.lower())
    elif sport == "MLB":
        gating = _MLB_FACTORS_BY_STAT.get(stat_key) or _MLB_FACTORS_BY_STAT.get(stat_key.lower())
    else:
        return False
    return bool(gating) and factor_name in gating


def apply_factors(expected: float, factors: dict[str, float]) -> float:
    """Multiply ``expected`` by every factor and return the new value."""
    out = float(expected)
    for value in factors.values():
        out *= float(value)
    return out

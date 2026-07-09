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

# Smarter #11 ``workload_factor`` is gated on the stat keys where fatigue
# measurably suppresses output. Defensive stats (steals, blocks) and
# ball-control negatives (turnovers) are intentionally excluded — a
# fatigued rotation regular is no less likely to commit a turnover.
#
# Smarter #10 ``nba_rest_factor`` and ``nba_travel_factor`` follow the
# same gating rationale — fatigue/travel meaningfully drags scoring
# output but not defensive counting stats. Both stack with the workload
# factor; the envelopes are conservative (±5% / ±6% / ±2%) so the
# combined worst-case suppression stays bounded.
_NBA_FACTORS_BY_STAT: dict[str, tuple[str, ...]] = {
    "points": ("efficiency_factor", "opp_def_factor", "opp_recent_form_factor",
               "pace_factor_advanced", "usage_factor_advanced", "workload_factor",
               "nba_rest_factor", "nba_travel_factor", "nba_referee_factor"),
    # ``made_threes`` is the canonical stat_key produced by market_support's
    # alias normalization; ``three_points_made`` is the raw_metrics column
    # name. Both must map to the same gating tuple so props arriving as
    # either key receive the advanced factors.
    "made_threes": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                     "usage_factor_advanced", "workload_factor",
                     "nba_rest_factor", "nba_travel_factor"),
    "three_points_made": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                           "usage_factor_advanced", "workload_factor",
                           "nba_rest_factor", "nba_travel_factor"),
    "field_goals_made": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                          "usage_factor_advanced", "workload_factor",
                          "nba_rest_factor", "nba_travel_factor"),
    "rebounds": ("pace_factor_advanced", "opp_recent_form_factor", "workload_factor",
                  "nba_rest_factor", "nba_travel_factor"),
    "assists": ("opp_def_factor", "pace_factor_advanced", "usage_factor_advanced",
                 "workload_factor", "nba_rest_factor", "nba_travel_factor"),
    "steals": ("pace_factor_advanced",),
    "blocks": ("pace_factor_advanced",),
    "turnovers": ("usage_factor_advanced",),
    "points_assists": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                       "usage_factor_advanced", "workload_factor",
                       "nba_rest_factor", "nba_travel_factor", "nba_referee_factor"),
    "points_rebounds": ("efficiency_factor", "opp_def_factor", "pace_factor_advanced",
                         "usage_factor_advanced", "workload_factor",
                         "nba_rest_factor", "nba_travel_factor", "nba_referee_factor"),
    "rebounds_assists": ("opp_def_factor", "pace_factor_advanced", "opp_recent_form_factor",
                          "workload_factor", "nba_rest_factor", "nba_travel_factor"),
    "points_rebounds_assists": ("efficiency_factor", "opp_def_factor",
                                  "pace_factor_advanced", "usage_factor_advanced",
                                  "workload_factor",
                                  "nba_rest_factor", "nba_travel_factor",
                                  "nba_referee_factor"),
}


_MLB_FACTORS_BY_STAT: dict[str, tuple[str, ...]] = {
    "hits": ("xstats_anchor_factor", "starter_factor_advanced",
             "pitcher_dominance_factor", "park_factor_singles",
             "batter_platoon_factor"),
    "home_runs": ("quality_of_contact_factor", "starter_factor_advanced",
                   "park_factor_hr_mult", "weather_factor",
                   "pitcher_dominance_factor", "batter_platoon_factor",
                   "park_weather_hr_interaction"),
    "rbis": ("lineup_factor", "park_factor_runs_mult", "starter_factor_advanced",
             "batter_platoon_factor", "opposing_bullpen_rest_factor"),
    "runs": ("lineup_factor", "park_factor_runs_mult", "batter_platoon_factor",
             "opposing_bullpen_rest_factor"),
    "total_bases": ("quality_of_contact_factor", "starter_factor_advanced",
                     "park_factor_singles", "weather_factor",
                     "batter_platoon_factor", "park_weather_hr_interaction"),
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


def _nba_workload_factor(features: dict[str, Any]) -> float:
    """Smarter #11: top-quartile recent minutes drags a player's prop
    slightly downward (fatigue suppresses output); below-median minutes
    nudge slightly upward (rested rotation regular is dangerous).

    League-wide rotation MPG averages around 28; top-quartile starts at
    ~34; bottom-quartile sits around 22. The envelope is intentionally
    narrow (±5%) — the heuristic should not dominate a real-stats-driven
    factor like ``usage_factor_advanced``.
    """
    mpg = features.get("recent_workload_minutes_per_game")
    # ``bool`` is a subclass of ``int`` in Python — reject explicitly so
    # a stray ``True`` doesn't get coerced to MPG=1 (which would falsely
    # fire the ≤22 rest-boost branch).
    if not isinstance(mpg, (int, float)) or isinstance(mpg, bool):
        return 1.0
    if mpg >= 34.0:
        return 0.96
    if mpg <= 22.0:
        return 1.03
    return 1.0


def _nba_rest_factor(features: dict[str, Any]) -> float:
    """Smarter #10: granular NBA fatigue / rest factor.

    Three mutually-exclusive cases (one fires, others can't by construction):
      - 4th game in 6 nights: 0.94 (strongest suppression — accumulated
        fatigue, common to draw a rested opponent).
      - 3rd game in 4 nights: 0.96 (the classic NBA fatigue trigger).
      - 3+ days rest: 1.02 (mild boost — rested rotation regular returns
        to baseline output).

    Suppressors win over the rest boost on the rare days where both could
    theoretically apply (in practice 3+ days rest is incompatible with
    3rd-in-four, which requires 2 games in the last 3 days). Envelope ±6%.
    """
    if bool(features.get("team_is_fourth_in_six")):
        return 0.94
    if bool(features.get("team_is_third_in_four")):
        return 0.96
    days_rest = features.get("team_days_rest")
    # ``bool`` is a subclass of ``int`` — reject so ``True`` isn't read as
    # 1 day of rest (and below the ≥3 threshold either way, but the
    # intent contract is "actual numeric days rest, not a flag").
    if (
        isinstance(days_rest, (int, float))
        and not isinstance(days_rest, bool)
        and days_rest >= 3.0
    ):
        return 1.02
    return 1.0


def _nba_travel_factor(features: dict[str, Any]) -> float:
    """Smarter #10 travel proxy (Phase 1): continuous road trip.

    Fires only when today is an away game AND the prior game was also
    away — the handoff's explicit "still on the road" case. Returns 0.98
    (mild suppression). All other home/away combinations return 1.0;
    Phase 2 will replace this with mileage between venue lat/lons.
    """
    is_home_today = features.get("team_is_home")
    last_game_away = features.get("team_last_game_away")
    if is_home_today is False and last_game_away is True:
        return 0.98
    return 1.0


# Smarter #13 phase 2d — referee tendency factor.
#
# League-average personal fouls per game is ~42 across both teams.
# A tight-calling crew (>42) means more FT trips, which boost total
# points. A loose crew (<42) suppresses. The envelope mirrors the
# existing workload / rest factors at ±5% so the heuristic doesn't
# dominate stronger signals like opp_def_factor.
#
# Each whole-foul deviation from league average shifts the factor by
# 0.5% (0.005). 42 → 1.0; 47 → 1.025; 37 → 0.975; clamped at ±5%.
#
# Gated on ``referee_data_complete == 1.0`` (≥2 of 3 crew matched in
# the tendency cache; see ``nba_referee_emit._MIN_CREW_FOR_DATA_COMPLETE``)
# so single-ref matches with high tendency-variance can't drive a
# factor.
_REFEREE_LEAGUE_AVG_FOULS_PER_GAME: float = 42.0
_REFEREE_FOUL_FACTOR_PER_FOUL: float = 0.005
_REFEREE_FACTOR_CLAMP_LOW: float = 0.95
_REFEREE_FACTOR_CLAMP_HIGH: float = 1.05


def _nba_referee_factor(features: dict[str, Any]) -> float:
    """Heuristic factor on the points-class stats from referee
    tendencies. See module-level constants for the math; gated on
    data-complete flag so partial / unverified crew matches return
    1.0 (no-op, filtered out). Clamp at ±5% (tighter than the
    default ±15% factor clamp) so the heuristic doesn't dominate
    stronger advanced-stats signals like ``opp_def_factor``."""
    if float(features.get("referee_data_complete") or 0.0) < 1.0:
        return 1.0
    avg_fouls = features.get("referee_avg_fouls_per_game")
    if not isinstance(avg_fouls, (int, float)) or isinstance(avg_fouls, bool):
        return 1.0
    delta = float(avg_fouls) - _REFEREE_LEAGUE_AVG_FOULS_PER_GAME
    return _clamp(
        1.0 + delta * _REFEREE_FOUL_FACTOR_PER_FOUL,
        lo=_REFEREE_FACTOR_CLAMP_LOW,
        hi=_REFEREE_FACTOR_CLAMP_HIGH,
    )


_NBA_FACTOR_FNS = {
    "efficiency_factor": _nba_efficiency_factor,
    "opp_def_factor": _nba_opp_def_factor,
    "opp_recent_form_factor": _nba_opp_recent_form_factor,
    "pace_factor_advanced": _nba_pace_factor_advanced,
    "usage_factor_advanced": _nba_usage_factor_advanced,
    "workload_factor": _nba_workload_factor,
    "nba_rest_factor": _nba_rest_factor,
    "nba_travel_factor": _nba_travel_factor,
    "nba_referee_factor": _nba_referee_factor,
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


# Smarter #7 — park × weather interaction term for HR / total bases.
#
# Park HR factors and the weather factor are independent multipliers
# today; their MULTIPLICATIVE combination already captures the basic
# case (Coors at 1.27 × warm weather at 1.05 → 1.33 base bonus). The
# interaction term adds a small NON-LINEAR bonus when both align in
# the SAME direction:
#
#  - HR-favorable park (>1.0) + warm temp (>70°F) → extra boost
#    (thin air + warm air + HR-friendly dimensions compound non-linearly).
#  - HR-suppressing park (<1.0) + cold temp (<70°F) → extra suppression.
#  - Mixed (favorable park + cold, or pitcher park + warm) → no extra
#    effect (the independent multipliers handle the basic case fine;
#    no theoretical reason for them to compound non-linearly).
#
# Envelope ±5% — same shape as the other heuristic factors so the
# interaction can't dominate the base park or weather signals it
# rides on top of. Dome games skip entirely (weather doesn't matter).
_PARK_WEATHER_TEMP_BASELINE: float = 70.0
_PARK_WEATHER_TEMP_RANGE: float = 30.0
_PARK_WEATHER_INTERACTION_SCALE: float = 0.10


def _mlb_park_weather_hr_interaction(features: dict[str, Any]) -> float:
    """Smarter #7 — park × weather interaction term.

    Returns 1.0 (no-op, filtered out) when:
    - Either ``park_data_complete`` or ``weather_data_complete`` is < 1.0.
    - Game is in a dome (``weather_is_dome == 1.0``).
    - Either signal is at neutral (park=1.0 or temp=70°F) — no
      interaction magnitude.
    - Signals point in OPPOSITE directions (one favorable, one not) —
      the independent multipliers already handle this case.

    Otherwise returns ``1.0 + (park_signal * temp_signal * scale)``,
    clamped at ±5%.
    """
    if float(features.get("park_data_complete") or 0.0) < 1.0:
        return 1.0
    if float(features.get("weather_data_complete") or 0.0) < 1.0:
        return 1.0
    if float(features.get("weather_is_dome") or 0.0) >= 1.0:
        return 1.0
    park_hr = features.get("park_factor_hr")
    temp_f = features.get("weather_temp_f")
    if not isinstance(park_hr, (int, float)) or isinstance(park_hr, bool):
        return 1.0
    if not isinstance(temp_f, (int, float)) or isinstance(temp_f, bool):
        return 1.0
    park_signal = float(park_hr) - 1.0
    temp_signal = (float(temp_f) - _PARK_WEATHER_TEMP_BASELINE) / _PARK_WEATHER_TEMP_RANGE
    # Same-sign requirement: only interact when both push the same way.
    if park_signal * temp_signal <= 0:
        return 1.0
    interaction = abs(park_signal) * abs(temp_signal) * _PARK_WEATHER_INTERACTION_SCALE
    sign = 1.0 if park_signal > 0 else -1.0
    return _clamp(1.0 + interaction * sign, lo=0.95, hi=1.05)


def _mlb_opposing_bullpen_rest_factor(features: dict[str, Any]) -> float:
    """Smarter #6 — bullpen rest multiplier for batter offense stats.

    The signal that matters for a hitter is the OPPOSING bullpen's rest:
    a tired opposing pen surrenders more runs / RBIs in late innings.
    Caller writes ``opposing_bullpen_rest_index_3d`` to features when the
    matchup data is available; this function reads it and returns a
    bounded multiplier:

    - Fully rested opp pen (index = 1.0) → 0.95 (slight suppression — fresh
      arms keep run production down).
    - Saturated opp pen (index = 0.0) → 1.05 (5% boost — tired arms allow
      more late-inning runs).
    - No data → 1.0 (no-op).

    The ±5% envelope is intentionally conservative; the punch list's
    "Make Sika Smarter" framing pairs this feature with a more
    sophisticated bullpen-state ingestion as a follow-up. Until then a
    modest, well-documented multiplier is better than a bigger swing
    that's wrong half the time.
    """

    raw = features.get("opposing_bullpen_rest_index_3d")
    if not isinstance(raw, (int, float)):
        return 1.0
    rest = max(0.0, min(1.0, float(raw)))
    # Linear: rest=1.0 → 0.95, rest=0.5 → 1.0, rest=0.0 → 1.05.
    return _clamp(1.0 + (0.5 - rest) * 0.10)


def _mlb_batter_platoon_factor(features: dict[str, Any]) -> float:
    """Smarter #5 — apply the batter-vs-starter platoon multiplier.

    The producer (``emit_mlb_platoon_features``) emits this already-clamped
    to ``[0.80, 1.20]`` so this function is just a straight read. Returns
    1.0 when the feature is missing (no platoon data → no adjustment),
    preserving the existing factor-applies semantics for stats where
    other multipliers may still fire."""

    raw = features.get("batter_vs_starter_platoon_factor")
    if isinstance(raw, (int, float)) and raw > 0:
        return _clamp(float(raw))
    return 1.0


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
    # Smarter #5 — batter-vs-starter platoon (vsLHP / vsRHP). Gated on
    # offense stats only (hits, home_runs, total_bases, rbis, runs).
    # Strikeouts/walks have their own pitcher-side factors that already
    # capture handedness implicitly via FIP/xFIP.
    "batter_platoon_factor": _mlb_batter_platoon_factor,
    # Smarter #6 — opposing bullpen rest (3-day window) gated on the most
    # bullpen-sensitive batter offense stats (rbis, runs). Conservative
    # ±5% envelope; pairs with a future per-game reliever-IP ingestion.
    "opposing_bullpen_rest_factor": _mlb_opposing_bullpen_rest_factor,
    # Smarter #7 — park × weather interaction term for HR / total bases.
    # Same-sign non-linear bonus when park HR factor and temperature
    # both push the same way (Coors + warm = extra boost; Petco + cold
    # = extra suppression). ±5% envelope.
    "park_weather_hr_interaction": _mlb_park_weather_hr_interaction,
}


# -----------------------------------------------------------------------------
# Public API

# -----------------------------------------------------------------------------
# NFL factors (Smarter NFL PR 7)
#
# Passing-shaped stats see the opponent-defense + weather factors;
# rushing sees defense only (wind doesn't touch the ground game); the
# snap-share volume proxy applies everywhere. Target-share and
# pass/rush-split defense factors are follow-ups once the gsis identity
# sidecar lands.

_NFL_PASSING_FACTOR_SET = (
    "nfl_opp_def_factor", "nfl_weather_passing_factor", "nfl_snap_share_factor",
)
_NFL_RUSHING_FACTOR_SET = ("nfl_opp_def_factor", "nfl_snap_share_factor")

_NFL_FACTORS_BY_STAT: dict[str, tuple[str, ...]] = {
    "passing_yards": _NFL_PASSING_FACTOR_SET,
    "completions": _NFL_PASSING_FACTOR_SET,
    "passing_touchdowns": _NFL_PASSING_FACTOR_SET,
    "receiving_yards": _NFL_PASSING_FACTOR_SET,
    "receptions": _NFL_PASSING_FACTOR_SET,
    "receiving_touchdowns": _NFL_PASSING_FACTOR_SET,
    "rushing_yards": _NFL_RUSHING_FACTOR_SET,
    "rushing_touchdowns": _NFL_RUSHING_FACTOR_SET,
    "rushing_yards_receiving_yards": _NFL_PASSING_FACTOR_SET,
    "passing_yards_rushing_yards": _NFL_PASSING_FACTOR_SET,
}


def _nfl_opp_def_factor(features: dict[str, Any]) -> float:
    """Opponent defensive EPA/play allowed -> offense environment. A
    defense allowing +0.067 EPA/play (bottom of the league) inflates
    the expectation ~10%; an elite defense (-0.067) suppresses it."""
    def_epa = features.get("nfl_opp_def_epa_per_play")
    if not isinstance(def_epa, (int, float)):
        return 1.0
    return _clamp(1.0 + float(def_epa) * 1.5, 0.90, 1.10)


def _nfl_weather_passing_factor(features: dict[str, Any]) -> float:
    """Wind kills passing volume/efficiency: >=20 mph -10%, >15 mph -5%.
    Domes never set ``nfl_wind_mph`` so this stays a no-op indoors."""
    wind = features.get("nfl_wind_mph")
    if not isinstance(wind, (int, float)):
        return 1.0
    if wind >= 20.0:
        return 0.90
    if wind > 15.0:
        return 0.95
    return 1.0


def _nfl_snap_share_factor(features: dict[str, Any]) -> float:
    """Recent-vs-season snap-share ratio, pre-clamped by the emitter to
    [0.88, 1.12] — a shrinking role deflates the volume expectation."""
    value = features.get("nfl_snap_share_factor_raw")
    if not isinstance(value, (int, float)):
        return 1.0
    return _clamp(float(value), 0.88, 1.12)


_NFL_FACTOR_FNS = {
    "nfl_opp_def_factor": _nfl_opp_def_factor,
    "nfl_weather_passing_factor": _nfl_weather_passing_factor,
    "nfl_snap_share_factor": _nfl_snap_share_factor,
}


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
    elif sport == "NFL":
        gating = _NFL_FACTORS_BY_STAT.get(stat_key) or _NFL_FACTORS_BY_STAT.get(stat_key.lower())
        fns = _NFL_FACTOR_FNS
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
    elif sport == "NFL":
        gating = _NFL_FACTORS_BY_STAT.get(stat_key) or _NFL_FACTORS_BY_STAT.get(stat_key.lower())
    else:
        return False
    return bool(gating) and factor_name in gating


def apply_factors(expected: float, factors: dict[str, float]) -> float:
    """Multiply ``expected`` by every factor and return the new value."""
    out = float(expected)
    for value in factors.values():
        out *= float(value)
    return out

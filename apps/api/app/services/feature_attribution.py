"""Driver attribution for player-prop scoring.

The heuristic factor pass writes ``features["advanced_factors"]`` — a
dict of ``factor_name → multiplier`` — but the raw factor names ("xstats
anchor factor", "pitcher dominance factor") are not user-friendly, and
they don't carry the underlying numbers ("recent barrel rate 14%") that
make the explanation actionable.

This module turns the multipliers into ``_drivers``: a sorted list of
``{key, label, delta_pct, direction, detail}`` dicts that the frontend
renders in the "Why this prediction?" panel. Each driver:

  - ``key``       — the original factor name (machine-readable)
  - ``label``     — short human label ("Quality of contact")
  - ``delta_pct`` — percentage delta from neutral (1.0); +12.0 = +12% boost
  - ``direction`` — "up" | "down" | "neutral" (sign of delta_pct)
  - ``detail``    — one-line explanation with the underlying numbers
                    (or ``None`` when the source data isn't in features)
"""

from __future__ import annotations

from typing import Any


# -----------------------------------------------------------------------------
# Labels — short, presentation-ready names for each factor.

_FACTOR_LABELS: dict[str, str] = {
    # NBA
    "efficiency_factor": "Shooting efficiency",
    "opp_def_factor": "Opponent defense",
    "opp_recent_form_factor": "Opponent recent form",
    "pace_factor_advanced": "Opponent pace",
    "usage_factor_advanced": "Usage rate",
    # MLB
    "xstats_anchor_factor": "Expected stats regression",
    "quality_of_contact_factor": "Quality of contact",
    "starter_factor_advanced": "Opposing starter quality",
    "k_rate_factor": "Opposing starter K rate",
    "pitcher_dominance_factor": "Pitcher dominance",
    "park_factor_hr_mult": "Park (home runs)",
    "park_factor_runs_mult": "Park (runs)",
    "park_factor_singles": "Park (singles)",
    "weather_factor": "Weather",
    "lineup_factor": "Batting order",
}


def _humanize_fallback(key: str) -> str:
    """Fallback label for factors not in the table — best-effort cleanup."""
    return (
        key.replace("_advanced", "")
        .replace("_factor", "")
        .replace("_", " ")
        .strip()
        .title()
        or key
    )


# -----------------------------------------------------------------------------
# Detail strings — embed the underlying numbers from the features dict so the
# user sees "Recent USG% 0.32 vs season 0.28" rather than just "1.14x".


def _fmt_pct(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return f"{float(value) * 100:.1f}%"


def _fmt_num(value: Any, decimals: int = 2) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return f"{float(value):.{decimals}f}"


def _detail_for_factor(key: str, features: dict[str, Any]) -> str | None:
    """Produce a one-line explanation for ``key`` using values from
    ``features``. Returns ``None`` when the source data isn't available
    (which means we'll fall back to a generic label-only row)."""

    if key == "efficiency_factor":
        recent = _fmt_pct(features.get("recent_true_shooting_pct"))
        season = _fmt_pct(features.get("season_true_shooting_pct"))
        if recent and season:
            return f"Recent TS% {recent} vs season {season}"
        return None

    if key == "opp_def_factor":
        drtg = _fmt_num(features.get("opponent_defensive_rating_season"), 1)
        if drtg:
            return f"Opponent season DRtg: {drtg}"
        return None

    if key == "opp_recent_form_factor":
        drtg = _fmt_num(features.get("opponent_def_rating_recent_5"), 1)
        if drtg:
            return f"Opponent recent-5 DRtg: {drtg}"
        return None

    if key == "pace_factor_advanced":
        recent = _fmt_num(features.get("opponent_pace_recent_5"), 1)
        season = _fmt_num(features.get("opponent_pace_season"), 1)
        if recent:
            return f"Opponent pace recent-5: {recent}"
        if season:
            return f"Opponent pace season: {season}"
        return None

    if key == "usage_factor_advanced":
        recent = _fmt_pct(features.get("recent_usage_pct"))
        season = _fmt_pct(features.get("season_usage_pct"))
        if recent and season:
            return f"Recent USG% {recent} vs season {season}"
        return None

    if key == "xstats_anchor_factor":
        recent = _fmt_num(features.get("recent_3_average") or features.get("recent_10_average"), 3)
        season = _fmt_num(features.get("season_average"), 3)
        xba = _fmt_num(features.get("season_xba"), 3)
        if recent and season and xba:
            return f"Recent AVG {recent} vs season {season} (xBA {xba})"
        return None

    if key == "quality_of_contact_factor":
        barrel = _fmt_pct(features.get("season_barrel_rate"))
        hard = _fmt_pct(features.get("season_hard_hit_rate"))
        if barrel:
            return f"Season barrel rate: {barrel}"
        if hard:
            return f"Season hard-hit rate: {hard}"
        return None

    if key == "starter_factor_advanced":
        xfip = _fmt_num(features.get("opposing_starter_xfip"))
        fip = _fmt_num(features.get("opposing_starter_fip"))
        if xfip:
            return f"Opposing starter xFIP: {xfip}"
        if fip:
            return f"Opposing starter FIP: {fip}"
        return None

    if key == "k_rate_factor":
        k9 = _fmt_num(features.get("opposing_starter_k_per_9"))
        if k9:
            return f"Opposing starter K/9: {k9}"
        return None

    if key == "pitcher_dominance_factor":
        csw = _fmt_pct(features.get("opposing_starter_csw_pct"))
        whiff = _fmt_pct(features.get("opposing_starter_whiff_pct"))
        if csw:
            return f"Opposing starter CSW%: {csw}"
        if whiff:
            return f"Opposing starter whiff%: {whiff}"
        return None

    if key == "park_factor_hr_mult":
        mult = _fmt_num(features.get("park_factor_hr"))
        if mult:
            return f"Park HR multiplier: {mult}"
        return None

    if key == "park_factor_runs_mult":
        mult = _fmt_num(features.get("park_factor_runs"))
        if mult:
            return f"Park runs multiplier: {mult}"
        return None

    if key == "park_factor_singles":
        mult = _fmt_num(features.get("park_factor_singles"))
        if mult:
            return f"Park singles multiplier: {mult}"
        return None

    if key == "weather_factor":
        if features.get("weather_is_dome") == 1.0:
            return None  # weather_factor never fires in domes
        temp = _fmt_num(features.get("weather_temp_f"), 0)
        wind = _fmt_num(features.get("weather_wind_speed_mph"), 0)
        wind_dir = features.get("weather_wind_dir_deg")
        if temp and wind and isinstance(wind_dir, (int, float)):
            return f"{temp}°F, wind {wind} mph @ {int(wind_dir)}°"
        if temp:
            return f"Temperature: {temp}°F"
        return None

    if key == "lineup_factor":
        order = features.get("batting_order_position")
        if isinstance(order, (int, float)):
            return f"Batting order position: {int(order)}"
        return None

    return None


# -----------------------------------------------------------------------------
# Public API


def top_drivers(
    features: dict[str, Any],
    expected_baseline: float,
    expected_final: float,
    *,
    limit: int = 3,
    min_abs_delta_pct: float = 0.5,
) -> list[dict[str, Any]]:
    """Return the top ``limit`` advanced factors driving the prediction.

    Reads ``features["advanced_factors"]`` (factor_name → multiplier),
    converts to delta_pct, attaches a label and a detail string built
    from the rest of the features dict, and sorts by ``|delta_pct|``.

    ``expected_baseline`` and ``expected_final`` are accepted for symmetry
    with the handoff spec but currently unused — the multiplicative model
    means each factor's contribution is fully described by its multiplier.
    They're reserved for future use (e.g. weighting by absolute impact on
    expected output).

    Filters:
      - factors that resolved to neutral (|delta| < ``min_abs_delta_pct``%)
        are dropped, matching the frontend's near-zero filter.
    """
    del expected_baseline  # reserved
    del expected_final  # reserved

    advanced_factors = features.get("advanced_factors") or {}
    if not isinstance(advanced_factors, dict):
        return []

    rows: list[dict[str, Any]] = []
    for key, raw in advanced_factors.items():
        try:
            multiplier = float(raw)
        except (TypeError, ValueError):
            continue
        delta_pct = round((multiplier - 1.0) * 100.0, 2)
        if abs(delta_pct) < min_abs_delta_pct:
            continue
        direction = "up" if delta_pct > 0 else ("down" if delta_pct < 0 else "neutral")
        label = _FACTOR_LABELS.get(key) or _humanize_fallback(key)
        detail = _detail_for_factor(key, features)
        rows.append(
            {
                "key": key,
                "label": label,
                "delta_pct": delta_pct,
                "direction": direction,
                "detail": detail,
            }
        )

    rows.sort(key=lambda row: abs(row["delta_pct"]), reverse=True)
    return rows[:limit]


def driver_reason_strings(drivers: list[dict[str, Any]], *, limit: int = 2) -> list[str]:
    """Render the top ``limit`` drivers as one-line reason strings.

    The scoring kernel appends these to the rationale list. Format:
      "Quality of contact +12.0%: Season barrel rate 14.0%"
    or, when no detail is available:
      "Quality of contact +12.0%"
    """
    out: list[str] = []
    for row in drivers[:limit]:
        sign = "+" if row["delta_pct"] >= 0 else ""
        head = f"{row['label']} {sign}{row['delta_pct']:.1f}%"
        detail = row.get("detail")
        if detail:
            out.append(f"{head}: {detail}")
        else:
            out.append(head)
    return out

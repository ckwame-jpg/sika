"""Pure NFL pricing math (Smarter NFL PR 5).

Lives in the shared ``ml_features`` package so ``apps/api`` (serving)
and ``apps/ml`` (the 2025-season replay backtest, Smarter NFL PR 9)
price with literally the same functions — the backtest validates the
exact code that goes live.

Margin model: ``data/nfl_margin_distribution.json`` holds a
precomputed kernel-weighted conditional tail grid,
``P(home margin > t | projected margin mu)``, built from 2011–2025
regular-season results conditioned on the closing spread (see
``scripts/build_nfl_margin_distribution.py``). This captures NFL's
key-number mass — a game projected at -3 lands on exactly 3 ~10% of
the time, so pricing a 2.5 vs 3.5 Kalshi threshold differs by ~9
probability points where a Normal would say 3. Bilinear interpolation
over (mu, threshold); at integer thresholds the interpolation midpoint
equals P(m > t) + ½·P(m = t), i.e. the correct tie treatment.

Totals: Normal tail with the empirical residual sigma (total vs
closing total_line, 2011–2025: 13.16). Totals don't exhibit the
key-number pathology margins do — Normal is adequate for v1 and the
PR 9 backtest rechecks the sigma.

Blending: consensus-anchored ("market-anchored + situational" design).
Weights: 0.70 on the books when ≥3 books quote, 0.50 for a thin 1–2
book consensus, 0 (pure internal) when no anchor — the caller applies
its own confidence penalty for the no-anchor case.
"""

from __future__ import annotations

import json
from importlib import resources
from statistics import NormalDist

# Empirical residual sd of (final total − closing total_line),
# 2011–2025 REG. Re-fit by the Smarter NFL PR 9 backtest.
NFL_TOTAL_SIGMA = 13.16
# Fallback margin sigma (residual sd of margin vs closing spread) when
# the grid data file is unavailable.
NFL_MARGIN_SIGMA_FALLBACK = 12.73

CONSENSUS_BLEND_WEIGHT_STRONG = 0.70  # ≥3 books
CONSENSUS_BLEND_WEIGHT_THIN = 0.50  # 1–2 books
STRONG_BOOK_COUNT = 3

_PROB_FLOOR = 0.005
_PROB_CEILING = 0.995

_GRID_CACHE: dict | None = None


def _load_grid() -> dict | None:
    global _GRID_CACHE
    if _GRID_CACHE is None:
        try:
            payload = (
                resources.files("ml_features")
                .joinpath("data/nfl_margin_distribution.json")
                .read_text()
            )
            _GRID_CACHE = json.loads(payload)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            _GRID_CACHE = {}
    return _GRID_CACHE or None


def _clamp_probability(value: float) -> float:
    return min(max(value, _PROB_FLOOR), _PROB_CEILING)


def _interpolate_row(values: list[float], grid: list[float], point: float) -> float:
    """Linear interpolation of ``values`` (aligned with sorted ``grid``)
    at ``point``, clamping to the edges."""
    if point <= grid[0]:
        return values[0]
    if point >= grid[-1]:
        return values[-1]
    # Uniform grids in this file — direct index math.
    step = grid[1] - grid[0]
    index = int((point - grid[0]) / step)
    index = min(index, len(grid) - 2)
    left, right = grid[index], grid[index + 1]
    fraction = 0.0 if right == left else (point - left) / (right - left)
    return values[index] + fraction * (values[index + 1] - values[index])


def nfl_margin_yes_probability(margin_mu: float, threshold: float) -> float:
    """``P(home margin > threshold)`` given a projected home margin.

    ``margin_mu`` is home-oriented (positive = home favored — note this
    is the NEGATION of book spread convention where home -3.5 means
    favored). Falls back to a Normal tail when the grid is missing.
    """
    grid = _load_grid()
    if grid is None:
        tail = 1.0 - NormalDist(margin_mu, NFL_MARGIN_SIGMA_FALLBACK).cdf(threshold)
        return _clamp_probability(tail)

    mu_values: list[float] = grid["mu_values"]
    thresholds: list[float] = grid["thresholds"]
    tails: list[list[float]] = grid["tails"]

    mu = min(max(float(margin_mu), mu_values[0]), mu_values[-1])
    step = mu_values[1] - mu_values[0]
    row_index = int((mu - mu_values[0]) / step)
    row_index = min(row_index, len(mu_values) - 2)
    left_mu, right_mu = mu_values[row_index], mu_values[row_index + 1]
    fraction = 0.0 if right_mu == left_mu else (mu - left_mu) / (right_mu - left_mu)

    lower = _interpolate_row(tails[row_index], thresholds, float(threshold))
    upper = _interpolate_row(tails[row_index + 1], thresholds, float(threshold))
    return _clamp_probability(lower + fraction * (upper - lower))


def nfl_win_probability(margin_mu: float) -> float:
    """``P(home wins)`` given a projected home margin (ties half-count)."""
    grid = _load_grid()
    if grid is None:
        return _clamp_probability(
            1.0 - NormalDist(margin_mu, NFL_MARGIN_SIGMA_FALLBACK).cdf(0.0)
        )
    mu_values: list[float] = grid["mu_values"]
    win_probs: list[float] = grid["win_probs"]
    mu = min(max(float(margin_mu), mu_values[0]), mu_values[-1])
    return _clamp_probability(_interpolate_row(win_probs, mu_values, mu))


def nfl_total_yes_probability(
    total_mu: float,
    threshold: float,
    *,
    direction: str = "over",
    sigma: float = NFL_TOTAL_SIGMA,
) -> float:
    """``P(total over/under threshold)`` given a projected game total."""
    over_tail = 1.0 - NormalDist(float(total_mu), float(sigma)).cdf(float(threshold))
    probability = over_tail if str(direction).lower() != "under" else 1.0 - over_tail
    return _clamp_probability(probability)


def normal_tail_yes_probability(mu: float, sd: float, threshold: float) -> float:
    """``P(stat >= threshold)`` under a Normal model with a 0.5
    continuity correction — the yardage-prop pricing shape (Smarter NFL
    PR 7). Kalshi props resolve YES at ``value >= threshold`` for
    integer thresholds, so the corrected cut sits at ``threshold - 0.5``.
    """
    safe_sd = max(float(sd), 1e-6)
    tail = 1.0 - NormalDist(float(mu), safe_sd).cdf(float(threshold) - 0.5)
    return _clamp_probability(tail)


def blend_probability(
    p_internal: float,
    p_consensus: float | None,
    book_count: int,
) -> tuple[float, float]:
    """Blend the internal probability toward the de-vigged consensus.

    Returns ``(blended_probability, consensus_weight)`` so callers can
    surface the weight as a feature. No consensus → pure internal with
    weight 0.0 (the caller applies its no-anchor confidence penalty).
    """
    if p_consensus is None or book_count <= 0:
        return _clamp_probability(float(p_internal)), 0.0
    weight = (
        CONSENSUS_BLEND_WEIGHT_STRONG
        if book_count >= STRONG_BOOK_COUNT
        else CONSENSUS_BLEND_WEIGHT_THIN
    )
    blended = weight * float(p_consensus) + (1.0 - weight) * float(p_internal)
    return _clamp_probability(blended), weight


def blend_line(
    internal_line: float,
    consensus_line: float | None,
    *,
    weight: float = CONSENSUS_BLEND_WEIGHT_STRONG,
) -> float:
    """Blend a projected line (margin or total) toward the consensus
    line. One blended margin prices every alternate Kalshi threshold
    via the pmf — this is how a single 3-credit Odds API fetch covers
    the whole KXNFLSPREAD ladder."""
    if consensus_line is None:
        return float(internal_line)
    return weight * float(consensus_line) + (1.0 - weight) * float(internal_line)

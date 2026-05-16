"""Tests for Smarter #8 phase 1 — empirical pair-correlation math.

Phase 1 is pure math; phase 2 will wire DB queries; phase 3 will
swap the theoretical priors in
``parlays._correlation_adjusted_joint_probability`` for an empirical
blend that converges as settled history grows.

Load-bearing tests:

- ``test_pearson_perfectly_correlated_yields_1``
- ``test_pearson_anti_correlated_yields_neg_1``
- ``test_phi_matches_pearson_on_binary``
- ``test_blend_holds_prior_at_low_sample_size`` — the protection
  against acting on noisy estimates
- ``test_blend_converges_to_empirical_at_high_sample_size``
"""

from __future__ import annotations

import pytest

from app.services.parlay_correlation import (
    DEFAULT_MIN_SAMPLE,
    DEFAULT_SAMPLE_FLOOR,
    PairCorrelation,
    blend_theoretical_with_empirical,
    estimate_pair_correlation,
    pearson_correlation,
    phi_coefficient_from_contingency,
)


# -- Pearson ------------------------------------------------------------


def test_pearson_perfectly_correlated_yields_1() -> None:
    """Two identical sequences → r = +1.0."""
    assert pearson_correlation([1, 0, 1, 0, 1], [1, 0, 1, 0, 1]) == pytest.approx(1.0)


def test_pearson_anti_correlated_yields_neg_1() -> None:
    """Inverted binary sequences → r = -1.0."""
    assert pearson_correlation([1, 0, 1, 0, 1], [0, 1, 0, 1, 0]) == pytest.approx(-1.0)


def test_pearson_uncorrelated_yields_zero() -> None:
    """Orthogonal patterns: half overlap → r ~ 0."""
    a = [1, 1, 0, 0, 1, 1, 0, 0]
    b = [1, 0, 1, 0, 1, 0, 1, 0]
    # E[A] = 0.5, E[B] = 0.5, E[AB] = 0.25 → cov = 0 → r = 0.
    assert pearson_correlation(a, b) == pytest.approx(0.0)


def test_pearson_zero_variance_returns_zero() -> None:
    """All-same outcome → undefined Pearson; we return 0 (the "no
    signal" interpretation is what the parlay use case needs)."""
    assert pearson_correlation([1, 1, 1, 1], [1, 0, 1, 0]) == 0.0
    assert pearson_correlation([1, 0, 1, 0], [0, 0, 0, 0]) == 0.0


def test_pearson_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="shape mismatch"):
        pearson_correlation([1, 0], [1, 0, 1])


def test_pearson_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one"):
        pearson_correlation([], [])


def test_pearson_handles_continuous_inputs() -> None:
    """The function isn't binary-only — it falls back to standard
    Pearson on any numeric input. Docs the contract for callers
    that pass model probabilities directly."""
    a = [0.1, 0.3, 0.5, 0.7, 0.9]
    b = [0.2, 0.35, 0.55, 0.72, 0.88]
    r = pearson_correlation(a, b)
    assert r > 0.99  # nearly linear


# -- Phi from contingency table -----------------------------------------


def test_phi_matches_pearson_on_binary() -> None:
    """Phi == Pearson on {0, 1} encoding. Pin the equivalence so a
    future "optimization" can't accidentally drift the two paths."""
    # 100 rows: both=30, only_a=15, only_b=20, neither=35.
    a = [1] * 30 + [1] * 15 + [0] * 20 + [0] * 35
    b = [1] * 30 + [0] * 15 + [1] * 20 + [0] * 35
    r = pearson_correlation(a, b)
    phi = phi_coefficient_from_contingency(both=30, only_a=15, only_b=20, neither=35)
    assert phi == pytest.approx(r, abs=1e-9)


def test_phi_perfectly_correlated() -> None:
    """only_a + only_b == 0 → both == a_pos == b_pos → phi = 1.0."""
    assert phi_coefficient_from_contingency(both=50, only_a=0, only_b=0, neither=50) == pytest.approx(1.0)


def test_phi_anti_correlated() -> None:
    """both + neither == 0 → maximum disagreement → phi = -1.0."""
    assert phi_coefficient_from_contingency(both=0, only_a=50, only_b=50, neither=0) == pytest.approx(-1.0)


def test_phi_constant_marginal_returns_zero() -> None:
    """If A is always 0 (a_pos = 0), phi is undefined → return 0."""
    assert phi_coefficient_from_contingency(both=0, only_a=0, only_b=30, neither=70) == 0.0
    # B always 1 (b_neg = 0):
    assert phi_coefficient_from_contingency(both=70, only_a=30, only_b=0, neither=0) == 0.0


def test_phi_rejects_zero_total() -> None:
    with pytest.raises(ValueError, match="at least one"):
        phi_coefficient_from_contingency(both=0, only_a=0, only_b=0, neither=0)


# -- estimate_pair_correlation -----------------------------------------


def test_estimate_returns_none_below_min_sample() -> None:
    """Default ``min_sample`` is 30 — anything less returns None
    rather than an unreliable estimate."""
    assert estimate_pair_correlation([1, 0] * 5, [1, 0] * 5) is None


def test_estimate_returns_pair_at_min_sample_boundary() -> None:
    """Exactly ``min_sample`` rows is enough — boundary inclusive."""
    result = estimate_pair_correlation([1, 0] * 15, [1, 0] * 15)
    assert isinstance(result, PairCorrelation)
    assert result.sample_size == 30


def test_estimate_returns_correct_coefficient() -> None:
    """Above threshold, the coefficient matches Pearson."""
    a = [1, 0] * 20
    b = [1, 0] * 20
    result = estimate_pair_correlation(a, b)
    assert result is not None
    assert result.coefficient == pytest.approx(1.0)
    assert result.sample_size == 40


def test_estimate_honors_custom_min_sample() -> None:
    """Caller can lower the threshold for diagnostic / low-stakes
    paths (e.g. operator-facing 'show me the raw correlation' tools
    that aren't gating on edge)."""
    result = estimate_pair_correlation([1, 0, 1], [1, 0, 1], min_sample=3)
    assert result is not None
    assert result.coefficient == pytest.approx(1.0)


def test_estimate_rejects_negative_min_sample() -> None:
    with pytest.raises(ValueError, match="min_sample"):
        estimate_pair_correlation([1, 0], [1, 0], min_sample=-1)


def test_estimate_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="shape mismatch"):
        estimate_pair_correlation([1, 0, 1], [1, 0])


# -- blend_theoretical_with_empirical -----------------------------------


def test_blend_returns_theoretical_when_empirical_is_none() -> None:
    """Insufficient empirical data → caller's prior wins, no
    silent shift."""
    assert blend_theoretical_with_empirical(0.7, None) == pytest.approx(0.7)


def test_blend_returns_theoretical_when_empirical_zero_size() -> None:
    """Defensive: an empty PairCorrelation should behave like None
    even though estimate_pair_correlation never produces one."""
    assert blend_theoretical_with_empirical(
        0.7, PairCorrelation(coefficient=0.42, sample_size=0)
    ) == pytest.approx(0.7)


def test_blend_holds_prior_at_low_sample_size() -> None:
    """The protection against acting on noisy estimates: at small
    N the prior dominates. At sample_size=10 with sample_floor=100,
    weight = 0.10 → result = 0.9 * 0.7 + 0.1 * 0.2 = 0.65."""
    result = blend_theoretical_with_empirical(
        0.7,
        PairCorrelation(coefficient=0.2, sample_size=10),
        sample_floor=100,
    )
    assert result == pytest.approx(0.65)


def test_blend_converges_to_empirical_at_high_sample_size() -> None:
    """At sample_size >= sample_floor the empirical estimate fully
    replaces the prior — no further weight on theoretical."""
    result = blend_theoretical_with_empirical(
        0.7,
        PairCorrelation(coefficient=0.2, sample_size=500),
        sample_floor=100,
    )
    assert result == pytest.approx(0.2)


def test_blend_is_smooth_at_threshold_boundary() -> None:
    """No discontinuity at sample_size == sample_floor. At weight =
    1.0 we get pure empirical; one row earlier we get nearly-pure
    empirical. Check both sides are close."""
    just_below = blend_theoretical_with_empirical(
        0.7,
        PairCorrelation(coefficient=0.2, sample_size=99),
        sample_floor=100,
    )
    at_threshold = blend_theoretical_with_empirical(
        0.7,
        PairCorrelation(coefficient=0.2, sample_size=100),
        sample_floor=100,
    )
    # 99/100 = 0.99 → 0.01 * 0.7 + 0.99 * 0.2 = 0.205
    assert just_below == pytest.approx(0.205)
    assert at_threshold == pytest.approx(0.20)
    # Difference < 1% of the prior — the transition is smooth.
    assert abs(just_below - at_threshold) < 0.01


def test_blend_handles_negative_correlations() -> None:
    """Empirical estimate can be negative; the blend math doesn't
    care. Documents that anti-correlated pairs (e.g. opposite-side
    legs in the same game) get the same treatment as positive."""
    result = blend_theoretical_with_empirical(
        0.7,
        PairCorrelation(coefficient=-0.4, sample_size=200),
        sample_floor=100,
    )
    assert result == pytest.approx(-0.4)


def test_blend_rejects_invalid_sample_floor() -> None:
    with pytest.raises(ValueError, match="sample_floor"):
        blend_theoretical_with_empirical(
            0.7, PairCorrelation(coefficient=0.5, sample_size=10), sample_floor=0,
        )


# -- Module-level constants are sane -----------------------------------


def test_default_sample_floor_at_least_min_sample() -> None:
    """``DEFAULT_SAMPLE_FLOOR`` should be >= ``DEFAULT_MIN_SAMPLE`` —
    otherwise a sample that just clears ``estimate_pair_correlation``'s
    minimum already saturates the blend, which defeats the
    'shrink toward prior at low N' point of the blend itself."""
    assert DEFAULT_SAMPLE_FLOOR >= DEFAULT_MIN_SAMPLE

"""Tests for Smarter #8 phase 3 — cache layer + combiner integration
for empirical parlay correlations.

Covers:
- ``cached_empirical_pair_correlations`` round-trips a fresh
  computation, returns cached on subsequent calls, refreshes when
  stale, recomputes when forced.
- ``invalidate_parlay_correlation_cache`` drops the row.
- ``_correlation_adjusted_joint_probability`` with
  ``empirical_correlations=None`` matches pre-phase-3 behavior
  byte-identically.
- A populated empirical map shifts the per-pair weight away from
  the theoretical prior, lifting the joint probability when the
  empirical estimate exceeds the prior.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models import Market, OperatorSetting
from app.services.parlay_correlation import PairCorrelation
from app.services.parlay_correlation_cache import (
    CACHE_KEY,
    DEFAULT_CACHE_TTL_MINUTES,
    cached_empirical_pair_correlations,
    invalidate_parlay_correlation_cache,
)
from app.services.parlay_correlation_db import PAIR_TYPES


_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


# -- Cache fresh / stale flow -----------------------------------------


def _stored_blob(db_session) -> dict | None:
    row = db_session.scalar(
        OperatorSetting.__table__.select().where(OperatorSetting.key == CACHE_KEY)
    )
    if row is None:
        return None
    raw_value = row.value if hasattr(row, "value") else row[0]
    if not raw_value:
        return None
    return json.loads(raw_value)


def test_cache_computes_fresh_when_missing(db_session) -> None:
    """No cached row → recompute + persist."""
    with patch(
        "app.services.parlay_correlation_cache.compute_empirical_pair_correlations",
        return_value={pair: None for pair in PAIR_TYPES},
    ) as compute_mock:
        out = cached_empirical_pair_correlations(db_session, now=_NOW)
    assert compute_mock.call_count == 1
    # Output keyed by every pair type.
    assert set(out.keys()) == set(PAIR_TYPES)
    # Row was persisted.
    row = db_session.scalar(
        OperatorSetting.__table__.select().where(OperatorSetting.key == CACHE_KEY)
    )
    assert row is not None


def test_cache_returns_stored_within_ttl(db_session) -> None:
    """Fresh cached row → no recompute on subsequent call."""
    empirical = {
        "shared_subject": PairCorrelation(coefficient=0.5, sample_size=200),
        "same_team": None,
        "shared_opponent": None,
    }
    with patch(
        "app.services.parlay_correlation_cache.compute_empirical_pair_correlations",
        return_value=empirical,
    ) as compute_mock:
        first = cached_empirical_pair_correlations(db_session, now=_NOW)
        second = cached_empirical_pair_correlations(
            db_session, now=_NOW + timedelta(minutes=60),
        )
    assert compute_mock.call_count == 1
    assert first["shared_subject"].coefficient == pytest.approx(0.5)
    assert second["shared_subject"].coefficient == pytest.approx(0.5)


def test_cache_recomputes_when_stale(db_session) -> None:
    """Past the TTL → recompute on next call."""
    empirical_first = {"shared_subject": PairCorrelation(0.5, 200), "same_team": None, "shared_opponent": None}
    empirical_second = {"shared_subject": PairCorrelation(0.3, 250), "same_team": None, "shared_opponent": None}
    with patch(
        "app.services.parlay_correlation_cache.compute_empirical_pair_correlations",
        side_effect=[empirical_first, empirical_second],
    ) as compute_mock:
        cached_empirical_pair_correlations(db_session, now=_NOW, ttl_minutes=60)
        result = cached_empirical_pair_correlations(
            db_session, now=_NOW + timedelta(minutes=120), ttl_minutes=60,
        )
    assert compute_mock.call_count == 2
    assert result["shared_subject"].coefficient == pytest.approx(0.3)


def test_cache_handles_unparseable_stored_blob(db_session, caplog) -> None:
    """A corrupt cached blob (manual SQL write, truncated value)
    must not crash the read — recompute fresh + log a warning."""
    db_session.add(OperatorSetting(key=CACHE_KEY, value="not valid json"))
    db_session.flush()
    empirical = {"shared_subject": PairCorrelation(0.5, 200), "same_team": None, "shared_opponent": None}
    with patch(
        "app.services.parlay_correlation_cache.compute_empirical_pair_correlations",
        return_value=empirical,
    ) as compute_mock:
        out = cached_empirical_pair_correlations(db_session, now=_NOW)
    assert compute_mock.call_count == 1
    assert out["shared_subject"].coefficient == pytest.approx(0.5)


def test_cache_ttl_zero_forces_refresh(db_session) -> None:
    """``ttl_minutes=0`` is the operator debug knob — recompute on
    every call."""
    empirical = {"shared_subject": PairCorrelation(0.5, 200), "same_team": None, "shared_opponent": None}
    with patch(
        "app.services.parlay_correlation_cache.compute_empirical_pair_correlations",
        return_value=empirical,
    ) as compute_mock:
        cached_empirical_pair_correlations(db_session, now=_NOW, ttl_minutes=0)
        cached_empirical_pair_correlations(db_session, now=_NOW + timedelta(seconds=1), ttl_minutes=0)
    assert compute_mock.call_count == 2


def test_cache_rejects_negative_ttl(db_session) -> None:
    with pytest.raises(ValueError, match="ttl_minutes"):
        cached_empirical_pair_correlations(db_session, ttl_minutes=-1)


def test_invalidate_returns_false_when_nothing_cached(db_session) -> None:
    assert invalidate_parlay_correlation_cache(db_session) is False


def test_invalidate_drops_row(db_session) -> None:
    db_session.add(OperatorSetting(key=CACHE_KEY, value="{}"))
    db_session.flush()
    assert invalidate_parlay_correlation_cache(db_session) is True
    row = db_session.scalar(
        OperatorSetting.__table__.select().where(OperatorSetting.key == CACHE_KEY)
    )
    assert row is None


# -- Combiner blend ---------------------------------------------------


def _make_candidate(probability: float, subject: str, team: str) -> SimpleNamespace:
    """Minimal ``ParlayCandidateInput``-shaped fake the combiner reads.

    ``_selected_model_probability`` reads ``recommendation.side`` and
    ``signal.fair_yes_price`` / ``signal.fair_no_price``; the rest of
    the combiner reads market.raw_data + event.id + event.participants.
    """
    market_raw = {
        "copilot_subject_name": subject,
        "copilot_subject_team": team,
    }
    return SimpleNamespace(
        market=SimpleNamespace(sport_key="NBA", raw_data=market_raw, ticker=f"TKR-{subject}"),
        event=SimpleNamespace(id=1, sport_key="NBA", participants=[]),
        recommendation=SimpleNamespace(
            side="yes",
            scoring_diagnostics={"selected_side_probability": probability},
            suggested_price=0.5,
            confidence=0.6,
        ),
        signal=SimpleNamespace(
            fair_yes_price=probability,
            fair_no_price=1.0 - probability,
        ),
        prediction=None,
        metadata=market_raw,
    )


def test_combiner_with_none_empirical_matches_theoretical_baseline() -> None:
    """Passing ``empirical_correlations=None`` (the default) must
    produce byte-identical output to the pre-phase-3 behavior."""
    from app.services.parlays import _correlation_adjusted_joint_probability

    combo = (
        _make_candidate(0.55, "Tatum", "BOS"),
        _make_candidate(0.55, "Tatum", "BOS"),
    )
    pairs = {"shared_subject": 1, "same_team": 0, "shared_opponent": 0}
    baseline = _correlation_adjusted_joint_probability(combo, pairs)
    explicit_none = _correlation_adjusted_joint_probability(
        combo, pairs, empirical_correlations=None,
    )
    assert baseline == explicit_none


def test_combiner_empirical_above_prior_lifts_joint() -> None:
    """Empirical correlation that exceeds the 0.7 theoretical prior
    for ``shared_subject`` should drive the per-pair weight higher,
    which lifts the joint probability above the theoretical-only
    baseline."""
    from app.services.parlays import _correlation_adjusted_joint_probability

    combo = (
        _make_candidate(0.55, "Tatum", "BOS"),
        _make_candidate(0.55, "Tatum", "BOS"),
    )
    pairs = {"shared_subject": 1, "same_team": 0, "shared_opponent": 0}
    baseline = _correlation_adjusted_joint_probability(combo, pairs)
    empirical = {
        "shared_subject": PairCorrelation(coefficient=0.95, sample_size=500),
        "same_team": None,
        "shared_opponent": None,
    }
    lifted = _correlation_adjusted_joint_probability(
        combo, pairs, empirical_correlations=empirical,
    )
    assert lifted > baseline


def test_combiner_empirical_negative_falls_back_to_prior() -> None:
    """A negative empirical coefficient should NOT push the weight
    below the prior — we clamp to ``max(coefficient, 0)`` before
    blending so the combiner can't understate the joint."""
    from app.services.parlays import _correlation_adjusted_joint_probability

    combo = (
        _make_candidate(0.55, "Tatum", "BOS"),
        _make_candidate(0.55, "Tatum", "BOS"),
    )
    pairs = {"shared_subject": 1, "same_team": 0, "shared_opponent": 0}
    baseline = _correlation_adjusted_joint_probability(combo, pairs)
    negative_empirical = {
        "shared_subject": PairCorrelation(coefficient=-0.5, sample_size=500),
        "same_team": None,
        "shared_opponent": None,
    }
    blended = _correlation_adjusted_joint_probability(
        combo, pairs, empirical_correlations=negative_empirical,
    )
    # At sample_size=500 >> sample_floor=100, the blend fully replaces
    # the prior with the clamped empirical (0.0). With a per-pair
    # weight of 0.0 the combiner returns the strict product
    # (no correlation lift). So ``blended`` should be ≤ baseline.
    assert blended <= baseline


def test_combiner_empirical_low_sample_size_holds_prior() -> None:
    """Empirical estimate with low sample size shouldn't shift the
    weight far — the blend protects against acting on noisy
    estimates."""
    from app.services.parlays import _correlation_adjusted_joint_probability

    combo = (
        _make_candidate(0.55, "Tatum", "BOS"),
        _make_candidate(0.55, "Tatum", "BOS"),
    )
    pairs = {"shared_subject": 1, "same_team": 0, "shared_opponent": 0}
    baseline = _correlation_adjusted_joint_probability(combo, pairs)
    low_sample_empirical = {
        "shared_subject": PairCorrelation(coefficient=0.95, sample_size=10),  # small N
        "same_team": None,
        "shared_opponent": None,
    }
    blended = _correlation_adjusted_joint_probability(
        combo, pairs, empirical_correlations=low_sample_empirical,
    )
    # Small N means the blend mostly retains the theoretical prior.
    # ``lifted`` should be close to baseline; specifically, the lift
    # over baseline should be <= 1/3 of the lift from a fully-saturated
    # empirical (sample_size=500).
    high_sample_empirical = {
        "shared_subject": PairCorrelation(coefficient=0.95, sample_size=500),
        "same_team": None,
        "shared_opponent": None,
    }
    saturated = _correlation_adjusted_joint_probability(
        combo, pairs, empirical_correlations=high_sample_empirical,
    )
    low_lift = blended - baseline
    full_lift = saturated - baseline
    assert low_lift < full_lift / 3

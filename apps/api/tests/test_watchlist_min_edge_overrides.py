"""Tests for Smarter #30 — per-family ``watchlist_min_edge`` tuning
mechanism.

The PR introduces the mechanism (a per-family override dict in
``model_families.py``) without actually tuning any family's floor —
every family resolves to ``settings.watchlist_min_edge`` (the
operator-set default). The regression tests below pin the
empty-registry contract + the default-fallback behavior, then verify
the override path lands in scoring + monotonicity exactly as Smarter
#28's quality-tier mechanism does.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services.model_families import (
    FAMILY_DEFINITIONS,
    WATCHLIST_MIN_EDGE_OVERRIDES,
    watchlist_min_edge_for,
)


# -- registry contract --------------------------------------------------


def test_override_registry_starts_empty() -> None:
    """The Smarter #30 PR ships only the mechanism; no family is
    tuned yet. Adding entries here should be a conscious calibration
    decision driven by Smarter #2's walk-forward backtest output."""
    assert WATCHLIST_MIN_EDGE_OVERRIDES == {}


def test_every_registered_family_falls_back_to_default() -> None:
    """With the registry empty, every family resolves to whatever
    ``settings.watchlist_min_edge`` is set to."""
    default = get_settings().watchlist_min_edge
    for definition in FAMILY_DEFINITIONS:
        assert watchlist_min_edge_for(definition.key, default) == default


def test_unknown_family_falls_back_to_default() -> None:
    """Family keys that don't appear in ``FAMILY_DEFINITIONS``
    (one-off scopes, malformed inputs) get the default — no
    KeyError, no special case."""
    assert watchlist_min_edge_for("some_unknown_family", 0.07) == 0.07
    assert watchlist_min_edge_for("", 0.05) == 0.05


def test_override_overrides_default(monkeypatch) -> None:
    """A populated override wins over the passed default."""
    monkeypatch.setitem(WATCHLIST_MIN_EDGE_OVERRIDES, "nba_props", 0.06)
    assert watchlist_min_edge_for("nba_props", 0.03) == 0.06
    # Siblings still get the default.
    assert watchlist_min_edge_for("mlb_props", 0.03) == 0.03


def test_override_can_be_lower_than_default(monkeypatch) -> None:
    """Overrides can both raise *and* lower the floor — a tight MLB
    game line market might warrant a stricter floor while a noisy
    NBA prop family might warrant a looser one."""
    monkeypatch.setitem(WATCHLIST_MIN_EDGE_OVERRIDES, "mlb_singles", 0.045)
    monkeypatch.setitem(WATCHLIST_MIN_EDGE_OVERRIDES, "nba_props", 0.02)
    assert watchlist_min_edge_for("mlb_singles", 0.03) == 0.045
    assert watchlist_min_edge_for("nba_props", 0.03) == 0.02


def test_default_argument_is_required() -> None:
    """The ``default`` arg is positional/required so callers can't
    accidentally fall through to a stale module-level constant when
    the operator setting changes mid-session."""
    import inspect

    sig = inspect.signature(watchlist_min_edge_for)
    default_param = sig.parameters["default"]
    assert default_param.default is inspect.Parameter.empty


# -- consumer wiring ----------------------------------------------------


def test_scoring_kernel_imports_per_family_lookup() -> None:
    """The scoring kernel must import the per-family lookup so the
    ``min_edge`` suppression check actually consults the override
    registry (vs. silently reading ``settings.watchlist_min_edge``
    directly)."""
    import app.services.scoring as scoring_module

    assert hasattr(scoring_module, "watchlist_min_edge_for")


def test_monotonicity_module_imports_per_family_lookup() -> None:
    """Same wiring for the bug #9 post-clamp floor check — both the
    recommendation and prediction paths must route through the
    per-family lookup."""
    from app.services.scoring import monotonicity as monotonicity_module

    assert hasattr(monotonicity_module, "watchlist_min_edge_for")
    assert hasattr(monotonicity_module, "single_family_key")


def test_monotonicity_consumer_honors_override(monkeypatch) -> None:
    """Behavior-level pin (not just an import check): a stricter
    ``nba_props`` floor causes the bug #9 suppression to fire on a
    clamped pick whose post-clamp edge clears the default floor but
    NOT the override. Without the per-family lookup wired through,
    the suppression would only trigger at the operator-setting floor.
    """
    from app.models import Market, Recommendation, SignalSnapshot
    from app.services.scoring.monotonicity import _enforce_prop_monotonicity
    from app.services.scoring.types import ScoredRecommendation

    # Anchor: default floor is 0.03; override raises it to 0.06.
    monkeypatch.setitem(WATCHLIST_MIN_EDGE_OVERRIDES, "nba_props", 0.06)

    lower_meta = {
        "copilot_market_family": "player_prop",
        "copilot_stat_key": "points",
        "copilot_threshold": 20.0,
        "copilot_subject_name": "Player X",
    }
    higher_meta = {**lower_meta, "copilot_threshold": 25.0}

    lower_market = Market(
        ticker="NBA-PROP-L", sport_key="NBA", event_id=1, title="20+",
        status="active", raw_data=lower_meta,
    )
    higher_market = Market(
        ticker="NBA-PROP-H", sport_key="NBA", event_id=1, title="25+",
        status="active", raw_data=higher_meta,
    )

    # Lower rung sits at fair 0.60. Higher rung clamps from 0.70 → 0.60.
    # Post-clamp edge = 0.60 - 0.55 = 0.05.
    # - With default 0.03 floor: 0.05 > 0.03 → pick survives.
    # - With override 0.06 floor: 0.05 < 0.06 → pick is suppressed.
    lower_scored = ScoredRecommendation(
        recommendation=Recommendation(
            event_id=1, market_id=1, side="yes", action="buy", status="active",
            suggested_price=0.55, edge=0.05, confidence=0.6,
            invalidation="t", rationale="lower",
            scoring_diagnostics={"selected_side_probability": 0.60},
        ),
        signal=SignalSnapshot(
            event_id=1, market_id=1, confidence=0.6,
            fair_yes_price=0.60, fair_no_price=0.40, edge=0.05,
            reasons=[], features={}, scoring_diagnostics={},
        ),
        metadata=lower_meta,
    )
    higher_scored = ScoredRecommendation(
        recommendation=Recommendation(
            event_id=1, market_id=2, side="yes", action="buy", status="active",
            suggested_price=0.55, edge=0.15, confidence=0.6,
            invalidation="t", rationale="higher",
            scoring_diagnostics={"selected_side_probability": 0.70},
        ),
        signal=SignalSnapshot(
            event_id=1, market_id=2, confidence=0.6,
            fair_yes_price=0.70, fair_no_price=0.30, edge=0.15,
            reasons=[], features={}, scoring_diagnostics={},
        ),
        metadata=higher_meta,
    )

    _enforce_prop_monotonicity(
        [(lower_market, lower_scored), (higher_market, higher_scored)],
    )

    # Override fires: the recommendation is suppressed despite clearing
    # the operator default floor.
    assert higher_scored.recommendation is None
    suppression_reasons = list(
        higher_scored.signal.scoring_diagnostics.get("suppression_reasons") or []
    )
    assert "monotonicity_edge_below_min" in suppression_reasons

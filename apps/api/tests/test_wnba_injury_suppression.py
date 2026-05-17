"""Smarter WNBA PR 7 — WNBA injury suppression wiring.

Mirrors ``test_nba_injury_suppression.py`` but for the WNBA path:

- ``wnba_injury_suppress_when`` callback (the SUPPRESS-policy
  callback gated to ``family_key == "wnba_props"``).
- ``_single_scoring_adjustments`` threading: a WNBA prop with an OUT
  / DOUBTFUL player and a fresh report surfaces
  ``injury_suppression_reason`` in ``scoring_diagnostics`` exactly
  like the NBA path does.
- Cross-sport isolation: an NBA prop with WNBA-shaped injury features
  must NOT trip the ``wnba_injury`` gate, and vice versa.

The shared ``emit_nba_injury_features`` emitter is sport-agnostic and
covered by ``test_nba_injury_suppression.py``; those tests aren't
duplicated here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


# -- wnba_injury_suppress_when callback ---------------------------------


def test_wnba_injury_suppress_when_returns_out_for_out_status_on_wnba_props() -> None:
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        wnba_injury_suppress_when,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        metadata={},
        family_key="wnba_props",
    )
    assert wnba_injury_suppress_when(ctx) == "player_injury_out"


def test_wnba_injury_suppress_when_returns_doubtful_for_doubtful_status() -> None:
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        wnba_injury_suppress_when,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_doubtful": 1.0,
        },
        metadata={},
        family_key="wnba_props",
    )
    assert wnba_injury_suppress_when(ctx) == "player_injury_doubtful"


def test_wnba_injury_suppress_when_returns_none_on_nba_props() -> None:
    # Codex Pattern 9 — the WNBA suppression must be gated to
    # ``wnba_props``. A stray injury feature on an NBA row must NOT
    # trip THIS callback (the NBA callback owns NBA rows).
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        wnba_injury_suppress_when,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        metadata={},
        family_key="nba_props",
    )
    assert wnba_injury_suppress_when(ctx) is None


def test_wnba_injury_suppress_when_returns_none_on_stale_report() -> None:
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        wnba_injury_suppress_when,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 0.0,
            "player_injury_status_out": 1.0,
        },
        metadata={},
        family_key="wnba_props",
    )
    assert wnba_injury_suppress_when(ctx) is None


def test_wnba_injury_suppress_when_returns_none_on_questionable_status() -> None:
    # Mirror NBA: ``questionable`` does NOT suppress.
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        wnba_injury_suppress_when,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_questionable": 1.0,
        },
        metadata={},
        family_key="wnba_props",
    )
    assert wnba_injury_suppress_when(ctx) is None


def test_wnba_injury_suppress_when_returns_none_when_data_incomplete() -> None:
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        wnba_injury_suppress_when,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 0.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        metadata={},
        family_key="wnba_props",
    )
    assert wnba_injury_suppress_when(ctx) is None


# -- registry wiring ----------------------------------------------------


def test_wnba_injury_is_registered_as_suppress_policy() -> None:
    from app.services.scoring.feature_groups import (
        FEATURE_GROUP_POLICIES,
        FeatureGroupSeverity,
        wnba_injury_suppress_when,
    )

    policy = FEATURE_GROUP_POLICIES.get("wnba_injury")
    assert policy is not None
    assert policy.severity is FeatureGroupSeverity.SUPPRESS
    assert policy.suppress_when is wnba_injury_suppress_when


def test_check_suppressions_dispatches_to_wnba_injury_for_wnba_props() -> None:
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        check_suppressions,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        metadata={},
        family_key="wnba_props",
    )
    result = check_suppressions(ctx)
    assert result.get("wnba_injury") == "player_injury_out"
    # The NBA callback is family-gated to nba_props; it must NOT fire
    # on a WNBA row.
    assert "nba_injury" not in result


def test_check_suppressions_does_not_fire_wnba_injury_on_nba_props() -> None:
    from app.services.scoring.feature_groups import (
        SuppressionContext,
        check_suppressions,
    )

    ctx = SuppressionContext(
        features={
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        metadata={},
        family_key="nba_props",
    )
    result = check_suppressions(ctx)
    assert "wnba_injury" not in result
    # NBA path still fires.
    assert result.get("nba_injury") == "player_injury_out"


# -- scoring kernel integration ----------------------------------------


def _adjust(
    features: dict[str, Any],
    *,
    family_key: str = "wnba_props",
) -> dict[str, Any]:
    from app.services.scoring import _single_scoring_adjustments

    db = MagicMock()
    event = MagicMock()
    event.starts_at = None
    metadata = {"copilot_market_family": "player_prop"}
    base_features = {
        "has_team_context": True,
        "has_opponent_context": True,
    }
    base_features.update(features)
    _, diagnostics = _single_scoring_adjustments(
        db,
        family_key=family_key,
        event=event,
        market=None,
        snapshot=None,
        metadata=metadata,
        features=base_features,
        probability_yes=0.5,
        base_confidence=0.7,
        left=None,
        right=None,
    )
    return diagnostics


def test_scoring_threads_injury_suppression_reason_for_wnba_out() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        }
    )
    assert diagnostics.get("injury_suppression_reason") == "player_injury_out"


def test_scoring_threads_injury_suppression_reason_for_wnba_doubtful() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_doubtful": 1.0,
        }
    )
    assert diagnostics.get("injury_suppression_reason") == "player_injury_doubtful"


def test_scoring_omits_injury_suppression_for_wnba_questionable() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_questionable": 1.0,
        }
    )
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_injury_suppression_when_wnba_report_is_stale() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 0.0,
            "player_injury_status_out": 1.0,
        }
    )
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_injury_suppression_for_non_props_wnba_family() -> None:
    # Mirror NBA: non-prop WNBA families (singles, parlay legs) don't
    # apply the WNBA prop-level injury gate.
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        family_key="wnba_singles",
    )
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_wnba_injury_for_mlb_props() -> None:
    # Stray WNBA injury features on an MLB row must NOT trip the gate.
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        family_key="mlb_props",
    )
    assert "injury_suppression_reason" not in diagnostics

"""Architecture #5 follow-up 2 — SUPPRESS-policy registry tests.

The bespoke gates for Smarter #16 (mlb_lineup) and Smarter #17
(nba_injury) used to live inline in
``_single_scoring_adjustments``. This follow-up moves them into named
``suppress_when`` callbacks on the policy registry; the kernel now
delegates via ``check_suppressions``.

These tests pin:

- The two callbacks return the right suppression reasons for every
  case the original inline logic handled (scratch / confirmed /
  pre-lineup / no-require for mlb_lineup; out / doubtful / questionable
  / stale / family-mismatch for nba_injury).
- ``check_suppressions`` walks ``FEATURE_GROUP_POLICIES`` and only
  invokes callbacks for groups whose ``severity`` is SUPPRESS.
- The registry now resolves ``mlb_lineup`` and ``nba_injury`` to
  SUPPRESS policies with the right callbacks attached (i.e. the
  consolidation actually happened).

End-to-end behavior preservation is covered by
``test_lineup_suppress.py``, ``test_nba_injury_suppression.py``, and
``test_nba_workload.py`` — those still pass against
``_single_scoring_adjustments`` because the kernel delegates here and
maps the result onto the existing diagnostic keys.
"""

from __future__ import annotations

from app.services.scoring.feature_groups import (
    FEATURE_GROUP_POLICIES,
    FeatureGroupSeverity,
    SuppressionContext,
    check_suppressions,
    mlb_lineup_suppress_when,
    nba_injury_suppress_when,
    policy_for_group,
)


def _ctx(
    *,
    features: dict | None = None,
    metadata: dict | None = None,
    family_key: str = "mlb_props",
) -> SuppressionContext:
    return SuppressionContext(
        features=features or {},
        metadata=metadata or {},
        family_key=family_key,
    )


# -- registry resolution ------------------------------------------------


def test_mlb_lineup_resolves_to_suppress_with_callback() -> None:
    policy = policy_for_group("mlb_lineup")
    assert policy.severity is FeatureGroupSeverity.SUPPRESS
    assert policy.suppress_when is mlb_lineup_suppress_when


def test_nba_injury_resolves_to_suppress_with_callback() -> None:
    policy = policy_for_group("nba_injury")
    assert policy.severity is FeatureGroupSeverity.SUPPRESS
    assert policy.suppress_when is nba_injury_suppress_when


def test_default_group_still_resolves_to_ignore_with_no_callback() -> None:
    """Sanity check: groups without an explicit registry entry don't
    accidentally get a suppression callback through the consolidation.
    """
    policy = policy_for_group("mlb_park")
    assert policy.severity is FeatureGroupSeverity.IGNORE
    assert policy.suppress_when is None


# -- mlb_lineup_suppress_when -------------------------------------------


def test_mlb_lineup_callback_fires_on_scratch() -> None:
    """Scratch: lineup confirmed, player NOT in starting lineup."""
    reason = mlb_lineup_suppress_when(
        _ctx(
            features={
                "lineup_data_complete": 1.0,
                "player_in_starting_lineup": 0.0,
            },
            metadata={"copilot_requires_lineup": True},
        )
    )
    assert reason == "player_not_in_starting_lineup"


def test_mlb_lineup_callback_returns_none_when_player_confirmed() -> None:
    reason = mlb_lineup_suppress_when(
        _ctx(
            features={
                "lineup_data_complete": 1.0,
                "player_in_starting_lineup": 1.0,
            },
            metadata={"copilot_requires_lineup": True},
        )
    )
    assert reason is None


def test_mlb_lineup_callback_returns_none_before_lineup_window() -> None:
    """Pre-lineup window: lineup_data_complete=0 → no suppression
    (the kernel falls back to a ``lineup_confirmation`` missing-
    context penalty instead)."""
    reason = mlb_lineup_suppress_when(
        _ctx(
            features={},
            metadata={"copilot_requires_lineup": True},
        )
    )
    assert reason is None


def test_mlb_lineup_callback_returns_none_when_market_does_not_require_lineup() -> None:
    """Markets that don't set ``copilot_requires_lineup`` opt out of
    the gate entirely — stray lineup features must not trip it."""
    reason = mlb_lineup_suppress_when(
        _ctx(
            features={
                "lineup_data_complete": 1.0,
                "player_in_starting_lineup": 0.0,
            },
            metadata={},
        )
    )
    assert reason is None


def test_mlb_lineup_callback_gated_to_props_families() -> None:
    """Codex review caught: the original inline gate was nested inside
    ``elif family_key.endswith("_props")``. A non-prop family (winner,
    game-line, first-five) that somehow set ``copilot_requires_lineup``
    + scratch-shaped lineup features was never reached pre-consolidation
    and must not fire now either — codex Pattern 9.
    """
    scratch_features = {
        "lineup_data_complete": 1.0,
        "player_in_starting_lineup": 0.0,
    }
    requires_lineup = {"copilot_requires_lineup": True}
    for non_prop_family in ("winner", "mlb_singles", "nba_singles", "game_line"):
        reason = mlb_lineup_suppress_when(
            _ctx(
                features=scratch_features,
                metadata=requires_lineup,
                family_key=non_prop_family,
            )
        )
        assert reason is None, (
            f"mlb_lineup_suppress_when must not fire for family_key={non_prop_family!r}"
        )
    # Sanity-check: the same inputs DO fire for a _props family so
    # we know the test setup is otherwise correct.
    reason_props = mlb_lineup_suppress_when(
        _ctx(
            features=scratch_features,
            metadata=requires_lineup,
            family_key="mlb_props",
        )
    )
    assert reason_props == "player_not_in_starting_lineup"


# -- nba_injury_suppress_when -------------------------------------------


def test_nba_injury_callback_fires_on_out_when_fresh() -> None:
    reason = nba_injury_suppress_when(
        _ctx(
            features={
                "injury_data_complete": 1.0,
                "injury_report_is_fresh": 1.0,
                "player_injury_status_out": 1.0,
            },
            family_key="nba_props",
        )
    )
    assert reason == "player_injury_out"


def test_nba_injury_callback_fires_on_doubtful_when_fresh() -> None:
    reason = nba_injury_suppress_when(
        _ctx(
            features={
                "injury_data_complete": 1.0,
                "injury_report_is_fresh": 1.0,
                "player_injury_status_doubtful": 1.0,
            },
            family_key="nba_props",
        )
    )
    assert reason == "player_injury_doubtful"


def test_nba_injury_callback_returns_none_for_questionable() -> None:
    """Questionable players still play more often than not; the
    suppression list intentionally stops at ``doubtful``."""
    reason = nba_injury_suppress_when(
        _ctx(
            features={
                "injury_data_complete": 1.0,
                "injury_report_is_fresh": 1.0,
                "player_injury_status_questionable": 1.0,
            },
            family_key="nba_props",
        )
    )
    assert reason is None


def test_nba_injury_callback_returns_none_when_report_stale() -> None:
    reason = nba_injury_suppress_when(
        _ctx(
            features={
                "injury_data_complete": 1.0,
                "injury_report_is_fresh": 0.0,
                "player_injury_status_out": 1.0,
            },
            family_key="nba_props",
        )
    )
    assert reason is None


def test_nba_injury_callback_returns_none_when_data_incomplete() -> None:
    reason = nba_injury_suppress_when(
        _ctx(
            features={
                "injury_report_is_fresh": 1.0,
                "player_injury_status_out": 1.0,
            },
            family_key="nba_props",
        )
    )
    assert reason is None


def test_nba_injury_callback_gated_to_nba_props_family() -> None:
    """Codex Pattern 9 — a stray injury feature on an MLB row must
    not suppress.
    """
    reason = nba_injury_suppress_when(
        _ctx(
            features={
                "injury_data_complete": 1.0,
                "injury_report_is_fresh": 1.0,
                "player_injury_status_out": 1.0,
            },
            family_key="mlb_props",
        )
    )
    assert reason is None


def test_nba_injury_callback_does_not_fire_on_nba_singles() -> None:
    """Family gate excludes non-prop NBA families too — winners /
    first-five don't have a per-player injury concept.
    """
    reason = nba_injury_suppress_when(
        _ctx(
            features={
                "injury_data_complete": 1.0,
                "injury_report_is_fresh": 1.0,
                "player_injury_status_out": 1.0,
            },
            family_key="nba_singles",
        )
    )
    assert reason is None


# -- check_suppressions orchestrator ------------------------------------


def test_check_suppressions_returns_mlb_lineup_reason_in_isolation() -> None:
    suppressions = check_suppressions(
        _ctx(
            features={
                "lineup_data_complete": 1.0,
                "player_in_starting_lineup": 0.0,
            },
            metadata={"copilot_requires_lineup": True},
            family_key="mlb_props",
        )
    )
    assert suppressions == {"mlb_lineup": "player_not_in_starting_lineup"}


def test_check_suppressions_returns_nba_injury_reason_in_isolation() -> None:
    suppressions = check_suppressions(
        _ctx(
            features={
                "injury_data_complete": 1.0,
                "injury_report_is_fresh": 1.0,
                "player_injury_status_out": 1.0,
            },
            family_key="nba_props",
        )
    )
    assert suppressions == {"nba_injury": "player_injury_out"}


def test_check_suppressions_returns_empty_when_no_gate_fires() -> None:
    """An MLB prop with no lineup requirement and an NBA prop with no
    injury data both produce empty suppression maps — the kernel
    proceeds to score normally.
    """
    assert check_suppressions(_ctx(family_key="mlb_props")) == {}
    assert check_suppressions(_ctx(family_key="nba_props")) == {}


def test_check_suppressions_only_invokes_suppress_policy_callbacks(monkeypatch) -> None:
    """Sanity check: registry walks the SUPPRESS-only path. Mutating
    a PENALIZE-policy entry to look like a callable in a roundabout
    way would be wrong; this test pins that callbacks attached to
    PENALIZE / IGNORE entries are never invoked.
    """
    invocations: list[str] = []

    def spy(ctx: SuppressionContext) -> str | None:
        invocations.append("called")
        return None

    # Attach a callable to a PENALIZE entry (which would be a registry
    # mistake — pin that the SUPPRESS gate path ignores it).
    original_policy = FEATURE_GROUP_POLICIES["mlb_weather"]
    bad_policy = type(original_policy)(
        severity=original_policy.severity,
        ttl=original_policy.ttl,
        penalty_confidence_delta=original_policy.penalty_confidence_delta,
        suppress_when=spy,
    )
    monkeypatch.setitem(FEATURE_GROUP_POLICIES, "mlb_weather", bad_policy)

    check_suppressions(_ctx(family_key="nba_props"))

    assert invocations == [], (
        "check_suppressions must skip PENALIZE-policy groups even when "
        "they carry a suppress_when callback"
    )

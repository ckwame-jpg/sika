"""Pin the contract of the Architecture #5 feature-groups scaffolding.

These tests cover only the pure-data module
(``app.services.scoring.feature_groups``); the kernel + persistence
integration lives in separate test files. The point of pinning the
scaffolding contract first is to catch regressions in the registry,
the derived-view, the freshness math, and the serialization round-trip
without having to spin up the full scoring kernel.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.scoring.feature_groups import (
    DEFAULT_POLICY,
    FEATURE_GROUP_POLICIES,
    FeatureGroupPolicy,
    FeatureGroupSeverity,
    FeatureGroupSnapshot,
    FreshnessAssessment,
    check_freshness,
    deserialize_feature_groups,
    emit_to_group,
    features_view,
    policy_for_group,
    register_group,
    serialize_feature_groups,
)


_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


# -- registry contract -------------------------------------------------


def test_default_policy_is_ignore() -> None:
    """Adding a new emitter without a registry entry must not
    accidentally gate scoring. The fallback is IGNORE with a
    365-day TTL so even derived features that happen to have a
    fresh_at won't trip the freshness check."""
    assert DEFAULT_POLICY.severity is FeatureGroupSeverity.IGNORE
    assert DEFAULT_POLICY.ttl >= timedelta(days=30)


def test_unknown_group_falls_back_to_default() -> None:
    assert policy_for_group("some_new_group_we_havent_registered") is DEFAULT_POLICY
    assert policy_for_group("") is DEFAULT_POLICY


def test_registered_penalize_groups_have_nonzero_delta() -> None:
    """Every PENALIZE entry needs a delta — a zero delta is a
    silent IGNORE wearing PENALIZE clothing. The registry should
    fail this assertion loudly if someone adds an entry without
    thinking through the penalty."""
    for group_key, policy in FEATURE_GROUP_POLICIES.items():
        if policy.severity is FeatureGroupSeverity.PENALIZE:
            assert policy.penalty_confidence_delta < 0.0, (
                f"{group_key} is PENALIZE but has non-negative delta "
                f"{policy.penalty_confidence_delta}"
            )


def test_initial_registry_has_expected_penalize_groups() -> None:
    """Pin the exact PENALIZE-policy group set so a typo in the
    group_key (e.g. "mlb_weather" → "mlb_wether") fails CI instead
    of silently flipping a group to IGNORE.

    Update this test when the registry intentionally grows; the
    failure is the whole point. Smarter WNBA PR 4 added
    ``wnba_workload`` (mirror of nba_workload — same -3% / 24h
    semantics, distinct group key so operator diagnostics stay clear
    about which sport's workload signal is stale)."""
    penalize_groups = {
        group for group, policy in FEATURE_GROUP_POLICIES.items()
        if policy.severity is FeatureGroupSeverity.PENALIZE
    }
    assert penalize_groups == {
        "mlb_weather", "mlb_bullpen", "nba_workload", "wnba_workload",
    }


def test_suppress_policies_match_consolidated_registry() -> None:
    """Architecture #5 follow-up 2 consolidated the Smarter #16
    (mlb_lineup) and Smarter #17 (nba_injury) bespoke gates into
    SUPPRESS-policy registry entries. Smarter WNBA PR 7 added a
    parallel ``wnba_injury`` SUPPRESS entry (separate group keyed by
    sport, callback gated to ``wnba_props``). mlb_starter remains
    bespoke (not suppression-shaped). Pin the exact set so adding a
    new SUPPRESS group is an explicit test update — not a silent
    activation."""
    suppress_groups = {
        group: policy for group, policy in FEATURE_GROUP_POLICIES.items()
        if policy.severity is FeatureGroupSeverity.SUPPRESS
    }
    assert set(suppress_groups) == {"mlb_lineup", "nba_injury", "wnba_injury"}
    for policy in suppress_groups.values():
        assert policy.suppress_when is not None, (
            "SUPPRESS policy entries must declare a suppress_when callback"
        )


# -- migration helpers -------------------------------------------------


def test_register_group_writes_snapshot_with_completeness() -> None:
    feature_groups: dict[str, FeatureGroupSnapshot] = {}
    register_group(
        feature_groups,
        "mlb_weather",
        {"weather_temp_f": 72.0},
        fresh_at=_NOW,
        source="load_weather",
    )
    assert "mlb_weather" in feature_groups
    snap = feature_groups["mlb_weather"]
    assert snap.group_key == "mlb_weather"
    assert snap.values == {"weather_temp_f": 72.0}
    assert snap.fresh_at == _NOW
    assert snap.source == "load_weather"
    assert snap.completeness == 1.0


def test_register_group_completeness_zero_for_empty_values() -> None:
    feature_groups: dict[str, FeatureGroupSnapshot] = {}
    register_group(feature_groups, "mlb_weather", {}, fresh_at=_NOW)
    assert feature_groups["mlb_weather"].completeness == 0.0


def test_emit_to_group_writes_to_both_structures() -> None:
    """The migration helper writes the same values dict to both the
    structured ``feature_groups`` (source of truth) AND the flat
    ``features`` (derived view). The kernel's interim reads —
    ``emit_nba_interaction_term`` reads ``recent_usage_pct`` right
    after ``emit_nba_player_features`` populates it — depend on
    ``features`` being current after each call."""
    feature_groups: dict[str, FeatureGroupSnapshot] = {}
    features: dict[str, Any] = {"venue_indoor": False}  # kernel-direct prior write
    emit_to_group(
        feature_groups,
        features,
        "mlb_weather",
        {"weather_temp_f": 72.0, "weather_wind_speed_mph": 8.0},
        fresh_at=_NOW,
        source="load_weather",
    )
    # Source of truth.
    assert feature_groups["mlb_weather"].values == {
        "weather_temp_f": 72.0, "weather_wind_speed_mph": 8.0,
    }
    # Derived view — kernel-direct keys preserved alongside the emitter's.
    assert features == {
        "venue_indoor": False,
        "weather_temp_f": 72.0,
        "weather_wind_speed_mph": 8.0,
    }


# -- derived view ------------------------------------------------------


def test_features_view_flattens_groups() -> None:
    snapshots = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather",
            values={"weather_temp_f": 72.0, "weather_wind_speed_mph": 8.0},
            fresh_at=_NOW,
            source="load_weather",
            completeness=1.0,
        ),
        "mlb_park": FeatureGroupSnapshot(
            group_key="mlb_park",
            values={"park_factor_hr": 1.04},
            source="load_park_factors",
            completeness=1.0,
        ),
    }
    assert features_view(snapshots) == {
        "weather_temp_f": 72.0,
        "weather_wind_speed_mph": 8.0,
        "park_factor_hr": 1.04,
    }


def test_features_view_empty_for_no_groups() -> None:
    assert features_view({}) == {}


def test_features_view_skips_empty_value_dicts() -> None:
    """A group with no populated values (e.g. cache miss) still
    counts as a present group but contributes no keys to the flat
    view. Pins the derived-view doesn't crash on the empty case."""
    snapshots = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather", values={}, fresh_at=_NOW,
        ),
    }
    assert features_view(snapshots) == {}


def test_features_view_last_group_wins_on_collision() -> None:
    """Groups should NOT share keys in practice — the migration
    enforces disjoint key namespaces. But if they do, last-write
    wins. Pin that behavior so a future regression is loud."""
    snapshots = {
        "a": FeatureGroupSnapshot(group_key="a", values={"shared_key": 1}),
        "b": FeatureGroupSnapshot(group_key="b", values={"shared_key": 2}),
    }
    # Python dict preserves insertion order; "b" inserts after "a".
    assert features_view(snapshots)["shared_key"] == 2


# -- freshness check ---------------------------------------------------


def test_freshness_fresh_group_is_not_stale() -> None:
    snapshots = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather",
            values={"weather_temp_f": 72.0},
            fresh_at=_NOW - timedelta(hours=1),
        ),
    }
    [assessment] = check_freshness(snapshots, now=_NOW)
    assert assessment.group_key == "mlb_weather"
    assert assessment.is_stale is False
    assert assessment.confidence_delta == 0.0
    assert assessment.age == timedelta(hours=1)


def test_freshness_stale_penalize_group_emits_delta() -> None:
    """mlb_weather TTL is 6h; data fresh_at 8h ago is stale, so
    the configured -0.05 delta should fire."""
    snapshots = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather",
            values={"weather_temp_f": 72.0},
            fresh_at=_NOW - timedelta(hours=8),
        ),
    }
    [assessment] = check_freshness(snapshots, now=_NOW)
    assert assessment.is_stale is True
    assert assessment.confidence_delta == pytest.approx(-0.05)


def test_freshness_stale_ignore_group_emits_no_delta() -> None:
    """Even if mlb_park were stale, IGNORE policy means no penalty
    fires. Confidence delta stays 0."""
    snapshots = {
        "mlb_park": FeatureGroupSnapshot(
            group_key="mlb_park",
            values={"park_factor_hr": 1.04},
            fresh_at=_NOW - timedelta(days=180),  # very stale
        ),
    }
    [assessment] = check_freshness(snapshots, now=_NOW)
    assert assessment.severity is FeatureGroupSeverity.IGNORE
    # Default TTL is 365 days; 180 days isn't stale anyway.
    assert assessment.is_stale is False
    assert assessment.confidence_delta == 0.0


def test_freshness_group_with_no_fresh_at_opts_out() -> None:
    """Derived feature groups (interaction terms, schedule
    density) don't have an externally-refreshed cache. They leave
    fresh_at None and must never trip the freshness check."""
    snapshots = {
        "nba_interaction": FeatureGroupSnapshot(
            group_key="nba_interaction",
            values={"nba_offense_interaction_term": 1.02},
            fresh_at=None,
        ),
    }
    [assessment] = check_freshness(snapshots, now=_NOW)
    assert assessment.is_stale is False
    assert assessment.age is None
    assert assessment.confidence_delta == 0.0


def test_freshness_coerces_naive_datetime_to_utc() -> None:
    """SQLite drops tz on DateTime columns, so emitters that read
    cache rows often get naive datetimes back. The freshness check
    has to treat them as UTC; Postgres returns tz-aware, so the
    two backends must produce identical assessments."""
    snapshots = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather",
            values={"weather_temp_f": 72.0},
            fresh_at=(_NOW - timedelta(hours=1)).replace(tzinfo=None),
        ),
    }
    [assessment] = check_freshness(snapshots, now=_NOW)
    assert assessment.is_stale is False
    assert assessment.age == timedelta(hours=1)


def test_freshness_multiple_groups_returns_one_assessment_each() -> None:
    snapshots = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather",
            values={"weather_temp_f": 72.0},
            fresh_at=_NOW - timedelta(hours=8),  # stale
        ),
        "mlb_park": FeatureGroupSnapshot(
            group_key="mlb_park",
            values={"park_factor_hr": 1.04},
            fresh_at=_NOW - timedelta(days=30),  # not-stale under 365-day default
        ),
    }
    assessments = check_freshness(snapshots, now=_NOW)
    by_key = {a.group_key: a for a in assessments}
    assert by_key["mlb_weather"].is_stale is True
    assert by_key["mlb_park"].is_stale is False


# -- serialization round-trip ------------------------------------------


def test_serialize_round_trips_through_deserialize() -> None:
    original = {
        "mlb_weather": FeatureGroupSnapshot(
            group_key="mlb_weather",
            values={"weather_temp_f": 72.0, "weather_wind_speed_mph": 8.0},
            fresh_at=_NOW,
            source="load_weather",
            completeness=1.0,
        ),
        "nba_interaction": FeatureGroupSnapshot(
            group_key="nba_interaction",
            values={"nba_offense_interaction_term": 1.02},
            fresh_at=None,
            source="emit_nba_interaction_term",
            completeness=0.5,
        ),
    }
    serialized = serialize_feature_groups(original)
    # JSON-compatible: every datetime is now a string or None.
    assert serialized["mlb_weather"]["fresh_at"] == _NOW.isoformat()
    assert serialized["nba_interaction"]["fresh_at"] is None
    # Round-trip restores identical FeatureGroupSnapshot values.
    restored = deserialize_feature_groups(serialized)
    assert restored == original


def test_deserialize_empty_or_none_returns_empty_dict() -> None:
    """Prediction rows written before Architecture #5 won't have
    a ``feature_groups`` field; the reader must round-trip those
    rows as an empty dict so the scoring kernel falls back to the
    flat ``features`` dict."""
    assert deserialize_feature_groups(None) == {}
    assert deserialize_feature_groups({}) == {}


def test_deserialize_tolerates_missing_fields() -> None:
    """A group payload missing ``source`` / ``completeness`` /
    ``values`` shouldn't crash the loader — those fields default
    to safe values."""
    raw = {"some_group": {"fresh_at": None}}
    restored = deserialize_feature_groups(raw)
    assert "some_group" in restored
    assert restored["some_group"].values == {}
    assert restored["some_group"].source == ""
    assert restored["some_group"].completeness == 0.0


def test_deserialize_tolerates_unparseable_fresh_at() -> None:
    raw = {
        "mlb_weather": {
            "values": {"weather_temp_f": 72.0},
            "fresh_at": "not-a-real-iso-string",
            "source": "load_weather",
            "completeness": 1.0,
        },
    }
    restored = deserialize_feature_groups(raw)
    # Unparseable fresh_at falls back to None (opt-out of freshness).
    assert restored["mlb_weather"].fresh_at is None
    # Other fields preserved.
    assert restored["mlb_weather"].values == {"weather_temp_f": 72.0}


def test_deserialize_skips_non_dict_payloads() -> None:
    """A corrupt persistence row that has, say, a string under a
    group_key shouldn't crash — just skip that group."""
    raw: dict = {
        "mlb_weather": {"values": {"weather_temp_f": 72.0}},
        "broken_group": "not a dict",
    }
    restored = deserialize_feature_groups(raw)
    assert "mlb_weather" in restored
    assert "broken_group" not in restored

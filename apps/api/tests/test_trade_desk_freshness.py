"""Tests for Smarter #22 — feature freshness SLAs (PR A: UI surface).

Pins the trade-desk builder's surfacing of
``scoring_diagnostics["freshness_stale_groups"]`` and the per-group
``feature_groups`` source labels onto a typed ``freshness_stale_groups``
list on ``TradeDeskThresholdRead`` + ``TradeDeskGameLineRead``. The
frontend ``FreshnessBadge`` component reads from these fields.

The scoring kernel already populates the underlying diagnostics
(``Architecture #5`` — sika#169 + follow-ups #173, #175) but no
downstream surface consumes them yet. This PR closes the visibility
gap; operators can finally see which picks were affected by stale
feature data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models import (
    Event,
    EventParticipant,
    Market,
    Participant,
    Recommendation,
)
from app.schemas import FreshnessStaleGroupRead, TradeDeskThresholdRead
from app.services.trade_desk import build_trade_desk_response


def _seed_player_prop_with_freshness_diagnostics(
    db_session,
    *,
    freshness_stale_groups: list[dict] | None = None,
    feature_groups: dict[str, dict] | None = None,
    freshness_confidence_delta: float | None = None,
):
    """Plant a minimal player-prop recommendation whose
    ``scoring_diagnostics`` carries the Architecture #5 freshness
    fields. Returns the seeded Recommendation."""
    player = Participant(
        external_id="freshness-player",
        sport_key="NBA",
        display_name="Test Player",
        short_name="Player",
        participant_type="player",
    )
    db_session.add(player)
    db_session.flush()

    event = Event(
        external_id="freshness-event",
        sport_key="NBA",
        name="Test event",
        status="in_progress",
        starts_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        EventParticipant(
            event_id=event.id, participant_id=player.id,
            role="home", is_home=True,
        )
    )
    db_session.flush()

    market = Market(
        ticker="KXFRESH-22",
        sport_key="NBA",
        event_id=event.id,
        title="Test Player: 22+ points?",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 22.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Test Player",
            "copilot_subject_team": "TOR",
        },
    )
    db_session.add(market)
    db_session.flush()

    diagnostics: dict = {"selected_side_probability": 0.78}
    if freshness_stale_groups is not None:
        diagnostics["freshness_stale_groups"] = freshness_stale_groups
    if feature_groups is not None:
        diagnostics["feature_groups"] = feature_groups
    if freshness_confidence_delta is not None:
        diagnostics["freshness_confidence_delta"] = freshness_confidence_delta

    rec = Recommendation(
        event_id=event.id, market_id=market.id,
        side="yes", action="buy", status="active",
        suggested_price=0.62, edge=0.16, confidence=0.71,
        invalidation="test", rationale="test",
        scoring_diagnostics=diagnostics,
    )
    db_session.add(rec)
    db_session.commit()
    return rec


# -- Schema tests ------------------------------------------------------


def test_freshness_stale_group_read_schema_round_trips() -> None:
    """The new Pydantic schema accepts the exact shape the scoring
    kernel writes into ``scoring_diagnostics``."""
    payload = {
        "group_key": "mlb_weather",
        "severity": "penalize",
        "age_seconds": 25200,
        "confidence_delta": -0.05,
        "source": "load_weather",
    }
    read = FreshnessStaleGroupRead.model_validate(payload)
    assert read.group_key == "mlb_weather"
    assert read.severity == "penalize"
    assert read.age_seconds == 25200
    assert read.confidence_delta == pytest.approx(-0.05)
    assert read.source == "load_weather"


def test_freshness_stale_group_read_rejects_unknown_severity() -> None:
    """Literal type pins the operator-facing band (matches
    ``FeatureGroupSeverity``)."""
    with pytest.raises(ValidationError):
        FreshnessStaleGroupRead.model_validate({
            "group_key": "mlb_weather",
            "severity": "ROFL",
            "age_seconds": 100,
            "confidence_delta": -0.05,
            "source": "load_weather",
        })


def test_freshness_stale_group_read_accepts_null_age_seconds() -> None:
    """The scoring kernel writes ``age_seconds: None`` when
    ``assessment.age`` is None (defensive nominal contract). Current
    code paths only append ``is_stale=True`` rows — which always have
    a real age — so this is mostly hypothetical, but the schema must
    not reject it or the whole stale-group list gets dropped + the
    drift log fires spuriously."""
    read = FreshnessStaleGroupRead.model_validate({
        "group_key": "mlb_weather",
        "severity": "penalize",
        "age_seconds": None,
        "confidence_delta": -0.05,
        "source": "load_weather",
    })
    assert read.age_seconds is None


def test_trade_desk_threshold_carries_default_empty_freshness_list() -> None:
    """Adding the freshness field must be additive: callers that
    don't pass ``freshness_stale_groups`` get an empty list."""
    threshold = TradeDeskThresholdRead(
        ticker="T", threshold=10.0, probability_yes=0.5,
        selected_side="yes", edge=0.1, confidence=0.7,
    )
    assert threshold.freshness_stale_groups == []
    assert threshold.freshness_confidence_delta is None


# -- Builder integration ----------------------------------------------


def test_trade_desk_surfaces_freshness_stale_groups_on_threshold(db_session) -> None:
    """Happy path: scoring_diagnostics carries a stale group; the
    trade-desk builder pulls the entry into the typed list AND enriches
    each row with the source label from ``feature_groups``."""
    _seed_player_prop_with_freshness_diagnostics(
        db_session,
        freshness_stale_groups=[
            {
                "group_key": "mlb_weather",
                "severity": "penalize",
                "age_seconds": 25200,
                "confidence_delta": -0.05,
            }
        ],
        feature_groups={
            "mlb_weather": {
                "values": {"weather_temp_f": 72.0},
                "fresh_at": "2026-05-16T18:00:00+00:00",
                "source": "load_weather",
                "completeness": 1.0,
            }
        },
        freshness_confidence_delta=-0.05,
    )

    response = build_trade_desk_response(db_session, sport="NBA")
    assert len(response.events) == 1
    threshold = response.events[0].player_props[0].stat_groups[0].thresholds[0]
    assert len(threshold.freshness_stale_groups) == 1
    row = threshold.freshness_stale_groups[0]
    assert row.group_key == "mlb_weather"
    assert row.severity == "penalize"
    assert row.age_seconds == 25200
    assert row.confidence_delta == pytest.approx(-0.05)
    # Source is enriched from feature_groups so the badge can show
    # the operator which cache fed the stale value.
    assert row.source == "load_weather"
    assert threshold.freshness_confidence_delta == pytest.approx(-0.05)


def test_trade_desk_emits_empty_freshness_list_when_no_stale_groups(db_session) -> None:
    """When scoring_diagnostics omits the freshness fields entirely
    (the common case once all groups are fresh) the typed list is empty
    rather than None — the frontend treats `[]` and `null` the same
    but the empty-list contract is easier to type-check against."""
    _seed_player_prop_with_freshness_diagnostics(db_session)

    response = build_trade_desk_response(db_session, sport="NBA")
    threshold = response.events[0].player_props[0].stat_groups[0].thresholds[0]
    assert threshold.freshness_stale_groups == []
    assert threshold.freshness_confidence_delta is None


def test_trade_desk_freshness_source_falls_back_when_feature_groups_missing(db_session) -> None:
    """Defensive: an older recommendation row (or one persisted before
    the source-enrichment landed) lacks ``feature_groups`` entirely.
    The builder still surfaces the stale entry with an empty source
    string rather than dropping the row or 500-ing."""
    _seed_player_prop_with_freshness_diagnostics(
        db_session,
        freshness_stale_groups=[
            {
                "group_key": "mlb_weather",
                "severity": "penalize",
                "age_seconds": 25200,
                "confidence_delta": -0.05,
            }
        ],
        # feature_groups is omitted entirely.
        freshness_confidence_delta=-0.05,
    )

    response = build_trade_desk_response(db_session, sport="NBA")
    threshold = response.events[0].player_props[0].stat_groups[0].thresholds[0]
    assert len(threshold.freshness_stale_groups) == 1
    assert threshold.freshness_stale_groups[0].source == ""


def test_trade_desk_logs_warning_when_freshness_payload_malformed(
    db_session, caplog: pytest.LogCaptureFixture,
) -> None:
    """Drift safety: a freshness_stale_groups entry that doesn't
    validate (e.g. unknown severity from a future schema version)
    must NOT 500 the trade-desk response. The builder logs a warning
    so persistent drift is observable, and skips the bad row."""
    _seed_player_prop_with_freshness_diagnostics(
        db_session,
        freshness_stale_groups=[
            {
                "group_key": "mlb_weather",
                "severity": "INVALID_SEVERITY_VALUE",  # not in Literal
                "age_seconds": 25200,
                "confidence_delta": -0.05,
            },
            # Valid entry alongside — should still surface.
            {
                "group_key": "nba_workload",
                "severity": "penalize",
                "age_seconds": 90000,
                "confidence_delta": -0.03,
            },
        ],
        feature_groups={
            "nba_workload": {
                "values": {},
                "fresh_at": "2026-05-15T18:00:00+00:00",
                "source": "load_nba_team_gamelog",
                "completeness": 1.0,
            },
        },
        freshness_confidence_delta=-0.03,
    )

    with caplog.at_level("WARNING", logger="app.services.trade_desk"):
        response = build_trade_desk_response(db_session, sport="NBA")
    threshold = response.events[0].player_props[0].stat_groups[0].thresholds[0]
    # Only the valid row surfaces.
    assert len(threshold.freshness_stale_groups) == 1
    assert threshold.freshness_stale_groups[0].group_key == "nba_workload"
    # And the drift was logged so the operator can see it in the API logs.
    assert any(
        "freshness_stale_groups_drift" in record.message
        for record in caplog.records
    )

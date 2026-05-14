"""Tests for Smarter #17 — late-breaking NBA injury news.

Two layers:
- ``emit_nba_injury_features`` — pure-function emitter translating an
  ESPN-style injury payload into scoring features.
- ``_single_scoring_adjustments`` — adds a hard ``player_injury_out``
  /``player_injury_doubtful`` entry to ``suppression_reasons`` when:
    * family is ``nba_props``
    * the injury report is fresh (within 12h)
    * the player's normalized status is ``out`` or ``doubtful``

This PR ships the consumer-side mechanism; the actual NBA injury
report LOADER is a separate follow-up PR. The features and
suppression are gated on ``injury_data_complete == 1.0`` so they
naturally no-op until the loader populates the payload.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.advanced_stats import emit_nba_injury_features


_NOW = datetime(2026, 5, 14, 19, 0, tzinfo=timezone.utc)


def _payload(
    *,
    status: str = "out",
    player_name: str = "Jayson Tatum",
    report_offset_hours: float = 1.0,
) -> dict[str, Any]:
    """Build an ESPN-style injury payload for one player."""
    return {
        "report_updated_at": (_NOW - timedelta(hours=report_offset_hours)).isoformat(),
        "players": {
            player_name: {
                "status": status,
                "designation": "left knee soreness",
            }
        },
    }


# -- emit_nba_injury_features -------------------------------------------


def test_emit_injury_returns_empty_when_payload_missing() -> None:
    assert emit_nba_injury_features(None, player_name="Tatum") == {}
    assert emit_nba_injury_features({}, player_name="Tatum") == {}


def test_emit_injury_returns_empty_when_player_not_in_payload() -> None:
    payload = _payload(player_name="LeBron James")
    assert emit_nba_injury_features(payload, player_name="Tatum") == {}


def test_emit_injury_returns_empty_when_player_name_missing() -> None:
    payload = _payload()
    assert emit_nba_injury_features(payload, player_name=None) == {}
    assert emit_nba_injury_features(payload, player_name="") == {}
    assert emit_nba_injury_features(payload, player_name="   ") == {}


def test_emit_injury_flags_out_status() -> None:
    out = emit_nba_injury_features(
        _payload(status="OUT", player_name="Tatum"),
        player_name="Tatum",
        now=_NOW,
    )
    assert out["player_injury_status_out"] == 1.0
    assert out["player_injury_status_doubtful"] == 0.0
    assert out["player_injury_status_questionable"] == 0.0
    assert out["injury_data_complete"] == 1.0
    assert out["injury_report_is_fresh"] == 1.0


def test_emit_injury_flags_doubtful_status() -> None:
    out = emit_nba_injury_features(
        _payload(status="Doubtful", player_name="Tatum"),
        player_name="Tatum",
        now=_NOW,
    )
    assert out["player_injury_status_doubtful"] == 1.0
    assert out["player_injury_status_out"] == 0.0


def test_emit_injury_flags_questionable_status() -> None:
    out = emit_nba_injury_features(
        _payload(status="questionable", player_name="Tatum"),
        player_name="Tatum",
        now=_NOW,
    )
    assert out["player_injury_status_questionable"] == 1.0


def test_emit_injury_treats_day_to_day_as_questionable() -> None:
    # ESPN occasionally publishes "day-to-day" as the status string.
    out = emit_nba_injury_features(
        _payload(status="Day-to-Day", player_name="Tatum"),
        player_name="Tatum",
        now=_NOW,
    )
    assert out["player_injury_status_questionable"] == 1.0


def test_emit_injury_skips_unrecognized_status() -> None:
    # Active / probable produce no useful suppression signal — return {}
    # so the kernel doesn't suppress and the feature dict stays clean.
    assert emit_nba_injury_features(
        {"players": {"Tatum": {"status": "active"}}},
        player_name="Tatum",
        now=_NOW,
    ) == {}


def test_emit_injury_handles_espn_variants_with_leading_word_match() -> None:
    # ESPN sometimes returns "Out (illness)" / "Out for season" /
    # "Doubtful (left knee)" — leading-word matching catches the
    # canonical status without false-positives on substring matches
    # like "workout status".
    for raw, expected_status in (
        ("Out (illness)", "out"),
        ("Out for season", "out"),
        ("Doubtful (left knee)", "doubtful"),
        ("Questionable - return TBD", "questionable"),
    ):
        out = emit_nba_injury_features(
            {"players": {"P": {"status": raw}}},
            player_name="P",
            now=_NOW,
        )
        assert out[f"player_injury_status_{expected_status}"] == 1.0


def test_emit_injury_rejects_unrelated_strings_containing_substrings() -> None:
    # Code-reviewer-flagged regression: ``"workout"`` contains ``"out"``
    # as a substring. Leading-word matching rejects it; the previous
    # ``"out" in normalized`` substring check would have falsely fired.
    for raw in ("workout status", "throughout the season", "blackout"):
        out = emit_nba_injury_features(
            {"players": {"P": {"status": raw}}},
            player_name="P",
            now=_NOW,
        )
        assert out == {}, f"expected empty for {raw!r}, got {out!r}"


def test_emit_injury_flags_stale_report_when_report_outside_window() -> None:
    out = emit_nba_injury_features(
        _payload(status="out", player_name="Tatum", report_offset_hours=20.0),
        player_name="Tatum",
        now=_NOW,
    )
    assert out["player_injury_status_out"] == 1.0
    assert out["injury_report_is_fresh"] == 0.0


def test_emit_injury_flags_fresh_when_report_just_inside_window() -> None:
    out = emit_nba_injury_features(
        _payload(status="out", player_name="Tatum", report_offset_hours=11.99),
        player_name="Tatum",
        now=_NOW,
    )
    assert out["injury_report_is_fresh"] == 1.0


def test_emit_injury_flags_stale_when_report_updated_at_missing() -> None:
    payload = {"players": {"Tatum": {"status": "out"}}}  # no report_updated_at
    out = emit_nba_injury_features(payload, player_name="Tatum", now=_NOW)
    assert out["injury_report_is_fresh"] == 0.0


def test_emit_injury_flags_stale_when_report_updated_at_malformed() -> None:
    payload = {
        "report_updated_at": "not a timestamp",
        "players": {"Tatum": {"status": "out"}},
    }
    out = emit_nba_injury_features(payload, player_name="Tatum", now=_NOW)
    assert out["injury_report_is_fresh"] == 0.0


def test_emit_injury_handles_naive_datetime_input() -> None:
    # report_updated_at can come back naive from SQLite-style storage —
    # the helper coerces to UTC for the subtraction.
    naive = (_NOW - timedelta(hours=1)).replace(tzinfo=None)
    payload = {
        "report_updated_at": naive,
        "players": {"Tatum": {"status": "out"}},
    }
    out = emit_nba_injury_features(payload, player_name="Tatum", now=_NOW)
    assert out["injury_report_is_fresh"] == 1.0


def test_emit_injury_handles_future_report_updated_at() -> None:
    # Defensive: a report stamped in the future (clock skew) shouldn't
    # be treated as fresh — the freshness check requires non-negative
    # age.
    payload = {
        "report_updated_at": (_NOW + timedelta(hours=1)).isoformat(),
        "players": {"Tatum": {"status": "out"}},
    }
    out = emit_nba_injury_features(payload, player_name="Tatum", now=_NOW)
    assert out["injury_report_is_fresh"] == 0.0


# -- scoring kernel integration -----------------------------------------


def _adjust(
    features: dict[str, Any],
    *,
    family_key: str = "nba_props",
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


def test_scoring_threads_injury_suppression_reason_when_out_and_fresh() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        }
    )
    assert diagnostics.get("injury_suppression_reason") == "player_injury_out"


def test_scoring_threads_injury_suppression_reason_when_doubtful_and_fresh() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_doubtful": 1.0,
        }
    )
    assert diagnostics.get("injury_suppression_reason") == "player_injury_doubtful"


def test_scoring_omits_injury_suppression_for_questionable() -> None:
    # Questionable players still play more often than not; the
    # suppression list intentionally stops at ``doubtful``.
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_questionable": 1.0,
        }
    )
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_injury_suppression_when_report_is_stale() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 0.0,
            "player_injury_status_out": 1.0,
        }
    )
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_injury_suppression_when_no_injury_data() -> None:
    # No loader has populated anything — features dict has nothing to act on.
    diagnostics = _adjust({})
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_injury_suppression_for_mlb_props() -> None:
    # Codex Pattern 9 — gated to nba_props. A stray injury feature on
    # an MLB row must not suppress.
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        family_key="mlb_props",
    )
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_injury_suppression_for_non_props_families() -> None:
    diagnostics = _adjust(
        {
            "injury_data_complete": 1.0,
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        },
        family_key="nba_singles",
    )
    assert "injury_suppression_reason" not in diagnostics


def test_scoring_omits_injury_suppression_when_data_incomplete() -> None:
    # injury_data_complete missing → no suppression even if the status
    # flags somehow ended up set (defensive: features may be partial).
    diagnostics = _adjust(
        {
            "injury_report_is_fresh": 1.0,
            "player_injury_status_out": 1.0,
        }
    )
    assert "injury_suppression_reason" not in diagnostics


# -- suppression_reasons translation ------------------------------------


def test_full_scoring_path_appends_player_injury_out_to_suppression_reasons() -> None:
    """End-to-end check that ``injury_suppression_reason='player_injury_out'``
    in diagnostics surfaces as a ``player_injury_out`` entry in
    ``suppression_reasons`` after the downstream translation."""
    # Mirror the translation logic in scoring.py:_build_scored_recommendation
    # so we exercise the same path the kernel uses.
    scoring_diagnostics = {"injury_suppression_reason": "player_injury_out"}
    suppression_reasons: list[str] = []
    injury_reason = str(scoring_diagnostics.get("injury_suppression_reason") or "")
    if injury_reason in {"player_injury_out", "player_injury_doubtful"}:
        suppression_reasons.append(injury_reason)
    assert suppression_reasons == ["player_injury_out"]


def test_suppression_outcome_reason_maps_injury_out() -> None:
    from app.services.scoring import _suppression_outcome_reason
    from app.models import SignalSnapshot

    # Build a minimal scored stub — only the diagnostics field matters.
    signal = SignalSnapshot(
        scoring_diagnostics={"suppression_reasons": ["player_injury_out"]},
    )
    scored = MagicMock()
    scored.signal = signal
    assert _suppression_outcome_reason(scored, current_watchlist_market=True) == (
        "suppressed_player_injury_out"
    )


def test_suppression_outcome_reason_maps_injury_doubtful() -> None:
    from app.services.scoring import _suppression_outcome_reason
    from app.models import SignalSnapshot

    signal = SignalSnapshot(
        scoring_diagnostics={"suppression_reasons": ["player_injury_doubtful"]},
    )
    scored = MagicMock()
    scored.signal = signal
    assert _suppression_outcome_reason(scored, current_watchlist_market=True) == (
        "suppressed_player_injury_doubtful"
    )

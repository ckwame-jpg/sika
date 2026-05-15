"""Tests for Smarter #18 phase 2d — sportsbook disagreement suppression.

Covers:
- Operator-settings helpers (toggle / threshold / min_book_count)
  default behavior + round-trip.
- ``_single_scoring_adjustments`` writes
  ``sportsbook_disagreement_suppression`` to the diagnostics dict
  when toggle is ON AND book count >= min AND abs gap >= threshold.
- ``_build_scored_recommendation`` appends ``model_book_disagreement``
  to ``suppression_reasons`` when the diagnostic flag is set.
- ``_suppression_outcome_reason`` maps the new suppression to
  ``suppressed_model_book_disagreement``.
- All gates respect toggle-OFF / low-book-count / below-threshold —
  i.e. the rule fires only when ALL three conditions are met.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.models import (
    Event,
    EventParticipant,
    OperatorSetting,
    Participant,
)
from app.services.operator_settings import (
    DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT,
    DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD,
    SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY,
    SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY,
    effective_sportsbook_disagreement_min_book_count,
    effective_sportsbook_disagreement_suppression_enabled,
    effective_sportsbook_disagreement_threshold,
    set_sportsbook_disagreement_suppression_enabled,
)


_NOW = datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc)
_event_counter = {"n": 0}


def _seed_event(db_session) -> Event:
    _event_counter["n"] += 1
    suffix = _event_counter["n"]
    home = Participant(sport_key="NBA", display_name="Boston Celtics", participant_type="competitor")
    away = Participant(sport_key="NBA", display_name="Brooklyn Nets", participant_type="competitor")
    db_session.add_all([home, away])
    db_session.flush()
    event = Event(
        external_id=f"evt-bos-bkn-{suffix}",
        sport_key="NBA",
        name="Brooklyn Nets @ Boston Celtics",
        starts_at=_NOW,
        status="scheduled",
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all([
        EventParticipant(event_id=event.id, participant_id=home.id, role="competitor", is_home=True),
        EventParticipant(event_id=event.id, participant_id=away.id, role="competitor", is_home=False),
    ])
    db_session.flush()
    return event


# -- Operator settings round-trip -------------------------------------


def test_toggle_defaults_to_off(db_session) -> None:
    assert effective_sportsbook_disagreement_suppression_enabled(db_session) is False


def test_toggle_round_trip(db_session) -> None:
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    db_session.commit()
    assert effective_sportsbook_disagreement_suppression_enabled(db_session) is True
    set_sportsbook_disagreement_suppression_enabled(db_session, False)
    db_session.commit()
    assert effective_sportsbook_disagreement_suppression_enabled(db_session) is False


def test_threshold_defaults_to_15pp(db_session) -> None:
    assert effective_sportsbook_disagreement_threshold(db_session) == DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD


def test_threshold_clamps_invalid_values_to_default(db_session) -> None:
    db_session.add(
        OperatorSetting(
            key=SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY,
            value={"threshold": 1.5},  # > 1.0 is meaningless
        )
    )
    db_session.flush()
    assert effective_sportsbook_disagreement_threshold(db_session) == DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD


def test_threshold_accepts_valid_override(db_session) -> None:
    db_session.add(
        OperatorSetting(
            key=SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY,
            value={"threshold": 0.10},
        )
    )
    db_session.flush()
    assert effective_sportsbook_disagreement_threshold(db_session) == 0.10


def test_min_book_count_defaults_to_3(db_session) -> None:
    assert (
        effective_sportsbook_disagreement_min_book_count(db_session)
        == DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    )


def test_min_book_count_accepts_override(db_session) -> None:
    db_session.add(
        OperatorSetting(
            key=SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY,
            value={"min_book_count": 5},
        )
    )
    db_session.flush()
    assert effective_sportsbook_disagreement_min_book_count(db_session) == 5


def test_min_book_count_rejects_negative_or_non_int(db_session) -> None:
    db_session.add(
        OperatorSetting(
            key=SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY,
            value={"min_book_count": -1},
        )
    )
    db_session.flush()
    assert (
        effective_sportsbook_disagreement_min_book_count(db_session)
        == DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    )


def test_min_book_count_clamps_unreasonably_high_value(db_session) -> None:
    # Reviewer LOW catch: operator typo of ``300`` would silently
    # disable the rule. Cap at 100 (well above any realistic book
    # count from The Odds API's free tier) so the silent no-op is
    # caught at clamp-time.
    db_session.add(
        OperatorSetting(
            key=SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT_KEY,
            value={"min_book_count": 300},
        )
    )
    db_session.flush()
    assert (
        effective_sportsbook_disagreement_min_book_count(db_session)
        == DEFAULT_SPORTSBOOK_DISAGREEMENT_MIN_BOOK_COUNT
    )


def test_min_book_count_setter_round_trips(db_session) -> None:
    from app.services.operator_settings import set_sportsbook_disagreement_min_book_count

    set_sportsbook_disagreement_min_book_count(db_session, 7)
    db_session.commit()
    assert effective_sportsbook_disagreement_min_book_count(db_session) == 7


def test_threshold_setter_round_trips(db_session) -> None:
    from app.services.operator_settings import set_sportsbook_disagreement_threshold

    set_sportsbook_disagreement_threshold(db_session, 0.08)
    db_session.commit()
    assert effective_sportsbook_disagreement_threshold(db_session) == 0.08


def test_threshold_setter_falls_back_via_clamp_on_invalid(db_session) -> None:
    # The setter accepts any numeric so operators can SEE clamping
    # via the next ``effective_*`` read.
    from app.services.operator_settings import set_sportsbook_disagreement_threshold

    set_sportsbook_disagreement_threshold(db_session, 2.5)  # > 1.0 invalid
    db_session.commit()
    # Read clamps to default.
    assert (
        effective_sportsbook_disagreement_threshold(db_session)
        == DEFAULT_SPORTSBOOK_DISAGREEMENT_THRESHOLD
    )


# -- Scoring kernel integration ---------------------------------------


def _run_adjustments(
    db_session,
    monkeypatch,
    *,
    probability_yes: float,
    consensus_prob: float,
    book_count: int,
) -> dict[str, Any]:
    """Stub the emitter to return the supplied consensus payload,
    then call ``_single_scoring_adjustments`` and return the
    resulting diagnostics dict."""
    from app.services import scoring

    event = _seed_event(db_session)

    monkeypatch.setattr(
        "app.services.sportsbook_consensus.emit_sportsbook_consensus_diagnostics",
        lambda db, evt, **kwargs: {
            "sportsbook_consensus_prob": consensus_prob,
            "sportsbook_book_count": book_count,
            "sportsbook_match_orientation": "same",
            "sportsbook_match_similarity": 0.95,
        },
    )

    _, diagnostics = scoring._single_scoring_adjustments(
        db_session,
        family_key="nba_singles",
        event=event,
        market=None,
        snapshot=None,
        metadata={},
        features={"family_key": "nba_singles"},
        probability_yes=probability_yes,
        base_confidence=0.65,
        left=event.participants[0],
        right=event.participants[1],
    )
    return diagnostics


def test_suppression_fires_when_all_conditions_met(db_session, monkeypatch) -> None:
    # Toggle ON; large gap (model 0.70 vs consensus 0.45 = 25pp); enough books.
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    db_session.commit()
    diagnostics = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.70, consensus_prob=0.45, book_count=5,
    )
    assert diagnostics.get("sportsbook_disagreement_suppression") == "model_book_disagreement"
    assert "sportsbook_disagreement_gap" in diagnostics
    assert "sportsbook_disagreement_threshold" in diagnostics


def test_suppression_gap_carries_signed_direction(db_session, monkeypatch) -> None:
    """``sportsbook_disagreement_gap`` is SIGNED so operators can see
    whether sika was more or less bullish than the book. Positive =
    model more bullish; negative = model more bearish."""
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    db_session.commit()

    # Model bullish (0.70 vs 0.45 consensus) → positive gap.
    bullish = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.70, consensus_prob=0.45, book_count=5,
    )
    assert bullish["sportsbook_disagreement_gap"] > 0

    # Model bearish (0.20 vs 0.45 consensus) → negative gap.
    bearish = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.20, consensus_prob=0.45, book_count=5,
    )
    assert bearish["sportsbook_disagreement_gap"] < 0
    # Suppression still fires either way (the threshold check uses abs).
    assert bearish["sportsbook_disagreement_suppression"] == "model_book_disagreement"


def test_suppression_skipped_when_toggle_off(db_session, monkeypatch) -> None:
    # Toggle OFF (default) — even with a large gap and many books,
    # no suppression diagnostic is written.
    diagnostics = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.70, consensus_prob=0.45, book_count=5,
    )
    assert "sportsbook_disagreement_suppression" not in diagnostics


def test_suppression_skipped_when_book_count_below_min(db_session, monkeypatch) -> None:
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    db_session.commit()
    # Only 2 books — default min is 3; suppression should NOT fire.
    diagnostics = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.70, consensus_prob=0.45, book_count=2,
    )
    assert "sportsbook_disagreement_suppression" not in diagnostics


def test_suppression_skipped_when_gap_below_threshold(db_session, monkeypatch) -> None:
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    db_session.commit()
    # 10pp gap is below the default 15pp threshold.
    diagnostics = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.55, consensus_prob=0.45, book_count=5,
    )
    assert "sportsbook_disagreement_suppression" not in diagnostics


def test_suppression_fires_at_exact_threshold_boundary(db_session, monkeypatch) -> None:
    # 15pp gap == threshold; using >= should fire.
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    db_session.commit()
    diagnostics = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.60, consensus_prob=0.45, book_count=3,
    )
    assert diagnostics.get("sportsbook_disagreement_suppression") == "model_book_disagreement"


def test_suppression_respects_custom_threshold(db_session, monkeypatch) -> None:
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    # Tighten to 10pp via operator override.
    db_session.add(
        OperatorSetting(
            key=SPORTSBOOK_DISAGREEMENT_THRESHOLD_KEY,
            value={"threshold": 0.10},
        )
    )
    db_session.commit()
    # 12pp gap fires under custom threshold but not default.
    diagnostics = _run_adjustments(
        db_session, monkeypatch,
        probability_yes=0.57, consensus_prob=0.45, book_count=3,
    )
    assert diagnostics.get("sportsbook_disagreement_suppression") == "model_book_disagreement"


def test_suppression_skipped_when_no_consensus_diagnostic(db_session, monkeypatch) -> None:
    # Stub emitter returns {} → no consensus → no suppression even
    # if toggle is on.
    set_sportsbook_disagreement_suppression_enabled(db_session, True)
    db_session.commit()
    from app.services import scoring

    event = _seed_event(db_session)
    monkeypatch.setattr(
        "app.services.sportsbook_consensus.emit_sportsbook_consensus_diagnostics",
        lambda db, evt, **kwargs: {},
    )

    _, diagnostics = scoring._single_scoring_adjustments(
        db_session,
        family_key="nba_singles",
        event=event,
        market=None,
        snapshot=None,
        metadata={},
        features={"family_key": "nba_singles"},
        probability_yes=0.7,
        base_confidence=0.65,
        left=event.participants[0],
        right=event.participants[1],
    )
    assert "sportsbook_disagreement_suppression" not in diagnostics


def test_suppression_outcome_reason_maps_correctly(db_session) -> None:
    # Build a ScoredRecommendation-like shape directly to exercise the
    # outcome-reason mapper without going through the full scoring
    # pipeline.
    from app.services import scoring
    from types import SimpleNamespace

    diagnostics = {"suppression_reasons": ["model_book_disagreement"]}
    signal = SimpleNamespace(scoring_diagnostics=diagnostics)
    scored = SimpleNamespace(signal=signal)

    reason = scoring._suppression_outcome_reason(scored, current_watchlist_market=True)
    assert reason == "suppressed_model_book_disagreement"


def test_suppression_outcome_ranks_below_injury_above_no_side(db_session) -> None:
    # When both injury and disagreement fire, injury wins (more
    # actionable / higher confidence signal). When disagreement and
    # no_side fire, disagreement wins.
    from app.services import scoring
    from types import SimpleNamespace

    # Injury + disagreement → injury wins.
    diagnostics_a = {
        "suppression_reasons": ["player_injury_out", "model_book_disagreement"],
    }
    signal_a = SimpleNamespace(scoring_diagnostics=diagnostics_a)
    scored_a = SimpleNamespace(signal=signal_a)
    assert (
        scoring._suppression_outcome_reason(scored_a, current_watchlist_market=True)
        == "suppressed_player_injury_out"
    )

    # Disagreement + no_side → disagreement wins.
    diagnostics_b = {
        "suppression_reasons": ["model_book_disagreement", "no_side_not_actionable_on_kalshi"],
    }
    signal_b = SimpleNamespace(scoring_diagnostics=diagnostics_b)
    scored_b = SimpleNamespace(signal=signal_b)
    assert (
        scoring._suppression_outcome_reason(scored_b, current_watchlist_market=True)
        == "suppressed_model_book_disagreement"
    )

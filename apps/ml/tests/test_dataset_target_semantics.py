"""Training target must encode P(YES wins), not P(selected side wins).

Background — bug #2 from SIKA_PUNCH_LIST.md
============================================

The training pipeline labels each row by ``prediction_outcome``:

    target = 1 if prediction_outcome == "won" else 0

That mixes two distinct quantities: when ``side == "yes"`` the target is
P(YES wins), but when ``side == "no"`` the target is P(NO wins) — i.e.
``1 - P(YES wins)``. Every serving path (``ml/runtime.py``, ``ml/shadow.py``,
``services/scoring.py``) reads ``predict_proba[:, 1]`` and treats it as
P(YES). So as soon as a single NO-side row reaches training, the model
silently learns a mix of two label conventions and serving inverts NO-side
predictions.

These tests pin the correct semantic: ``target == 1`` iff YES wins,
regardless of which side the recommendation took.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ml.dataset import settled_predictions_from_records


def _record(*, side: str, outcome: str, market_id: int = 1) -> dict:
    """Minimal settled prediction row for ``settled_predictions_from_records``."""
    return {
        "id": market_id,
        "market_id": market_id,
        "event_id": market_id,
        "ticker": f"TEST-{market_id}",
        "sport_key": "NBA",
        "event_name": f"Event {market_id}",
        "market_family": "player_prop",
        "market_kind": "player_prop",
        "stat_key": "points",
        "threshold": 22.5,
        "subject_name": f"Player {market_id}",
        "subject_team": "TEAM",
        "capture_scope": "recommendation",
        "side": side,
        "suggested_price": 0.45,
        "fair_yes_price": 0.55,
        "edge": 0.05,
        "confidence": 0.6,
        "selection_score": 0.1,
        "features": {"family_key": "nba_props"},
        "scoring_diagnostics": {},
        "market_status_at_capture": "active",
        "prediction_outcome": outcome,
        "settled_at": datetime(2026, 4, 17, 18, 0, tzinfo=timezone.utc).isoformat(),
        "realized_pnl": None,
        "captured_at": datetime(2026, 4, 17, 17, 0, tzinfo=timezone.utc).isoformat(),
    }


def test_target_is_one_when_yes_side_wins():
    """YES recommendation that won → YES won → target = 1."""
    frame = settled_predictions_from_records([_record(side="yes", outcome="won")])
    assert frame["target"].tolist() == [1]


def test_target_is_zero_when_yes_side_loses():
    """YES recommendation that lost → YES lost → target = 0."""
    frame = settled_predictions_from_records([_record(side="yes", outcome="lost")])
    assert frame["target"].tolist() == [0]


def test_target_is_zero_when_no_side_wins():
    """NO recommendation that won → NO won → YES lost → target = 0.

    This is the case the existing pipeline gets wrong: it labels this row
    with target=1 because ``prediction_outcome == "won"``, even though YES
    actually lost. A model trained on this label and served as P(YES) will
    invert the NO-side signal.
    """
    frame = settled_predictions_from_records([_record(side="no", outcome="won")])
    assert frame["target"].tolist() == [0]


def test_target_is_one_when_no_side_loses():
    """NO recommendation that lost → NO lost → YES won → target = 1.

    The mirror case: existing pipeline says target=0, but YES actually won.
    """
    frame = settled_predictions_from_records([_record(side="no", outcome="lost")])
    assert frame["target"].tolist() == [1]


def test_mixed_sides_produce_yes_centric_targets():
    """Order-preserving check across all four (side, outcome) combinations."""
    records = [
        _record(side="yes", outcome="won", market_id=1),
        _record(side="yes", outcome="lost", market_id=2),
        _record(side="no", outcome="won", market_id=3),
        _record(side="no", outcome="lost", market_id=4),
    ]
    frame = settled_predictions_from_records(records)
    # Sorted by captured_at, id — all share captured_at, so id order is preserved.
    assert frame["target"].tolist() == [1, 0, 0, 1]


def test_side_value_is_case_insensitive():
    """Uppercase sides must be normalized — DB values can vary."""
    frame = settled_predictions_from_records(
        [_record(side="NO", outcome="lost", market_id=1)]
    )
    assert frame["target"].tolist() == [1]


def test_rows_with_unexpected_side_are_dropped():
    """Side outside {yes,no} would otherwise silently get target=0 (XNOR
    against False) — drop those rows so the contract is fail-loud."""
    records = [
        _record(side="yes", outcome="won", market_id=1),
        _record(side="", outcome="won", market_id=2),
        _record(side="maybe", outcome="lost", market_id=3),
        _record(side="no", outcome="lost", market_id=4),
    ]
    frame = settled_predictions_from_records(records)
    assert frame["market_id"].tolist() == [1, 4]
    assert frame["target"].tolist() == [1, 1]

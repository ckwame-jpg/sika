"""Held-out metrics must rank by selected-side edge, not raw YES edge.

Codex P3 (bug #2 follow-up): after the target fix, ``predict_proba[:, 1]``
is P(YES). The training metric helper still computed
``edge = P(YES) - suggested_price`` and used that to pick the top decile.
For NO-side rows ``suggested_price`` is the NO contract price, so a
profitable NO bet (P(YES)=0.2, NO price=0.3) lands at edge=-0.10 and is
excluded from ``top_decile_roi``. Edge ranking has to use the
selected-side probability.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ml.training import _metrics_for_predictions


def _frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows).reset_index(drop=True)
    # _metrics_for_predictions uses .loc[indices] so the columns it touches
    # need to be in the frame.
    return frame


def test_no_side_profitable_bet_ranks_in_top_decile():
    """A strong NO bet — P(YES)=0.2, NO price=0.30 — has selected-side
    edge of 0.5 (P(NO)=0.8 vs NO price 0.3). Before the fix, edge was
    computed as 0.2 - 0.3 = -0.1 and the row was excluded from the top.
    """
    # 10 yes-side rows with edge ~0, one strong no-side bet that should
    # dominate the top decile.
    rows = [
        {"side": "yes", "suggested_price": 0.5, "realized_pnl": 0.0, "prediction_outcome": "won", "target": 1}
        for _ in range(9)
    ] + [
        {"side": "no", "suggested_price": 0.3, "realized_pnl": 0.7, "prediction_outcome": "won", "target": 0},
    ]
    frame = _frame(rows)
    probs = np.array([0.5] * 9 + [0.2])  # P(YES) on the NO row is 0.2 — strong NO
    indices = frame.index.to_numpy()

    metrics = _metrics_for_predictions(frame, indices, probs)

    # The NO bet is the most-profitable row; it must be in the top decile.
    assert metrics["top_decile_roi"] >= 0.65, (
        f"top decile should reflect the strong NO bet's realized pnl 0.7, "
        f"got {metrics['top_decile_roi']}"
    )


def test_yes_side_profitable_bet_still_ranks_correctly():
    """Regression guard: the YES-side path that the original code handled
    correctly must keep working under the new edge formula."""
    rows = [
        {"side": "no", "suggested_price": 0.5, "realized_pnl": 0.0, "prediction_outcome": "lost", "target": 1}
        for _ in range(9)
    ] + [
        {"side": "yes", "suggested_price": 0.3, "realized_pnl": 0.7, "prediction_outcome": "won", "target": 1},
    ]
    frame = _frame(rows)
    probs = np.array([0.5] * 9 + [0.8])  # P(YES) = 0.8, suggested YES price 0.3
    indices = frame.index.to_numpy()

    metrics = _metrics_for_predictions(frame, indices, probs)

    assert metrics["top_decile_roi"] >= 0.65

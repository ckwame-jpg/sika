"""Parity vectors for the Python side of the Kalshi fee calculation."""

import pytest

from app.services.kalshi_fees import (
    estimate_taker_fee_dollars,
    worst_case_taker_fee_dollars,
)


# Keep these vectors identical to apps/web/lib/kalshi-fees.test.ts.
@pytest.mark.parametrize(
    ("quantity", "price", "expected_fee", "expected_worst_case_fee"),
    [
        (25, 0.40, 0.42, 0.42),
        (50, 0.50, 0.88, 0.88),
        (50, 0.75, 0.66, 0.88),
        (1, 0.01, 0.01, 0.01),
        (3, 0.99, 0.01, 0.06),
    ],
)
def test_fee_estimates_match_typescript_parity_vectors(
    quantity: int,
    price: float,
    expected_fee: float,
    expected_worst_case_fee: float,
) -> None:
    assert estimate_taker_fee_dollars(quantity, price) == expected_fee
    assert worst_case_taker_fee_dollars(quantity, price) == expected_worst_case_fee


@pytest.mark.parametrize(
    ("quantity", "price"),
    [(0, 0.4), (-1, 0.4), (1, 0), (1, 1), (float("inf"), 0.4)],
)
def test_estimate_taker_fee_rejects_invalid_inputs(
    quantity: float, price: float
) -> None:
    assert estimate_taker_fee_dollars(quantity, price) == 0.0

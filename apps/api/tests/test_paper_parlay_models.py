"""Smoke tests for the paper-parlay models.

Phase 1 of PAPER_PARLAY_SCOPE.md: schema-only tests that prove the
new tables are created cleanly by ``Base.metadata.create_all``, the
``PaperParlay → PaperParlayLeg`` relationship cascades correctly, and
the composite uniqueness index forbids duplicate ``leg_index`` values
within the same parlay (which would break settlement aggregation).

Service-layer tests (create_paper_parlay, settlement rollup) land in
later phases — they have their own files and depend on this schema.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PaperParlay, PaperParlayLeg


def _make_parlay(**overrides) -> PaperParlay:
    base: dict = {
        "stake": 100.0,
        "leg_count": 2,
        "sport_scope": "NBA",
        "participating_sports": ["NBA"],
        "combined_market_price": 0.42,
        "combined_model_probability": 0.55,
        "american_odds": "+138",
        "edge": 0.13,
    }
    base.update(overrides)
    return PaperParlay(**base)


def _make_leg(*, leg_index: int, ticker: str = "TEST-LEG", **overrides) -> PaperParlayLeg:
    base: dict = {
        "leg_index": leg_index,
        "ticker": ticker,
        "market_title": f"Test market {leg_index}",
        "side": "yes",
        "suggested_price": 0.65,
    }
    base.update(overrides)
    return PaperParlayLeg(**base)


def test_paper_parlay_table_created_with_expected_columns(db_session: Session) -> None:
    """Sanity: the conftest's create_all picks up the new tables and
    the expected columns exist. If this fails, the new models aren't
    being registered on Base.metadata correctly."""
    parlay = _make_parlay()
    db_session.add(parlay)
    db_session.flush()
    assert parlay.id is not None
    # Defaults flow through the ORM layer, not just on commit.
    assert parlay.settlement_status == "pending"
    assert parlay.outcome == "pending"
    assert parlay.realized_pnl is None
    assert parlay.settled_at is None
    assert isinstance(parlay.created_at, datetime)
    assert parlay.created_at.tzinfo == timezone.utc


def test_paper_parlay_legs_relationship_roundtrip(db_session: Session) -> None:
    """The legs ORM relationship orders by leg_index and round-trips
    cleanly after a commit + refresh."""
    parlay = _make_parlay(leg_count=3)
    parlay.legs = [
        _make_leg(leg_index=2, ticker="LEG-C"),
        _make_leg(leg_index=0, ticker="LEG-A"),
        _make_leg(leg_index=1, ticker="LEG-B"),
    ]
    db_session.add(parlay)
    db_session.commit()
    db_session.refresh(parlay)
    # ``order_by="PaperParlayLeg.leg_index"`` on the relationship guarantees
    # the loaded list is sorted regardless of insert order — settlement and
    # display both depend on this invariant.
    assert [leg.ticker for leg in parlay.legs] == ["LEG-A", "LEG-B", "LEG-C"]
    # Cascade deletion: removing the parent removes the children. Without
    # this, settlement runs on orphan legs would leak rows over time.
    parent_id = parlay.id
    db_session.delete(parlay)
    db_session.commit()
    leftover = db_session.query(PaperParlayLeg).filter_by(paper_parlay_id=parent_id).count()
    assert leftover == 0


def test_paper_parlay_leg_unique_index_rejects_duplicate_leg_index(
    db_session: Session,
) -> None:
    """The ``ix_paper_parlay_legs_parlay_index`` unique index prevents
    two legs from claiming the same leg_index within a parlay.

    This is load-bearing for settlement: ``_settle_parlay_rows``-style
    aggregation iterates legs by index, and duplicate indexes would
    double-count or skip a leg's outcome. Codex pattern 5 (reset edge
    cases): if the service layer ever has a bug that tries to insert
    two leg #0s, the DB stops it cold instead of silently corrupting
    the rollup."""
    parlay = _make_parlay()
    parlay.legs = [_make_leg(leg_index=0, ticker="ORIG")]
    db_session.add(parlay)
    db_session.commit()
    # Same parlay, same leg_index → unique constraint violation.
    dup = PaperParlayLeg(
        paper_parlay_id=parlay.id,
        leg_index=0,
        ticker="DUP",
        market_title="dup",
        side="yes",
        suggested_price=0.5,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_paper_parlay_leg_unique_index_allows_same_leg_index_in_different_parlays(
    db_session: Session,
) -> None:
    """Codex pattern 6 (implicit data shape): the unique index is
    scoped to ``(paper_parlay_id, leg_index)``, NOT ``leg_index``
    alone. Two parlays should freely share leg_index=0 — otherwise
    only one parlay could ever exist with a first leg."""
    parlay_a = _make_parlay()
    parlay_a.legs = [_make_leg(leg_index=0, ticker="A0")]
    parlay_b = _make_parlay()
    parlay_b.legs = [_make_leg(leg_index=0, ticker="B0")]
    db_session.add_all([parlay_a, parlay_b])
    db_session.commit()
    # Both saved, both with a leg at index 0 — no constraint violation.
    assert parlay_a.id != parlay_b.id
    assert parlay_a.legs[0].leg_index == 0
    assert parlay_b.legs[0].leg_index == 0

"""Tests for bug #28 — ``/positions`` returns paper positions and
demo orders without pagination.

Polling endpoint: the portfolio page hits ``/positions`` every ~15 s.
Without bounds, every poll serializes every historical row in both
tables, so a long-lived account would see the response steadily grow
until JSON serialization dominates request latency. Bug #28 caps each
list with ``Query(default=200, ge=1, le=500)`` and slices via
``.limit(...)`` server-side. Cursor pagination is documented as a
follow-up if anyone genuinely needs more than 500 rows in a single
poll.

The most-recent rows win the cap (``opened_at desc, id desc`` for
paper positions; ``id desc`` for demo orders) so operators always see
their freshest activity first.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from app.models import DemoOrder, Market, PaperParlay, PaperPosition


def _seed_market(db_session, *, ticker: str = "NBA-BOS-MIA") -> Market:
    market = Market(
        ticker=ticker,
        sport_key="NBA",
        title=f"{ticker} market",
        status="open",
        raw_data={},
    )
    db_session.add(market)
    db_session.flush()
    return market


def _seed_paper_positions(db_session, market: Market, count: int) -> None:
    """Seed ``count`` paper positions with strictly increasing
    ``opened_at`` so order-by stays deterministic across SQLite +
    Postgres."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(count):
        db_session.add(
            PaperPosition(
                market_id=market.id,
                ticker=market.ticker,
                side="yes",
                quantity=1,
                entry_price=0.50,
                opened_at=base + timedelta(minutes=index),
                status="open",
            )
        )
    db_session.commit()


def _seed_demo_orders(db_session, market: Market, count: int) -> None:
    """Seed ``count`` demo orders. ``DemoOrder.id.desc()`` is the
    sort key, so insertion order determines who survives the cap."""
    for index in range(count):
        db_session.add(
            DemoOrder(
                market_id=market.id,
                ticker=market.ticker,
                client_order_id=f"client-{index}",
                side="yes",
                action="buy",
                quantity=1,
                limit_price=0.50,
                status="resting",
                approved_by_user=True,
            )
        )
    db_session.commit()


# -- Default cap -------------------------------------------------------


def test_paper_positions_capped_at_default_200(client, db_session) -> None:
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=205)

    response = client.get("/positions")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["paper_positions"]) == 200


def test_demo_orders_capped_at_default_200(client, db_session) -> None:
    market = _seed_market(db_session)
    _seed_demo_orders(db_session, market, count=205)

    response = client.get("/positions")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["demo_orders"]) == 200


def test_under_default_cap_returns_all_rows(client, db_session) -> None:
    """Bug #28 must not regress the small-account case — anything
    under the cap still returns every row (no false truncation)."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=3)
    _seed_demo_orders(db_session, market, count=4)

    payload = client.get("/positions").json()

    assert len(payload["paper_positions"]) == 3
    assert len(payload["demo_orders"]) == 4


# -- Custom limits -----------------------------------------------------


def test_paper_limit_query_param_applies(client, db_session) -> None:
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=15)

    payload = client.get("/positions?paper_limit=5").json()

    assert len(payload["paper_positions"]) == 5


def test_demo_limit_query_param_applies(client, db_session) -> None:
    market = _seed_market(db_session)
    _seed_demo_orders(db_session, market, count=15)

    payload = client.get("/positions?demo_limit=5").json()

    assert len(payload["demo_orders"]) == 5


def test_limits_are_independent(client, db_session) -> None:
    """A small ``paper_limit`` must not constrain ``demo_orders``
    (and vice versa) — they're separately tunable."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=10)
    _seed_demo_orders(db_session, market, count=10)

    payload = client.get("/positions?paper_limit=2&demo_limit=8").json()

    assert len(payload["paper_positions"]) == 2
    assert len(payload["demo_orders"]) == 8


# -- Clamping ----------------------------------------------------------


def test_paper_limit_above_ceiling_is_rejected(client) -> None:
    """``Query(le=500)`` should 422 over-large values rather than
    silently clamping; the ceiling is part of the public contract."""
    response = client.get("/positions?paper_limit=10000")
    assert response.status_code == 422


def test_paper_limit_below_floor_is_rejected(client) -> None:
    response = client.get("/positions?paper_limit=0")
    assert response.status_code == 422


def test_demo_limit_above_ceiling_is_rejected(client) -> None:
    response = client.get("/positions?demo_limit=10000")
    assert response.status_code == 422


def test_paper_limit_at_ceiling_accepted(client, db_session) -> None:
    """Boundary check: ``500`` is the documented max and must be
    accepted — operators that need the historical tail set this
    explicitly."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=10)

    response = client.get("/positions?paper_limit=500")
    assert response.status_code == 200
    assert len(response.json()["paper_positions"]) == 10


# -- Ordering preserved across the cap ---------------------------------


def test_paper_positions_keep_most_recent_when_capped(client, db_session) -> None:
    """When the cap trims rows, the newest ones must survive
    (``opened_at desc`` ordering preserved). Operators care about
    fresh activity — losing today's positions while keeping
    yesterday's would invert the product expectation."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=10)

    payload = client.get("/positions?paper_limit=3").json()

    paper = payload["paper_positions"]
    assert len(paper) == 3
    # Index 9 (most recently opened) ↔ index 7 (3rd most recent).
    opened_at = [item["opened_at"] for item in paper]
    assert opened_at == sorted(opened_at, reverse=True)


def test_demo_orders_keep_most_recent_when_capped(client, db_session) -> None:
    market = _seed_market(db_session)
    _seed_demo_orders(db_session, market, count=10)

    payload = client.get("/positions?demo_limit=3").json()

    demo = payload["demo_orders"]
    assert len(demo) == 3
    # Highest 3 ids by descending order.
    ids = [item["id"] for item in demo]
    assert ids == sorted(ids, reverse=True)


# -- Truncation signal -------------------------------------------------


def test_paper_truncated_true_when_more_rows_exist(client, db_session) -> None:
    """Reviewer P1: silent truncation must surface a flag so the UI
    can warn the operator. ``paper_truncated`` must be True whenever
    the cap dropped at least one row."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=10)

    payload = client.get("/positions?paper_limit=3").json()

    assert payload["paper_truncated"] is True
    assert len(payload["paper_positions"]) == 3


def test_demo_truncated_true_when_more_rows_exist(client, db_session) -> None:
    market = _seed_market(db_session)
    _seed_demo_orders(db_session, market, count=10)

    payload = client.get("/positions?demo_limit=3").json()

    assert payload["demo_truncated"] is True
    assert len(payload["demo_orders"]) == 3


def test_truncated_false_when_under_cap(client, db_session) -> None:
    """A table that fits inside the cap must NOT report truncated;
    the UI shouldn't warn for nothing."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=5)
    _seed_demo_orders(db_session, market, count=5)

    payload = client.get("/positions?paper_limit=10&demo_limit=10").json()

    assert payload["paper_truncated"] is False
    assert payload["demo_truncated"] is False


def test_truncated_false_at_exact_boundary(client, db_session) -> None:
    """Exactly ``limit`` rows in the table is the off-by-one case the
    ``limit + 1`` fetch trick guards against. ``len(...) == limit``
    would false-positive here; the extra-row probe must report
    truncated=False because nothing was actually dropped."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=3)

    payload = client.get("/positions?paper_limit=3").json()

    assert payload["paper_truncated"] is False
    assert len(payload["paper_positions"]) == 3


def test_truncated_flags_are_independent(client, db_session) -> None:
    """One side overflowing the cap must not contaminate the other
    side's truncation flag."""
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=10)
    _seed_demo_orders(db_session, market, count=2)

    payload = client.get("/positions?paper_limit=3&demo_limit=10").json()

    assert payload["paper_truncated"] is True
    assert payload["demo_truncated"] is False


def test_default_response_does_not_set_truncated_when_empty(client) -> None:
    """The defensive ``False`` defaults on ``PositionsRead`` mean an
    empty install must still report ``False`` for both flags."""
    payload = client.get("/positions").json()

    assert payload["paper_truncated"] is False
    assert payload["demo_truncated"] is False
    assert payload["paper_positions"] == []
    assert payload["demo_orders"] == []


def test_exact_totals_include_older_open_position_past_list_cap(
    client,
    db_session,
) -> None:
    """A capped list must not cap the KPI inputs returned beside it."""

    market = _seed_market(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(
        PaperPosition(
            market_id=market.id,
            ticker=market.ticker,
            side="yes",
            quantity=1000,
            entry_price=0.50,
            opened_at=now - timedelta(days=30),
            status="open",
        )
    )
    for index in range(201):
        db_session.add(
            PaperPosition(
                market_id=market.id,
                ticker=f"{market.ticker}-{index}",
                side="yes",
                quantity=1,
                entry_price=0.50,
                exit_price=0.75,
                opened_at=now - timedelta(days=2) + timedelta(minutes=index),
                closed_at=now - timedelta(days=1),
                status="closed",
                pnl=1.0,
            )
        )
    db_session.commit()

    payload = client.get("/positions").json()

    assert payload["paper_truncated"] is True
    assert len(payload["paper_positions"]) == 200
    assert all(row["status"] == "closed" for row in payload["paper_positions"])
    assert payload["paper_totals"] == {
        "open_count": 1,
        "closed_count": 201,
        "open_exposure_dollars": 500.0,
        "realized_pnl_dollars": 201.0,
        "pending_parlay_count": 0,
        "settled_parlay_count": 0,
        "pending_parlay_exposure_dollars": 0.0,
        "parlay_realized_pnl_dollars": 0.0,
        "settled_7d_count": 201,
        "wins_7d_count": 201,
        "realized_pnl_7d_dollars": 201.0,
    }


def test_exact_totals_include_pending_and_settled_parlays(client, db_session) -> None:
    _seed_market(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            PaperParlay(
                stake=40.0,
                leg_count=2,
                sport_scope="NBA",
                participating_sports=["NBA"],
                combined_market_price=0.25,
                combined_model_probability=0.35,
                american_odds="+300",
                edge=0.10,
                outcome="pending",
                settlement_status="pending",
                created_at=now - timedelta(hours=2),
            ),
            PaperParlay(
                stake=10.0,
                leg_count=2,
                sport_scope="NBA",
                participating_sports=["NBA"],
                combined_market_price=0.25,
                combined_model_probability=0.35,
                american_odds="+300",
                edge=0.10,
                outcome="unresolved",
                settlement_status="pending",
                created_at=now - timedelta(hours=1),
            ),
            PaperParlay(
                stake=20.0,
                leg_count=2,
                sport_scope="NBA",
                participating_sports=["NBA"],
                combined_market_price=0.25,
                combined_model_probability=0.35,
                american_odds="+300",
                edge=0.10,
                outcome="won",
                settlement_status="settled",
                realized_pnl=30.0,
                settled_at=now - timedelta(days=1),
            ),
            PaperParlay(
                stake=10.0,
                leg_count=2,
                sport_scope="NBA",
                participating_sports=["NBA"],
                combined_market_price=0.25,
                combined_model_probability=0.35,
                american_odds="+300",
                edge=0.10,
                outcome="lost",
                settlement_status="settled",
                realized_pnl=-10.0,
                settled_at=now - timedelta(days=10),
            ),
        ]
    )
    db_session.commit()

    totals = client.get("/positions").json()["paper_totals"]

    assert totals["pending_parlay_count"] == 2
    assert totals["pending_parlay_exposure_dollars"] == 50.0
    assert totals["settled_parlay_count"] == 2
    assert totals["parlay_realized_pnl_dollars"] == 20.0
    assert totals["settled_7d_count"] == 1
    assert totals["wins_7d_count"] == 1
    assert totals["realized_pnl_7d_dollars"] == 30.0


def test_positions_export_streams_full_csv_past_response_cap(client, db_session) -> None:
    market = _seed_market(db_session)
    _seed_paper_positions(db_session, market, count=205)

    response = client.get("/positions/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 205
    assert {row["type"] for row in rows} == {"single"}
    assert {row["id"] for row in rows} == {
        str(position.id) for position in db_session.query(PaperPosition).all()
    }


def test_positions_export_stream_owns_its_database_session(
    client,
    db_session,
    monkeypatch,
) -> None:
    market = _seed_market(db_session, ticker="NBA-EXPORT-SESSION")
    _seed_paper_positions(db_session, market, count=1)

    def _request_session_must_not_stream(*_args, **_kwargs):
        raise RuntimeError("request-scoped session used during response streaming")

    # The route may use ``scalar`` before returning to resolve visibility, but
    # its delayed row iteration must happen through a generator-owned session.
    monkeypatch.setattr(db_session, "scalars", _request_session_must_not_stream)

    response = client.get("/positions/export")

    assert response.status_code == 200
    assert "NBA-EXPORT-SESSION" in response.text


def test_positions_export_openapi_declares_csv_response(client) -> None:
    operation = client.get("/openapi.json").json()["paths"]["/positions/export"]["get"]

    csv_schema = operation["responses"]["200"]["content"]["text/csv"]["schema"]
    assert csv_schema["type"] == "string"

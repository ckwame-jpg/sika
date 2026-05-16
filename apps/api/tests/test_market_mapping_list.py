"""Tests for Smarter #25 — operator review queue for fuzzy market
mappings.

The existing ``GET /ops/market-mapping/{ticker}`` returns the full
state for one market; this test pins the new list endpoint that
surfaces the worst-confidence matches for batch review.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import Event, Market


def _seed_market(
    db_session,
    *,
    ticker: str,
    sport_key: str = "NBA",
    confidence: float | None = None,
    candidates: list[dict] | None = None,
    overridden_at: datetime | None = None,
    overridden_reason: str | None = None,
    event_id: int | None = None,
) -> Market:
    market = Market(
        ticker=ticker,
        sport_key=sport_key,
        title=f"{ticker} title",
        status="open",
        raw_data={},
        mapping_confidence=confidence,
        mapping_candidates=candidates,
        mapping_overridden_at=overridden_at,
        mapping_overridden_reason=overridden_reason,
        event_id=event_id,
    )
    db_session.add(market)
    db_session.flush()
    return market


def _seed_event(db_session, *, name: str = "Event A", sport_key: str = "NBA") -> Event:
    event = Event(
        external_id=f"evt-{name}",
        sport_key=sport_key,
        name=name,
        starts_at=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        status="scheduled",
    )
    db_session.add(event)
    db_session.flush()
    return event


# -- Happy path -------------------------------------------------------


def test_list_returns_summary_rows(client, db_session) -> None:
    _seed_market(
        db_session, ticker="NBA-A",
        confidence=0.5,
        candidates=[{"event_id": 1, "event_name": "A vs B", "score": 0.5}],
    )
    db_session.commit()
    response = client.get("/ops/market-mapping")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["ticker"] == "NBA-A"
    assert payload[0]["mapping_confidence"] == 0.5
    assert payload[0]["candidate_count"] == 1
    assert payload[0]["top_candidate_event_id"] == 1
    assert payload[0]["top_candidate_score"] == 0.5


def test_list_orders_by_confidence_ascending(client, db_session) -> None:
    """Worst matches surface first — operator triage starts at the
    top of the list."""
    _seed_market(db_session, ticker="NBA-HIGH", confidence=0.9)
    _seed_market(db_session, ticker="NBA-LOW", confidence=0.3)
    _seed_market(db_session, ticker="NBA-MID", confidence=0.6)
    db_session.commit()
    response = client.get("/ops/market-mapping")
    tickers = [item["ticker"] for item in response.json()]
    assert tickers == ["NBA-LOW", "NBA-MID", "NBA-HIGH"]


def test_list_puts_null_confidence_last(client, db_session) -> None:
    """``mapping_confidence IS NULL`` rows (never auto-mapped) sort
    after the scored rows so they don't crowd the ambiguous-match
    review."""
    _seed_market(db_session, ticker="NBA-NULL", confidence=None)
    _seed_market(db_session, ticker="NBA-LOW", confidence=0.3)
    db_session.commit()
    response = client.get("/ops/market-mapping")
    tickers = [item["ticker"] for item in response.json()]
    assert tickers == ["NBA-LOW", "NBA-NULL"]


# -- Filters ----------------------------------------------------------


def test_max_confidence_filter(client, db_session) -> None:
    _seed_market(db_session, ticker="NBA-HIGH", confidence=0.9)
    _seed_market(db_session, ticker="NBA-LOW", confidence=0.3)
    db_session.commit()
    response = client.get("/ops/market-mapping?max_confidence=0.7")
    tickers = [item["ticker"] for item in response.json()]
    assert tickers == ["NBA-LOW"]


def test_excludes_overridden_by_default(client, db_session) -> None:
    """The default view is the unresolved queue — overridden rows
    are already resolved."""
    _seed_market(db_session, ticker="NBA-OVERRIDE", confidence=0.5,
                 overridden_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
                 overridden_reason="manual pin")
    _seed_market(db_session, ticker="NBA-PENDING", confidence=0.5)
    db_session.commit()
    response = client.get("/ops/market-mapping")
    tickers = [item["ticker"] for item in response.json()]
    assert tickers == ["NBA-PENDING"]


def test_include_overridden_flag(client, db_session) -> None:
    """``include_overridden=true`` is the audit view."""
    _seed_market(db_session, ticker="NBA-OVERRIDE", confidence=0.5,
                 overridden_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
                 overridden_reason="manual pin")
    _seed_market(db_session, ticker="NBA-PENDING", confidence=0.5)
    db_session.commit()
    response = client.get("/ops/market-mapping?include_overridden=true")
    tickers = [item["ticker"] for item in response.json()]
    assert set(tickers) == {"NBA-OVERRIDE", "NBA-PENDING"}


def test_sport_filter(client, db_session) -> None:
    _seed_market(db_session, ticker="NBA-A", sport_key="NBA", confidence=0.5)
    _seed_market(db_session, ticker="MLB-A", sport_key="MLB", confidence=0.5)
    db_session.commit()
    response = client.get("/ops/market-mapping?sport=NBA")
    tickers = [item["ticker"] for item in response.json()]
    assert tickers == ["NBA-A"]


def test_limit_clamped(client) -> None:
    response = client.get("/ops/market-mapping?limit=10000")
    assert response.status_code == 422


def test_limit_floor(client) -> None:
    response = client.get("/ops/market-mapping?limit=0")
    assert response.status_code == 422


# -- Linked event surfacing -------------------------------------------


def test_lists_linked_event_name_when_mapped(client, db_session) -> None:
    event = _seed_event(db_session, name="Lakers @ Celtics")
    _seed_market(
        db_session, ticker="NBA-LINKED",
        confidence=0.5, event_id=event.id,
    )
    db_session.commit()
    response = client.get("/ops/market-mapping")
    payload = response.json()[0]
    assert payload["event_id"] == event.id
    assert payload["event_name"] == "Lakers @ Celtics"


def test_top_candidate_uses_highest_score(client, db_session) -> None:
    """Among multiple candidates, the table summary surfaces the
    one with the highest score (which is what the auto-mapper
    actually picked)."""
    _seed_market(
        db_session, ticker="NBA-MULTI",
        confidence=0.6,
        candidates=[
            {"event_id": 1, "event_name": "A", "score": 0.4},
            {"event_id": 2, "event_name": "B", "score": 0.6},
            {"event_id": 3, "event_name": "C", "score": 0.5},
        ],
    )
    db_session.commit()
    payload = client.get("/ops/market-mapping").json()[0]
    assert payload["candidate_count"] == 3
    assert payload["top_candidate_event_id"] == 2
    assert payload["top_candidate_event_name"] == "B"
    assert payload["top_candidate_score"] == 0.6


def test_skips_malformed_candidate_rows(client, db_session) -> None:
    """A non-dict entry or one missing required keys is silently
    skipped — pre-bug-#17 rows may have malformed candidate
    payloads."""
    _seed_market(
        db_session, ticker="NBA-MIXED",
        confidence=0.5,
        candidates=[
            "not a dict",
            {"event_id": 1, "event_name": "OK", "score": 0.5},
            {"missing_event_id": True, "score": 0.7},
        ],
    )
    db_session.commit()
    payload = client.get("/ops/market-mapping").json()[0]
    # Only the well-formed entry counted.
    assert payload["candidate_count"] == 1
    assert payload["top_candidate_event_id"] == 1

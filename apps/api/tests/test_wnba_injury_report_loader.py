"""Smarter WNBA PR 7 — ESPN WNBA injury-report loader tests.

Mirrors ``test_nba_injury_report_loader.py`` for the WNBA path. The
shared ``parse_espn_injury_report`` is sport-agnostic and covered
exhaustively by the NBA tests; this file pins the WNBA-specific
behavior:

- ``load_wnba_injury_report`` cache-hit / cache-miss / stale-fallback /
  fresh-fetch / network-failure flows against the
  ``WnbaInjuryReportCache`` table.
- TTL policy (Smarter #29 helper) shortens when a WNBA tip-off is
  inside the final hour; ignores NBA / MLB tip-offs.
- ``EspnPublicClient.fetch_wnba_injury_report`` hits the WNBA URL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from sqlalchemy import select

from app.clients.espn import EspnPublicClient
from app.models import Event, WnbaInjuryReportCache
from app.services.wnba_injury_report import load_wnba_injury_report


_NOW = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)


def _make_event(
    db_session, *, sport_key: str = "WNBA", offset: timedelta, status: str = "scheduled"
) -> Event:
    event = Event(
        sport_key=sport_key,
        external_id=f"wnba-injury-evt-{id(offset)}",
        name="Test Event",
        starts_at=_NOW + offset,
        status=status,
    )
    db_session.add(event)
    db_session.flush()
    return event


class _StubEspnClient:
    """Captures fetch calls and returns canned responses."""

    def __init__(self, *, response: Any = None, raise_exc: Exception | None = None) -> None:
        self._response = response or {"injuries": []}
        self._raise = raise_exc
        self.calls = 0

    def fetch_wnba_injury_report(self) -> dict[str, Any]:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return self._response


# -- cache flows --------------------------------------------------------


def test_load_returns_empty_when_no_cache_and_network_disabled(db_session) -> None:
    payload = load_wnba_injury_report(db_session, allow_network=False, now=_NOW)
    assert payload == {"report_updated_at": None, "players": {}}


def test_load_returns_cached_payload_when_fresh(db_session) -> None:
    db_session.add(
        WnbaInjuryReportCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={
                "report_updated_at": _NOW.isoformat(),
                "players": {"Caitlin Clark": {"status": "Out", "designation": "Foot"}},
            },
            cached_at=_NOW - timedelta(minutes=5),
            expires_at=_NOW + timedelta(minutes=30),
        )
    )
    db_session.flush()

    stub = _StubEspnClient()
    payload = load_wnba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload["players"] == {"Caitlin Clark": {"status": "Out", "designation": "Foot"}}
    assert stub.calls == 0  # fresh cache hit → no network


def test_load_fetches_on_cache_miss_and_persists(db_session) -> None:
    stub = _StubEspnClient(
        response={
            "injuries": [
                {
                    "injuries": [
                        {
                            "athlete": {"fullName": "A'ja Wilson"},
                            "status": "Questionable",
                            "details": {"type": "Ankle"},
                        }
                    ]
                }
            ]
        }
    )
    payload = load_wnba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert stub.calls == 1
    assert "A'ja Wilson" in payload["players"]
    cached = db_session.scalar(
        select(WnbaInjuryReportCache).where(
            WnbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached is not None
    assert cached.fetched_date == _NOW.strftime("%Y-%m-%d")
    assert cached.cached_at.replace(tzinfo=timezone.utc) == _NOW
    # Default TTL of 60min when no upcoming WNBA tip is near.
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=60)


def test_load_falls_back_to_stale_cache_on_network_failure(db_session) -> None:
    db_session.add(
        WnbaInjuryReportCache(
            fetched_date=_NOW.strftime("%Y-%m-%d"),
            payload={
                "report_updated_at": (_NOW - timedelta(hours=3)).isoformat(),
                "players": {"Stale Player": {"status": "Out", "designation": ""}},
            },
            cached_at=_NOW - timedelta(hours=3),
            expires_at=_NOW - timedelta(minutes=10),  # expired
        )
    )
    db_session.flush()

    stub = _StubEspnClient(
        raise_exc=httpx.HTTPStatusError(
            "boom", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(503),
        )
    )
    payload = load_wnba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert stub.calls == 1
    assert "Stale Player" in payload["players"]


def test_load_returns_empty_on_network_failure_with_no_cache(db_session) -> None:
    stub = _StubEspnClient(raise_exc=httpx.ConnectError("dns fail"))
    payload = load_wnba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert stub.calls == 1
    assert payload == {"report_updated_at": None, "players": {}}


def test_load_returns_empty_when_fetch_returns_non_dict(db_session) -> None:
    stub = _StubEspnClient(response=["not", "a", "dict"])
    payload = load_wnba_injury_report(
        db_session, client=stub, allow_network=True, now=_NOW,
    )
    assert payload == {"report_updated_at": None, "players": {}}
    rows = db_session.execute(select(WnbaInjuryReportCache)).all()
    assert rows == []


# -- TTL policy integration (Smarter #29) -----------------------------


def test_load_uses_near_tip_ttl_when_wnba_game_within_one_hour(db_session) -> None:
    # WNBA event tips in 45 min → near-tip TTL of 15 min applies.
    _make_event(db_session, sport_key="WNBA", offset=timedelta(minutes=45))

    stub = _StubEspnClient(response={"injuries": []})
    load_wnba_injury_report(db_session, client=stub, allow_network=True, now=_NOW)

    cached = db_session.scalar(
        select(WnbaInjuryReportCache).where(
            WnbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached is not None
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=15)


def test_load_ignores_nba_events_for_wnba_ttl(db_session) -> None:
    # Codex Pattern 9 — an NBA tip in 30 min must NOT collapse the
    # WNBA cache TTL. Cross-sport tip-offs are independent.
    _make_event(db_session, sport_key="NBA", offset=timedelta(minutes=30))

    stub = _StubEspnClient(response={"injuries": []})
    load_wnba_injury_report(db_session, client=stub, allow_network=True, now=_NOW)

    cached = db_session.scalar(
        select(WnbaInjuryReportCache).where(
            WnbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    # Default 60min TTL, not 15min.
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=60)


def test_load_ignores_completed_wnba_events_for_ttl(db_session) -> None:
    _make_event(
        db_session, sport_key="WNBA", offset=timedelta(minutes=30), status="completed"
    )

    stub = _StubEspnClient(response={"injuries": []})
    load_wnba_injury_report(db_session, client=stub, allow_network=True, now=_NOW)

    cached = db_session.scalar(
        select(WnbaInjuryReportCache).where(
            WnbaInjuryReportCache.fetched_date == _NOW.strftime("%Y-%m-%d")
        )
    )
    assert cached.expires_at.replace(tzinfo=timezone.utc) == _NOW + timedelta(minutes=60)


# -- EspnPublicClient.fetch_wnba_injury_report ------------------------


class _StubHttpClient:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = payload or {"injuries": []}
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, "kwargs": kwargs})
        return httpx.Response(
            status_code=self.status_code,
            json=self.payload,
            request=httpx.Request("GET", url),
        )


def test_espn_client_fetch_wnba_injury_report_calls_correct_url() -> None:
    stub = _StubHttpClient(payload={"injuries": [{"team": {}, "injuries": []}]})
    client = EspnPublicClient(http_client=stub)
    payload = client.fetch_wnba_injury_report()
    assert len(stub.calls) == 1
    assert stub.calls[0]["url"].endswith("/basketball/wnba/injuries")
    assert payload == {"injuries": [{"team": {}, "injuries": []}]}


def test_espn_client_fetch_wnba_injury_report_raises_on_http_error() -> None:
    stub = _StubHttpClient(status_code=503)
    client = EspnPublicClient(http_client=stub)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_wnba_injury_report()


def test_espn_client_fetch_nba_injury_report_still_uses_nba_url() -> None:
    # The NBA fetcher is preserved as a thin wrapper around the
    # generalized ``fetch_injury_report``; the URL slug must NOT have
    # shifted as a side effect of the WNBA generalization.
    stub = _StubHttpClient(payload={"injuries": []})
    client = EspnPublicClient(http_client=stub)
    client.fetch_nba_injury_report()
    assert stub.calls[0]["url"].endswith("/basketball/nba/injuries")

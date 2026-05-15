"""Tests for Smarter #18 phase 2 part (a) — Odds API cache layer.

Covers:
- ``cached_h2h_odds`` returns the cached payload on hit; fetches on
  miss / expiry; falls back to stale cache on network failure;
  short-circuits when ``allow_network=False`` AND no cache;
  returns ``[]`` for unmapped sport keys or missing API key.
- TTL is sourced from ``settings.the_odds_api_cache_ttl_minutes`` by
  default but accepts the per-call ``ttl_minutes=`` override.
- Upstream-health board records success on fresh fetch + failure on
  exception.
- The cache upserts in place on the unique ``OperatorSetting.key``
  rather than appending a new row each time.
- ``invalidate_cached_h2h_odds`` drops the row so the next call
  forces a fresh fetch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.clients.the_odds_api import MissingApiKeyError, TheOddsApiClient
from app.config import get_settings
from app.models import OperatorSetting
from app.services.odds_api_cache import (
    _cache_key,
    cached_h2h_odds,
    invalidate_cached_h2h_odds,
)
from app.services.upstream_health import get_upstream_health


_NOW = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)


def _sample_event(home: str = "Boston Celtics", away: str = "Brooklyn Nets") -> dict[str, Any]:
    return {
        "id": f"{home.lower().replace(' ', '-')}-vs-{away.lower().replace(' ', '-')}",
        "sport_key": "basketball_nba",
        "commence_time": _NOW.isoformat(),
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": _NOW.isoformat(),
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": 1.4},
                            {"name": away, "price": 3.0},
                        ],
                    }
                ],
            }
        ],
    }


class _StubOddsApiClient:
    """Captures fetch calls and returns canned responses."""

    def __init__(
        self,
        *,
        response: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._response: list[dict[str, Any]] = response if response is not None else []
        self._raise = raise_exc
        self.calls: list[str] = []

    def fetch_h2h_odds(self, sika_sport_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(sika_sport_key)
        if self._raise is not None:
            raise self._raise
        return self._response


# -- short-circuit paths ----------------------------------------------


def test_cached_returns_empty_for_unmapped_sport(db_session) -> None:
    stub = _StubOddsApiClient(response=[_sample_event()])
    out = cached_h2h_odds(
        db_session,
        "CRICKET",  # not in the sport-key map
        client=stub,
        allow_network=True,
        now=_NOW,
    )
    assert out == []
    assert stub.calls == []  # never made the network call


def test_cached_returns_empty_when_api_key_missing(db_session, monkeypatch) -> None:
    monkeypatch.setenv("THE_ODDS_API_KEY", "")
    get_settings.cache_clear()
    stub = _StubOddsApiClient(raise_exc=MissingApiKeyError("not configured"))
    out = cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW,
    )
    assert out == []
    # Missing-key is a config choice, not a fault — health should NOT
    # have been recorded as a failure.
    odds_health = next(
        (row for row in get_upstream_health(db_session) if row.source == "the_odds_api"),
        None,
    )
    assert odds_health is not None
    assert odds_health.last_failure_at is None
    get_settings.cache_clear()


def test_cached_returns_empty_when_network_disabled_and_no_cache(db_session) -> None:
    out = cached_h2h_odds(db_session, "NBA", allow_network=False, now=_NOW)
    assert out == []


# -- happy path: fetch + persist + reuse ------------------------------


def test_cached_fetches_on_miss_and_persists(db_session) -> None:
    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    events = cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW,
    )
    assert events == sample
    assert stub.calls == ["NBA"]
    cached_row = db_session.scalar(
        select(OperatorSetting).where(OperatorSetting.key == _cache_key("NBA"))
    )
    assert cached_row is not None
    assert cached_row.value["event_count"] == 1
    assert cached_row.value["events"] == sample


def test_cached_returns_payload_on_hit_without_fetching(db_session) -> None:
    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    # First call writes the cache.
    cached_h2h_odds(db_session, "NBA", client=stub, allow_network=True, now=_NOW)
    assert stub.calls == ["NBA"]
    # Second call within TTL → cache hit, no extra fetch.
    second = cached_h2h_odds(
        db_session,
        "NBA",
        client=stub,
        allow_network=True,
        now=_NOW + timedelta(minutes=5),
    )
    assert second == sample
    assert stub.calls == ["NBA"]  # still one call


def test_cached_refetches_after_ttl_expires(db_session) -> None:
    initial = [_sample_event()]
    refreshed = [_sample_event(home="Lakers", away="Celtics")]
    stub = _StubOddsApiClient(response=initial)
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=5,
    )
    stub._response = refreshed  # type: ignore[attr-defined]
    # Past the 5-minute TTL → fetch again.
    events = cached_h2h_odds(
        db_session,
        "NBA",
        client=stub,
        allow_network=True,
        now=_NOW + timedelta(minutes=6),
        ttl_minutes=5,
    )
    assert events == refreshed
    assert stub.calls == ["NBA", "NBA"]


def test_cached_upserts_in_place_not_appends(db_session) -> None:
    stub = _StubOddsApiClient(response=[_sample_event()])
    cached_h2h_odds(db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=1)
    # Past TTL → second fetch overwrites the row.
    cached_h2h_odds(
        db_session,
        "NBA",
        client=stub,
        allow_network=True,
        now=_NOW + timedelta(minutes=5),
        ttl_minutes=1,
    )
    rows = db_session.execute(
        select(OperatorSetting).where(OperatorSetting.key == _cache_key("NBA"))
    ).all()
    assert len(rows) == 1


# -- stale-fallback path ----------------------------------------------


def test_cached_serves_stale_on_network_failure(db_session) -> None:
    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=10,
    )
    # Past TTL but WITHIN the 2*ttl stale ceiling (15 min after fetch
    # with 10-min TTL → 5 min past expiry, ceiling at +20 min from
    # fetch). Fetch raises → fall back to the stale payload.
    failing = _StubOddsApiClient(raise_exc=RuntimeError("HTTP 503"))
    events = cached_h2h_odds(
        db_session,
        "NBA",
        client=failing,
        allow_network=True,
        now=_NOW + timedelta(minutes=15),
        ttl_minutes=10,
    )
    assert events == sample
    # Failure was recorded on the upstream-health board.
    odds_health = next(
        row for row in get_upstream_health(db_session) if row.source == "the_odds_api"
    )
    assert odds_health.last_failure_at is not None
    assert "HTTP 503" in (odds_health.last_error or "")


def test_cached_serves_stale_when_network_disabled_after_ttl(db_session) -> None:
    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=10,
    )
    # Past TTL but within 2*ttl ceiling, allow_network=False → serve
    # stale rather than empty (a brief network-disabled window
    # shouldn't blank out the prior).
    events = cached_h2h_odds(
        db_session,
        "NBA",
        allow_network=False,
        now=_NOW + timedelta(minutes=15),
        ttl_minutes=10,
    )
    assert events == sample


def test_cached_returns_empty_when_fetch_returns_non_list(db_session) -> None:
    # The Odds API has been known to return a JSON object error rather
    # than the documented list shape on rate-limit. Don't blow up the
    # consumer path — serve empty and let the caller skip the prior.
    stub = _StubOddsApiClient(response={"message": "rate limited"})  # type: ignore[arg-type]
    out = cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW,
    )
    assert out == []


# -- upstream-health wiring -------------------------------------------


def test_cached_records_health_success_on_fresh_fetch(db_session) -> None:
    stub = _StubOddsApiClient(response=[_sample_event()])
    cached_h2h_odds(db_session, "NBA", client=stub, allow_network=True, now=_NOW)
    odds_health = next(
        row for row in get_upstream_health(db_session) if row.source == "the_odds_api"
    )
    assert odds_health.last_success_at is not None


# -- invalidate -------------------------------------------------------


def test_invalidate_drops_cached_row(db_session) -> None:
    stub = _StubOddsApiClient(response=[_sample_event()])
    cached_h2h_odds(db_session, "NBA", client=stub, allow_network=True, now=_NOW)
    assert db_session.scalar(
        select(OperatorSetting).where(OperatorSetting.key == _cache_key("NBA"))
    ) is not None

    invalidate_cached_h2h_odds(db_session, "NBA")
    assert db_session.scalar(
        select(OperatorSetting).where(OperatorSetting.key == _cache_key("NBA"))
    ) is None


def test_invalidate_is_idempotent_when_row_missing(db_session) -> None:
    # Should not raise when there's nothing to delete.
    invalidate_cached_h2h_odds(db_session, "NBA")


# -- ttl override -----------------------------------------------------


def test_cached_respects_per_call_ttl_override(db_session) -> None:
    stub = _StubOddsApiClient(response=[_sample_event()])
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=90,
    )
    cached_row = db_session.scalar(
        select(OperatorSetting).where(OperatorSetting.key == _cache_key("NBA"))
    )
    # Expires_at should reflect the 90-minute override, not the default.
    expires_at = datetime.fromisoformat(cached_row.value["expires_at"].replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    assert expires_at == _NOW + timedelta(minutes=90)


# -- canonical UPSTREAM_SOURCES inclusion ------------------------------


def test_the_odds_api_in_canonical_sources_tuple() -> None:
    # Pin membership so the operator surface always shows the row,
    # even before the first fetch lands.
    from app.services.upstream_health import UPSTREAM_SOURCES
    assert "the_odds_api" in UPSTREAM_SOURCES


# -- Reviewer MEDIUM follow-up: staleness ceiling ---------------------


def test_cached_returns_empty_when_stale_exceeds_max_ceiling_on_network_failure(
    db_session,
) -> None:
    # Seed a cache row whose expires_at is well past the 2*ttl ceiling.
    # Stale-fallback should return [] (the "skip prior" signal),
    # NOT the day-old payload — preventing the future suppression rule
    # from acting on stale odds.
    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=30,
    )
    # 3 hours later — way past the 2*30min = 1h ceiling.
    later = _NOW + timedelta(hours=3)
    failing = _StubOddsApiClient(raise_exc=RuntimeError("HTTP 503"))
    events = cached_h2h_odds(
        db_session, "NBA", client=failing, allow_network=True, now=later, ttl_minutes=30,
    )
    assert events == []


def test_cached_returns_empty_when_stale_exceeds_max_ceiling_and_network_disabled(
    db_session,
) -> None:
    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=30,
    )
    # 90 min past TTL (= cached at NOW, expires at NOW+30, ceiling at
    # NOW+30+60 = NOW+90; query at NOW+91 → above ceiling). Network
    # disabled. Stale ceiling fires → return [].
    later = _NOW + timedelta(minutes=91)
    events = cached_h2h_odds(
        db_session, "NBA", allow_network=False, now=later, ttl_minutes=30,
    )
    assert events == []


def test_cached_serves_stale_when_within_ceiling(db_session) -> None:
    # Sanity check the BOUNDARY of the new ceiling: a payload 45 min
    # past expiry (= 75 min after fetch, with 30-min TTL → ceiling at
    # 90 min) should STILL be served on network failure.
    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=30,
    )
    later = _NOW + timedelta(minutes=75)  # expires at +30, ceiling at +90
    failing = _StubOddsApiClient(raise_exc=RuntimeError("brief blip"))
    events = cached_h2h_odds(
        db_session, "NBA", client=failing, allow_network=True, now=later, ttl_minutes=30,
    )
    assert events == sample


def test_cached_returns_empty_when_missing_key_and_stale_beyond_ceiling(
    db_session, monkeypatch,
) -> None:
    # Key revoked mid-run: cache is stale beyond the ceiling → caller
    # gets [] rather than zombie-fresh data after a 6-hour outage.
    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    get_settings.cache_clear()

    sample = [_sample_event()]
    stub = _StubOddsApiClient(response=sample)
    cached_h2h_odds(
        db_session, "NBA", client=stub, allow_network=True, now=_NOW, ttl_minutes=30,
    )

    monkeypatch.setenv("THE_ODDS_API_KEY", "")
    get_settings.cache_clear()
    # 6 hours later — well past the 1h ceiling.
    later = _NOW + timedelta(hours=6)
    # Stub raises MissingApiKeyError to mimic the revocation path.
    revoked = _StubOddsApiClient(raise_exc=MissingApiKeyError("key revoked"))
    events = cached_h2h_odds(
        db_session, "NBA", client=revoked, allow_network=True, now=later, ttl_minutes=30,
    )
    assert events == []
    get_settings.cache_clear()

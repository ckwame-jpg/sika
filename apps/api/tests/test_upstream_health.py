"""Tests for Smarter #23 — per-upstream-source freshness tracking.

Covers:
- The helper module's recording API + read function.
- The NBA Stats success/failure recorders wiring upstream_health alongside
  the existing circuit-breaker counters.
- The /health endpoint surfacing the upstream_sources list.
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.services.advanced_stats import _record_nba_failure, _record_nba_success
from app.services.upstream_health import (
    DEFAULT_STALE_AFTER,
    UPSTREAM_SOURCES,
    UpstreamSourceHealth,
    get_upstream_health,
    record_upstream_failure,
    record_upstream_success,
)


# -- helper module API ---------------------------------------------------


def test_record_upstream_success_creates_row_with_last_success_at(db_session) -> None:
    moment = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    record_upstream_success(db_session, "espn_scoreboard", now=moment)
    health = next(row for row in get_upstream_health(db_session) if row.source == "espn_scoreboard")
    assert health.last_success_at == moment
    assert health.last_failure_at is None
    assert health.last_error is None


def test_record_upstream_failure_creates_row_with_error(db_session) -> None:
    moment = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    record_upstream_failure(db_session, "kalshi_markets", "HTTP 429 Too Many Requests", now=moment)
    health = next(row for row in get_upstream_health(db_session) if row.source == "kalshi_markets")
    assert health.last_failure_at == moment
    assert health.last_error == "HTTP 429 Too Many Requests"
    assert health.last_success_at is None


def test_success_after_failure_preserves_failure_timestamp_but_clears_error(db_session) -> None:
    fail_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    record_upstream_failure(db_session, "nba_stats", "boom", now=fail_at)
    success_at = fail_at + timedelta(minutes=30)
    record_upstream_success(db_session, "nba_stats", now=success_at)
    health = next(row for row in get_upstream_health(db_session) if row.source == "nba_stats")
    assert health.last_success_at == success_at
    # Failure timestamp is preserved so operators see the timeline.
    assert health.last_failure_at == fail_at
    # But the error message clears so a stale message doesn't linger.
    assert health.last_error is None


def test_failure_after_success_preserves_success_timestamp(db_session) -> None:
    success_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    record_upstream_success(db_session, "mlb_stats", now=success_at)
    fail_at = success_at + timedelta(minutes=15)
    record_upstream_failure(db_session, "mlb_stats", "connection reset", now=fail_at)
    health = next(row for row in get_upstream_health(db_session) if row.source == "mlb_stats")
    assert health.last_success_at == success_at
    assert health.last_failure_at == fail_at
    assert health.last_error == "connection reset"


def test_get_upstream_health_returns_all_canonical_sources(db_session) -> None:
    rows = get_upstream_health(db_session)
    assert {row.source for row in rows} == set(UPSTREAM_SOURCES)
    # Stable order matches the canonical tuple.
    assert [row.source for row in rows] == list(UPSTREAM_SOURCES)


def test_unrecorded_source_returns_none_filled_row(db_session) -> None:
    # No record_* calls — every source should still appear with None fields.
    rows = get_upstream_health(db_session)
    for row in rows:
        assert row.last_success_at is None
        assert row.last_failure_at is None
        assert row.last_error is None


def test_is_stale_when_never_recorded() -> None:
    # A source that has never reported in is stale by definition — the
    # operator surface should show this as the explicit signal.
    health = UpstreamSourceHealth(
        source="espn_scoreboard",
        last_success_at=None,
        last_failure_at=None,
        last_error=None,
    )
    assert health.is_stale() is True


def test_is_stale_when_success_outside_window() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    long_ago = now - DEFAULT_STALE_AFTER - timedelta(minutes=1)
    health = UpstreamSourceHealth(
        source="nba_stats",
        last_success_at=long_ago,
        last_failure_at=None,
        last_error=None,
    )
    assert health.is_stale(now=now) is True


def test_is_fresh_when_success_inside_window() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    recent = now - timedelta(hours=1)
    health = UpstreamSourceHealth(
        source="nba_stats",
        last_success_at=recent,
        last_failure_at=None,
        last_error=None,
    )
    assert health.is_stale(now=now) is False


def test_is_stale_respects_custom_threshold() -> None:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=30)
    health = UpstreamSourceHealth(
        source="nba_stats",
        last_success_at=recent,
        last_failure_at=None,
        last_error=None,
    )
    # Custom 15-minute window — 30 min ago is stale.
    assert health.is_stale(now=now, stale_after=timedelta(minutes=15)) is True
    # Custom 1-hour window — 30 min ago is fresh.
    assert health.is_stale(now=now, stale_after=timedelta(hours=1)) is False


def test_is_stale_coerces_naive_datetimes_to_utc() -> None:
    # SQLite drops tz info on read — last_success_at can be naive in
    # test fixtures even though production Postgres preserves it.
    now = datetime(2026, 5, 14, 12, 0)  # naive
    recent = datetime(2026, 5, 14, 11, 30)  # naive
    health = UpstreamSourceHealth(
        source="nba_stats",
        last_success_at=recent,
        last_failure_at=None,
        last_error=None,
    )
    assert health.is_stale(now=now) is False


def test_get_upstream_health_filters_to_requested_subset(db_session) -> None:
    record_upstream_success(db_session, "nba_stats")
    record_upstream_success(db_session, "espn_scoreboard")
    rows = get_upstream_health(db_session, sources=("nba_stats",))
    assert [row.source for row in rows] == ["nba_stats"]


# -- NBA Stats wiring ----------------------------------------------------


def test_record_nba_success_updates_upstream_health(db_session) -> None:
    _record_nba_success(db_session)
    health = next(row for row in get_upstream_health(db_session) if row.source == "nba_stats")
    assert health.last_success_at is not None
    assert health.last_error is None


def test_record_nba_failure_updates_upstream_health_with_error(db_session) -> None:
    _record_nba_failure(db_session, error="HTTP 503 Service Unavailable")
    health = next(row for row in get_upstream_health(db_session) if row.source == "nba_stats")
    assert health.last_failure_at is not None
    assert health.last_error == "HTTP 503 Service Unavailable"


def test_record_nba_failure_without_error_uses_fallback_message(db_session) -> None:
    # Legacy call sites that didn't yet pass ``error=`` should still
    # record SOMETHING on the operator surface rather than ``None`` —
    # ``None`` would erase a prior error message.
    _record_nba_failure(db_session)
    health = next(row for row in get_upstream_health(db_session) if row.source == "nba_stats")
    assert health.last_error == "unknown error"


# -- /health surface -----------------------------------------------------


def test_health_endpoint_surfaces_upstream_sources(client: TestClient, db_session) -> None:
    moment = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    record_upstream_success(db_session, "nba_stats", now=moment)
    record_upstream_failure(db_session, "espn_scoreboard", "HTTP 500", now=moment)
    db_session.commit()

    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert "upstream_sources" in payload
    by_source = {row["source"]: row for row in payload["upstream_sources"]}
    assert set(by_source.keys()) == set(UPSTREAM_SOURCES)
    nba = by_source["nba_stats"]
    assert nba["last_success_at"] is not None
    assert nba["last_error"] is None
    espn = by_source["espn_scoreboard"]
    assert espn["last_failure_at"] is not None
    assert espn["last_error"] == "HTTP 500"


def test_health_endpoint_marks_never_recorded_sources_as_stale(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    by_source = {row["source"]: row for row in payload["upstream_sources"]}
    # No record_* calls in this test → every source is stale-by-default.
    for source in UPSTREAM_SOURCES:
        assert by_source[source]["is_stale"] is True
        assert by_source[source]["last_success_at"] is None

"""Tests for the NBA injury-report TTL policy helper (Smarter #29).

The helper is consulted at cache WRITE time. READ paths trust the persisted
``expires_at`` — this test file therefore only exercises the pure-function
helper, not a loader (no NBA injury-report loader exists yet; the helper
ships first as a prereq for Smarter #11).
"""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.services import nba_long_tail
from app.services.nba_long_tail import (
    _NBA_INJURY_TTL_NEAR_TIP_MINUTES,
    _NBA_INJURY_TTL_NEAR_TIP_WINDOW_SECONDS,
    _effective_injury_report_ttl_minutes,
)


def _now_utc() -> datetime:
    # Fixed instant so we can offset relative to a known anchor.
    return datetime(2026, 5, 14, 19, 0, tzinfo=timezone.utc)


def test_returns_default_ttl_when_event_start_missing():
    default = int(get_settings().nba_injury_report_cache_minutes)
    assert (
        _effective_injury_report_ttl_minutes(now=_now_utc(), event_start=None)
        == default
    )


def test_returns_default_ttl_when_now_missing():
    default = int(get_settings().nba_injury_report_cache_minutes)
    assert (
        _effective_injury_report_ttl_minutes(now=None, event_start=_now_utc())
        == default
    )


def test_tightens_to_near_tip_during_final_hour():
    # 30 minutes pre-tip → near-tip TTL applies.
    now = _now_utc()
    event_start = now + timedelta(minutes=30)
    assert (
        _effective_injury_report_ttl_minutes(now=now, event_start=event_start)
        == _NBA_INJURY_TTL_NEAR_TIP_MINUTES
    )


def test_uses_default_outside_near_tip_window():
    # 2 hours pre-tip → coarse default applies.
    now = _now_utc()
    event_start = now + timedelta(hours=2)
    assert _effective_injury_report_ttl_minutes(
        now=now, event_start=event_start
    ) == int(get_settings().nba_injury_report_cache_minutes)


def test_uses_default_after_tip_off():
    # Negative seconds_until_tip — game already started. Don't keep tightening.
    now = _now_utc()
    event_start = now - timedelta(minutes=30)
    assert _effective_injury_report_ttl_minutes(
        now=now, event_start=event_start
    ) == int(get_settings().nba_injury_report_cache_minutes)


def test_boundary_inclusive_at_one_hour_exactly():
    # Exactly 3600s out → still inside the near-tip window.
    now = _now_utc()
    event_start = now + timedelta(
        seconds=_NBA_INJURY_TTL_NEAR_TIP_WINDOW_SECONDS
    )
    assert (
        _effective_injury_report_ttl_minutes(now=now, event_start=event_start)
        == _NBA_INJURY_TTL_NEAR_TIP_MINUTES
    )


def test_boundary_inclusive_at_tip_off_exactly():
    # 0 seconds out → tip-off this very moment, still near-tip.
    now = _now_utc()
    assert (
        _effective_injury_report_ttl_minutes(now=now, event_start=now)
        == _NBA_INJURY_TTL_NEAR_TIP_MINUTES
    )


def test_one_second_outside_window_uses_default():
    # 3601s out → just outside the near-tip window.
    now = _now_utc()
    event_start = now + timedelta(
        seconds=_NBA_INJURY_TTL_NEAR_TIP_WINDOW_SECONDS + 1
    )
    assert _effective_injury_report_ttl_minutes(
        now=now, event_start=event_start
    ) == int(get_settings().nba_injury_report_cache_minutes)


def test_naive_event_start_coerced_to_utc():
    # Event.starts_at may be naive — handoff trap #5. Coerce defensively.
    now = _now_utc()
    naive_event_start = (now + timedelta(minutes=30)).replace(tzinfo=None)
    assert (
        _effective_injury_report_ttl_minutes(
            now=now, event_start=naive_event_start
        )
        == _NBA_INJURY_TTL_NEAR_TIP_MINUTES
    )


def test_naive_now_coerced_to_utc():
    now_aware = _now_utc()
    naive_now = now_aware.replace(tzinfo=None)
    event_start = now_aware + timedelta(minutes=30)
    assert (
        _effective_injury_report_ttl_minutes(
            now=naive_now, event_start=event_start
        )
        == _NBA_INJURY_TTL_NEAR_TIP_MINUTES
    )


def test_non_utc_aware_inputs_normalize_through_coerce():
    # A caller might hand in an event_start expressed in a different TZ.
    # The helper should normalize before subtracting.
    eastern = timezone(timedelta(hours=-5))
    now_eastern = datetime(2026, 5, 14, 14, 0, tzinfo=eastern)  # 19:00 UTC
    event_eastern = datetime(2026, 5, 14, 14, 30, tzinfo=eastern)  # 19:30 UTC
    assert (
        _effective_injury_report_ttl_minutes(
            now=now_eastern, event_start=event_eastern
        )
        == _NBA_INJURY_TTL_NEAR_TIP_MINUTES
    )


def test_default_reflects_runtime_settings(monkeypatch):
    # If an operator tunes the coarse setting, the helper picks it up.
    # (We can't monkeypatch the settings dataclass cleanly — patch the
    # accessor instead.)
    class _StubSettings:
        nba_injury_report_cache_minutes = 90

    monkeypatch.setattr(nba_long_tail, "get_settings", lambda: _StubSettings())
    now = _now_utc()
    event_start = now + timedelta(hours=3)
    assert (
        _effective_injury_report_ttl_minutes(now=now, event_start=event_start)
        == 90
    )


def test_near_tip_value_is_strictly_below_default():
    # Drift guard: the near-tip TTL must be smaller than the default,
    # else the whole policy is a no-op. If a future operator drops the
    # default below 15, this test will flag the contradiction explicitly.
    default = int(get_settings().nba_injury_report_cache_minutes)
    assert _NBA_INJURY_TTL_NEAR_TIP_MINUTES < default, (
        "Near-tip TTL must be strictly smaller than the coarse default "
        "or the Smarter #29 policy has no effect."
    )

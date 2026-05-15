"""Tests for Smarter #13 phase 2b — NBA referee tendency cache + loader.

Phase 2a (PR #101 + PR #103) shipped the daily ASSIGNMENTS cache —
which crew works which game. This phase ships the per-referee
TENDENCY cache: how many fouls a given crew chief tends to call,
their free-throw-rate impact, etc. Phase 2c (deferred) joins the
two via a feature emitter; phase 2d (deferred) wires the heuristic
factor onto points / fouls / FT props.

The actual fetch from basketball-reference.com is deferred to phase
2b-2: this module accepts a ``fetcher`` callable so tests inject a
deterministic stub and production wires the real scraper once the BR
URL + table layout have been validated against a manual fetch.

Tested behaviors:
- Parser turns BR-shaped raw rows into the consumer-facing
  ``{season, fetched_at, referees: {name: {...}}}`` shape.
- Parser is defensive against missing / non-numeric fields, ignores
  rows with zero games (a referee who didn't officiate this season).
- Loader follows the same cache-or-fetch shape as PR #100's
  ``cached_h2h_odds`` (OperatorSetting JSON blob, per-season key).
- Stale-fallback ceiling at ``2 * ttl`` so a multi-day BR outage
  doesn't serve days-old data forever.
- ``invalidate`` removes the cache row outright.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from app.models import OperatorSetting
from app.services.nba_referee_tendencies import (
    DEFAULT_TENDENCY_CACHE_MINUTES,
    NbaRefereeTendency,
    invalidate_nba_referee_tendencies,
    load_nba_referee_tendencies,
    parse_referee_tendency_rows,
    tendency_cache_key,
)


_SEASON = 2026
_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _br_rows(*, n: int = 3) -> list[dict[str, Any]]:
    """Return BR-shaped raw rows. Mirrors basketball-reference's
    ``Officials Per Game`` style table — column names are best guesses
    based on BR's standard conventions; the parser tolerates extras
    and skips missing keys defensively, so the contract is robust to
    minor column-name drift between scrape attempts."""
    return [
        {
            "Referee": "Scott Foster",
            "G": "60",
            "PF/G": "42.1",
            "FT/G": "44.5",
            "T": "12",
        },
        {
            "Referee": "Tony Brothers",
            "G": "55",
            "PF/G": "39.8",
            "FT/G": "42.0",
            "T": "8",
        },
        {
            "Referee": "James Capers",
            "G": "47",
            "PF/G": "40.5",
            "FT/G": "43.2",
            "T": "5",
        },
    ][:n]


# -- Parser ------------------------------------------------------------


def test_parser_returns_consumer_shape_with_per_referee_tendencies() -> None:
    payload = parse_referee_tendency_rows(_br_rows(), season=_SEASON, fetched_at=_NOW)

    assert payload["season"] == _SEASON
    assert payload["fetched_at"] == _NOW.isoformat()
    assert set(payload["referees"].keys()) == {"Scott Foster", "Tony Brothers", "James Capers"}
    foster = payload["referees"]["Scott Foster"]
    assert foster["games_officiated"] == 60
    assert foster["fouls_per_game"] == 42.1
    assert foster["fta_per_game"] == 44.5
    assert foster["technicals"] == 12


def test_parser_drops_rows_with_zero_games() -> None:
    """A referee who didn't officiate this season has G=0; the row
    is informational at best and the per-game ratios are undefined.
    Drop them so consumers don't accidentally read divide-by-zero
    placeholder values."""
    rows = _br_rows(n=2) + [
        {"Referee": "Inactive Ref", "G": "0", "PF/G": "0.0", "FT/G": "0.0", "T": "0"},
    ]
    payload = parse_referee_tendency_rows(rows, season=_SEASON, fetched_at=_NOW)

    assert "Inactive Ref" not in payload["referees"]
    assert len(payload["referees"]) == 2


def test_parser_skips_rows_missing_referee_name() -> None:
    rows = _br_rows(n=1) + [{"G": "30", "PF/G": "40.0", "FT/G": "42.0"}]
    payload = parse_referee_tendency_rows(rows, season=_SEASON, fetched_at=_NOW)
    assert len(payload["referees"]) == 1


def test_parser_tolerates_non_numeric_fields() -> None:
    """BR sometimes serves dashes (``--``) or empty strings for
    missing values. Coerce safely to None and keep the rest of the
    row intact rather than dropping the whole entry."""
    rows = [
        {"Referee": "Spotty Ref", "G": "20", "PF/G": "--", "FT/G": "", "T": "n/a"},
    ]
    payload = parse_referee_tendency_rows(rows, season=_SEASON, fetched_at=_NOW)
    spotty = payload["referees"]["Spotty Ref"]
    assert spotty["games_officiated"] == 20
    assert spotty["fouls_per_game"] is None
    assert spotty["fta_per_game"] is None
    assert spotty["technicals"] is None


def test_parser_returns_empty_referees_for_empty_input() -> None:
    payload = parse_referee_tendency_rows([], season=_SEASON, fetched_at=_NOW)
    assert payload["referees"] == {}
    assert payload["season"] == _SEASON


def test_parser_skips_non_dict_rows(caplog) -> None:
    """Subagent review P1: a fetcher that emits mixed-type lists
    (header rows, totals rows, stray ``None`` from a table parse
    error) would otherwise crash ``row.get(...)`` with
    AttributeError. Skip non-dict elements silently."""
    rows: list[Any] = [
        None,
        "header row from BR",
        42,
        ["wrong", "shape"],
        {"Referee": "Real Ref", "G": "30", "PF/G": "40.0"},
    ]
    payload = parse_referee_tendency_rows(rows, season=_SEASON, fetched_at=_NOW)
    assert "Real Ref" in payload["referees"]
    assert len(payload["referees"]) == 1


def test_loader_treats_legacy_schema_version_as_cache_miss(db_session, caplog) -> None:
    """Subagent review P1: a row written under an older blob shape
    must NOT be silently misinterpreted by the new loader. With
    ``allow_network=False``, a legacy row falls through to the empty
    payload (forcing the operator to either re-run with allow_network
    or invalidate)."""
    import logging

    from app.models import OperatorSetting
    from app.services.nba_referee_tendencies import tendency_cache_key

    db_session.add(OperatorSetting(
        key=tendency_cache_key(_SEASON),
        value={
            "schema_version": 999,  # not the current version
            "payload": {"season": _SEASON, "referees": {"Old Ref": {}}},
            "expires_at": (_NOW + timedelta(days=1)).isoformat(),
        },
    ))
    db_session.commit()

    with caplog.at_level(logging.WARNING):
        payload = load_nba_referee_tendencies(
            db_session, season=_SEASON, fetcher=_make_fetcher([]),
            allow_network=False, now=_NOW,
        )
    assert payload["referees"] == {}
    assert any("schema_version" in record.message for record in caplog.records)


def test_loader_treats_legacy_blob_without_schema_version_as_cache_miss(
    db_session,
) -> None:
    """A blob written before this PR (no ``schema_version`` key) must
    also be treated as a cache miss."""
    from app.models import OperatorSetting
    from app.services.nba_referee_tendencies import tendency_cache_key

    db_session.add(OperatorSetting(
        key=tendency_cache_key(_SEASON),
        value={
            # No schema_version key.
            "payload": {"season": _SEASON, "referees": {"Old Ref": {}}},
            "expires_at": (_NOW + timedelta(days=1)).isoformat(),
        },
    ))
    db_session.commit()

    payload = load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=_make_fetcher([]),
        allow_network=False, now=_NOW,
    )
    assert payload["referees"] == {}


def test_loader_warns_and_misses_when_payload_present_without_expires_at(
    db_session, caplog,
) -> None:
    """Subagent review P1: a partially-written or hand-edited blob
    with ``payload`` but no parseable ``expires_at`` should NOT be
    silently treated as fresh OR silently dropped — log + miss so
    operators can debug the malformed row."""
    import logging

    from app.models import OperatorSetting
    from app.services.nba_referee_tendencies import tendency_cache_key

    db_session.add(OperatorSetting(
        key=tendency_cache_key(_SEASON),
        value={
            "schema_version": 1,
            "payload": {"season": _SEASON, "referees": {"Stuck Ref": {}}},
            # No expires_at key.
        },
    ))
    db_session.commit()

    with caplog.at_level(logging.WARNING):
        payload = load_nba_referee_tendencies(
            db_session, season=_SEASON, fetcher=_make_fetcher([]),
            allow_network=False, now=_NOW,
        )
    assert payload["referees"] == {}
    assert any(
        "expires_at" in record.message for record in caplog.records
    )


def test_parser_filters_nan_and_inf_from_numeric_fields() -> None:
    """Self-review catch: ``float('nan')`` and ``float('inf')`` parse
    successfully via ``float()`` and would otherwise slip into the
    cached payload, corrupting every downstream comparison the
    consumer makes (NaN != NaN; inf > anything)."""
    rows = [
        {
            "Referee": "NaN Ref",
            "G": "30",
            "PF/G": "nan",
            "FT/G": float("nan"),
            "T": "inf",
        },
        {
            "Referee": "Inf Ref",
            "G": float("inf"),
            "PF/G": "40.0",
            "FT/G": "42.0",
            "T": "5",
        },
    ]
    payload = parse_referee_tendency_rows(rows, season=_SEASON, fetched_at=_NOW)
    # NaN ref: G=30 stays, but the NaN/inf ratios coerce to None.
    nan_ref = payload["referees"]["NaN Ref"]
    assert nan_ref["games_officiated"] == 30
    assert nan_ref["fouls_per_game"] is None
    assert nan_ref["fta_per_game"] is None
    assert nan_ref["technicals"] is None
    # Inf ref: G is inf → games_officiated parses as None → row dropped.
    assert "Inf Ref" not in payload["referees"]


def test_dataclass_round_trip() -> None:
    """``NbaRefereeTendency`` is the frozen dataclass that consumer
    side will read; verify the parser builds it in the expected shape."""
    payload = parse_referee_tendency_rows(_br_rows(n=1), season=_SEASON, fetched_at=_NOW)
    raw = payload["referees"]["Scott Foster"]
    rebuilt = NbaRefereeTendency(
        name="Scott Foster",
        games_officiated=raw["games_officiated"],
        fouls_per_game=raw["fouls_per_game"],
        fta_per_game=raw["fta_per_game"],
        technicals=raw["technicals"],
    )
    assert rebuilt.fouls_per_game == 42.1


# -- Loader -----------------------------------------------------------


def _make_fetcher(rows: list[dict[str, Any]]) -> Callable[[int], list[dict[str, Any]]]:
    """Test stub for the ``fetcher`` argument. Records every call so
    tests can assert it WASN'T invoked when the cache should have
    short-circuited."""
    calls: list[int] = []

    def fetcher(season: int) -> list[dict[str, Any]]:
        calls.append(season)
        return rows

    fetcher.calls = calls  # type: ignore[attr-defined]
    return fetcher


def test_loader_returns_empty_payload_when_cache_missing_and_no_network(db_session) -> None:
    fetcher = _make_fetcher(_br_rows())
    payload = load_nba_referee_tendencies(
        db_session,
        season=_SEASON,
        fetcher=fetcher,
        allow_network=False,
        now=_NOW,
    )
    assert payload["season"] == _SEASON
    assert payload["referees"] == {}
    assert fetcher.calls == []  # type: ignore[attr-defined]


def test_loader_fetches_when_cache_missing_and_network_allowed(db_session) -> None:
    fetcher = _make_fetcher(_br_rows())
    payload = load_nba_referee_tendencies(
        db_session,
        season=_SEASON,
        fetcher=fetcher,
        allow_network=True,
        now=_NOW,
    )
    assert "Scott Foster" in payload["referees"]
    assert fetcher.calls == [_SEASON]
    # Cache row was written.
    cached = db_session.scalar(
        OperatorSetting.__table__.select().where(
            OperatorSetting.key == tendency_cache_key(_SEASON)
        )
    )
    assert cached is not None


def test_loader_serves_cache_within_ttl_without_invoking_fetcher(db_session) -> None:
    fetcher = _make_fetcher(_br_rows())
    load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=fetcher,
        allow_network=True, now=_NOW,
    )
    db_session.commit()
    fetcher.calls.clear()  # type: ignore[attr-defined]

    later = _NOW + timedelta(minutes=DEFAULT_TENDENCY_CACHE_MINUTES // 2)
    payload = load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=fetcher,
        allow_network=True, now=later,
    )
    assert "Scott Foster" in payload["referees"]
    assert fetcher.calls == []  # cache hit, no fetch


def test_loader_serves_stale_when_network_blocked(db_session) -> None:
    fetcher = _make_fetcher(_br_rows())
    load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=fetcher,
        allow_network=True, now=_NOW,
    )
    db_session.commit()

    # Past expiry but well within the stale ceiling.
    later = _NOW + timedelta(minutes=DEFAULT_TENDENCY_CACHE_MINUTES + 60)
    payload = load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=_make_fetcher([]),
        allow_network=False, now=later,
    )
    # Stale fallback returns the cached payload, not the empty fetcher result.
    assert "Scott Foster" in payload["referees"]


def test_loader_drops_to_empty_past_stale_ceiling(db_session) -> None:
    """Beyond ``2 * ttl`` past expiry, even the cached payload is
    too old to serve. Mirrors the PR #100 stale-ceiling pattern."""
    fetcher = _make_fetcher(_br_rows())
    load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=fetcher,
        allow_network=True, now=_NOW,
    )
    db_session.commit()

    # 3x TTL past now → past the 2x stale ceiling.
    way_later = _NOW + timedelta(minutes=DEFAULT_TENDENCY_CACHE_MINUTES * 3)
    payload = load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=_make_fetcher([]),
        allow_network=False, now=way_later,
    )
    assert payload["referees"] == {}


def test_loader_falls_back_to_stale_cache_on_fetch_failure(db_session) -> None:
    fetcher = _make_fetcher(_br_rows())
    load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=fetcher,
        allow_network=True, now=_NOW,
    )
    db_session.commit()

    def raising_fetcher(season: int) -> list[dict[str, Any]]:
        raise RuntimeError("BR scraping blocked by Cloudflare")

    later = _NOW + timedelta(minutes=DEFAULT_TENDENCY_CACHE_MINUTES + 30)
    payload = load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=raising_fetcher,
        allow_network=True, now=later,
    )
    assert "Scott Foster" in payload["referees"]


def test_loader_returns_empty_when_fetch_fails_and_no_cache(db_session) -> None:
    def raising_fetcher(season: int) -> list[dict[str, Any]]:
        raise RuntimeError("BR returned 403")

    payload = load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=raising_fetcher,
        allow_network=True, now=_NOW,
    )
    assert payload["referees"] == {}


def test_loader_per_season_isolation(db_session) -> None:
    """Each season has its own cache row — reading season 2025 must
    not return season 2026's payload (an obvious bug if the cache
    key forgot the season suffix)."""
    fetcher_2026 = _make_fetcher(_br_rows())
    load_nba_referee_tendencies(
        db_session, season=2026, fetcher=fetcher_2026,
        allow_network=True, now=_NOW,
    )
    db_session.commit()

    fetcher_2025 = _make_fetcher([])
    payload = load_nba_referee_tendencies(
        db_session, season=2025, fetcher=fetcher_2025,
        allow_network=False, now=_NOW,
    )
    assert payload["referees"] == {}
    assert payload["season"] == 2025


# -- Invalidate -------------------------------------------------------


def test_invalidate_removes_cache_row(db_session) -> None:
    fetcher = _make_fetcher(_br_rows())
    load_nba_referee_tendencies(
        db_session, season=_SEASON, fetcher=fetcher,
        allow_network=True, now=_NOW,
    )
    db_session.commit()

    invalidate_nba_referee_tendencies(db_session, season=_SEASON)
    db_session.commit()

    cached = db_session.scalar(
        OperatorSetting.__table__.select().where(
            OperatorSetting.key == tendency_cache_key(_SEASON)
        )
    )
    assert cached is None


def test_invalidate_is_idempotent_when_no_cache_exists(db_session) -> None:
    # Should not raise — operator may invalidate proactively.
    invalidate_nba_referee_tendencies(db_session, season=_SEASON)
    db_session.commit()


def test_cache_key_is_per_season() -> None:
    assert tendency_cache_key(2026) == "nba_referee_tendencies_2026"
    assert tendency_cache_key(2025) == "nba_referee_tendencies_2025"
    assert tendency_cache_key(2026) != tendency_cache_key(2025)

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Event, EventParticipant, Market, MarketSnapshot, Prediction, Recommendation
from app.query_utils import chunked
from app.services.predictions import OPEN_MARKET_STATUSES

if TYPE_CHECKING:
    from app.services.scoring import PropStatsResolver


# Smarter WNBA PR 6 — WNBA joins the current-slate watchlist scope so
# KXWNBAGAME / KXWNBA player-prop markets surface in the trade desk and
# the ``/product/freshness`` endpoint enumerates a per-WNBA scope row.
# Smarter NFL PR 10a — NFL goes live behind the PR 9 backtest's GO
# verdict (SMARTER_NFL_PREP.md), game lines first via the family
# allowlist below; props/parlays follow in PR 10b.
CURRENT_WATCHLIST_SPORTS = frozenset({"NBA", "MLB", "WNBA", "NFL"})
CURRENT_WATCHLIST_MARKET_FAMILIES = frozenset({"winner", "game_line", "player_prop"})
# Smarter NFL PR 8 — per-sport family allowlist. Sports without an
# entry get the full family set (existing behavior). This is the
# "lines live before props" mechanism: PR 10a sets
# ``"NFL": frozenset({"winner", "game_line"})`` and PR 10b removes it.
CURRENT_WATCHLIST_FAMILIES_BY_SPORT: dict[str, frozenset[str]] = {
    # Smarter NFL PR 10a — lines live first; PR 10b removes this entry
    # once research-mode props have a week or two of observed output.
    "NFL": frozenset({"winner", "game_line"}),
}


def current_families_for_sport(sport_key: str | None) -> frozenset[str]:
    return CURRENT_WATCHLIST_FAMILIES_BY_SPORT.get(
        (sport_key or "").upper(), CURRENT_WATCHLIST_MARKET_FAMILIES
    )
TERMINAL_EVENT_STATUSES = frozenset({"completed", "cancelled"})
CURRENT_WATCHLIST_MAX_IN_PROGRESS_AGE = timedelta(hours=18)


def _coverage_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().default_timezone)


def _coverage_reference_now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coverage_day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    reference_now = _coerce_utc(_coverage_reference_now(now)) or datetime.now(timezone.utc)
    local_tz = _coverage_timezone()
    local_day_start = datetime.combine(reference_now.astimezone(local_tz).date(), time.min, tzinfo=local_tz)
    local_day_end = local_day_start + timedelta(days=1)
    return local_day_start.astimezone(timezone.utc), local_day_end.astimezone(timezone.utc)


def _in_progress_cutoff(now: datetime | None = None) -> datetime:
    reference_now = _coerce_utc(_coverage_reference_now(now)) or datetime.now(timezone.utc)
    return reference_now - CURRENT_WATCHLIST_MAX_IN_PROGRESS_AGE


def is_current_watchlist_status(
    event_status: str | None,
    starts_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    if starts_at is None:
        return False

    normalized_status = str(event_status or "").lower()
    if normalized_status in TERMINAL_EVENT_STATUSES:
        return False

    starts_at_utc = _coerce_utc(starts_at)
    reference_now = _coerce_utc(_coverage_reference_now(now))
    if starts_at_utc is None or reference_now is None:
        return False

    if normalized_status == "in_progress":
        return starts_at_utc >= _in_progress_cutoff(reference_now)

    local_tz = _coverage_timezone()
    event_local_date = starts_at_utc.astimezone(local_tz).date()
    current_local_date = reference_now.astimezone(local_tz).date()
    return event_local_date == current_local_date


def is_current_watchlist_event(event: Event | None, *, now: datetime | None = None) -> bool:
    if event is None:
        return False
    return is_current_watchlist_status(event.status, event.starts_at, now=now)


def is_current_watchlist_market(market: Market | None, *, now: datetime | None = None) -> bool:
    if market is None or market.event is None:
        return False
    if (market.sport_key or "").upper() not in CURRENT_WATCHLIST_SPORTS:
        return False
    if (market.status or "").lower() not in OPEN_MARKET_STATUSES:
        return False
    if str((market.raw_data or {}).get("copilot_market_family") or "") not in current_families_for_sport(market.sport_key):
        return False
    return is_current_watchlist_event(market.event, now=now)


def _winner_source_priority(market: Market) -> tuple[int, str]:
    source_type = str((market.raw_data or {}).get("copilot_source_type") or "standalone")
    priority = 1 if source_type == "combo_derived" else 0
    return priority, market.ticker


def _coverage_market_sort_key(market: Market) -> tuple[datetime, int, int, str, float, str]:
    event = market.event
    starts_at = event.starts_at if event and event.starts_at else datetime.max.replace(tzinfo=timezone.utc)
    family = str((market.raw_data or {}).get("copilot_market_family") or "")
    family_priority = 0 if family == "winner" else 1
    threshold = float((market.raw_data or {}).get("copilot_threshold") or 0.0)
    subject_name = str((market.raw_data or {}).get("copilot_subject_name") or "")
    return starts_at, 0 if (market.sport_key or "") == "NBA" else 1, family_priority, market.title, threshold, subject_name


def current_watchlist_event_ids(
    db: Session,
    *,
    sport: str | None = None,
    now: datetime | None = None,
) -> list[int]:
    allowed_sports = {sport.upper()} if sport else set(CURRENT_WATCHLIST_SPORTS)
    day_start, day_end = _coverage_day_window(now)
    in_progress_cutoff = _in_progress_cutoff(now)
    stmt = (
        select(Event.id)
        .where(
            Event.sport_key.in_(tuple(allowed_sports)),
            Event.status.notin_(tuple(TERMINAL_EVENT_STATUSES)),
            (
                ((func.lower(Event.status) == "in_progress") & (Event.starts_at >= in_progress_cutoff))
                | ((Event.starts_at >= day_start) & (Event.starts_at < day_end))
            ),
        )
        .order_by(Event.starts_at.asc(), Event.id.asc())
    )
    return list(db.scalars(stmt).all())


def recommendation_market_ids_for_sports(db: Session, *, sports: set[str] | None = None) -> list[int]:
    scoped_sports = tuple(sorted(sports or set(CURRENT_WATCHLIST_SPORTS)))
    if not scoped_sports:
        return []
    rows = db.execute(
        select(Recommendation.market_id)
        .join(Market, Recommendation.market_id == Market.id)
        .where(
            Recommendation.status == "active",
            Market.sport_key.in_(scoped_sports),
        )
        .group_by(Recommendation.market_id)
        .order_by(Recommendation.market_id.asc())
    ).all()
    return [int(market_id) for market_id, in rows]


def load_current_watchlist_markets(
    db: Session,
    *,
    sport: str | None = None,
    now: datetime | None = None,
    market_ids: set[int] | None = None,
    event_ids: set[int] | None = None,
) -> list[Market]:
    allowed_sports = {sport.upper()} if sport else set(CURRENT_WATCHLIST_SPORTS)
    scoped_event_ids = set(event_ids or current_watchlist_event_ids(db, sport=sport, now=now))
    if not scoped_event_ids:
        return []
    # Bug #23: push the ``copilot_market_family`` filter into SQL.
    # ``is_current_watchlist_market`` (called downstream by
    # ``current_watchlist_markets``) checks the same set in Python
    # against ``raw_data["copilot_market_family"]``; doing the filter
    # in the DB cuts the row set so the watchlist tick doesn't load
    # every open market into memory just to drop the non-watchlist
    # families. The Python check stays for the event-time / sport
    # logic that depends on the joined event row.
    stmt = (
        select(Market)
        .options(
            joinedload(Market.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant)
        )
        .where(
            Market.event_id.in_(tuple(sorted(scoped_event_ids))),
            Market.status.in_(tuple(OPEN_MARKET_STATUSES)),
            Market.sport_key.in_(tuple(allowed_sports)),
            Market.raw_data["copilot_market_family"]
            .as_string()
            .in_(tuple(CURRENT_WATCHLIST_MARKET_FAMILIES)),
        )
    )
    if market_ids is not None:
        if not market_ids:
            return []
        stmt = stmt.where(Market.id.in_(tuple(sorted(market_ids))))
    # Smarter NFL PR 8 — the SQL filter above uses the GLOBAL family
    # set (per-sport filtering in JSON-extract SQL is awkward across
    # SQLite/Postgres); the per-sport allowlist applies here in Python.
    return [
        market
        for market in db.scalars(stmt).all()
        if str((market.raw_data or {}).get("copilot_market_family") or "")
        in current_families_for_sport(market.sport_key)
    ]


def current_watchlist_markets(
    db: Session,
    *,
    sport: str | None = None,
    now: datetime | None = None,
) -> list[Market]:
    markets = load_current_watchlist_markets(db, sport=sport, now=now)

    winners: dict[tuple[int, str], Market] = {}
    game_lines: list[Market] = []
    props: list[Market] = []
    for market in markets:
        if not is_current_watchlist_market(market, now=now):
            continue
        family = str((market.raw_data or {}).get("copilot_market_family") or "")
        if family == "winner":
            winner_key = (market.event_id or 0, str((market.raw_data or {}).get("copilot_market_kind") or "winner"))
            existing = winners.get(winner_key)
            if existing is None or _winner_source_priority(market) < _winner_source_priority(existing):
                winners[winner_key] = market
        elif family == "game_line":
            game_lines.append(market)
        elif family == "player_prop":
            props.append(market)

    selected = [*winners.values(), *game_lines, *props]
    selected.sort(key=_coverage_market_sort_key)
    return selected


def warm_current_watchlist_prop_context(
    db: Session,
    resolver: PropStatsResolver | None = None,
    *,
    sport: str | None = None,
    now: datetime | None = None,
    markets: list[Market] | None = None,
) -> dict[str, int]:
    if resolver is None:
        from app.services.scoring import PropStatsResolver

        active_resolver = PropStatsResolver(db)
    else:
        active_resolver = resolver
    unique_subjects: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
    selected_markets = markets if markets is not None else current_watchlist_markets(db, sport=sport, now=now)
    for market in selected_markets:
        raw_data = market.raw_data or {}
        if raw_data.get("copilot_market_family") != "player_prop":
            continue
        sport_key = str(market.sport_key or "")
        subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
        team_hint = str(raw_data.get("copilot_subject_team") or "").strip() or None
        if not sport_key or not subject_name:
            continue
        key = (sport_key, subject_name.lower(), (team_hint or "").upper())
        unique_subjects[key] = (sport_key, subject_name, team_hint)

    for sport_key, subject_name, team_hint in unique_subjects.values():
        try:
            active_resolver.resolve(sport_key, subject_name, team_hint=team_hint)
        except Exception:
            continue

    return active_resolver.stats.as_dict()


def _latest_per_market_by_captured_at(
    db: Session,
    model: type,
    market_ids: list[int],
) -> dict[int, Any]:
    """Return the latest row per ``market_id``, ordered by
    ``captured_at DESC, id DESC``.

    Bug #8: callers used to use ``func.max(model.id)``, which only
    matches "latest captured" while inserts are strictly monotonic by
    capture time. Real production paths violate that — the retry queue
    inserts old rows after newer ones, backfill jobs insert historical
    rows into a live table, and concurrent workers interleave commit
    order. When the latest captured row has a lower id than a stale
    one, ``max(id)`` returns the stale row. Ranking on ``captured_at``
    with ``id`` as the deterministic tiebreaker fixes both cases.
    """
    if not market_ids:
        return {}
    result: dict[int, Any] = {}
    # Chunk market_ids so a large id list can't overflow SQLite's
    # host-parameter cap ("too many SQL variables"). Each market's
    # latest row is independent, so per-chunk results merge cleanly.
    for market_id_chunk in chunked(market_ids):
        ranked = (
            select(
                model,
                func.row_number()
                .over(
                    partition_by=model.market_id,
                    order_by=(model.captured_at.desc(), model.id.desc()),
                )
                .label("rn"),
            )
            .where(model.market_id.in_(market_id_chunk))
            .subquery()
        )
        rows = db.scalars(
            select(model)
            .join(ranked, model.id == ranked.c.id)
            .where(ranked.c.rn == 1)
        ).all()
        result.update({row.market_id: row for row in rows})
    return result


def latest_snapshot_by_market_id(db: Session, market_ids: list[int]) -> dict[int, MarketSnapshot]:
    return _latest_per_market_by_captured_at(db, MarketSnapshot, market_ids)


def recent_snapshots_by_market_id(
    db: Session,
    market_ids: list[int],
    *,
    limit_per_market: int,
) -> dict[int, list[MarketSnapshot]]:
    """Return up to ``limit_per_market`` most recent snapshots per market
    in chronological (oldest → newest) order.

    Uses the same ``row_number() over (partition_by market_id order_by
    captured_at desc, id desc)`` window as
    :func:`_latest_per_market_by_captured_at`, with ``rn <=
    limit_per_market`` instead of ``rn == 1``. The output list is
    re-ordered oldest-first so callers can pass it straight to a
    sparkline.
    """
    if not market_ids or limit_per_market <= 0:
        return {}
    by_market: dict[int, list[MarketSnapshot]] = {}
    for market_id_chunk in chunked(market_ids):
        ranked = (
            select(
                MarketSnapshot,
                func.row_number()
                .over(
                    partition_by=MarketSnapshot.market_id,
                    order_by=(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc()),
                )
                .label("rn"),
            )
            .where(MarketSnapshot.market_id.in_(market_id_chunk))
            .subquery()
        )
        rows = db.scalars(
            select(MarketSnapshot)
            .join(ranked, MarketSnapshot.id == ranked.c.id)
            .where(ranked.c.rn <= limit_per_market)
            .order_by(
                MarketSnapshot.market_id.asc(),
                MarketSnapshot.captured_at.asc(),
                MarketSnapshot.id.asc(),
            )
        ).all()
        for row in rows:
            by_market.setdefault(row.market_id, []).append(row)
    return by_market


def latest_recommendation_by_market_id(db: Session, market_ids: list[int]) -> dict[int, Recommendation]:
    return _latest_per_market_by_captured_at(db, Recommendation, market_ids)


def latest_prediction_by_market_id(db: Session, market_ids: list[int]) -> dict[int, Prediction]:
    return _latest_per_market_by_captured_at(db, Prediction, market_ids)

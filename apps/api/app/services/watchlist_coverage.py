from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Event, EventParticipant, Market, MarketSnapshot, Prediction, Recommendation
from app.services.predictions import OPEN_MARKET_STATUSES

if TYPE_CHECKING:
    from app.services.scoring import PropStatsResolver


CURRENT_WATCHLIST_SPORTS = frozenset({"NBA", "MLB"})
CURRENT_WATCHLIST_MARKET_FAMILIES = frozenset({"winner", "game_line", "player_prop"})
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
    if str((market.raw_data or {}).get("copilot_market_family") or "") not in CURRENT_WATCHLIST_MARKET_FAMILIES:
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
        )
    )
    if market_ids is not None:
        if not market_ids:
            return []
        stmt = stmt.where(Market.id.in_(tuple(sorted(market_ids))))
    return list(db.scalars(stmt).all())


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


def latest_snapshot_by_market_id(db: Session, market_ids: list[int]) -> dict[int, MarketSnapshot]:
    if not market_ids:
        return {}
    max_id_per_market = (
        select(
            MarketSnapshot.market_id,
            func.max(MarketSnapshot.id).label("max_id"),
        )
        .where(MarketSnapshot.market_id.in_(market_ids))
        .group_by(MarketSnapshot.market_id)
        .subquery()
    )
    rows = db.scalars(
        select(MarketSnapshot)
        .join(max_id_per_market, MarketSnapshot.id == max_id_per_market.c.max_id)
    ).all()
    return {row.market_id: row for row in rows}


def latest_recommendation_by_market_id(db: Session, market_ids: list[int]) -> dict[int, Recommendation]:
    if not market_ids:
        return {}
    max_id_per_market = (
        select(
            Recommendation.market_id,
            func.max(Recommendation.id).label("max_id"),
        )
        .where(Recommendation.market_id.in_(market_ids))
        .group_by(Recommendation.market_id)
        .subquery()
    )
    rows = db.scalars(
        select(Recommendation)
        .join(max_id_per_market, Recommendation.id == max_id_per_market.c.max_id)
    ).all()
    return {row.market_id: row for row in rows}


def latest_prediction_by_market_id(db: Session, market_ids: list[int]) -> dict[int, Prediction]:
    if not market_ids:
        return {}
    max_id_per_market = (
        select(
            Prediction.market_id,
            func.max(Prediction.id).label("max_id"),
        )
        .where(Prediction.market_id.in_(market_ids))
        .group_by(Prediction.market_id)
        .subquery()
    )
    rows = db.scalars(
        select(Prediction)
        .join(max_id_per_market, Prediction.id == max_id_per_market.c.max_id)
    ).all()
    return {row.market_id: row for row in rows}

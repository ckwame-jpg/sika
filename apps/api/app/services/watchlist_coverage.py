from __future__ import annotations

from datetime import datetime, timezone
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


def is_current_watchlist_event(event: Event | None, *, now: datetime | None = None) -> bool:
    if event is None or event.starts_at is None:
        return False
    if (event.status or "").lower() in TERMINAL_EVENT_STATUSES:
        return False

    starts_at = _coerce_utc(event.starts_at)
    if starts_at is None:
        return False
    reference_now = _coerce_utc(_coverage_reference_now(now))
    if reference_now is None:
        return False
    local_tz = _coverage_timezone()
    event_local_date = starts_at.astimezone(local_tz).date()
    current_local_date = reference_now.astimezone(local_tz).date()
    return (event.status or "").lower() == "in_progress" or event_local_date == current_local_date


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


def current_watchlist_markets(
    db: Session,
    *,
    sport: str | None = None,
    now: datetime | None = None,
) -> list[Market]:
    allowed_sports = {sport.upper()} if sport else set(CURRENT_WATCHLIST_SPORTS)
    markets = db.scalars(
        select(Market)
        .options(
            joinedload(Market.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant)
        )
        .where(Market.event_id.is_not(None), Market.status.in_(tuple(OPEN_MARKET_STATUSES)), Market.sport_key.in_(tuple(allowed_sports)))
    ).all()

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

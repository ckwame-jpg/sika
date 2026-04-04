from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Event, Market, MarketSnapshot, Prediction, Recommendation
from app.services.predictions import OPEN_MARKET_STATUSES

if TYPE_CHECKING:
    from app.services.scoring import PropStatsResolver


CURRENT_WATCHLIST_SPORTS = frozenset({"NBA", "MLB"})
CURRENT_WATCHLIST_MARKET_FAMILIES = frozenset({"winner", "player_prop"})
TERMINAL_EVENT_STATUSES = frozenset({"completed", "cancelled"})


def _coverage_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().default_timezone)


def _coverage_reference_now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def is_current_watchlist_event(event: Event | None, *, now: datetime | None = None) -> bool:
    if event is None or event.starts_at is None:
        return False
    if (event.status or "").lower() in TERMINAL_EVENT_STATUSES:
        return False

    reference_now = _coverage_reference_now(now)
    local_tz = _coverage_timezone()
    event_local_date = event.starts_at.astimezone(local_tz).date()
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
        .options(joinedload(Market.event))
        .where(Market.event_id.is_not(None), Market.status.in_(tuple(OPEN_MARKET_STATUSES)), Market.sport_key.in_(tuple(allowed_sports)))
    ).all()

    winners: dict[tuple[int, str], Market] = {}
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
        elif family == "player_prop":
            props.append(market)

    selected = [*winners.values(), *props]
    selected.sort(key=_coverage_market_sort_key)
    return selected


def warm_current_watchlist_prop_context(
    db: Session,
    resolver: PropStatsResolver | None = None,
    *,
    sport: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    if resolver is None:
        from app.services.scoring import PropStatsResolver

        active_resolver = PropStatsResolver(db)
    else:
        active_resolver = resolver
    unique_subjects: dict[tuple[str, str, str], tuple[str, str, str | None]] = {}
    for market in current_watchlist_markets(db, sport=sport, now=now):
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
    rows = db.scalars(
        select(MarketSnapshot)
        .where(MarketSnapshot.market_id.in_(market_ids))
        .order_by(MarketSnapshot.market_id.asc(), MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
    ).all()
    latest: dict[int, MarketSnapshot] = {}
    for row in rows:
        latest.setdefault(row.market_id, row)
    return latest


def latest_recommendation_by_market_id(db: Session, market_ids: list[int]) -> dict[int, Recommendation]:
    if not market_ids:
        return {}
    rows = db.scalars(
        select(Recommendation)
        .where(Recommendation.market_id.in_(market_ids))
        .order_by(Recommendation.market_id.asc(), Recommendation.captured_at.desc(), Recommendation.id.desc())
    ).all()
    latest: dict[int, Recommendation] = {}
    for row in rows:
        latest.setdefault(row.market_id, row)
    return latest


def latest_prediction_by_market_id(db: Session, market_ids: list[int]) -> dict[int, Prediction]:
    if not market_ids:
        return {}
    rows = db.scalars(
        select(Prediction)
        .where(Prediction.market_id.in_(market_ids))
        .order_by(Prediction.market_id.asc(), Prediction.captured_at.desc(), Prediction.id.desc())
    ).all()
    latest: dict[int, Prediction] = {}
    for row in rows:
        latest.setdefault(row.market_id, row)
    return latest

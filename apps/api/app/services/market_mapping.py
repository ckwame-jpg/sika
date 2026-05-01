from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Event, EventParticipant, Market
from app.services.market_support import infer_market_sport_key, market_anchor_time
from app.sports.base import alias_tokens


def _market_text(market: Market) -> str:
    parts = [market.title or "", market.subtitle or ""]
    for key in ("subtitle", "yes_sub_title", "no_sub_title", "title", "rules_primary"):
        value = (market.raw_data or {}).get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _event_tokens(event: Event) -> set[str]:
    tokens = alias_tokens(event.name)
    for entry in event.participants:
        participant = entry.participant
        tokens.update(alias_tokens(participant.display_name, participant.short_name))
    return tokens


def _token_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    shared = left & right
    if not shared:
        return 0.0
    strong_shared = [token for token in shared if len(token) >= 4]
    return min(1.0, (len(shared) * 0.15) + (len(strong_shared) * 0.2))


def _normalize_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def map_markets_to_events(db: Session, *, candidate_market_ids: set[int] | None = None) -> int:
    stmt = select(Market)
    if candidate_market_ids is None:
        stmt = stmt.where(Market.event_id.is_(None))
    else:
        if not candidate_market_ids:
            return 0
        stmt = stmt.where(Market.id.in_(tuple(sorted(candidate_market_ids))))
    markets = db.scalars(stmt).all()
    if not markets:
        return 0

    events = db.scalars(
        select(Event)
        .options(selectinload(Event.participants).selectinload(EventParticipant.participant))
        .where(Event.status != "completed")
    ).all()

    event_context = [
        (
            event,
            _event_tokens(event),
            _normalize_utc(event.starts_at),
        )
        for event in events
    ]

    updated = 0
    for market in markets:
        market_tokens = alias_tokens(_market_text(market))
        market_payload = {
            "event_ticker": market.event_ticker,
            "ticker": market.ticker,
            "series_ticker": market.series_ticker,
            **(market.raw_data or {}),
        }
        market_sport_key = infer_market_sport_key(market_payload)
        anchor_time = _normalize_utc(market_anchor_time(market.raw_data or {}) or market.close_time)
        best_event = None
        best_score = 0.0
        best_time_delta: float | None = None
        for event, event_tokens, event_starts_at in event_context:
            if market_sport_key and event.sport_key != market_sport_key:
                continue
            time_delta: float | None = None
            if anchor_time and event_starts_at:
                time_delta = abs((event_starts_at - anchor_time).total_seconds())
                if time_delta > timedelta(hours=36).total_seconds():
                    continue
            score = _token_score(market_tokens, event_tokens)
            if score < best_score:
                continue
            if score > best_score or (
                time_delta is not None
                and (best_time_delta is None or time_delta < best_time_delta)
            ):
                best_score = score
                best_event = event
                best_time_delta = time_delta
        if best_event and best_score >= 0.35:
            market.event_id = best_event.id
            market.sport_key = best_event.sport_key
            updated += 1
    db.flush()
    return updated

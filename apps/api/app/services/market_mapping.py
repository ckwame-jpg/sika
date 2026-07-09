from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Event, EventParticipant, Market
from app.query_utils import chunked
from app.services.market_support import infer_market_sport_key, market_anchor_time
from app.sports.base import alias_tokens


# Bug #17 tuning constants.
#
# ``MIN_MAPPING_CONFIDENCE`` is the historical accept threshold (kept
# at 0.35 — the value the codebase has run with). ``CANDIDATE_TOP_K``
# bounds how many runners-up we persist on the Market row so the
# stored JSON stays small.
MIN_MAPPING_CONFIDENCE = 0.35
CANDIDATE_TOP_K = 5


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
    # Bug #17: never auto-remap a market that ops has manually
    # overridden — the override stamp is sticky across refresh cycles.
    base = select(Market).where(Market.mapping_overridden_at.is_(None))
    if candidate_market_ids is None:
        markets = db.scalars(base.where(Market.event_id.is_(None))).all()
    else:
        if not candidate_market_ids:
            return 0
        # Chunk the id list to stay under SQLite's host-parameter cap.
        markets: list[Market] = []
        for market_id_chunk in chunked(sorted(candidate_market_ids)):
            markets.extend(
                db.scalars(base.where(Market.id.in_(tuple(market_id_chunk)))).all()
            )
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
        # Bug #17: track *every* viable candidate (passes sport +
        # time-window filter and has any token overlap), not just the
        # running best, so the persisted record reflects the ambiguity
        # the auto-mapper actually saw.
        scored_candidates: list[dict[str, object]] = []
        for event, event_tokens, event_starts_at in event_context:
            if market_sport_key and event.sport_key != market_sport_key:
                continue
            time_delta: float | None = None
            if anchor_time and event_starts_at:
                time_delta = abs((event_starts_at - anchor_time).total_seconds())
                if time_delta > timedelta(hours=36).total_seconds():
                    continue
            score = _token_score(market_tokens, event_tokens)
            if score <= 0.0:
                continue
            scored_candidates.append(
                {
                    "event_id": event.id,
                    "event_name": event.name,
                    "sport_key": event.sport_key,
                    "score": round(score, 4),
                    "time_delta_seconds": (
                        round(time_delta, 1) if time_delta is not None else None
                    ),
                }
            )
        scored_candidates.sort(
            key=lambda candidate: (
                -float(candidate["score"]),
                # Tiebreak by smallest time delta (None sorts last).
                float("inf")
                if candidate["time_delta_seconds"] is None
                else float(candidate["time_delta_seconds"]),
            )
        )
        top_candidates = scored_candidates[:CANDIDATE_TOP_K]
        if scored_candidates and scored_candidates[0]["score"] >= MIN_MAPPING_CONFIDENCE:
            winner = scored_candidates[0]
            best_event_id = int(winner["event_id"])
            best_event = next(event for event, *_ in event_context if event.id == best_event_id)
            market.event_id = best_event.id
            market.sport_key = best_event.sport_key
            market.mapping_confidence = float(winner["score"])
            market.mapping_candidates = top_candidates
            updated += 1
        else:
            # Below threshold — leave event_id alone but record what
            # we saw so ops can decide whether to override manually.
            if scored_candidates:
                market.mapping_confidence = float(scored_candidates[0]["score"])
                market.mapping_candidates = top_candidates
            else:
                market.mapping_confidence = 0.0
                market.mapping_candidates = []
    db.flush()
    return updated


def override_market_mapping(
    db: Session,
    *,
    ticker: str,
    event_id: int | None,
    reason: str | None = None,
) -> Market:
    """Manually map ``ticker`` to ``event_id`` (or clear the mapping
    if ``event_id is None``). Stamps ``mapping_overridden_at`` so the
    auto-mapper skips this row on subsequent refresh cycles.

    Bug #17: lets ops correct silent best-match errors (doubleheaders,
    abbreviation collisions, postponed games) without the next
    ``map_markets_to_events`` call clobbering the fix.
    """
    market = db.scalar(select(Market).where(Market.ticker == ticker))
    if market is None:
        raise LookupError(f"Market not found for ticker {ticker!r}")
    if event_id is not None:
        event = db.get(Event, event_id)
        if event is None:
            raise LookupError(f"Event {event_id} not found")
        market.event_id = event.id
        market.sport_key = event.sport_key
    else:
        market.event_id = None
    market.mapping_overridden_at = datetime.now(timezone.utc)
    market.mapping_overridden_reason = reason
    # Confidence/candidates are intentionally left as-is — they're a
    # snapshot of what the auto-mapper saw and are useful audit context
    # even after the override.
    db.flush()
    return market

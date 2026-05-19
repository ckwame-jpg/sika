from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload

from app.clients.kalshi import KalshiPublicClient, parse_price_dollars
from app.config import get_settings
from app.models import Event, Market, Prediction, Recommendation, SignalSnapshot
from app.services.clv import closing_yes_price_for_market, compute_clv
from app.services.ml.lineage import HEURISTIC_SINGLE_MODEL
from app.services.market_support import parse_market_datetime

logger = logging.getLogger(__name__)

MODEL_NAME = HEURISTIC_SINGLE_MODEL.model_name
OPEN_MARKET_STATUSES = {"open", "active", "paused", "initialized"}

# Bulk-fetch budget for the settlement pre-pass. The previous per-ticker
# ``client.get_market`` loop took ~0.7s/ticker (rate limiter + HTTP RTT), so
# 100 unsettled rows = ~70s/batch. ``list_markets(status="settled")`` returns
# up to 1000 rows per page in a single round trip — one page is typically
# enough to cover all of today's settled tickers across all sports. We cap
# the wall clock at 15s so the bulk pre-pass can NEVER be slower than the
# per-ticker fallback for the same batch size.
_BULK_SETTLEMENT_WALL_CLOCK_SECONDS = 15.0
_BULK_SETTLEMENT_MAX_PAGES = 10
_BULK_SETTLEMENT_PAGE_LIMIT = 1000


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# Smarter #26 — settlement-aging bucket boundaries (hours past market
# close_time without a settled prediction). Ops badge on the readiness
# panel surfaces these counts so operators see when Kalshi settlement
# is lagging.
SETTLEMENT_AGING_BUCKET_BOUNDARIES_HOURS: tuple[float, ...] = (1.0, 6.0, 24.0)


@dataclass(frozen=True, slots=True)
class SettlementAging:
    """Counts of predictions stuck in ``pending`` past their market close.

    Each bucket is the number of predictions whose ``market.close_time``
    is in the past by the specified window. Buckets are non-overlapping —
    a prediction at 8h past close lands in ``bucket_6_to_24h`` and NOT
    in the earlier buckets.

    ``total_pending_past_close`` is the sum across all buckets and
    matches the count operators see as the badge.
    """
    bucket_0_to_1h: int
    bucket_1_to_6h: int
    bucket_6_to_24h: int
    bucket_beyond_24h: int
    total_pending_past_close: int


def compute_settlement_aging(db: Session, *, now: datetime | None = None) -> SettlementAging:
    """Count predictions stuck in ``settlement_status='pending'`` past
    their market's ``close_time``, bucketed by how long ago the close
    was.

    Predictions whose market has no ``close_time`` (early market state
    or non-Kalshi sources) are skipped — we don't know when they
    SHOULD have settled. Cancelled / resolved predictions are skipped
    because their ``settlement_status`` is not ``pending`` anymore.
    """
    moment = now or _now_utc()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    bucket_1h, bucket_6h, bucket_24h = SETTLEMENT_AGING_BUCKET_BOUNDARIES_HOURS
    rows = db.execute(
        select(Market.close_time)
        .join(Prediction, Prediction.market_id == Market.id)
        .where(
            Prediction.settlement_status == "pending",
            Market.close_time.is_not(None),
            Market.close_time < moment,
        )
    ).all()

    counts = {"0_to_1h": 0, "1_to_6h": 0, "6_to_24h": 0, "beyond_24h": 0}
    for (close_time,) in rows:
        if close_time is None:
            continue
        # SQLite drops tz info on read; coerce to UTC for the subtraction.
        anchor = close_time if close_time.tzinfo is not None else close_time.replace(tzinfo=timezone.utc)
        hours_past_close = (moment - anchor).total_seconds() / 3600.0
        if hours_past_close < bucket_1h:
            counts["0_to_1h"] += 1
        elif hours_past_close < bucket_6h:
            counts["1_to_6h"] += 1
        elif hours_past_close < bucket_24h:
            counts["6_to_24h"] += 1
        else:
            counts["beyond_24h"] += 1

    return SettlementAging(
        bucket_0_to_1h=counts["0_to_1h"],
        bucket_1_to_6h=counts["1_to_6h"],
        bucket_6_to_24h=counts["6_to_24h"],
        bucket_beyond_24h=counts["beyond_24h"],
        total_pending_past_close=sum(counts.values()),
    )


def _normalized_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


def _round_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 4)


def _market_settlement_value(payload: dict[str, Any]) -> float | None:
    for field in ("settlement_value_dollars", "settlement_value"):
        parsed = parse_price_dollars(payload.get(field))
        if parsed is not None:
            return parsed
    return None


def _prediction_payout(side: str, settlement_value: float) -> float:
    return settlement_value if side == "yes" else 1.0 - settlement_value


def _coverage_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().default_timezone)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coverage_day_window(captured_at: datetime | None) -> tuple[datetime, datetime]:
    local_tz = _coverage_timezone()
    reference = _coerce_utc(captured_at) or _now_utc()
    captured_local = reference.astimezone(local_tz)
    local_day_start = datetime.combine(captured_local.date(), time.min, tzinfo=local_tz)
    local_day_end = local_day_start + timedelta(days=1)
    return local_day_start.astimezone(timezone.utc), local_day_end.astimezone(timezone.utc)


@dataclass(slots=True)
class PredictionSettlement:
    settlement_status: str
    prediction_outcome: str
    market_result: str | None
    winning_side: str | None
    settlement_value: float | None
    settled_at: datetime | None
    realized_pnl: float | None
    settlement_notes: str | None


# Bug #43 — pre-fix ``_session_predictions`` walked the entire
# SQLAlchemy identity_map plus ``db.new`` on every ``capture_prediction``
# call, producing O(N×M) cost over a refresh that captured M markets
# while N in-session predictions grew. The per-session lookup dicts
# below give O(1) hit rate for predictions added via
# ``capture_prediction`` itself; the existing fall-through to
# ``db.scalar(select(...))`` catches predictions that entered the
# session via a query.
_SESSION_PRED_RUN_MARKET = "_session_prediction_by_run_market"
_SESSION_PRED_MARKET_SCOPE = "_session_prediction_by_market_scope"


def _session_prediction_by_run_market(db: Session, run_id: int, market_id: int) -> Prediction | None:
    return db.info.get(_SESSION_PRED_RUN_MARKET, {}).get((run_id, market_id))


def _session_predictions_by_market_scope(db: Session, market_id: int, capture_scope: str) -> list[Prediction]:
    return list(db.info.get(_SESSION_PRED_MARKET_SCOPE, {}).get((market_id, capture_scope), ()))


def _record_session_prediction(db: Session, prediction: Prediction) -> None:
    """Update the per-session lookup dicts with a freshly-added or
    freshly-updated prediction so subsequent lookups hit O(1)."""
    if prediction.run_id is not None and prediction.market_id is not None:
        index = db.info.setdefault(_SESSION_PRED_RUN_MARKET, {})
        index[(prediction.run_id, prediction.market_id)] = prediction
    if prediction.market_id is not None and prediction.capture_scope:
        scope_index = db.info.setdefault(_SESSION_PRED_MARKET_SCOPE, {})
        bucket = scope_index.setdefault((prediction.market_id, prediction.capture_scope), [])
        if prediction not in bucket:
            bucket.append(prediction)


def _apply_prediction_capture(
    prediction: Prediction,
    *,
    run_id: int | None,
    event: Event,
    market: Market,
    recommendation: Recommendation | None,
    signal: SignalSnapshot,
    metadata: dict[str, Any],
    capture_scope: str,
    diagnostics: dict[str, Any],
    selected_side: str,
    action: str,
    suggested_price: float,
    edge: float,
    confidence: float,
    selection_score: float | None,
    invalidation: str | None,
    rationale: str,
    captured_at: datetime,
    reset_settlement: bool = False,
) -> Prediction:
    prediction.run_id = run_id
    prediction.event_id = event.id
    prediction.market_id = market.id
    prediction.ticker = market.ticker
    prediction.sport_key = market.sport_key or event.sport_key
    prediction.event_name = event.name
    prediction.market_title = market.title
    prediction.market_family = str(metadata.get("copilot_market_family") or "") or None
    prediction.market_kind = str(metadata.get("copilot_market_kind") or "") or None
    prediction.stat_key = str(metadata.get("copilot_stat_key") or "") or None
    prediction.threshold = float(metadata.get("copilot_threshold")) if metadata.get("copilot_threshold") is not None else None
    prediction.subject_name = str(metadata.get("copilot_subject_name") or "") or None
    prediction.subject_team = str(metadata.get("copilot_subject_team") or "") or None
    prediction.capture_scope = capture_scope
    prediction.side = recommendation.side if recommendation else selected_side
    prediction.action = action
    prediction.suggested_price = suggested_price
    prediction.fair_yes_price = signal.fair_yes_price
    prediction.fair_no_price = signal.fair_no_price
    prediction.edge = edge
    prediction.confidence = confidence
    prediction.selection_score = selection_score
    prediction.model_name = signal.model_name or MODEL_NAME
    prediction.model_version = signal.model_version
    prediction.calibration_version = signal.calibration_version
    prediction.feature_set_version = signal.feature_set_version
    prediction.model_metadata = dict(signal.model_metadata or {})
    prediction.invalidation = invalidation
    prediction.rationale = rationale
    prediction.reasons = list(signal.reasons or [])
    prediction.features = dict(signal.features or {})
    prediction.scoring_diagnostics = diagnostics
    prediction.market_status_at_capture = market.status
    prediction.captured_at = captured_at
    if reset_settlement:
        prediction.settlement_status = "pending"
        prediction.prediction_outcome = "pending"
        prediction.market_result = None
        prediction.winning_side = None
        prediction.settlement_value = None
        prediction.settled_at = None
        prediction.realized_pnl = None
        prediction.settlement_source = None
        prediction.settlement_notes = None
    return prediction


def capture_prediction(
    db: Session,
    *,
    run_id: int | None,
    event: Event,
    market: Market,
    recommendation: Recommendation | None,
    signal: SignalSnapshot,
    metadata: dict[str, Any],
    capture_scope: str = "recommendation",
) -> Prediction:
    diagnostics = dict(recommendation.scoring_diagnostics or signal.scoring_diagnostics or {}) if recommendation else dict(signal.scoring_diagnostics or {})
    selected_side = str(diagnostics.get("selected_side") or "yes")
    suggested_price_value = diagnostics.get("suggested_price")
    if suggested_price_value is None:
        suggested_price_value = signal.fair_yes_price if selected_side == "yes" else signal.fair_no_price
    suggested_price = recommendation.suggested_price if recommendation else float(suggested_price_value or 0.0)
    action = recommendation.action if recommendation else "buy"
    invalidation = recommendation.invalidation if recommendation else (diagnostics.get("invalidation") or None)
    rationale = recommendation.rationale if recommendation else "; ".join(str(reason) for reason in list(signal.reasons or []))
    edge = recommendation.edge if recommendation else signal.edge
    confidence = recommendation.confidence if recommendation else signal.confidence
    selection_score = recommendation.selection_score if recommendation else signal.selection_score
    captured_at = recommendation.captured_at if recommendation else signal.captured_at
    if captured_at is None:
        # Column defaults only populate on flush, but capture_prediction is
        # called on freshly-constructed SignalSnapshot/Recommendation objects
        # before the session is flushed. Fall back to now so downstream
        # tz-aware arithmetic (_coverage_day_window, _coerce_utc) never sees
        # a None datetime.
        captured_at = _now_utc()
        if recommendation is not None and recommendation.captured_at is None:
            recommendation.captured_at = captured_at
        if signal.captured_at is None:
            signal.captured_at = captured_at

    if run_id is not None:
        # Bug #43 — O(1) per-session lookup; falls through to the DB
        # query for predictions loaded via a non-capture_prediction path.
        existing = _session_prediction_by_run_market(db, run_id, market.id)
        if existing is None:
            existing = db.scalar(
                select(Prediction).where(
                    Prediction.run_id == run_id,
                    Prediction.market_id == market.id,
                )
            )
        if existing is not None:
            if recommendation is not None or existing.capture_scope == "coverage":
                _apply_prediction_capture(
                    existing,
                    run_id=run_id,
                    event=event,
                    market=market,
                    recommendation=recommendation,
                    signal=signal,
                    metadata=metadata,
                    capture_scope="recommendation" if recommendation is not None else capture_scope,
                    diagnostics=diagnostics,
                    selected_side=selected_side,
                    action=action,
                    suggested_price=suggested_price,
                    edge=edge,
                    confidence=confidence,
                    selection_score=selection_score,
                    invalidation=invalidation,
                    rationale=rationale,
                    captured_at=captured_at,
                    reset_settlement=recommendation is None,
                )
            # Bug #43 — register in the per-session index so the next
            # call hits the O(1) lookup even when ``existing`` came
            # from the DB-query fall-through path.
            _record_session_prediction(db, existing)
            return existing

    if capture_scope == "coverage":
        coverage_window_start, coverage_window_end = _coverage_day_window(captured_at)
        # Bug #43 — same O(1) per-session lookup pattern as the
        # run_id+market_id branch above; the index narrows to
        # (market_id, "coverage") and the in-window filter runs only
        # on that bucket instead of every Prediction in the session.
        sampled = next(
            (
                item
                for item in _session_predictions_by_market_scope(db, market.id, "coverage")
                if item.captured_at is not None
                and coverage_window_start <= _coerce_utc(item.captured_at) < coverage_window_end
            ),
            None,
        )
        if sampled is None:
            sampled = db.scalar(
                select(Prediction)
                .where(
                    Prediction.market_id == market.id,
                    Prediction.capture_scope == "coverage",
                    Prediction.captured_at >= coverage_window_start,
                    Prediction.captured_at < coverage_window_end,
                )
                .order_by(Prediction.captured_at.desc(), Prediction.id.desc())
                .limit(1)
            )
        if sampled is not None:
            updated = _apply_prediction_capture(
                sampled,
                run_id=run_id,
                event=event,
                market=market,
                recommendation=None,
                signal=signal,
                metadata=metadata,
                capture_scope="coverage",
                diagnostics=diagnostics,
                selected_side=selected_side,
                action=action,
                suggested_price=suggested_price,
                edge=edge,
                confidence=confidence,
                selection_score=selection_score,
                invalidation=invalidation,
                rationale=rationale,
                captured_at=captured_at,
                reset_settlement=True,
            )
            # Bug #43 — register so subsequent capture_prediction
            # invocations against the same (market, coverage) bucket
            # hit O(1) cache instead of repeating the DB fallthrough.
            _record_session_prediction(db, updated)
            return updated

    prediction = _apply_prediction_capture(
        Prediction(),
        run_id=run_id,
        event=event,
        market=market,
        recommendation=recommendation,
        signal=signal,
        metadata=metadata,
        capture_scope=capture_scope,
        diagnostics=diagnostics,
        selected_side=selected_side,
        action=action,
        suggested_price=suggested_price,
        edge=edge,
        confidence=confidence,
        selection_score=selection_score,
        invalidation=invalidation,
        rationale=rationale,
        captured_at=captured_at,
        reset_settlement=capture_scope == "coverage",
    )
    db.add(prediction)
    _record_session_prediction(db, prediction)
    return prediction


def resolve_prediction_settlement(prediction: Prediction, payload: dict[str, Any]) -> PredictionSettlement | None:
    market_status = _normalized_text(payload.get("status"))
    market_result = _normalized_text(payload.get("result"))
    settlement_value = _market_settlement_value(payload)
    settled_at = parse_market_datetime(payload.get("settlement_ts"))

    if market_result in OPEN_MARKET_STATUSES or market_status in OPEN_MARKET_STATUSES:
        return None

    if market_result in {"yes", "no"}:
        winning_side = market_result
        binary_value = 1.0 if winning_side == "yes" else 0.0
        pnl = _prediction_payout(prediction.side, settlement_value if settlement_value is not None else binary_value) - prediction.suggested_price
        return PredictionSettlement(
            settlement_status="settled",
            prediction_outcome="won" if prediction.side == winning_side else "lost",
            market_result=market_result,
            winning_side=winning_side,
            settlement_value=_round_or_none(settlement_value if settlement_value is not None else binary_value),
            settled_at=settled_at or _now_utc(),
            realized_pnl=round(pnl, 4),
            settlement_notes=None,
        )

    if market_result in {"void", "cancelled", "canceled"}:
        return PredictionSettlement(
            settlement_status="cancelled",
            prediction_outcome="cancelled",
            market_result=market_result,
            winning_side=None,
            settlement_value=_round_or_none(settlement_value),
            settled_at=settled_at or _now_utc(),
            realized_pnl=0.0,
            settlement_notes="Kalshi reported a void/cancelled result.",
        )

    if settlement_value is not None:
        if settlement_value >= 0.999:
            winning_side = "yes"
            pnl = _prediction_payout(prediction.side, 1.0) - prediction.suggested_price
            return PredictionSettlement(
                settlement_status="settled",
                prediction_outcome="won" if prediction.side == winning_side else "lost",
                market_result=market_result or winning_side,
                winning_side=winning_side,
                settlement_value=1.0,
                settled_at=settled_at or _now_utc(),
                realized_pnl=round(pnl, 4),
                settlement_notes="Derived binary result from Kalshi settlement value.",
            )
        if settlement_value <= 0.001:
            winning_side = "no"
            pnl = _prediction_payout(prediction.side, 0.0) - prediction.suggested_price
            return PredictionSettlement(
                settlement_status="settled",
                prediction_outcome="won" if prediction.side == winning_side else "lost",
                market_result=market_result or winning_side,
                winning_side=winning_side,
                settlement_value=0.0,
                settled_at=settled_at or _now_utc(),
                realized_pnl=round(pnl, 4),
                settlement_notes="Derived binary result from Kalshi settlement value.",
            )

        pnl = _prediction_payout(prediction.side, settlement_value) - prediction.suggested_price
        return PredictionSettlement(
            settlement_status="settled",
            prediction_outcome="push",
            market_result=market_result or "fair_market_price",
            winning_side=None,
            settlement_value=_round_or_none(settlement_value),
            settled_at=settled_at or _now_utc(),
            realized_pnl=round(pnl, 4),
            settlement_notes="Non-binary settlement value treated as a push/fair-market-price resolution.",
        )

    if market_status in OPEN_MARKET_STATUSES or market_status is None:
        return None

    return PredictionSettlement(
        settlement_status="unresolved",
        prediction_outcome="unresolved",
        market_result=market_result,
        winning_side=None,
        settlement_value=None,
        settled_at=None,
        realized_pnl=None,
        settlement_notes="Market is no longer open but no settlement result is available yet.",
    )


def _empty_settlement_summary() -> dict[str, int]:
    return {
        "processed": 0,
        "updated": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "cancelled": 0,
        "pending": 0,
        "unresolved": 0,
        "errors": 0,
    }


def _prediction_batch_cursor_filter(captured_at_field, id_field, cursor: dict[str, Any] | None):
    if not cursor:
        return None
    raw_captured_at = cursor.get("captured_at")
    raw_prediction_id = cursor.get("prediction_id")
    if not raw_captured_at or raw_prediction_id is None:
        return None
    cursor_captured_at = datetime.fromisoformat(str(raw_captured_at).replace("Z", "+00:00"))
    cursor_prediction_id = int(raw_prediction_id)
    return or_(
        captured_at_field < cursor_captured_at,
        and_(captured_at_field == cursor_captured_at, id_field < cursor_prediction_id),
    )


def _load_prediction_settlement_batch(
    db: Session,
    *,
    sport_keys: set[str] | None = None,
    limit: int = 250,
    cursor: dict[str, Any] | None = None,
) -> list[Prediction]:
    # Bug #12: previously a ``latest_only_per_key`` toggle filtered to
    # the most recent unresolved prediction per ``(ticker, scope,
    # side)`` partition, leaving older stacked predictions stuck in
    # ``pending`` forever. ``_settle_prediction_rows`` already caches
    # the Kalshi ``get_market`` payload per ticker, so settling all
    # stacked rows costs extra DB iteration but no extra upstream
    # calls — and gives correct hit-rate, calibration, and PnL.
    stmt = (
        select(Prediction)
        .options(joinedload(Prediction.market))
        .where(Prediction.settlement_status.in_(("pending", "unresolved")))
        .order_by(Prediction.captured_at.desc(), Prediction.id.desc())
    )
    if sport_keys:
        stmt = stmt.where(Prediction.sport_key.in_(tuple(sorted(sport_keys))))
    cursor_filter = _prediction_batch_cursor_filter(Prediction.captured_at, Prediction.id, cursor)
    if cursor_filter is not None:
        stmt = stmt.where(cursor_filter)
    return db.scalars(stmt.limit(limit)).all()


def _bulk_fetch_settled_market_payloads(
    client: KalshiPublicClient,
    tickers_needing_refresh: set[str],
) -> dict[str, dict[str, Any]]:
    """Pre-fetch a ``{ticker -> payload}`` dict using paginated
    ``list_markets(status="settled")`` instead of one ``get_market``
    call per ticker.

    Bug #N (settlement-bulk): the per-prediction ``get_market`` loop was
    serialized through the process-level Kalshi rate limiter (5 rps),
    making a 100-prediction batch take ~70s — long enough to starve the
    refresh + prop_refresh workers. ``list_markets`` returns up to 1000
    markets per HTTP round trip, so one page typically covers every
    ticker we need for a batch across all sports.

    Returns an empty dict on ANY failure (timeout, transport error,
    unexpected schema). The caller is expected to fall back to
    per-ticker ``client.get_market`` so settlement still drains a
    batch even when the bulk path is unavailable.

    Returned payloads are intersected with ``tickers_needing_refresh``
    — markets the listing returns for OTHER batches are dropped here
    to avoid polluting downstream telemetry with hits we never asked
    about.
    """
    if not tickers_needing_refresh:
        return {}

    found: dict[str, dict[str, Any]] = {}
    pages_scanned = 0
    try:
        for page_markets, _next_cursor in client.iter_market_pages(
            status="settled",
            limit=_BULK_SETTLEMENT_PAGE_LIMIT,
            mve_filter="exclude",
            max_pages=_BULK_SETTLEMENT_MAX_PAGES,
            wall_clock_budget_seconds=_BULK_SETTLEMENT_WALL_CLOCK_SECONDS,
        ):
            pages_scanned += 1
            for payload in page_markets or []:
                ticker = payload.get("ticker")
                if not ticker or not isinstance(ticker, str):
                    continue
                if ticker not in tickers_needing_refresh:
                    continue
                # First write wins — if a later page somehow returns a
                # duplicate ticker we keep the earlier (typically more
                # recent) entry.
                found.setdefault(ticker, payload)
            # Early exit: if we've already covered every ticker in the
            # batch, don't burn more pages.
            if len(found) >= len(tickers_needing_refresh):
                break
    except Exception:
        # Pattern 6 (data-shape assumptions): degrade gracefully on
        # transport / pagination errors. The caller's per-ticker
        # fallback handles every ticker we didn't return.
        logger.warning(
            "settlement.bulk_fetch.failed pages_scanned=%d requested=%d found=%d",
            pages_scanned,
            len(tickers_needing_refresh),
            len(found),
            exc_info=True,
        )
        return found

    return found


def _settle_prediction_rows(
    db: Session,
    unsettled: list[Prediction],
    *,
    client: KalshiPublicClient,
    open_market_tickers: set[str] | None = None,
) -> dict[str, int]:
    if not unsettled:
        return _empty_settlement_summary()

    summary = _empty_settlement_summary()
    summary["processed"] = len(unsettled)
    open_market_tickers = set(open_market_tickers or set())

    # Bulk pre-fetch: collect every unique ticker that needs an upstream
    # refresh, then ask Kalshi for them in ONE paginated call instead of
    # N per-ticker round trips. ``payload_cache`` is scoped to this
    # function call only — pattern 4 (reduction reuse): no cross-call
    # state means a follow-up batch always re-fetches.
    tickers_needing_refresh: set[str] = set()
    for prediction in unsettled:
        if prediction.market is None:
            continue
        if open_market_tickers and prediction.ticker in open_market_tickers:
            continue
        if prediction.ticker:
            tickers_needing_refresh.add(prediction.ticker)

    bulk_payloads = _bulk_fetch_settled_market_payloads(
        client, tickers_needing_refresh
    )
    # Pre-seed the per-ticker cache with bulk results so the per-prediction
    # loop just looks them up. Each entry is tagged with its source so we
    # can preserve the existing ``settlement_source`` telemetry distinction
    # between bulk-listed and per-ticker fetches.
    payload_cache: dict[str, dict[str, Any]] = dict(bulk_payloads)
    bulk_hit_tickers: set[str] = set(bulk_payloads.keys())

    logger.debug(
        "settlement.bulk_fetch tickers_needing_refresh=%d bulk_hits=%d",
        len(tickers_needing_refresh),
        len(bulk_hit_tickers),
    )

    fallback_count = 0
    for prediction in unsettled:
        market = prediction.market
        if open_market_tickers and prediction.ticker in open_market_tickers:
            summary["pending"] += 1
            continue

        payload = dict(market.raw_data or {}) if market else {}
        settlement_source = "stored_market_raw_data"
        should_refresh = market is not None and (
            not open_market_tickers
            or prediction.ticker not in open_market_tickers
        )

        if should_refresh:
            try:
                latest_payload = payload_cache.get(prediction.ticker)
                bulk_hit = prediction.ticker in bulk_hit_tickers
                if latest_payload is None:
                    latest_payload = client.get_market(prediction.ticker)
                    payload_cache[prediction.ticker] = latest_payload
                    fallback_count += 1
            except Exception:
                summary["errors"] += 1
                if prediction.settlement_status == "pending":
                    summary["pending"] += 1
                else:
                    summary["unresolved"] += 1
                continue
            if latest_payload:
                payload = {**payload, **latest_payload}
                settlement_source = (
                    "kalshi_list_markets" if bulk_hit else "kalshi_get_market"
                )
                if market is not None:
                    market.status = latest_payload.get("status") or market.status
                    close_time = parse_market_datetime(latest_payload.get("close_time"))
                    if close_time is not None:
                        market.close_time = close_time
                    market.raw_data = {**(market.raw_data or {}), **latest_payload}

        resolution = resolve_prediction_settlement(prediction, payload)
        if resolution is None:
            summary["pending"] += 1
            continue

        previous_outcome = prediction.prediction_outcome
        previous_status = prediction.settlement_status

        prediction.settlement_status = resolution.settlement_status
        prediction.prediction_outcome = resolution.prediction_outcome
        prediction.market_result = resolution.market_result
        prediction.winning_side = resolution.winning_side
        prediction.settlement_value = resolution.settlement_value
        prediction.settled_at = resolution.settled_at
        prediction.realized_pnl = resolution.realized_pnl
        prediction.settlement_source = settlement_source
        prediction.settlement_notes = resolution.settlement_notes

        # Smarter #3: capture closing YES price + signed CLV. The lookup
        # tolerates missing snapshots (returns None), so any prediction whose
        # market has no captured history just keeps its existing values.
        # We only fill these once — if a re-settlement pass touches a row
        # already carrying a CLV, leave it alone to preserve the original
        # close.
        if prediction.closing_yes_price is None and market is not None:
            close_cutoff = market.close_time or prediction.settled_at
            closing_yes = closing_yes_price_for_market(db, market.id, before=close_cutoff)
            if closing_yes is not None:
                prediction.closing_yes_price = closing_yes
                prediction.closing_line_value = compute_clv(
                    side=prediction.side,
                    suggested_price=prediction.suggested_price,
                    closing_yes_price=closing_yes,
                )

        if (previous_status, previous_outcome) != (prediction.settlement_status, prediction.prediction_outcome):
            summary["updated"] += 1

        if prediction.prediction_outcome in summary:
            summary[prediction.prediction_outcome] += 1
        elif prediction.prediction_outcome == "won":
            summary["won"] += 1
        elif prediction.prediction_outcome == "lost":
            summary["lost"] += 1
        elif prediction.prediction_outcome == "push":
            summary["push"] += 1
        elif prediction.prediction_outcome == "cancelled":
            summary["cancelled"] += 1
        else:
            summary["unresolved"] += 1

    logger.info(
        "settlement.batch_complete processed=%d updated=%d bulk_hits=%d per_ticker_fallbacks=%d errors=%d",
        summary["processed"],
        summary["updated"],
        len(bulk_hit_tickers),
        fallback_count,
        summary["errors"],
    )

    db.flush()
    return summary


def settle_predictions_batch(
    db: Session,
    *,
    client: KalshiPublicClient | None = None,
    open_market_tickers: set[str] | None = None,
    sport_keys: set[str] | None = None,
    limit: int = 250,
    cursor: dict[str, Any] | None = None,
) -> tuple[dict[str, int], dict[str, Any] | None]:
    unsettled = _load_prediction_settlement_batch(
        db,
        sport_keys=sport_keys,
        limit=limit,
        cursor=cursor,
    )
    if not unsettled:
        return _empty_settlement_summary(), None

    client = client or KalshiPublicClient()
    summary = _settle_prediction_rows(
        db,
        unsettled,
        client=client,
        open_market_tickers=open_market_tickers,
    )
    next_cursor = None
    if len(unsettled) >= limit:
        tail = unsettled[-1]
        next_cursor = {
            "captured_at": tail.captured_at.isoformat() if tail.captured_at is not None else None,
            "prediction_id": tail.id,
        }
    return summary, next_cursor


def settle_predictions(
    db: Session,
    *,
    client: KalshiPublicClient | None = None,
    open_market_tickers: set[str] | None = None,
    sport_keys: set[str] | None = None,
) -> dict[str, int]:
    combined = _empty_settlement_summary()
    cursor: dict[str, Any] | None = None
    while True:
        batch_summary, cursor = settle_predictions_batch(
            db,
            client=client,
            open_market_tickers=open_market_tickers,
            sport_keys=sport_keys,
            limit=250,
            cursor=cursor,
        )
        for key, value in batch_summary.items():
            combined[key] = combined.get(key, 0) + int(value or 0)
        if cursor is None:
            break
    return combined

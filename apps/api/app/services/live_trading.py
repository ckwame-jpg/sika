from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone
from math import ceil, floor, isfinite
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.clients.kalshi import KalshiLiveClient
from app.config import Settings, get_settings
from app.models import (
    AutoTradeControl,
    AutoTradeDecision,
    AutoTradeRun,
    Event,
    KalshiAccountSnapshot,
    LiveFill,
    LiveOrder,
    Market,
    MarketSnapshot,
    Recommendation,
)
from app.services.predictions import OPEN_MARKET_STATUSES
from app.services.trade_desk import load_trade_desk_snapshot
from app.services.watchlist_coverage import is_current_watchlist_market, latest_snapshot_by_market_id

AUTO_TRADE_STRATEGY_KEY = "nba_mlb_current_slate_v1"
AUTO_TRADE_SPORTS = ("NBA", "MLB")
AUTO_TRADE_MARKET_FAMILIES = frozenset({"winner", "game_line", "player_prop"})
SUBMITTABLE_MARKET_STATUSES = frozenset(OPEN_MARKET_STATUSES - {"paused"})
LOW_QUALITY_TIERS = frozenset({"low", "poor", "unsupported"})
AUTO_TRADE_SINGLE_ORDER_CAP_CENTS = 500


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _settings_timezone(settings: Settings | None = None) -> ZoneInfo:
    return ZoneInfo((settings or get_settings()).default_timezone)


def auto_trade_local_date(now: datetime | None = None, settings: Settings | None = None) -> str:
    reference = _as_utc(now) or datetime.now(timezone.utc)
    return reference.astimezone(_settings_timezone(settings)).date().isoformat()


def _local_day_bounds(local_date: str, settings: Settings | None = None) -> tuple[datetime, datetime]:
    local_tz = _settings_timezone(settings)
    year, month, day = [int(part) for part in local_date.split("-", 2)]
    start_local = datetime(year, month, day, tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def kalshi_live_credentials_configured(settings: Settings | None = None) -> bool:
    current = settings or get_settings()
    path = Path(current.kalshi_live_private_key_path)
    return bool(current.kalshi_live_key_id and path.exists())


def get_auto_trade_control(db: Session) -> AutoTradeControl:
    control = db.get(AutoTradeControl, 1)
    if control is None:
        control = AutoTradeControl(id=1, enabled=True, updated_at=datetime.now(timezone.utc))
        db.add(control)
        db.flush()
    return control


def disable_auto_trading(db: Session, *, reason: str = "manual_disable") -> AutoTradeControl:
    control = get_auto_trade_control(db)
    control.enabled = False
    control.disabled_at = datetime.now(timezone.utc)
    control.disabled_reason = reason
    control.updated_at = control.disabled_at
    db.flush()
    return control


def enable_auto_trading(db: Session) -> AutoTradeControl:
    control = get_auto_trade_control(db)
    control.enabled = True
    control.disabled_at = None
    control.disabled_reason = None
    control.updated_at = datetime.now(timezone.utc)
    db.flush()
    return control


def auto_trading_effective_enabled(db: Session, settings: Settings | None = None) -> bool:
    current = settings or get_settings()
    control = get_auto_trade_control(db)
    return bool(current.auto_trading_enabled and control.enabled)


def _parse_cents(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if key.endswith("_dollars") or key.endswith("_dollar"):
            return int(round(value * 100))
        return int(round(value))
    return None


def _parse_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _latest_account_snapshot(db: Session) -> KalshiAccountSnapshot | None:
    return db.scalar(
        select(KalshiAccountSnapshot)
        .where(KalshiAccountSnapshot.environment == "production")
        .order_by(KalshiAccountSnapshot.captured_at.desc(), KalshiAccountSnapshot.id.desc())
        .limit(1)
    )


def capture_kalshi_account_snapshot(
    db: Session,
    *,
    client: KalshiLiveClient | None = None,
) -> KalshiAccountSnapshot:
    kalshi_client = client or KalshiLiveClient()
    balance_payload = kalshi_client.get_balance()
    positions = kalshi_client.list_positions()
    orders = kalshi_client.list_orders()
    balance_cents = _parse_cents(balance_payload, "balance", "balance_cents", "available_balance", "available_balance_cents", "balance_dollars")
    portfolio_value_cents = _parse_cents(
        balance_payload,
        "portfolio_value",
        "portfolio_value_cents",
        "portfolio_value_dollars",
    )
    snapshot = KalshiAccountSnapshot(
        environment="production",
        balance_cents=balance_cents,
        portfolio_value_cents=portfolio_value_cents,
        open_positions_count=len(positions),
        open_orders_count=len([item for item in orders if str(item.get("status") or "").lower() in {"open", "resting", "pending"}]),
        payload={
            "balance": balance_payload,
            "positions": positions,
            "orders": orders,
        },
        captured_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _fill_price(remote_fill: dict[str, Any]) -> float:
    dollars = _parse_float(remote_fill, "yes_price_dollars", "no_price_dollars", "price_dollars")
    if dollars is not None:
        return dollars
    cents = _parse_float(remote_fill, "yes_price", "no_price", "price")
    if cents is not None:
        return cents / 100 if cents > 1 else cents
    return 0.0


def reconcile_live_state(db: Session, *, client: KalshiLiveClient | None = None) -> None:
    kalshi_client = client or KalshiLiveClient()
    orders = kalshi_client.list_orders()
    fills = kalshi_client.list_fills()
    now = datetime.now(timezone.utc)

    local_orders = db.scalars(select(LiveOrder)).all()
    orders_by_client_id = {item.get("client_order_id"): item for item in orders if item.get("client_order_id")}
    orders_by_remote_id = {item.get("order_id"): item for item in orders if item.get("order_id")}
    for local in local_orders:
        remote = orders_by_client_id.get(local.client_order_id) or orders_by_remote_id.get(local.kalshi_order_id)
        if not remote:
            continue
        local.kalshi_order_id = remote.get("order_id") or local.kalshi_order_id
        local.status = remote.get("status") or local.status
        local.response_body = remote
        local.last_synced_at = now

    known_fill_ids = {item[0] for item in db.execute(select(LiveFill.kalshi_fill_id)).all() if item[0]}
    orders_by_kalshi_id = {item.kalshi_order_id: item for item in db.scalars(select(LiveOrder)).all() if item.kalshi_order_id}
    for remote_fill in fills:
        fill_id = remote_fill.get("fill_id")
        order_id = remote_fill.get("order_id")
        if fill_id in known_fill_ids or order_id not in orders_by_kalshi_id:
            continue
        order = orders_by_kalshi_id[order_id]
        db.add(
            LiveFill(
                live_order_id=order.id,
                kalshi_fill_id=fill_id,
                count=float(remote_fill.get("count_fp") or remote_fill.get("count") or 0),
                price=_fill_price(remote_fill),
                side=remote_fill.get("side") or order.side,
                raw_data=remote_fill,
            )
        )
    db.flush()


def _daily_spent_cents(db: Session, local_date: str, settings: Settings | None = None) -> int:
    start_utc, end_utc = _local_day_bounds(local_date, settings)
    submitted_statuses = ("submitted", "resting", "executed", "filled", "partially_filled", "accepted")
    spent = db.scalar(
        select(func.coalesce(func.sum(LiveOrder.max_cost_cents), 0)).where(
            LiveOrder.source == "auto_trade",
            LiveOrder.submitted_at >= start_utc,
            LiveOrder.submitted_at < end_utc,
            LiveOrder.status.in_(submitted_statuses),
        )
    )
    return int(spent or 0)


def _price_cents(limit_price: float) -> int:
    return max(1, min(99, int(ceil(limit_price * 100 - 1e-9))))


def _score_value(recommendation: Recommendation) -> float:
    values = [
        recommendation.selection_score,
        (recommendation.edge or 0.0) * max(recommendation.confidence or 0.0, 0.01),
        recommendation.edge,
        recommendation.confidence,
    ]
    for value in values:
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(numeric) and numeric > 0:
            return numeric
    return 0.01


def _market_payload(recommendation: Recommendation) -> dict[str, Any]:
    market = recommendation.market
    event = recommendation.event
    raw_data = dict(market.raw_data or {}) if market else {}
    starts_at = _as_utc(event.starts_at) if event else None
    return {
        "market_family": raw_data.get("copilot_market_family"),
        "market_kind": raw_data.get("copilot_market_kind"),
        "source_type": raw_data.get("copilot_source_type"),
        "event_name": event.name if event else None,
        "starts_at": starts_at.isoformat() if starts_at else None,
        "quality_tier": dict(recommendation.scoring_diagnostics or {}).get("quality_tier"),
        "selected_side_probability": dict(recommendation.scoring_diagnostics or {}).get("selected_side_probability"),
    }


def _recommendation_skip_reason(
    recommendation: Recommendation,
    *,
    snapshot: MarketSnapshot | None,
    now: datetime,
    settings: Settings,
) -> str | None:
    market = recommendation.market
    event = recommendation.event
    if market is None or event is None:
        return "missing_market_or_event"
    raw_data = dict(market.raw_data or {})
    diagnostics = dict(recommendation.scoring_diagnostics or {})
    family = str(raw_data.get("copilot_market_family") or "")
    if (market.sport_key or "").upper() not in AUTO_TRADE_SPORTS:
        return "outside_market_scope"
    if family not in AUTO_TRADE_MARKET_FAMILIES:
        return "unsupported_market_family"
    if not settings.auto_trading_allow_parlays:
        if str(raw_data.get("copilot_source_type") or "") == "combo_derived":
            return "combo_derived_excluded"
        if str(diagnostics.get("source_type") or "") == "combo_derived":
            return "combo_derived_excluded"
    if (market.status or "").lower() not in SUBMITTABLE_MARKET_STATUSES:
        return "market_not_submittable"
    starts_at = _as_utc(event.starts_at)
    if starts_at is None or starts_at <= now:
        return "event_already_started"
    if not is_current_watchlist_market(market, now=now):
        return "outside_current_slate"
    if snapshot is None:
        return "missing_latest_snapshot"
    if recommendation.action.lower() != "buy":
        return "non_buy_recommendation"
    if recommendation.edge < settings.watchlist_min_edge:
        return "edge_below_threshold"
    if recommendation.confidence < settings.watchlist_min_confidence:
        return "confidence_below_threshold"
    quality_tier = str(diagnostics.get("quality_tier") or "").lower()
    if quality_tier in LOW_QUALITY_TIERS:
        return "low_quality_recommendation"
    if recommendation.suggested_price <= 0 or recommendation.suggested_price >= 1:
        return "invalid_limit_price"
    if (recommendation.selection_score or 0.0) <= 0:
        return "missing_selection_score"
    return None


def _slate_fresh_for_today(db: Session, *, now: datetime, settings: Settings) -> tuple[bool, str | None, dict[str, Any]]:
    snapshot = load_trade_desk_snapshot(db, sport=None)
    if snapshot is None:
        return False, "missing_current_slate_snapshot", {}
    generated_at = _as_utc(snapshot.generated_at)
    payload = {
        "freshness_status": snapshot.freshness_status,
        "generated_at": generated_at.isoformat() if generated_at else None,
        "event_count": snapshot.event_count,
        "candidate_market_count": snapshot.candidate_market_count,
        "recommendation_count": snapshot.recommendation_count,
        "blocking_reason": snapshot.blocking_reason,
        "generated_from_run_id": snapshot.generated_from_run_id,
    }
    if snapshot.freshness_status != "fresh":
        return False, f"current_slate_{snapshot.freshness_status}", payload
    if generated_at is None:
        return False, "missing_current_slate_generated_at", payload
    if generated_at.astimezone(_settings_timezone(settings)).date() != now.astimezone(_settings_timezone(settings)).date():
        return False, "current_slate_not_from_local_day", payload
    return True, None, payload


def _candidate_recommendations(db: Session) -> list[Recommendation]:
    stmt = (
        select(Recommendation)
        .options(joinedload(Recommendation.market).joinedload(Market.event), joinedload(Recommendation.event))
        .join(Market, Recommendation.market_id == Market.id)
        .join(Event, Recommendation.event_id == Event.id)
        .where(
            Recommendation.status == "active",
            Recommendation.action == "buy",
            Market.sport_key.in_(AUTO_TRADE_SPORTS),
        )
        .order_by(
            Recommendation.selection_score.desc().nullslast(),
            Recommendation.edge.desc(),
            Recommendation.confidence.desc(),
            Recommendation.id.asc(),
        )
        .limit(500)
    )
    return list(db.scalars(stmt).unique().all())


def _create_decision(
    db: Session,
    *,
    run: AutoTradeRun,
    recommendation: Recommendation,
    status: str,
    skip_reason: str | None = None,
    quantity: int = 0,
    max_cost_cents: int = 0,
) -> AutoTradeDecision:
    market = recommendation.market
    decision = AutoTradeDecision(
        run_id=run.id,
        recommendation_id=recommendation.id,
        market_id=market.id if market else recommendation.market_id,
        ticker=market.ticker if market else "",
        sport_key=market.sport_key if market else None,
        side=recommendation.side.lower(),
        action=recommendation.action.lower(),
        limit_price=recommendation.suggested_price,
        quantity=quantity,
        max_cost_cents=max_cost_cents,
        edge=recommendation.edge,
        confidence=recommendation.confidence,
        selection_score=recommendation.selection_score,
        status=status,
        skip_reason=skip_reason,
        payload=_market_payload(recommendation),
    )
    db.add(decision)
    db.flush()
    return decision


def run_auto_trade_strategy(
    db: Session,
    *,
    requested_by: str = "scheduled",
    client: KalshiLiveClient | None = None,
    now: datetime | None = None,
) -> AutoTradeRun:
    settings = get_settings()
    reference_now = _as_utc(now) or datetime.now(timezone.utc)
    local_date = auto_trade_local_date(reference_now, settings)
    existing = db.scalar(
        select(AutoTradeRun)
        .options(selectinload(AutoTradeRun.decisions), selectinload(AutoTradeRun.orders))
        .where(
            AutoTradeRun.strategy_key == AUTO_TRADE_STRATEGY_KEY,
            AutoTradeRun.local_trade_date == local_date,
        )
        .limit(1)
    )
    if existing is not None:
        return existing

    run = AutoTradeRun(
        strategy_key=AUTO_TRADE_STRATEGY_KEY,
        local_trade_date=local_date,
        requested_by=requested_by,
        status="running",
        budget_cents=max(0, int(settings.auto_trading_daily_budget_cents or 0)),
        details={
            "market_scope": settings.auto_trading_market_scope,
            "allow_parlays": settings.auto_trading_allow_parlays,
            "max_orders_per_day": settings.auto_trading_max_orders_per_day,
        },
        started_at=reference_now,
    )
    db.add(run)
    db.flush()
    db.commit()
    db.refresh(run)

    def finish(status: str, *, skipped_reason: str | None = None, error_message: str | None = None) -> AutoTradeRun:
        run.status = status
        run.skipped_reason = skipped_reason
        run.error_message = error_message
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        db.commit()
        db.refresh(run)
        return run

    control = get_auto_trade_control(db)
    if not settings.auto_trading_enabled:
        return finish("skipped", skipped_reason="disabled_by_environment")
    if not control.enabled:
        return finish("skipped", skipped_reason="disabled_by_kill_switch")
    if settings.auto_trading_market_scope != "nba_mlb_current_slate":
        return finish("skipped", skipped_reason="unsupported_market_scope")
    if not settings.auto_trading_allow_parlays:
        run.details = {**dict(run.details or {}), "parlays": "excluded"}
    if client is None and not kalshi_live_credentials_configured(settings):
        return finish("skipped", skipped_reason="kalshi_live_credentials_missing")

    slate_is_fresh, slate_skip_reason, slate_payload = _slate_fresh_for_today(db, now=reference_now, settings=settings)
    run.details = {**dict(run.details or {}), "slate": slate_payload}
    if not slate_is_fresh:
        return finish("skipped", skipped_reason=slate_skip_reason or "current_slate_not_fresh")

    account_snapshot: KalshiAccountSnapshot | None = None
    try:
        account_snapshot = capture_kalshi_account_snapshot(db, client=client)
        db.commit()
        db.refresh(account_snapshot)
    except FileNotFoundError as exc:
        return finish("skipped", skipped_reason="kalshi_live_credentials_missing", error_message=str(exc))
    except Exception as exc:
        return finish("failed", error_message=f"Kalshi account snapshot failed: {exc}")

    available_balance_cents = account_snapshot.balance_cents if account_snapshot.balance_cents is not None else 0
    already_spent_cents = _daily_spent_cents(db, local_date, settings)
    remaining_budget_cents = max(run.budget_cents - already_spent_cents, 0)
    available_to_spend_cents = min(remaining_budget_cents, max(available_balance_cents, 0))
    run.details = {
        **dict(run.details or {}),
        "account_snapshot_id": account_snapshot.id,
        "available_balance_cents": available_balance_cents,
        "already_spent_cents": already_spent_cents,
        "available_to_spend_cents": available_to_spend_cents,
    }
    if available_to_spend_cents <= 0:
        return finish("skipped", skipped_reason="daily_budget_or_balance_exhausted")

    recommendations = _candidate_recommendations(db)
    market_ids = [item.market_id for item in recommendations]
    snapshots = latest_snapshot_by_market_id(db, market_ids)
    valid_decisions: list[AutoTradeDecision] = []
    skip_counts: Counter[str] = Counter()
    for recommendation in recommendations:
        skip_reason = _recommendation_skip_reason(
            recommendation,
            snapshot=snapshots.get(recommendation.market_id),
            now=reference_now,
            settings=settings,
        )
        if skip_reason:
            skip_counts[skip_reason] += 1
            _create_decision(
                db,
                run=run,
                recommendation=recommendation,
                status="skipped",
                skip_reason=skip_reason,
            )
            continue
        decision = _create_decision(db, run=run, recommendation=recommendation, status="candidate")
        valid_decisions.append(decision)

    run.candidate_count = len(recommendations)
    run.details = {**dict(run.details or {}), "skip_counts": dict(skip_counts)}
    db.flush()
    db.commit()
    db.refresh(run)

    if not valid_decisions:
        return finish("skipped", skipped_reason="no_eligible_candidates")

    max_orders = max(1, min(int(settings.auto_trading_max_orders_per_day or 1), 5))
    selected = valid_decisions[:max_orders]
    for decision in valid_decisions[max_orders:]:
        decision.status = "skipped"
        decision.skip_reason = "ranked_below_daily_limit"
    db.flush()
    db.commit()

    score_sum = sum(_score_value(decision.recommendation) for decision in selected if decision.recommendation)
    remaining_cents = available_to_spend_cents
    submitted_count = 0
    fatal_error: str | None = None
    kalshi_client = client or KalshiLiveClient()
    for decision in selected:
        recommendation = decision.recommendation
        if recommendation is None:
            decision.status = "skipped"
            decision.skip_reason = "missing_recommendation"
            continue
        price_cents = _price_cents(float(decision.limit_price or recommendation.suggested_price))
        if remaining_cents < price_cents:
            decision.status = "skipped"
            decision.skip_reason = "daily_budget_cap_reached"
            continue
        raw_target = int(round(available_to_spend_cents * (_score_value(recommendation) / score_sum))) if score_sum > 0 else price_cents
        target_cents = min(max(raw_target, price_cents), AUTO_TRADE_SINGLE_ORDER_CAP_CENTS, remaining_cents)
        quantity = floor(target_cents / price_cents)
        if quantity <= 0:
            decision.status = "skipped"
            decision.skip_reason = "target_size_below_one_contract"
            continue
        max_cost_cents = quantity * price_cents
        if max_cost_cents > remaining_cents:
            decision.status = "skipped"
            decision.skip_reason = "daily_budget_cap_reached"
            continue

        client_order_id = f"sika-{local_date.replace('-', '')}-{run.id}-{decision.id}"
        request_body = {
            "ticker": decision.ticker,
            "side": decision.side,
            "action": "buy",
            "client_order_id": client_order_id,
            "count": quantity,
            "type": "limit",
            "time_in_force": "fill_or_kill",
            "buy_max_cost": max_cost_cents,
            "cancel_order_on_pause": True,
            "price_cents": price_cents,
        }
        live_order = LiveOrder(
            environment="production",
            source="auto_trade",
            auto_trade_run_id=run.id,
            market_id=decision.market_id,
            ticker=decision.ticker,
            client_order_id=client_order_id,
            side=decision.side,
            action="buy",
            quantity=quantity,
            limit_price=float(decision.limit_price or recommendation.suggested_price),
            max_cost_cents=max_cost_cents,
            time_in_force="fill_or_kill",
            cancel_order_on_pause=True,
            status="submitting",
            request_body=request_body,
        )
        db.add(live_order)
        db.flush()
        decision.live_order_id = live_order.id
        decision.quantity = quantity
        decision.max_cost_cents = max_cost_cents
        decision.status = "submitting"
        db.flush()
        db.commit()
        db.refresh(live_order)
        db.refresh(decision)

        try:
            response = kalshi_client.create_order(
                ticker=decision.ticker,
                side=decision.side,
                action="buy",
                quantity=quantity,
                limit_price=live_order.limit_price,
                time_in_force="fill_or_kill",
                client_order_id=client_order_id,
                buy_max_cost=max_cost_cents,
                cancel_order_on_pause=True,
                no_resting=True,
                price_format="cents",
            )
        except Exception as exc:
            live_order.status = "submission_failed"
            live_order.last_synced_at = datetime.now(timezone.utc)
            decision.status = "failed"
            decision.skip_reason = "submission_failed"
            fatal_error = str(exc).strip() or exc.__class__.__name__
            db.flush()
            db.commit()
            break

        remote_order = response.get("order", {})
        live_order.kalshi_order_id = remote_order.get("order_id")
        live_order.client_order_id = remote_order.get("client_order_id") or live_order.client_order_id
        live_order.status = remote_order.get("status") or "submitted"
        live_order.request_body = response.get("request") or request_body
        live_order.response_body = response
        live_order.submitted_at = datetime.now(timezone.utc)
        live_order.last_synced_at = live_order.submitted_at
        decision.status = "submitted"
        remaining_cents -= max_cost_cents
        submitted_count += 1
        run.spent_cents += max_cost_cents
        run.submitted_order_count = submitted_count
        db.flush()
        db.commit()

    if fatal_error:
        return finish("failed", error_message=f"Kalshi live order submission failed: {fatal_error}")
    if submitted_count <= 0:
        return finish("skipped", skipped_reason="no_orders_submitted")
    return finish("completed")


def auto_trading_status(db: Session) -> dict[str, Any]:
    settings = get_settings()
    local_date = auto_trade_local_date(settings=settings)
    control = get_auto_trade_control(db)
    spent_today = _daily_spent_cents(db, local_date, settings)
    latest_run = db.scalar(
        select(AutoTradeRun)
        .options(selectinload(AutoTradeRun.decisions), selectinload(AutoTradeRun.orders))
        .order_by(AutoTradeRun.started_at.desc(), AutoTradeRun.id.desc())
        .limit(1)
    )
    latest_snapshot = _latest_account_snapshot(db)
    return {
        "enabled_by_env": settings.auto_trading_enabled,
        "kill_switch_active": not control.enabled,
        "effective_enabled": bool(settings.auto_trading_enabled and control.enabled),
        "daily_budget_cents": settings.auto_trading_daily_budget_cents,
        "spent_today_cents": spent_today,
        "remaining_budget_cents": max(int(settings.auto_trading_daily_budget_cents or 0) - spent_today, 0),
        "max_orders_per_day": settings.auto_trading_max_orders_per_day,
        "local_trade_date": local_date,
        "local_run_time": settings.auto_trading_local_time,
        "market_scope": settings.auto_trading_market_scope,
        "allow_parlays": settings.auto_trading_allow_parlays,
        "live_credentials_configured": kalshi_live_credentials_configured(settings),
        "latest_run": latest_run,
        "latest_account_snapshot": latest_snapshot,
    }


def live_account_state(
    db: Session,
    *,
    refresh: bool = False,
    client: KalshiLiveClient | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    snapshot = _latest_account_snapshot(db)
    if refresh and (client is not None or kalshi_live_credentials_configured(settings)):
        snapshot = capture_kalshi_account_snapshot(db, client=client)
        try:
            reconcile_live_state(db, client=client)
        except Exception:
            pass
        db.flush()
    orders = db.scalars(
        select(LiveOrder)
        .options(selectinload(LiveOrder.fills))
        .order_by(desc(LiveOrder.submitted_at), desc(LiveOrder.id))
        .limit(50)
    ).all()
    fills = db.scalars(select(LiveFill).order_by(desc(LiveFill.created_at), desc(LiveFill.id)).limit(100)).all()
    return {
        "environment": "production",
        "credentials_configured": kalshi_live_credentials_configured(settings),
        "snapshot": snapshot,
        "live_orders": orders,
        "live_fills": fills,
    }


def parse_auto_trade_local_time(settings: Settings | None = None) -> time:
    value = (settings or get_settings()).auto_trading_local_time.strip()
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(hour=max(0, min(23, int(hour_text))), minute=max(0, min(59, int(minute_text))))
    except (TypeError, ValueError):
        return time(hour=10, minute=15)

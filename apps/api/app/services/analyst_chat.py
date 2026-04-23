from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DemoOrder, LiveOrder, Market, PaperPosition, Recommendation, Run
from app.services.live_trading import auto_trading_status, live_account_state
from app.services.ml.readiness import build_model_readiness_summary
from app.services.stats_query import StatsQueryService
from app.services.trade_desk import load_trade_desk_snapshot

logger = logging.getLogger(__name__)

TRADE_PLACEMENT_TERMS = (
    "place a trade",
    "place trade",
    "place an order",
    "place order",
    "buy ",
    "sell ",
    "cancel order",
    "cancel my order",
    "trade for me",
    "bet for me",
)
TRADE_DESK_TERMS = (
    "pick",
    "picks",
    "watchlist",
    "board",
    "slate",
    "market",
    "markets",
    "recommendation",
    "recommendations",
    "edge",
)
MODEL_READINESS_TERMS = (
    "readiness",
    "shadow",
    "fallback",
    "runtime",
    "model",
    "ml",
    "family",
    "calibration",
)
AUTO_TRADING_TERMS = (
    "auto-trade",
    "autotrade",
    "auto trade",
    "run",
    "runs",
    "skipped",
    "budget",
    "kill switch",
    "max orders",
)
PORTFOLIO_TERMS = (
    "portfolio",
    "account",
    "position",
    "positions",
    "balance",
    "fill",
    "fills",
    "live order",
    "open order",
)
DEFAULT_EVIDENCE_BUCKETS = ("trade_desk", "model_readiness", "auto_trading", "portfolio")
RESEARCH_CONTEXT_LIMIT = 18_000


@dataclass(frozen=True, slots=True)
class ResearchQueryResult:
    message: str
    model: str
    context: dict[str, Any]
    citations: list[dict[str, str]]
    used_web_search: bool
    mode: str


def _safe_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _normalize_sport_key(sport_key: str | None) -> str | None:
    if sport_key is None:
        return None
    normalized = sport_key.strip().upper()
    return normalized or None


def _compact_recommendations(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Recommendation)
        .join(Market, Recommendation.market_id == Market.id)
        .where(Recommendation.status == "active")
        .order_by(
            Recommendation.selection_score.desc().nullslast(),
            Recommendation.edge.desc(),
            Recommendation.confidence.desc(),
        )
        .limit(10)
    ).all()
    payload: list[dict[str, Any]] = []
    for item in rows:
        market = item.market
        raw_data = dict(market.raw_data or {}) if market else {}
        payload.append(
            {
                "ticker": market.ticker if market else None,
                "sport_key": market.sport_key if market else None,
                "market_title": market.title if market else None,
                "market_family": raw_data.get("copilot_market_family"),
                "market_kind": raw_data.get("copilot_market_kind"),
                "side": item.side,
                "suggested_price": item.suggested_price,
                "edge": item.edge,
                "confidence": item.confidence,
                "selection_score": item.selection_score,
                "quality_tier": dict(item.scoring_diagnostics or {}).get("quality_tier"),
            }
        )
    return payload


def _compact_recent_runs(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Run)
        .order_by(Run.started_at.desc(), Run.id.desc())
        .limit(5)
    ).all()
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "status": item.status,
            "started_at": _safe_iso(item.started_at),
            "finished_at": _safe_iso(item.finished_at),
            "records_processed": item.records_processed,
            "error_message": item.error_message,
        }
        for item in rows
    ]


def _compact_model_readiness(db: Session) -> list[dict[str, Any]]:
    readiness = build_model_readiness_summary(db)
    return [
        {
            "family_key": family.get("family_key"),
            "effective_mode": family.get("effective_mode"),
            "readiness_state": family.get("readiness_state"),
            "runtime_health": family.get("runtime_health"),
            "fallback_active": family.get("fallback_active"),
            "pending_predictions": family.get("pending_predictions"),
            "settled_predictions": family.get("settled_predictions"),
        }
        for family in list(readiness.get("families") or [])[:12]
        if isinstance(family, dict)
    ]


def _compact_auto_status(db: Session) -> dict[str, Any]:
    status = auto_trading_status(db)
    latest_run = status.get("latest_run")
    latest_snapshot = status.get("latest_account_snapshot")
    return {
        "enabled_by_env": status["enabled_by_env"],
        "kill_switch_active": status["kill_switch_active"],
        "effective_enabled": status["effective_enabled"],
        "daily_budget_cents": status["daily_budget_cents"],
        "spent_today_cents": status["spent_today_cents"],
        "remaining_budget_cents": status["remaining_budget_cents"],
        "local_trade_date": status["local_trade_date"],
        "market_scope": status["market_scope"],
        "allow_parlays": status["allow_parlays"],
        "live_credentials_configured": status["live_credentials_configured"],
        "latest_run": (
            {
                "id": latest_run.id,
                "status": latest_run.status,
                "local_trade_date": latest_run.local_trade_date,
                "spent_cents": latest_run.spent_cents,
                "submitted_order_count": latest_run.submitted_order_count,
                "skipped_reason": latest_run.skipped_reason,
                "error_message": latest_run.error_message,
            }
            if latest_run is not None
            else None
        ),
        "latest_account_snapshot": (
            {
                "id": latest_snapshot.id,
                "balance_cents": latest_snapshot.balance_cents,
                "open_positions_count": latest_snapshot.open_positions_count,
                "open_orders_count": latest_snapshot.open_orders_count,
                "captured_at": _safe_iso(latest_snapshot.captured_at),
            }
            if latest_snapshot is not None
            else None
        ),
    }


def _compact_live_account(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = state.get("snapshot")
    live_orders = state.get("live_orders") or []
    live_fills = state.get("live_fills") or []
    return {
        "credentials_configured": bool(state.get("credentials_configured")),
        "snapshot": (
            {
                "id": snapshot.id,
                "balance_cents": snapshot.balance_cents,
                "portfolio_value_cents": snapshot.portfolio_value_cents,
                "open_positions_count": snapshot.open_positions_count,
                "open_orders_count": snapshot.open_orders_count,
                "captured_at": _safe_iso(snapshot.captured_at),
            }
            if snapshot is not None
            else None
        ),
        "live_orders": [
            {
                "id": order.id,
                "ticker": order.ticker,
                "status": order.status,
                "side": order.side,
                "action": order.action,
                "quantity": order.quantity,
                "limit_price": order.limit_price,
                "submitted_at": _safe_iso(order.submitted_at),
            }
            for order in live_orders[:5]
        ],
        "live_fills": [
            {
                "id": fill.id,
                "live_order_id": fill.live_order_id,
                "price": fill.price,
                "count": fill.count,
                "side": fill.side,
                "created_at": _safe_iso(fill.created_at),
            }
            for fill in live_fills[:5]
        ],
    }


def _compact_portfolio(db: Session, state: dict[str, Any]) -> dict[str, Any]:
    snapshot = state.get("snapshot")
    live_order_count = db.scalar(select(func.count()).select_from(LiveOrder)) or 0
    paper_position_count = db.scalar(select(func.count()).select_from(PaperPosition)) or 0
    demo_order_count = db.scalar(select(func.count()).select_from(DemoOrder)) or 0
    return {
        "paper_position_count": int(paper_position_count),
        "demo_order_count": int(demo_order_count),
        "live_order_count": int(live_order_count),
        "latest_live_account_snapshot_id": snapshot.id if snapshot is not None else None,
    }


def _compact_stats_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": result.get("question"),
        "sport_key": result.get("sport_key"),
        "entity_name": result.get("entity_name"),
        "team_name": result.get("team_name"),
        "query_type": result.get("query_type"),
        "season": result.get("season"),
        "games_requested": result.get("games_requested"),
        "games_analyzed": result.get("games_analyzed"),
        "summary": result.get("summary"),
        "explanation": result.get("explanation"),
        "coverage_note": result.get("coverage_note"),
        "source": result.get("source"),
        "game_logs": list(result.get("game_logs") or [])[:5],
    }


def _classify_evidence_buckets(message: str, sport_key: str | None = None) -> list[str]:
    lowered = f" {message.lower()} "
    buckets: list[str] = []
    if any(term in lowered for term in TRADE_DESK_TERMS):
        buckets.append("trade_desk")
    if any(term in lowered for term in MODEL_READINESS_TERMS):
        buckets.append("model_readiness")
    if any(term in lowered for term in AUTO_TRADING_TERMS):
        buckets.append("auto_trading")
    if any(term in lowered for term in PORTFOLIO_TERMS):
        buckets.append("portfolio")
    if sport_key:
        buckets.append("stats_query")
    if not buckets:
        buckets.extend(DEFAULT_EVIDENCE_BUCKETS)
    return list(dict.fromkeys(buckets))


def build_research_context(
    db: Session,
    *,
    message: str,
    sport_key: str | None = None,
    season: int | None = None,
    include_web: bool = True,
) -> dict[str, Any]:
    normalized_sport = _normalize_sport_key(sport_key)
    buckets = _classify_evidence_buckets(message, normalized_sport)
    context: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": {
            "sport_key": normalized_sport,
            "season": season,
            "include_web": include_web,
        },
        "evidence_buckets": buckets,
    }

    if "trade_desk" in buckets:
        trade_desk = load_trade_desk_snapshot(db, sport=None)
        context["trade_desk"] = (
            {
                "freshness_status": trade_desk.freshness_status,
                "generated_at": _safe_iso(trade_desk.generated_at),
                "event_count": trade_desk.event_count,
                "recommendation_count": trade_desk.recommendation_count,
                "blocking_reason": trade_desk.blocking_reason,
            }
            if trade_desk is not None
            else {"freshness_status": "missing"}
        )
        context["recommendations"] = _compact_recommendations(db)

    if "model_readiness" in buckets:
        context["model_readiness"] = _compact_model_readiness(db)

    account_state: dict[str, Any] | None = None
    if "auto_trading" in buckets or "portfolio" in buckets:
        account_state = live_account_state(db, refresh=False)

    if "auto_trading" in buckets:
        context["auto_trading"] = _compact_auto_status(db)
        context["recent_runs"] = _compact_recent_runs(db)

    if "portfolio" in buckets and account_state is not None:
        context["portfolio"] = _compact_portfolio(db, account_state)
        context["live_account"] = _compact_live_account(account_state)

    if "stats_query" in buckets and normalized_sport:
        try:
            stats_result = StatsQueryService().query(
                message,
                sport_key=normalized_sport,
                season=season,
            )
        except (LookupError, ValueError) as exc:
            context["stats_query_error"] = str(exc)
        except Exception as exc:  # pragma: no cover - defensive external-provider guard
            context["stats_query_error"] = f"Stats query failed: {exc}"
        else:
            context["stats_query"] = _compact_stats_result(stats_result)

    return context


def build_analyst_context(db: Session) -> dict[str, Any]:
    return build_research_context(
        db,
        message="",
        sport_key=None,
        season=None,
        include_web=True,
    )


def _extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    parts: list[str] = []
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _append_citation(
    citations: list[dict[str, str]],
    seen: set[tuple[str, str]],
    *,
    title: str | None,
    url: str | None,
) -> None:
    clean_title = (title or "").strip()
    clean_url = (url or "").strip()
    if not clean_url:
        return
    if not clean_title:
        clean_title = clean_url
    key = (clean_title, clean_url)
    if key in seen:
        return
    seen.add(key)
    citations.append({"title": clean_title, "url": clean_url})


def _extract_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.get("output") or []:
        if item.get("type") == "message":
            for content in item.get("content") or []:
                for annotation in content.get("annotations") or []:
                    if annotation.get("type") != "url_citation":
                        continue
                    _append_citation(
                        citations,
                        seen,
                        title=annotation.get("title"),
                        url=annotation.get("url"),
                    )
        if item.get("type") == "web_search_call":
            action = item.get("action")
            if isinstance(action, dict):
                for source in action.get("sources") or []:
                    _append_citation(
                        citations,
                        seen,
                        title=source.get("title") if isinstance(source, dict) else None,
                        url=source.get("url") if isinstance(source, dict) else None,
                    )
    return citations


def _used_web_search(payload: dict[str, Any]) -> bool:
    return any(item.get("type") == "web_search_call" for item in payload.get("output") or [])


def _responses_payload(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    include_web: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if include_web:
        payload["tools"] = [{"type": "web_search"}]
        payload["tool_choice"] = "auto"
        payload["include"] = ["web_search_call.action.sources"]
    return payload


def _call_responses_api(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    include_web: bool,
) -> dict[str, Any]:
    settings = get_settings()
    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        json=_responses_payload(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            include_web=include_web,
        ),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def _request_type(message: str, context: dict[str, Any]) -> str:
    normalized = f" {message.lower()} "
    if any(term in normalized for term in TRADE_PLACEMENT_TERMS):
        return "policy_block"
    if "stats_query" in context or "stats_query_error" in context:
        return "stats_query"
    return "general"


def query_site_research(
    db: Session,
    *,
    message: str,
    sport_key: str | None = None,
    season: int | None = None,
    include_web: bool = True,
) -> ResearchQueryResult:
    started_at = perf_counter()
    normalized = f" {message.lower()} "
    context = build_research_context(
        db,
        message=message,
        sport_key=sport_key,
        season=season,
        include_web=include_web,
    )
    request_type = _request_type(message, context)

    if any(term in normalized for term in TRADE_PLACEMENT_TERMS):
        logger.info(
            "research_query_completed",
            extra={
                "event": "research_query_completed",
                "request_type": request_type,
                "mode": "internal_only",
                "used_web_search": False,
                "citation_count": 0,
                "fallback_used": False,
                "failure_class": None,
                "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            },
        )
        return ResearchQueryResult(
            message="I can explain picks, risk, model readiness, account snapshots, and auto-trade runs, but I cannot create, cancel, or modify orders from chat.",
            model="policy",
            context=context,
            citations=[],
            used_web_search=False,
            mode="internal_only",
        )

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    system_prompt = (
        "You are Sika's read-only research analyst for operators. "
        "Use the provided Sika context to explain current market recommendations, model readiness, "
        "auto-trading runs, portfolio/account state, and optional stats-query results. "
        "If web results are available, use them only for current external context such as injuries, "
        "lineup news, or market-moving headlines. Clearly separate Sika-internal facts from web-derived facts. "
        "Never claim to place, cancel, submit, approve, or modify trades or orders. "
        "If the user asks to trade or bet, refuse briefly and redirect to read-only analysis."
    )
    user_prompt = (
        "Sika context JSON:\n"
        f"{json.dumps(context, default=str)[:RESEARCH_CONTEXT_LIMIT]}\n\n"
        f"Operator question: {message}"
    )

    fallback_used = False
    failure_class: str | None = None
    mode = "internal_only"
    used_web_search = False
    citations: list[dict[str, str]] = []
    payload: dict[str, Any]

    try:
        payload = _call_responses_api(
            model=settings.openai_chat_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            include_web=include_web,
        )
    except Exception as exc:
        failure_class = type(exc).__name__
        if not include_web:
            logger.exception(
                "research_query_failed",
                extra={
                    "event": "research_query_failed",
                    "request_type": request_type,
                    "mode": "internal_only",
                    "used_web_search": False,
                    "citation_count": 0,
                    "fallback_used": False,
                    "failure_class": failure_class,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                },
            )
            raise
        fallback_used = True
        logger.warning(
            "research_query_web_retry",
            extra={
                "event": "research_query_web_retry",
                "request_type": request_type,
                "failure_class": failure_class,
            },
        )
        payload = _call_responses_api(
            model=settings.openai_chat_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            include_web=False,
        )
        mode = "internal_fallback"
    else:
        used_web_search = _used_web_search(payload)
        if include_web and used_web_search:
            mode = "internal_plus_web"

    citations = _extract_citations(payload)
    if mode == "internal_fallback":
        used_web_search = False
    elif not used_web_search:
        mode = "internal_only"

    message_text = _extract_response_text(payload)
    if not message_text:
        message_text = "I could not produce a research response from the current context."

    duration_ms = round((perf_counter() - started_at) * 1000, 2)
    logger.info(
        "research_query_completed",
        extra={
            "event": "research_query_completed",
            "request_type": request_type,
            "mode": mode,
            "used_web_search": used_web_search,
            "citation_count": len(citations),
            "fallback_used": fallback_used,
            "failure_class": failure_class,
            "duration_ms": duration_ms,
        },
    )
    return ResearchQueryResult(
        message=message_text,
        model=settings.openai_chat_model,
        context=context,
        citations=citations,
        used_web_search=used_web_search,
        mode=mode,
    )


def ask_site_analyst(
    db: Session,
    *,
    message: str,
    sport_key: str | None = None,
    season: int | None = None,
    include_web: bool = True,
) -> ResearchQueryResult:
    return query_site_research(
        db,
        message=message,
        sport_key=sport_key,
        season=season,
        include_web=include_web,
    )


def research_result_payload(result: ResearchQueryResult) -> dict[str, Any]:
    return asdict(result)

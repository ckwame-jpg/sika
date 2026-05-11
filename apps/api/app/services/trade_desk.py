from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import CurrentSlateSnapshot, Event, EventParticipant, Market, Prediction, Recommendation, Run
from app.schemas import (
    SportAvailabilityRead,
    TradeDeskArchivedSlateRead,
    TradeDeskEventRead,
    TradeDeskGameLineRead,
    TradeDeskPlayerPropRead,
    TradeDeskResponse,
    TradeDeskStatGroupRead,
    TradeDeskThresholdRead,
)
from app.services.watchlist_coverage import (
    CURRENT_WATCHLIST_SPORTS,
    TERMINAL_EVENT_STATUSES,
    current_watchlist_event_ids,
    current_watchlist_markets,
    is_current_watchlist_market,
    is_current_watchlist_status,
)
from app.sports.base import alias_tokens

KALSHI_SPORT_CATEGORY_ROOTS = {
    "NBA": "https://kalshi.com/category/sports/basketball/pro-basketball-m",
    "MLB": "https://kalshi.com/category/sports/baseball/pro-baseball",
    "NFL": "https://kalshi.com/category/sports/football/pro-football",
    "SOCCER": "https://kalshi.com/category/sports/soccer",
    "TENNIS": "https://kalshi.com/category/sports/tennis",
}
KALSHI_EVENT_SERIES = {
    "NBA": ("kxnbagame", "professional-basketball-game"),
    "MLB": ("kxmlbgame", "professional-baseball-game"),
}
KALSHI_PROP_CATEGORY_SLUGS = {
    "NBA": {
        "points": "player-points",
        "rebounds": "player-rebounds",
        "assists": "player-assists",
        "made_threes": "player-threes",
        "steals": "player-steals",
        "blocks": "player-blocks",
        "turnovers": "player-turnovers",
    },
    "MLB": {
        "hits": "hits",
        "runs": "runs",
        "home_runs": "home-runs",
        "rbis": "rbis",
        "strikeouts": "strikeouts",
        "walks": "walks",
        "total_bases": "total-bases",
    },
}
SNAPSHOT_SCOPE_ALL = "all"
PRODUCT_SLATE_EMPTY_REASON = "Current slate scored successfully, but no markets cleared recommendation thresholds."
PRODUCT_SLATE_NO_CANDIDATES_REASON = "Current NBA/MLB events exist, but no current Kalshi markets are mapped to them."
PRODUCT_SLATE_FILTERED_BEFORE_SCORING_REASON = (
    "Current slate candidate markets were filtered before scoring; no current open supported markets reached the scorer."
)
PRODUCT_SLATE_UNSCORED_REASON = "Current slate markets exist, but none were scored successfully."


def visible_sports() -> list[str]:
    return [sport.upper() for sport in get_settings().enabled_sports if sport.upper() != "UFC"]


def sport_order(sport_key: str | None) -> int:
    sport_map = {sport: index for index, sport in enumerate(visible_sports())}
    return sport_map.get((sport_key or "").upper(), len(sport_map))


def latest_successful_refresh_at(db: Session) -> datetime | None:
    return db.scalar(
        select(Run.finished_at)
        .where(Run.kind == "refresh", Run.status == "completed", Run.finished_at.is_not(None))
        .order_by(Run.finished_at.desc(), Run.id.desc())
        .limit(1)
    )


def sport_availability_rows(db: Session) -> list[SportAvailabilityRead]:
    visible = visible_sports()
    now = datetime.now(timezone.utc)
    recent_window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    event_counts = {
        sport_key: int(count or 0)
        for sport_key, count in db.execute(
            select(Event.sport_key, func.count(Event.id))
            .where(
                Event.sport_key.in_(tuple(visible)),
                Event.status.notin_(tuple(TERMINAL_EVENT_STATUSES)),
                Event.starts_at >= recent_window_start,
            )
            .group_by(Event.sport_key)
        ).all()
    }
    recommendation_counts = {
        sport_key: int(count or 0)
        for sport_key, count in db.execute(
            select(Market.sport_key, func.count(Recommendation.id))
            .join(Market, Recommendation.market_id == Market.id)
            .where(Market.sport_key.in_(tuple(visible)))
            .group_by(Market.sport_key)
        ).all()
    }
    last_refresh_at = latest_successful_refresh_at(db)
    rows: list[SportAvailabilityRead] = []
    for sport_key in visible:
        rows.append(
            SportAvailabilityRead(
                sport_key=sport_key,
                availability_mode="live" if sport_key in CURRENT_WATCHLIST_SPORTS else "research_only",
                events_count=event_counts.get(sport_key, 0),
                recommendations_count=recommendation_counts.get(sport_key, 0),
                last_refresh_at=last_refresh_at,
            )
        )
    return rows


def normalize_snapshot_scope(sport: str | None) -> str:
    normalized = (sport or "").upper().strip()
    if not normalized:
        return SNAPSHOT_SCOPE_ALL
    return normalized


def _participant_token_score(lookup_text: str, display_name: str, short_name: str | None = None) -> float:
    left_tokens = alias_tokens(lookup_text)
    right_tokens = alias_tokens(display_name, short_name)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    if not shared:
        return 0.0
    strong_shared = [token for token in shared if len(token) >= 4]
    return min(1.0, (len(shared) * 0.15) + (len(strong_shared) * 0.2))


def trade_desk_market_matches_event(market: Market) -> bool:
    if market.event is None:
        return False
    raw_data = market.raw_data or {}
    if str(raw_data.get("copilot_market_family") or "") == "player_prop":
        return True
    lookup_text = " ".join(
        part
        for part in [
            market.title,
            market.subtitle or "",
            str(raw_data.get("copilot_source_market_title") or ""),
            market.event_ticker or "",
        ]
        if part
    )
    participant_matches = 0
    for entry in market.event.participants:
        participant = entry.participant
        if _participant_token_score(lookup_text, participant.display_name, participant.short_name) >= 0.15:
            participant_matches += 1
    return participant_matches >= 2


def _kalshi_event_url(market: Market) -> str | None:
    sport_key = str(market.sport_key or "").upper()
    series = KALSHI_EVENT_SERIES.get(sport_key)
    event_ticker = str(market.event_ticker or (market.raw_data or {}).get("event_ticker") or market.ticker or "").strip()
    _, separator, suffix = event_ticker.partition("-")
    if not series or not separator or not suffix:
        return None

    series_ticker, series_slug = series
    return f"https://kalshi.com/markets/{series_ticker}/{series_slug}/{series_ticker}-{suffix.lower()}"


def kalshi_market_url(market: Market) -> str:
    event_url = _kalshi_event_url(market)
    if event_url:
        return event_url

    sport_key = str(market.sport_key or "").upper()
    category_root = KALSHI_SPORT_CATEGORY_ROOTS.get(sport_key)
    if not category_root:
        return "https://kalshi.com/markets"

    raw_data = market.raw_data or {}
    stat_key = str(raw_data.get("copilot_stat_key") or "").strip()
    stat_slug = KALSHI_PROP_CATEGORY_SLUGS.get(sport_key, {}).get(stat_key)
    if stat_slug:
        return f"{category_root}/{stat_slug}"
    return category_root


def _signed_numeric_line(market_kind: str, raw_data: dict, selected_side: str) -> float | None:
    """Return the threshold value for a game-line market, pre-signed from
    the picked side's perspective.

    Conventions chosen to match game_line_projected_label below — that
    label flips ``-{threshold}`` vs ``+{threshold}`` based on
    ``selected_side``. We mirror that flip here so the frontend can use
    the value directly without re-reading selected_side.

    - spread + yes (favored)   → ``-threshold`` (team must win by more)
    - spread + no  (dog)        → ``+threshold`` (team can lose by less)
    - total  + yes (over)       → ``+threshold``
    - total  + no  (under)      → ``-threshold``
    - moneyline / first_five_winner / unknown → None (no number to chart)
    """
    threshold = raw_data.get("copilot_threshold")
    if threshold is None:
        return None
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return None
    side = (selected_side or "").lower()
    if market_kind == "spread":
        return -value if side == "yes" else value
    if market_kind == "total":
        return value if side == "yes" else -value
    return None


def game_line_projected_label(market: Market, recommendation: Recommendation) -> str | None:
    raw_data = market.raw_data or {}
    diagnostics = dict(recommendation.scoring_diagnostics or {})
    market_kind = str(raw_data.get("copilot_market_kind") or "")
    threshold = raw_data.get("copilot_threshold")
    direction = str(raw_data.get("copilot_direction") or "over").lower()
    subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
    selected_side = recommendation.side.lower()

    if market_kind in {"game_winner", "first_five_winner"}:
        return str(diagnostics.get("selected_subject_name") or subject_name or "").strip() or None
    if market_kind == "spread":
        if not subject_name:
            return None
        if threshold is None:
            return subject_name
        if selected_side == "yes":
            return f"{subject_name} -{float(threshold):g}"
        return f"{subject_name} +{float(threshold):g}"
    if market_kind == "total":
        selected_direction = direction if selected_side == "yes" else ("under" if direction == "over" else "over")
        if threshold is None:
            return selected_direction.title()
        return f"{selected_direction.title()} {float(threshold):g}"
    return str(diagnostics.get("selected_subject_name") or "").strip() or None


def game_line_display_label(market: Market, recommendation: Recommendation) -> str:
    raw_data = market.raw_data or {}
    market_kind = str(raw_data.get("copilot_market_kind") or "")
    if market_kind in {"game_winner", "first_five_winner"}:
        projected = game_line_projected_label(market, recommendation)
        if projected:
            return f"{projected} to win"
    return str(
        dict(recommendation.scoring_diagnostics or {}).get("display_market_title")
        or raw_data.get("copilot_display_market_title")
        or market.title
    )


def thresholds_are_monotonic(thresholds: list[TradeDeskThresholdRead]) -> bool:
    for index in range(1, len(thresholds)):
        if thresholds[index].probability_yes > thresholds[index - 1].probability_yes:
            return False
    return True


def _latest_trade_recommendations(db: Session, *, sport: str | None = None) -> list[Recommendation]:
    normalized_sport = sport.upper() if sport else None
    # Bug #8: rank by captured_at to survive out-of-order inserts.
    ranked = (
        select(
            Recommendation,
            func.row_number()
            .over(
                partition_by=Recommendation.market_id,
                order_by=(Recommendation.captured_at.desc(), Recommendation.id.desc()),
            )
            .label("rn"),
        )
        .join(Market, Recommendation.market_id == Market.id)
        .where(
            Recommendation.status == "active",
            Market.event_id.is_not(None),
            Market.sport_key.in_(tuple(CURRENT_WATCHLIST_SPORTS)),
        )
        .subquery()
    )
    stmt = (
        select(Recommendation)
        .join(ranked, Recommendation.id == ranked.c.id)
        .where(ranked.c.rn == 1)
        .options(
            joinedload(Recommendation.market)
            .joinedload(Market.event)
            .selectinload(Event.participants)
            .joinedload(EventParticipant.participant)
        )
    )
    if normalized_sport in CURRENT_WATCHLIST_SPORTS:
        stmt = stmt.join(Market, Recommendation.market_id == Market.id).where(Market.sport_key == normalized_sport)
    return list(db.scalars(stmt).all())


def _recommendation_count_from_events(events: list[TradeDeskEventRead]) -> int:
    count = 0
    for event in events:
        count += len(event.game_lines)
        for player in event.player_props:
            for stat_group in player.stat_groups:
                count += len(stat_group.thresholds)
    return count


def _event_market_stat_totals(events: list[TradeDeskEventRead]) -> tuple[int, int]:
    scored_market_count = 0
    coverage_prediction_count = 0
    for event in events:
        scored_market_count += event.scored_market_count
        coverage_prediction_count += event.coverage_prediction_count
    return scored_market_count, coverage_prediction_count


def _current_event_market_stats(
    db: Session,
    *,
    sport: str | None = None,
    source_run_id: int | None = None,
) -> dict[int, dict[str, object]]:
    markets = current_watchlist_markets(db, sport=sport)
    stats: dict[int, dict[str, object]] = {}
    market_event_ids: dict[int, int] = {}
    for market in markets:
        if market.event is None or market.event_id is None:
            continue
        if not trade_desk_market_matches_event(market):
            continue
        market_event_ids[market.id] = market.event_id
        bucket = stats.setdefault(
            market.event_id,
            {
                "event": market.event,
                "candidate_market_count": 0,
                "scored_market_ids": set(),
                "coverage_market_ids": set(),
            },
        )
        bucket["candidate_market_count"] = int(bucket["candidate_market_count"]) + 1

    if not market_event_ids:
        return stats

    market_ids = list(market_event_ids)
    if source_run_id is None:
        # Bug #8: rank by captured_at to survive out-of-order inserts.
        ranked = (
            select(
                Prediction,
                func.row_number()
                .over(
                    partition_by=Prediction.market_id,
                    order_by=(Prediction.captured_at.desc(), Prediction.id.desc()),
                )
                .label("rn"),
            )
            .where(Prediction.market_id.in_(tuple(market_ids)))
            .subquery()
        )
        predictions = db.scalars(
            select(Prediction).join(ranked, Prediction.id == ranked.c.id).where(ranked.c.rn == 1)
        ).all()
    else:
        predictions = db.scalars(
            select(Prediction)
            .where(
                Prediction.run_id == source_run_id,
                Prediction.market_id.in_(tuple(market_ids)),
            )
            .order_by(Prediction.id.asc())
        ).all()

    for prediction in predictions:
        if prediction.market_id is None:
            continue
        event_id = market_event_ids.get(prediction.market_id)
        if event_id is None:
            continue
        bucket = stats.get(event_id)
        if bucket is None:
            continue
        scored_market_ids = bucket["scored_market_ids"]
        coverage_market_ids = bucket["coverage_market_ids"]
        assert isinstance(scored_market_ids, set)
        assert isinstance(coverage_market_ids, set)
        scored_market_ids.add(prediction.market_id)
        if (prediction.capture_scope or "recommendation") == "coverage":
            coverage_market_ids.add(prediction.market_id)

    return stats


def _prediction_counts_for_run(db: Session, *, run_id: int | None, sport: str | None = None) -> tuple[int, int]:
    if run_id is None:
        return 0, 0
    stmt = select(Prediction.capture_scope, func.count(Prediction.id)).where(Prediction.run_id == run_id)
    if sport:
        stmt = stmt.where(Prediction.sport_key == sport)
    rows = db.execute(stmt.group_by(Prediction.capture_scope)).all()
    by_scope = {str(scope or ""): int(count or 0) for scope, count in rows}
    scored = sum(by_scope.values())
    coverage = int(by_scope.get("coverage") or 0)
    return scored, coverage


def _classify_product_slate(
    *,
    event_count: int,
    candidate_market_count: int,
    scored_market_count: int,
    recommendation_count: int,
    coverage_prediction_count: int,
    loaded_candidate_market_count: int | None = None,
    filtered_candidate_market_count: int = 0,
) -> tuple[str, str | None]:
    if event_count <= 0:
        return "fresh", None
    if candidate_market_count <= 0:
        return "degraded", PRODUCT_SLATE_NO_CANDIDATES_REASON
    if (
        loaded_candidate_market_count is not None
        and loaded_candidate_market_count <= 0
        and filtered_candidate_market_count > 0
    ):
        return "degraded", PRODUCT_SLATE_FILTERED_BEFORE_SCORING_REASON
    if scored_market_count <= 0:
        return "degraded", PRODUCT_SLATE_UNSCORED_REASON
    if recommendation_count <= 0:
        if coverage_prediction_count > 0:
            return "empty", PRODUCT_SLATE_EMPTY_REASON
        return "degraded", PRODUCT_SLATE_UNSCORED_REASON
    return "fresh", None


def _candidate_loading_counts_for_run(db: Session, *, run_id: int | None) -> tuple[int | None, int]:
    if run_id is None:
        return None, 0
    run = db.get(Run, run_id)
    if run is None:
        return None, 0
    details = dict(run.details or {})
    watchlist_summary = dict(details.get("watchlist_summary") or {})
    loaded_raw = details.get("current_slate_loaded_candidate_market_count", watchlist_summary.get("loaded_candidate_market_count"))
    filtered_raw = details.get(
        "current_slate_filtered_candidate_market_count",
        watchlist_summary.get("filtered_candidate_market_count"),
    )
    return (int(loaded_raw) if loaded_raw is not None else None), int(filtered_raw or 0)


def _apply_product_slate_health(
    db: Session,
    response: TradeDeskResponse,
    *,
    scope: str,
    source_run_id: int | None,
) -> None:
    sport = None if scope == SNAPSHOT_SCOPE_ALL else scope
    event_count = len(current_watchlist_event_ids(db, sport=sport))
    candidate_market_count = len(current_watchlist_markets(db, sport=sport))
    scored_market_count, coverage_prediction_count = _prediction_counts_for_run(
        db,
        run_id=source_run_id,
        sport=sport,
    )
    loaded_candidate_market_count, filtered_candidate_market_count = _candidate_loading_counts_for_run(
        db,
        run_id=source_run_id,
    )
    recommendation_count = _recommendation_count_from_events(response.events)
    if source_run_id is None:
        event_scored_market_count, event_coverage_prediction_count = _event_market_stat_totals(response.events)
        if event_scored_market_count > 0:
            scored_market_count = event_scored_market_count
        if event_coverage_prediction_count > 0:
            coverage_prediction_count = event_coverage_prediction_count
    if scored_market_count <= 0 and source_run_id is None and recommendation_count > 0:
        scored_market_count = recommendation_count

    status, blocking_reason = _classify_product_slate(
        event_count=event_count,
        candidate_market_count=candidate_market_count,
        scored_market_count=scored_market_count,
        recommendation_count=recommendation_count,
        coverage_prediction_count=coverage_prediction_count,
        loaded_candidate_market_count=loaded_candidate_market_count,
        filtered_candidate_market_count=filtered_candidate_market_count,
    )
    response.event_count = event_count
    response.candidate_market_count = candidate_market_count
    response.scored_market_count = scored_market_count
    response.recommendation_count = recommendation_count
    response.coverage_prediction_count = coverage_prediction_count
    response.freshness_status = status  # type: ignore[assignment]
    response.blocking_reason = blocking_reason
    response.generated_from_run_id = source_run_id


def build_trade_desk_response(
    db: Session,
    *,
    sport: str | None = None,
    source_run_id: int | None = None,
) -> TradeDeskResponse:
    normalized_sport = sport.upper() if sport else None
    availability_rows = sorted(sport_availability_rows(db), key=lambda item: sport_order(item.sport_key))
    if normalized_sport and normalized_sport not in CURRENT_WATCHLIST_SPORTS:
        research_rows = [
            row for row in availability_rows if row.sport_key == normalized_sport and row.availability_mode == "research_only"
        ]
        return TradeDeskResponse(events=[], research_sports=research_rows)

    recommendations = _latest_trade_recommendations(
        db,
        sport=normalized_sport if normalized_sport in CURRENT_WATCHLIST_SPORTS else None,
    )
    event_market_stats = _current_event_market_stats(
        db,
        sport=normalized_sport if normalized_sport in CURRENT_WATCHLIST_SPORTS else None,
        source_run_id=source_run_id,
    )

    event_buckets: dict[int, dict[str, object]] = {}
    for recommendation in recommendations:
        market = recommendation.market
        if market is None or market.event is None:
            continue
        if recommendation.edge <= 0:
            raw_data = market.raw_data or {}
            diagnostics = dict(recommendation.scoring_diagnostics or {})
            is_monotonicity_adjusted_prop = (
                str(raw_data.get("copilot_market_family") or "") == "player_prop"
                and diagnostics.get("monotonicity_adjusted") is True
            )
            if not is_monotonicity_adjusted_prop:
                continue
        if not is_current_watchlist_market(market):
            continue
        if not trade_desk_market_matches_event(market):
            continue

        raw_data = market.raw_data or {}
        family = str(raw_data.get("copilot_market_family") or "")
        bucket = event_buckets.setdefault(
            market.event.id,
            {
                "event": market.event,
                "game_lines": [],
                "props": {},
            },
        )

        if family == "player_prop":
            if recommendation.side.lower() != "yes":
                continue
            subject_name = str(raw_data.get("copilot_subject_name") or "").strip()
            subject_team = str(raw_data.get("copilot_subject_team") or "").strip() or None
            stat_key = str(raw_data.get("copilot_stat_key") or "").strip()
            threshold = raw_data.get("copilot_threshold")
            selected_probability = dict(recommendation.scoring_diagnostics or {}).get("selected_side_probability")
            if not subject_name or not stat_key or threshold is None or selected_probability is None:
                continue

            player_map = bucket["props"]
            assert isinstance(player_map, dict)
            player_key = (subject_name, subject_team)
            stat_map = player_map.setdefault(player_key, {})
            assert isinstance(stat_map, dict)
            thresholds = stat_map.setdefault(stat_key, [])
            assert isinstance(thresholds, list)
            thresholds.append(
                TradeDeskThresholdRead(
                    ticker=market.ticker,
                    threshold=float(threshold),
                    probability_yes=float(selected_probability),
                    selected_side=recommendation.side,
                    selected_side_probability=float(selected_probability),
                    entry_price=recommendation.suggested_price,
                    edge=recommendation.edge,
                    confidence=recommendation.confidence,
                    kalshi_url=kalshi_market_url(market),
                )
            )
            continue

        if family not in {"winner", "game_line"}:
            continue

        selected_probability = dict(recommendation.scoring_diagnostics or {}).get("selected_side_probability")
        if selected_probability is None:
            continue
        game_lines = bucket["game_lines"]
        assert isinstance(game_lines, list)
        market_kind = str(raw_data.get("copilot_market_kind") or "")
        numeric_line = _signed_numeric_line(market_kind, raw_data, recommendation.side)
        game_lines.append(
            TradeDeskGameLineRead(
                ticker=market.ticker,
                market_title=market.title,
                display_label=game_line_display_label(market, recommendation),
                sport_key=market.sport_key,
                market_kind=market_kind,
                selected_side=recommendation.side,
                projected_side_label=game_line_projected_label(market, recommendation),
                selected_side_probability=float(selected_probability),
                entry_price=recommendation.suggested_price,
                edge=recommendation.edge,
                confidence=recommendation.confidence,
                kalshi_url=kalshi_market_url(market),
                numeric_line=numeric_line,
            )
        )

    for event_id, stats in event_market_stats.items():
        event = stats.get("event")
        if not isinstance(event, Event):
            continue
        event_buckets.setdefault(
            event_id,
            {
                "event": event,
                "game_lines": [],
                "props": {},
            },
        )

    events: list[TradeDeskEventRead] = []
    game_line_order = {"game_winner": 0, "first_five_winner": 0, "spread": 1, "total": 2}
    for bucket in event_buckets.values():
        event = bucket["event"]
        game_lines = bucket["game_lines"]
        props = bucket["props"]
        assert isinstance(event, Event)
        assert isinstance(game_lines, list)
        assert isinstance(props, dict)

        player_props: list[TradeDeskPlayerPropRead] = []
        for (subject_name, subject_team), stat_map in props.items():
            assert isinstance(stat_map, dict)
            stat_groups: list[TradeDeskStatGroupRead] = []
            best_edge = 0.0
            best_win_prob: float | None = None
            for stat_key, thresholds in stat_map.items():
                assert isinstance(thresholds, list)
                thresholds.sort(key=lambda item: item.threshold)
                for idx in range(1, len(thresholds)):
                    if thresholds[idx].probability_yes > thresholds[idx - 1].probability_yes:
                        clamped = thresholds[idx - 1].probability_yes
                        thresholds[idx].probability_yes = clamped
                        if thresholds[idx].selected_side_probability is not None:
                            thresholds[idx].selected_side_probability = clamped
                        # Bug #7: edge was originally computed against the
                        # pre-clamp probability — leaving it stale shows the
                        # operator a more attractive trade than the model
                        # actually believes in, and biases best_index toward
                        # the clamped row. Recompute against entry_price.
                        if thresholds[idx].entry_price is not None:
                            thresholds[idx].edge = round(clamped - thresholds[idx].entry_price, 4)
                best_index = max(range(len(thresholds)), key=lambda index: thresholds[index].edge)
                thresholds[best_index].is_best = True
                best_edge = max(best_edge, thresholds[best_index].edge)
                group_win_prob = max((threshold.probability_yes for threshold in thresholds), default=0.0)
                best_win_prob = group_win_prob if best_win_prob is None else max(best_win_prob, group_win_prob)
                stat_groups.append(
                    TradeDeskStatGroupRead(
                        stat_key=str(stat_key),
                        thresholds=thresholds,
                    )
                )
            if stat_groups:
                player_props.append(
                    TradeDeskPlayerPropRead(
                        subject_name=str(subject_name),
                        subject_team=subject_team,
                        stat_groups=stat_groups,
                        best_edge=best_edge,
                        best_win_prob=best_win_prob,
                    )
                )

        player_props.sort(key=lambda item: (-item.best_edge, item.subject_name.lower()))
        stats = event_market_stats.get(event.id, {})
        scored_market_ids = stats.get("scored_market_ids", set())
        coverage_market_ids = stats.get("coverage_market_ids", set())
        if not isinstance(scored_market_ids, set):
            scored_market_ids = set()
        if not isinstance(coverage_market_ids, set):
            coverage_market_ids = set()
        candidate_market_count = int(stats.get("candidate_market_count") or 0)
        scored_market_count = len(scored_market_ids)
        coverage_prediction_count = len(coverage_market_ids)
        sorted_game_lines = sorted(
            game_lines,
            key=lambda item: (
                game_line_order.get(item.market_kind, 99),
                -item.edge,
                item.display_label.lower(),
            ),
        )
        if not sorted_game_lines and not player_props and candidate_market_count <= 0:
            continue
        events.append(
            TradeDeskEventRead(
                event_id=event.id,
                event_name=event.name,
                event_status=event.status,
                starts_at=event.starts_at,
                sport_key=event.sport_key,
                candidate_market_count=candidate_market_count,
                scored_market_count=scored_market_count,
                coverage_prediction_count=coverage_prediction_count,
                game_lines=sorted_game_lines,
                player_props=player_props,
            )
        )

    events.sort(
        key=lambda item: (
            sport_order(item.sport_key),
            item.starts_at or datetime.max.replace(tzinfo=timezone.utc),
            item.event_name.lower(),
        )
    )
    research_sports = [
        row
        for row in availability_rows
        if row.availability_mode == "research_only" and (normalized_sport is None or row.sport_key == normalized_sport)
    ]
    response = TradeDeskResponse(
        events=events,
        research_sports=research_sports,
        generated_at=None,
        freshness_status="fresh",
    )
    _apply_product_slate_health(
        db,
        response,
        scope=SNAPSHOT_SCOPE_ALL if normalized_sport is None else normalized_sport,
        source_run_id=source_run_id,
    )
    return response


def persist_current_slate_snapshots(
    db: Session,
    *,
    source_run_id: int | None,
    generated_at: datetime | None = None,
) -> dict[str, datetime]:
    """Append a new snapshot row per scope.

    Slice 2 made this table versioned and append-only: every call inserts a
    fresh ``CurrentSlateSnapshot`` row and leaves the prior row in place. The
    read path (``load_trade_desk_snapshot``) selects the latest row by
    ``generated_at``, so a mid-phase crash on the write side can never empty
    the product — readers continue serving the previous snapshot until a
    newer one lands. Retention of old rows lives in ``prune_runtime_artifacts``.
    """
    timestamp = generated_at or datetime.now(timezone.utc)
    persisted: dict[str, datetime] = {}
    for scope in (SNAPSHOT_SCOPE_ALL, *sorted(CURRENT_WATCHLIST_SPORTS)):
        response = build_trade_desk_response(
            db,
            sport=None if scope == SNAPSHOT_SCOPE_ALL else scope,
            source_run_id=source_run_id,
        )
        # Stamp freshness metadata directly into the persisted payload so that
        # the stored snapshot is self-describing regardless of DB-row fields.
        response.generated_at = timestamp
        _apply_product_slate_health(
            db,
            response,
            scope=scope,
            source_run_id=source_run_id,
        )
        snapshot = CurrentSlateSnapshot(
            scope=scope,
            source_run_id=source_run_id,
            generated_at=timestamp,
            payload=response.model_dump(mode="json"),
        )
        db.add(snapshot)
        persisted[scope] = timestamp
    db.flush()
    return persisted


def _snapshot_response(
    db: Session,
    snapshot: CurrentSlateSnapshot,
    *,
    scope: str,
) -> TradeDeskResponse:
    response = TradeDeskResponse.model_validate(snapshot.payload)
    if response.generated_at is None and snapshot.generated_at is not None:
        row_generated_at = snapshot.generated_at
        if row_generated_at.tzinfo is None:
            row_generated_at = row_generated_at.replace(tzinfo=timezone.utc)
        response.generated_at = row_generated_at
    if response.generated_from_run_id is None:
        response.generated_from_run_id = snapshot.source_run_id
    if (
        response.event_count == 0
        and response.candidate_market_count == 0
        and response.scored_market_count == 0
        and response.recommendation_count == 0
        and response.coverage_prediction_count == 0
        and response.blocking_reason is None
    ):
        _apply_product_slate_health(
            db,
            response,
            scope=scope,
            source_run_id=snapshot.source_run_id,
        )
    return response


def _has_stale_snapshot_events(response: TradeDeskResponse) -> bool:
    for event in response.events:
        if event.sport_key not in CURRENT_WATCHLIST_SPORTS:
            continue
        if not is_current_watchlist_status(event.event_status, event.starts_at):
            return True
    return False


def _latest_prior_useful_snapshot(
    db: Session,
    *,
    scope: str,
    latest: CurrentSlateSnapshot,
) -> CurrentSlateSnapshot | None:
    rows = db.scalars(
        select(CurrentSlateSnapshot)
        .where(CurrentSlateSnapshot.scope == scope)
        .where(
            (CurrentSlateSnapshot.generated_at < latest.generated_at)
            | (
                (CurrentSlateSnapshot.generated_at == latest.generated_at)
                & (CurrentSlateSnapshot.id < latest.id)
            )
        )
        .order_by(CurrentSlateSnapshot.generated_at.desc(), CurrentSlateSnapshot.id.desc())
    ).all()
    for row in rows:
        if not row.payload:
            continue
        candidate = TradeDeskResponse.model_validate(row.payload)
        if candidate.events and _recommendation_count_from_events(candidate.events) > 0:
            return row
    return None


def _archived_slate_from_response(response: TradeDeskResponse) -> TradeDeskArchivedSlateRead:
    recommendation_count = response.recommendation_count or _recommendation_count_from_events(response.events)
    scored_market_count = response.scored_market_count or recommendation_count
    candidate_market_count = response.candidate_market_count or scored_market_count
    return TradeDeskArchivedSlateRead(
        events=response.events,
        generated_at=response.generated_at,
        freshness_status="stale",
        event_count=response.event_count or len(response.events),
        candidate_market_count=candidate_market_count,
        scored_market_count=scored_market_count,
        recommendation_count=recommendation_count,
        coverage_prediction_count=response.coverage_prediction_count,
        blocking_reason=response.blocking_reason,
        generated_from_run_id=response.generated_from_run_id,
    )


def load_trade_desk_snapshot(db: Session, *, sport: str | None = None) -> TradeDeskResponse | None:
    """Return the most recent snapshotted trade-desk response for ``sport``.

    Unlike earlier versions of this function, a snapshot that contains stale
    events is still returned — with ``freshness_status="stale"`` — instead of
    being suppressed. Suppression here previously caused the route handler to
    fall back to a live ``Recommendation`` read, which is precisely the
    behaviour we want to avoid when the refresh pipeline is stuck: stale data
    with a visible freshness flag is strictly better than silently serving
    whatever the write path has left behind on the live tables.
    """
    normalized_sport = (sport or "").upper().strip() or None
    if normalized_sport and normalized_sport not in CURRENT_WATCHLIST_SPORTS:
        return build_trade_desk_response(db, sport=normalized_sport)
    scope = normalize_snapshot_scope(normalized_sport)
    # Slice 2: the table is versioned per scope. Read the latest row by
    # generated_at, breaking ties on id so two writes at the same instant
    # still return a deterministic winner.
    snapshot = db.scalar(
        select(CurrentSlateSnapshot)
        .where(CurrentSlateSnapshot.scope == scope)
        .order_by(CurrentSlateSnapshot.generated_at.desc(), CurrentSlateSnapshot.id.desc())
        .limit(1)
    )
    if snapshot is None or not snapshot.payload:
        return None
    response = _snapshot_response(db, snapshot, scope=scope)
    if _has_stale_snapshot_events(response):
        response.freshness_status = "stale"
    if response.freshness_status in {"degraded", "empty"} or response.recommendation_count <= 0:
        prior = _latest_prior_useful_snapshot(db, scope=scope, latest=snapshot)
        if prior is not None:
            prior_response = _snapshot_response(db, prior, scope=scope)
            prior_response.freshness_status = "stale"
            response.previous_slate = _archived_slate_from_response(prior_response)
    return response

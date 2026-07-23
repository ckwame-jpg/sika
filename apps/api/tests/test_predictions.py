from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import EspnPlayerGamelogCache, EspnPlayerSearchCache, Event, EventParticipant, Market, MarketSnapshot, Participant, Prediction, SignalSnapshot
from app.services.ingestion import (
    _refresh_combo_prop_discovery_batch,
    refresh_current_slate_kalshi_markets,
    refresh_kalshi_markets,
    run_prop_refresh_cycle,
    run_refresh_cycle,
)
from app.services.predictions import capture_prediction, settle_predictions
from app.services.scoring import PropStatsResolver, warm_prop_context_cache
from app.services.watchlist_coverage import warm_current_watchlist_prop_context


def _seed_team_event(db_session, *, sport_key: str = "NBA", external_id: str = "evt-1"):
    home = Participant(
        external_id=f"{external_id}-home",
        sport_key=sport_key,
        display_name="Boston Celtics" if sport_key == "NBA" else "Seattle Mariners",
        short_name="Celtics" if sport_key == "NBA" else "Mariners",
        participant_type="team",
    )
    away = Participant(
        external_id=f"{external_id}-away",
        sport_key=sport_key,
        display_name="Brooklyn Nets" if sport_key == "NBA" else "New York Yankees",
        short_name="Nets" if sport_key == "NBA" else "Yankees",
        participant_type="team",
    )
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id=external_id,
        sport_key=sport_key,
        name=f"{away.display_name} at {home.display_name}",
        status="scheduled",
        starts_at=datetime(2026, 4, 2, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )
    db_session.flush()
    return event


def _create_prediction(
    db_session,
    *,
    ticker: str,
    side: str = "yes",
    sport_key: str = "NBA",
    market_family: str = "player_prop",
    market_kind: str = "player_prop",
    stat_key: str = "points",
    outcome: str = "pending",
    settlement_status: str = "pending",
    captured_at: datetime | None = None,
    suggested_price: float = 0.42,
    edge: float = 0.08,
    confidence: float = 0.66,
):
    event = _seed_team_event(db_session, sport_key=sport_key, external_id=f"{ticker}-event")
    market = Market(
        ticker=ticker,
        sport_key=sport_key,
        event_id=event.id,
        title=f"{ticker} title",
        status="active",
        raw_data={
            "copilot_market_family": market_family,
            "copilot_market_kind": market_kind,
            "copilot_stat_key": stat_key,
            "copilot_threshold": 30.0 if sport_key == "NBA" else 2.0,
            "copilot_subject_name": "Jalen Brunson" if sport_key == "NBA" else "Aaron Judge",
            "copilot_subject_team": "NYK" if sport_key == "NBA" else "NYY",
        },
    )
    db_session.add(market)
    db_session.flush()

    prediction = Prediction(
        event_id=event.id,
        market_id=market.id,
        ticker=ticker,
        sport_key=sport_key,
        event_name=event.name,
        market_title=market.title,
        market_family=market_family,
        market_kind=market_kind,
        stat_key=stat_key,
        threshold=30.0 if sport_key == "NBA" else 2.0,
        subject_name="Jalen Brunson" if sport_key == "NBA" else "Aaron Judge",
        subject_team="NYK" if sport_key == "NBA" else "NYY",
        side=side,
        action="buy",
        suggested_price=suggested_price,
        fair_yes_price=0.55,
        fair_no_price=0.45,
        edge=edge,
        confidence=confidence,
        model_name="heuristic-v1",
        invalidation="Pull if price moves away from fair value.",
        rationale="Snapshot rationale",
        reasons=["Reason 1", "Reason 2"],
        features={"sample_size": 8},
        market_status_at_capture="active",
        settlement_status=settlement_status,
        prediction_outcome=outcome,
        captured_at=captured_at or datetime(2026, 4, 2, 1, 0, tzinfo=timezone.utc),
    )
    db_session.add(prediction)
    db_session.flush()
    return prediction


class FakeSportsProvider:
    def fetch_events_window(self, sport_name, start_day, end_day):
        if sport_name != "Basketball":
            return []
        now = datetime(2026, 3, 30, 18, 0, tzinfo=timezone.utc)
        games = []
        for index in range(4):
            games.append(
                {
                    "idEvent": f"nba-past-{index}",
                    "idLeague": "4387",
                    "strLeague": "NBA",
                    "strHomeTeam": "Boston Celtics",
                    "strAwayTeam": "Brooklyn Nets",
                    "idHomeTeam": "celtics",
                    "idAwayTeam": "nets",
                    "strEvent": "Brooklyn Nets at Boston Celtics",
                    "strTimestamp": (now - timedelta(days=4 - index)).isoformat().replace("+00:00", "Z"),
                    "dateEvent": (now - timedelta(days=4 - index)).date().isoformat(),
                    "strStatus": "completed",
                    "intHomeScore": "115",
                    "intAwayScore": "101",
                }
            )
        games.append(
            {
                "idEvent": "nba-future-1",
                "idLeague": "4387",
                "strLeague": "NBA",
                "strHomeTeam": "Boston Celtics",
                "strAwayTeam": "Brooklyn Nets",
                "idHomeTeam": "celtics",
                "idAwayTeam": "nets",
                "strEvent": "Brooklyn Nets at Boston Celtics",
                "strTimestamp": (now + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
                "dateEvent": (now + timedelta(hours=6)).date().isoformat(),
                "strStatus": "scheduled",
                "intHomeScore": None,
                "intAwayScore": None,
            }
        )
        return games


class FakeKalshiRefreshClient:
    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        return [
            {
                "ticker": "KXNBAGAME-26MAR30BOSBKN-BOS",
                "event_ticker": "KXNBAGAME-26MAR30BOSBKN",
                "title": "Brooklyn at Boston Winner?",
                "subtitle": "NBA regular season",
                "status": "active",
                "close_time": "2026-04-13T23:30:00Z",
                "expected_expiration_time": "2026-03-31T00:00:00Z",
                "yes_sub_title": "Boston",
                "no_sub_title": "Brooklyn",
                "yes_ask_dollars": "0.42",
                "no_ask_dollars": "0.62",
                "last_price_dollars": "0.43",
            }
        ]


class FakeSettlementClient:
    def __init__(self, payloads):
        self.payloads = payloads

    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        return []

    def get_market(self, ticker):
        return self.payloads[ticker]


class CurrentSlateTargetClient:
    def __init__(self):
        self.get_market_calls: list[str] = []
        self.list_market_calls = 0

    def get_market(self, ticker):
        self.get_market_calls.append(ticker)
        return {
            "ticker": ticker,
            "event_ticker": "KXNBAGAME-CURRENT",
            "title": "Brooklyn at Boston Winner?",
            "subtitle": "NBA regular season",
            "status": "active",
            "yes_sub_title": "Boston",
            "no_sub_title": "Brooklyn",
            "yes_ask_dollars": "0.42",
            "no_ask_dollars": "0.62",
            "last_price_dollars": "0.43",
        }

    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        self.list_market_calls += 1
        return []


class FakeKalshiComboClient:
    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        if mve_filter == "exclude":
            return []
        return [
            {
                "ticker": "KXMVE-NBA-PROPS-1",
                "event_ticker": "KXMVE-NBA-PROPS",
                "title": "NBA prop combo",
                "status": "active",
                "close_time": "2026-04-13T23:30:00Z",
                "mve_collection_ticker": "KXMVE-NBA-PROPS-COLLECTION",
                "mve_selected_legs": [
                    {
                        "event_ticker": "KXNBAPTS-26APR05NYKBOS",
                        "market_ticker": "KXNBAPTS-26APR05NYKBOS-NYKJBRUNSON11-30",
                        "side": "yes",
                    }
                ],
            }
        ]

    def get_market(self, ticker):
        assert ticker == "KXNBAPTS-26APR05NYKBOS-NYKJBRUNSON11-30"
        return {
            "ticker": ticker,
            "event_ticker": "KXNBAPTS-26APR05NYKBOS",
            "title": "Jalen Brunson: 30+ points?",
            "subtitle": "Boston at New York",
            "status": "active",
            "close_time": "2026-04-13T23:30:00Z",
            "yes_ask_dollars": "0.41",
            "no_ask_dollars": "0.61",
            "last_price_dollars": "0.42",
        }


class FakeKalshiMixedComboClient:
    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        if mve_filter == "exclude":
            return []
        return [
            {
                "ticker": "KXMVE-NBA-MIXED-1",
                "event_ticker": "KXMVE-NBA-MIXED",
                "title": "yes Boston,yes Bam Adebayo: 15+",
                "status": "active",
                "close_time": "2026-04-13T23:30:00Z",
                "mve_collection_ticker": "KXMVE-NBA-MIXED-COLLECTION",
                "mve_selected_legs": [
                    {
                        "event_ticker": "KXNBAGAME-26APR05BOSNYK",
                        "market_ticker": "KXNBAGAME-26APR05BOSNYK-BOS",
                        "side": "yes",
                    },
                    {
                        "event_ticker": "KXNBAPTS-26APR04WASMIA",
                        "market_ticker": "KXNBAPTS-26APR04WASMIA-MIABADEBAYO13-15",
                        "side": "yes",
                    },
                ],
            }
        ]

    def get_market(self, ticker):
        if ticker == "KXNBAGAME-26APR05BOSNYK-BOS":
            return {
                "ticker": ticker,
                "event_ticker": "KXNBAGAME-26APR05BOSNYK",
                "title": "New York at Boston Winner?",
                "subtitle": "Knicks at Celtics",
                "status": "active",
                "close_time": "2026-04-13T23:30:00Z",
                "yes_sub_title": "Boston",
                "yes_ask_dollars": "0.62",
                "no_ask_dollars": "0.42",
                "last_price_dollars": "0.58",
            }
        assert ticker == "KXNBAPTS-26APR04WASMIA-MIABADEBAYO13-15"
        return {
            "ticker": ticker,
            "event_ticker": "KXNBAPTS-26APR04WASMIA",
            "title": "Bam Adebayo: 15+ points",
            "subtitle": "Washington at Miami",
            "status": "active",
            "close_time": "2026-04-13T23:30:00Z",
            "yes_ask_dollars": "0.44",
            "no_ask_dollars": "0.58",
            "last_price_dollars": "0.45",
            "yes_sub_title": "Bam Adebayo: 15+",
            "rules_primary": "If Bam Adebayo records 15+ Points in the Washington at Miami professional basketball game, then the market resolves to Yes.",
            "primary_participant_key": "basketball_player",
        }


NBA_PROP_GAMELOG_PAYLOAD = {
    "names": [
        "minutes",
        "points",
        "totalRebounds",
        "assists",
        "steals",
        "blocks",
        "turnovers",
        "fieldGoalsMade-fieldGoalsAttempted",
        "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
        "freeThrowsMade-freeThrowsAttempted",
    ],
    "events": {
        f"evt-{index}": {
            "gameDate": f"2026-04-{index + 1:02d}T00:00Z",
            "opponent": {"displayName": "Boston Celtics", "abbreviation": "BOS"},
            "atVs": "vs",
            "team": {"displayName": "New York Knicks"},
            "gameResult": "W",
        }
        for index in range(5)
    },
    "seasonTypes": [
        {
            "categories": [
                {
                    "events": [
                        {
                            "eventId": f"evt-{index}",
                            "stats": ["34", "31", "4", "7", "1", "0", "2", "11-20", "3-7", "6-7"],
                        }
                        for index in range(5)
                    ]
                }
            ]
        }
    ],
}


class CountingEspnPropClient:
    def __init__(self):
        self.search_calls: list[tuple[str, str]] = []
        self.gamelog_calls: list[tuple[str, str, int]] = []

    def search_player(self, query: str, sport_key: str = "NBA", *, team_hint: str | None = None):
        self.search_calls.append((query, sport_key))
        return {
            "athlete_id": "3934672",
            "display_name": "Jalen Brunson",
            "team_name": "New York Knicks",
        }

    def fetch_player_gamelog(self, sport_key: str, athlete_id: str, season: int):
        self.gamelog_calls.append((sport_key, athlete_id, season))
        return NBA_PROP_GAMELOG_PAYLOAD


class TrackedComboRefreshClient:
    def __init__(self):
        self.include_calls = 0
        self.get_market_calls: list[str] = []

    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        if mve_filter == "include":
            self.include_calls += 1
        return []

    def get_market(self, ticker):
        self.get_market_calls.append(ticker)
        return {
            "ticker": ticker,
            "event_ticker": "KXNBAPTS-26APR05NYKBOS",
            "title": "Jalen Brunson: 30+ points",
            "subtitle": "Boston at New York",
            "status": "active",
            "close_time": "2026-04-13T23:30:00Z",
            "yes_ask_dollars": "0.39",
            "no_ask_dollars": "0.63",
            "last_price_dollars": "0.41",
        }


class ComboDiscoveryPrefilterClient:
    def __init__(self):
        self.get_market_calls: list[str] = []

    def list_markets_page(self, status="open", limit=50, mve_filter="include", cursor=None):
        assert mve_filter == "include"
        return (
            [
                {
                    "ticker": "KXMVE-MIXED-1",
                    "title": "mixed combo",
                    "mve_collection_ticker": "KXMVE-MIXED-COLLECTION",
                    "mve_selected_legs": [
                        {"market_ticker": "KXEPLGAME-26APR11ARSBOU-ARS"},
                        {"market_ticker": "KXNBATOTAL-26APR10CLEATL-219"},
                        {
                            "event_ticker": "KXNBAPTS-26APR10DETCHA",
                            "market_ticker": "KXNBAPTS-26APR10DETCHA-CHAMBRIDGES0-10",
                        },
                    ],
                }
            ],
            None,
        )

    def get_market(self, ticker):
        self.get_market_calls.append(ticker)
        assert ticker == "KXNBAPTS-26APR10DETCHA-CHAMBRIDGES0-10"
        return {
            "ticker": ticker,
            "event_ticker": "KXNBAPTS-26APR10DETCHA",
            "title": "Miles Bridges: 10+ points?",
            "subtitle": "Detroit at Charlotte",
            "status": "active",
            "close_time": "2026-04-13T23:30:00Z",
            "yes_ask_dollars": "0.39",
            "no_ask_dollars": "0.63",
            "last_price_dollars": "0.41",
            "yes_sub_title": "Miles Bridges: 10+",
            "rules_primary": "If Miles Bridges records 10+ Points in the Detroit at Charlotte professional basketball game, then the market resolves to Yes.",
            "primary_participant_key": "basketball_player",
        }


def test_prediction_model_persists_snapshot_fields(db_session):
    prediction = _create_prediction(db_session, ticker="KXNBA-PERSIST-1")
    db_session.commit()

    stored = db_session.scalar(select(Prediction).where(Prediction.id == prediction.id))
    assert stored is not None
    assert stored.ticker == "KXNBA-PERSIST-1"
    assert stored.subject_name == "Jalen Brunson"
    assert stored.reasons == ["Reason 1", "Reason 2"]
    assert stored.prediction_outcome == "pending"


def test_capture_prediction_supports_coverage_scope_without_recommendation(db_session):
    event = _seed_team_event(db_session, sport_key="NBA", external_id="coverage-prediction")
    market = Market(
        ticker="KXNBA-COVERAGE-ONLY-1",
        sport_key="NBA",
        event_id=event.id,
        title="Jalen Brunson: 30+ points?",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 30.0,
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
        },
    )
    signal = SignalSnapshot(
        event_id=event.id,
        market_id=None,
        model_name="heuristic-v1",
        confidence=0.68,
        fair_yes_price=0.59,
        fair_no_price=0.41,
        edge=0.12,
        selection_score=0.14,
        reasons=["Coverage prediction for the current slate."],
        features={"uses_stale_prop_context": True},
        scoring_diagnostics={
            "selected_side": "yes",
            "suggested_price": 0.47,
            "invalidation": "Pull if YES entry moves above 0.6300",
            "selected_side_probability": 0.59,
        },
    )
    db_session.add_all([market, signal])
    db_session.flush()

    prediction = capture_prediction(
        db_session,
        run_id=12,
        event=event,
        market=market,
        recommendation=None,
        signal=signal,
        metadata=market.raw_data or {},
        capture_scope="coverage",
    )
    db_session.commit()

    stored = db_session.scalar(select(Prediction).where(Prediction.id == prediction.id))
    assert stored is not None
    assert stored.capture_scope == "coverage"
    assert stored.side == "yes"
    assert stored.suggested_price == 0.47
    assert stored.invalidation == "Pull if YES entry moves above 0.6300"


def test_capture_prediction_samples_coverage_once_per_market_per_local_day(db_session):
    event = _seed_team_event(db_session, sport_key="NBA", external_id="coverage-sampled")
    market = Market(
        ticker="KXNBA-COVERAGE-SAMPLED-1",
        sport_key="NBA",
        event_id=event.id,
        title="Jalen Brunson: 30+ points?",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 30.0,
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
        },
    )
    db_session.add(market)
    db_session.flush()

    first_signal = SignalSnapshot(
        event_id=event.id,
        market_id=market.id,
        model_name="heuristic-v1",
        confidence=0.62,
        fair_yes_price=0.57,
        fair_no_price=0.43,
        edge=0.08,
        selection_score=0.09,
        reasons=["Morning sample"],
        features={},
        scoring_diagnostics={"selected_side": "yes", "suggested_price": 0.49},
        captured_at=datetime(2026, 4, 5, 14, 0, tzinfo=timezone.utc),
    )
    second_signal = SignalSnapshot(
        event_id=event.id,
        market_id=market.id,
        model_name="heuristic-v1",
        confidence=0.71,
        fair_yes_price=0.64,
        fair_no_price=0.36,
        edge=0.12,
        selection_score=0.13,
        reasons=["Evening sample"],
        features={},
        scoring_diagnostics={"selected_side": "yes", "suggested_price": 0.53},
        captured_at=datetime(2026, 4, 5, 23, 30, tzinfo=timezone.utc),
    )
    db_session.add_all([first_signal, second_signal])
    db_session.flush()

    first = capture_prediction(
        db_session,
        run_id=21,
        event=event,
        market=market,
        recommendation=None,
        signal=first_signal,
        metadata=market.raw_data or {},
        capture_scope="coverage",
    )
    second = capture_prediction(
        db_session,
        run_id=22,
        event=event,
        market=market,
        recommendation=None,
        signal=second_signal,
        metadata=market.raw_data or {},
        capture_scope="coverage",
    )
    db_session.commit()

    rows = db_session.scalars(select(Prediction).where(Prediction.market_id == market.id)).all()
    assert len(rows) == 1
    assert first.id == second.id
    stored = rows[0]
    assert stored.run_id == 22
    assert stored.capture_scope == "coverage"
    assert stored.confidence == 0.71
    assert stored.suggested_price == 0.53


def test_refresh_cycle_captures_durable_predictions(db_session):
    run = run_refresh_cycle(
        db_session,
        provider=FakeSportsProvider(),
        public_client=FakeKalshiRefreshClient(),
        sports=["NBA"],
    )
    db_session.commit()

    predictions = db_session.scalars(select(Prediction).order_by(Prediction.id.asc())).all()
    assert len(predictions) == 1
    assert predictions[0].run_id == run.id
    assert predictions[0].ticker == "KXNBAGAME-26MAR30BOSBKN-BOS"
    assert predictions[0].fair_yes_price is not None
    assert predictions[0].prediction_outcome == "pending"
    assert run.details["predictions_captured"] == 1


def test_current_slate_refresh_skips_prediction_settlement_sweep(db_session):
    _create_prediction(
        db_session,
        ticker="KXNBA-PREEXISTING-PENDING-1",
        sport_key="NBA",
        outcome="pending",
        settlement_status="pending",
    )
    db_session.commit()

    run = run_refresh_cycle(
        db_session,
        provider=FakeSportsProvider(),
        public_client=FakeKalshiRefreshClient(),
        sports=["NBA"],
        current_slate_only=True,
    )
    db_session.commit()

    assert run.status == "completed"
    assert run.details["prediction_settlement_updated"] == 0


def test_prop_refresh_cycle_runs_maintenance_settlement_on_all_stacked_predictions(db_session):
    older = _create_prediction(
        db_session,
        ticker="KXNBA-MAINT-SETTLE-1",
        sport_key="NBA",
        market_family="winner",
        market_kind="game_winner",
        stat_key="winner",
        captured_at=datetime(2026, 4, 4, 1, 0, tzinfo=timezone.utc),
        outcome="pending",
        settlement_status="pending",
    )
    latest = Prediction(
        event_id=older.event_id,
        market_id=older.market_id,
        ticker=older.ticker,
        sport_key=older.sport_key,
        event_name=older.event_name,
        market_title=older.market_title,
        market_family=older.market_family,
        market_kind=older.market_kind,
        stat_key=older.stat_key,
        threshold=older.threshold,
        subject_name=older.subject_name,
        subject_team=older.subject_team,
        side=older.side,
        action=older.action,
        suggested_price=older.suggested_price,
        fair_yes_price=older.fair_yes_price,
        fair_no_price=older.fair_no_price,
        edge=older.edge,
        confidence=older.confidence,
        model_name=older.model_name,
        invalidation=older.invalidation,
        rationale=older.rationale,
        reasons=list(older.reasons or []),
        features=dict(older.features or {}),
        market_status_at_capture=older.market_status_at_capture,
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=datetime(2026, 4, 4, 3, 0, tzinfo=timezone.utc),
    )
    older.capture_scope = "coverage"
    latest.capture_scope = "coverage"
    db_session.add(latest)
    db_session.commit()

    run = run_prop_refresh_cycle(
        db_session,
        public_client=FakeSettlementClient(
            {
                "KXNBA-MAINT-SETTLE-1": {
                    "ticker": "KXNBA-MAINT-SETTLE-1",
                    "status": "settled",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                    "settlement_ts": "2026-04-05T03:00:00Z",
                }
            }
        ),
        sports=["NBA"],
    )
    db_session.commit()

    db_session.refresh(older)
    db_session.refresh(latest)
    assert run.status == "completed"
    # Bug #12: maintenance settlement must settle every unresolved row
    # on the ticker, not just the latest per (ticker, scope, side).
    assert run.details["prediction_settlement_updated"] == 2
    assert older.prediction_outcome == "won"
    assert latest.prediction_outcome == "won"


def test_refresh_kalshi_markets_ingests_combo_derived_prop_legs(db_session):
    summary = refresh_kalshi_markets(db_session, client=FakeKalshiComboClient())
    db_session.commit()

    market = db_session.scalar(
        select(Market).where(Market.ticker == "KXNBAPTS-26APR05NYKBOS-NYKJBRUNSON11-30")
    )
    assert market is not None
    assert market.raw_data["copilot_market_family"] == "player_prop"
    assert market.raw_data["copilot_source_type"] == "combo_derived"
    assert market.raw_data["copilot_source_market_ticker"] == "KXMVE-NBA-PROPS-1"
    assert summary["supported_nba_props_seen"] == 1


def test_refresh_kalshi_markets_skips_combo_derived_winner_legs(db_session):
    summary = refresh_kalshi_markets(db_session, client=FakeKalshiMixedComboClient())
    db_session.commit()

    winner_market = db_session.scalar(
        select(Market).where(Market.ticker == "KXNBAGAME-26APR05BOSNYK-BOS")
    )
    prop_market = db_session.scalar(
        select(Market).where(Market.ticker == "KXNBAPTS-26APR04WASMIA-MIABADEBAYO13-15")
    )

    assert winner_market is None
    assert prop_market is not None
    assert prop_market.raw_data["copilot_market_family"] == "player_prop"
    assert prop_market.raw_data["copilot_source_type"] == "combo_derived"
    assert summary["supported_nba_props_seen"] == 1


def test_refresh_kalshi_markets_refreshes_tracked_combo_prop_tickers_without_discovery_scan(db_session):
    market = Market(
        ticker="KXNBAPTS-26APR05NYKBOS-NYKJBRUNSON11-30",
        sport_key="NBA",
        title="Jalen Brunson: 30+ points",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 30.0,
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
            "copilot_source_type": "combo_derived",
            "copilot_source_market_ticker": "KXMVE-NBA-PROPS-1",
            "copilot_source_market_title": "NBA prop combo",
        },
    )
    db_session.add(market)
    db_session.commit()

    client = TrackedComboRefreshClient()
    summary = refresh_kalshi_markets(
        db_session,
        client=client,
        include_standalone=False,
        refresh_combo_prop_tickers=True,
        discover_combo_props=False,
    )

    assert client.include_calls == 0
    assert client.get_market_calls == [market.ticker]
    assert summary["combo_prop_legs_refreshed"] == 1
    assert summary["combo_prop_legs_discovered"] == 0


def test_tracked_combo_prop_refresh_filters_in_sql_not_python(db_session):
    """Bug #23: the combo-prop refresh path used to load every open market
    into Python and filter ``raw_data["copilot_market_family"]`` /
    ``raw_data["copilot_source_type"]`` on each row. That's O(all open
    markets) per refresh tick. After the fix, only markets that match
    BOTH JSON keys at the DB level are visited — the others never load.

    The behavioral fingerprint of the SQL filter: an open market with the
    wrong family (e.g. ``winner``) or the wrong source (``standalone``)
    must NOT have ``client.get_market`` called against it."""
    # A real combo-derived prop leg — should be visited.
    combo_prop = Market(
        ticker="KXNBAPTS-FILTER-COMBO",
        sport_key="NBA",
        title="Filter combo prop",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_source_type": "combo_derived",
        },
    )
    # Same family but standalone — must be excluded.
    standalone_prop = Market(
        ticker="KXNBAPTS-FILTER-STANDALONE",
        sport_key="NBA",
        title="Filter standalone prop",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_source_type": "standalone",
        },
    )
    # Winner market with the matching source — must be excluded (family
    # doesn't match).
    combo_winner = Market(
        ticker="KXNBAGAME-FILTER-WINNER",
        sport_key="NBA",
        title="Filter winner",
        status="active",
        raw_data={
            "copilot_market_family": "winner",
            "copilot_source_type": "combo_derived",
        },
    )
    # Market with no copilot_* keys at all — must be excluded.
    legacy_market = Market(
        ticker="KX-FILTER-LEGACY",
        sport_key="NBA",
        title="Filter legacy",
        status="active",
        raw_data={},
    )
    db_session.add_all([combo_prop, standalone_prop, combo_winner, legacy_market])
    db_session.commit()

    client = TrackedComboRefreshClient()
    refresh_kalshi_markets(
        db_session,
        client=client,
        include_standalone=False,
        refresh_combo_prop_tickers=True,
        discover_combo_props=False,
    )

    assert client.get_market_calls == [combo_prop.ticker], (
        "Only the combo-derived player_prop market should be visited. "
        f"Got: {client.get_market_calls}"
    )


def test_refresh_current_slate_kalshi_markets_uses_targeted_get_market_before_broad_scan(db_session):
    event = _seed_team_event(db_session, sport_key="NBA", external_id="target-current")
    event.starts_at = datetime.now(timezone.utc)
    market = Market(
        ticker="KXNBAGAME-CURRENT-BOS",
        sport_key="NBA",
        event_id=event.id,
        title="Brooklyn at Boston Winner?",
        status="active",
        raw_data={
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "event_ticker": "KXNBAGAME-CURRENT",
        },
    )
    db_session.add(market)
    db_session.commit()

    client = CurrentSlateTargetClient()
    summary = refresh_current_slate_kalshi_markets(db_session, client=client)

    assert client.get_market_calls == [market.ticker]
    assert client.list_market_calls == 0
    assert summary["broad_market_fallback_used"] is False
    assert summary["current_slate_targeted_markets_refreshed"] == 1


def test_refresh_combo_prop_discovery_batch_prefilters_obvious_non_target_legs(db_session):
    client = ComboDiscoveryPrefilterClient()

    summary, next_cursor, complete = _refresh_combo_prop_discovery_batch(
        db_session,
        client=client,
        cursor_payload=None,
        limit=50,
        leg_batch_size=10,
    )

    assert client.get_market_calls == ["KXNBAPTS-26APR10DETCHA-CHAMBRIDGES0-10"]
    assert summary["processed"] == 1
    assert next_cursor is None
    assert complete is True


def test_refresh_kalshi_markets_only_writes_snapshots_on_change_or_heartbeat(db_session):
    initial = refresh_kalshi_markets(db_session, client=FakeKalshiRefreshClient())
    db_session.commit()

    market = db_session.scalar(select(Market).where(Market.ticker == "KXNBAGAME-26MAR30BOSBKN-BOS"))
    assert market is not None
    assert initial["market_snapshots_written"] == 1

    second = refresh_kalshi_markets(db_session, client=FakeKalshiRefreshClient())
    db_session.commit()
    snapshot_count = len(db_session.scalars(select(MarketSnapshot).where(MarketSnapshot.market_id == market.id)).all())
    assert second["market_snapshots_written"] == 0
    assert snapshot_count == 1

    latest_snapshot = db_session.scalars(
        select(MarketSnapshot).where(MarketSnapshot.market_id == market.id).order_by(MarketSnapshot.id.desc()).limit(1)
    ).first()
    assert latest_snapshot is not None
    latest_snapshot.captured_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db_session.commit()

    third = refresh_kalshi_markets(db_session, client=FakeKalshiRefreshClient())
    db_session.commit()
    snapshot_count = len(db_session.scalars(select(MarketSnapshot).where(MarketSnapshot.market_id == market.id)).all())
    assert third["market_snapshots_written"] == 1
    assert snapshot_count == 2


def test_warm_prop_context_cache_resolves_unique_subjects_once(db_session):
    event = _seed_team_event(db_session, sport_key="NBA", external_id="warm-evt")
    for ticker in ("KXNBAPTS-WARM-1", "KXNBAPTS-WARM-2"):
        db_session.add(
            Market(
                ticker=ticker,
                sport_key="NBA",
                event_id=event.id,
                title="Jalen Brunson: 30+ points",
                status="active",
                raw_data={
                    "copilot_market_family": "player_prop",
                    "copilot_market_kind": "player_prop",
                    "copilot_stat_key": "points",
                    "copilot_threshold": 30.0,
                    "copilot_subject_name": "Jalen Brunson",
                    "copilot_subject_team": "NYK",
                },
            )
        )
    db_session.commit()

    espn_client = CountingEspnPropClient()
    resolver = PropStatsResolver(db_session, espn_client=espn_client, allow_network=True)

    summary = warm_prop_context_cache(db_session, resolver=resolver)

    assert summary["prop_subjects_warmed"] == 1
    assert summary["player_search_cache_misses"] == 1
    assert summary["gamelog_cache_misses"] == 1
    assert len(espn_client.search_calls) == 1
    assert len(espn_client.gamelog_calls) == 1


def test_warm_current_watchlist_prop_context_only_warms_current_slate(db_session):
    # Pin `now` to local-noon to avoid date-rollover flakes when the test runs
    # near midnight Central (is_current_watchlist_event compares local dates).
    from zoneinfo import ZoneInfo

    from app.config import get_settings

    local_tz = ZoneInfo(get_settings().default_timezone)
    local_noon = datetime.now(local_tz).replace(hour=12, minute=0, second=0, microsecond=0)
    now = local_noon.astimezone(timezone.utc)
    current_event = _seed_team_event(db_session, sport_key="NBA", external_id="current-slate")
    current_event.starts_at = now + timedelta(hours=1)
    future_event = _seed_team_event(db_session, sport_key="NBA", external_id="future-slate")
    future_event.starts_at = now + timedelta(days=1)
    db_session.flush()

    db_session.add_all(
        [
            Market(
                ticker="KXNBAPTS-CURRENT-1",
                sport_key="NBA",
                event_id=current_event.id,
                title="Jalen Brunson: 30+ points",
                status="active",
                raw_data={
                    "copilot_market_family": "player_prop",
                    "copilot_market_kind": "player_prop",
                    "copilot_stat_key": "points",
                    "copilot_threshold": 30.0,
                    "copilot_subject_name": "Jalen Brunson",
                    "copilot_subject_team": "NYK",
                },
            ),
            Market(
                ticker="KXNBAPTS-FUTURE-1",
                sport_key="NBA",
                event_id=future_event.id,
                title="Bam Adebayo: 15+ points",
                status="active",
                raw_data={
                    "copilot_market_family": "player_prop",
                    "copilot_market_kind": "player_prop",
                    "copilot_stat_key": "points",
                    "copilot_threshold": 15.0,
                    "copilot_subject_name": "Bam Adebayo",
                    "copilot_subject_team": "MIA",
                },
            ),
        ]
    )
    db_session.commit()

    class SlateEspnClient:
        def __init__(self):
            self.search_calls: list[tuple[str, str]] = []
            self.gamelog_calls: list[tuple[str, str, int]] = []

        def search_player(self, query: str, sport_key: str = "NBA", *, team_hint: str | None = None):
            self.search_calls.append((query, sport_key))
            return {
                "athlete_id": "athlete-current" if query == "Jalen Brunson" else "athlete-future",
                "display_name": query,
                "team_name": "Test Team",
            }

        def fetch_player_gamelog(self, sport_key: str, athlete_id: str, season: int):
            self.gamelog_calls.append((sport_key, athlete_id, season))
            return NBA_PROP_GAMELOG_PAYLOAD

    espn_client = SlateEspnClient()
    resolver = PropStatsResolver(db_session, espn_client=espn_client, allow_network=True, now=now)

    summary = warm_current_watchlist_prop_context(db_session, resolver=resolver, now=now)

    assert summary["prop_subjects_warmed"] == 1
    assert espn_client.search_calls == [("Jalen Brunson", "NBA")]
    assert len(espn_client.gamelog_calls) == 1


def test_prop_stats_resolver_uses_persistent_cache_without_network(db_session):
    cached_at = datetime(2026, 4, 4, 3, 0, tzinfo=timezone.utc)
    db_session.add(
        EspnPlayerSearchCache(
            sport_key="NBA",
            query_normalized="jalen brunson",
            payload={"athlete_id": "3934672", "display_name": "Jalen Brunson", "team_name": "New York Knicks"},
            cached_at=cached_at,
            expires_at=cached_at + timedelta(days=1),
        )
    )
    db_session.add(
        EspnPlayerGamelogCache(
            sport_key="NBA",
            athlete_id="3934672",
            season=2026,
            payload=NBA_PROP_GAMELOG_PAYLOAD,
            cached_at=cached_at,
            expires_at=cached_at + timedelta(minutes=30),
        )
    )
    db_session.commit()

    class FailingEspnClient:
        def search_player(self, query: str, sport_key: str = "NBA", *, team_hint: str | None = None):
            raise AssertionError("search_player should not be called")

        def fetch_player_gamelog(self, sport_key: str, athlete_id: str, season: int):
            raise AssertionError("fetch_player_gamelog should not be called")

    resolver = PropStatsResolver(
        db_session,
        espn_client=FailingEspnClient(),
        allow_network=False,
        now=datetime(2026, 4, 4, 3, 10, tzinfo=timezone.utc),
    )

    resolved = resolver.resolve("NBA", "Jalen Brunson", team_hint="NYK")

    assert resolved.display_name == "Jalen Brunson"
    assert len(resolved.game_logs) == 5
    assert resolver.stats.player_search_cache_hits == 1
    assert resolver.stats.gamelog_cache_hits == 1
    assert resolver.stats.player_search_cache_misses == 0
    assert resolver.stats.gamelog_cache_misses == 0


def test_settle_predictions_updates_pending_rows_idempotently(db_session):
    won = _create_prediction(db_session, ticker="KXNBA-WON-1", side="yes", suggested_price=0.42)
    lost = _create_prediction(db_session, ticker="KXNBA-LOST-1", side="no", suggested_price=0.38)
    push = _create_prediction(db_session, ticker="KXNBA-PUSH-1", side="yes", suggested_price=0.40)
    cancelled = _create_prediction(db_session, ticker="KXNBA-CANCEL-1", side="yes", suggested_price=0.51)
    unresolved = _create_prediction(db_session, ticker="KXNBA-UNRES-1", side="yes", suggested_price=0.48)
    pending = _create_prediction(db_session, ticker="KXNBA-PENDING-1", side="yes", suggested_price=0.44)
    db_session.commit()

    summary = settle_predictions(
        db_session,
        client=FakeSettlementClient(
            {
                won.ticker: {
                    "ticker": won.ticker,
                    "status": "settled",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                    "settlement_ts": "2026-04-02T03:00:00Z",
                },
                lost.ticker: {
                    "ticker": lost.ticker,
                    "status": "settled",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                    "settlement_ts": "2026-04-02T03:05:00Z",
                },
                push.ticker: {
                    "ticker": push.ticker,
                    "status": "settled",
                    "settlement_value_dollars": "0.4300",
                    "settlement_ts": "2026-04-02T03:10:00Z",
                },
                cancelled.ticker: {
                    "ticker": cancelled.ticker,
                    "status": "settled",
                    "result": "void",
                    "settlement_ts": "2026-04-02T03:15:00Z",
                },
                unresolved.ticker: {
                    "ticker": unresolved.ticker,
                    "status": "closed",
                },
            }
        ),
        open_market_tickers={pending.ticker},
    )
    db_session.commit()

    assert summary["won"] == 1
    assert summary["lost"] == 1
    assert summary["push"] == 1
    assert summary["cancelled"] == 1
    assert summary["unresolved"] == 1
    assert summary["pending"] == 1
    assert summary["updated"] == 5

    db_session.expire_all()
    assert db_session.scalar(select(Prediction).where(Prediction.ticker == won.ticker)).prediction_outcome == "won"
    assert db_session.scalar(select(Prediction).where(Prediction.ticker == lost.ticker)).prediction_outcome == "lost"

    pushed = db_session.scalar(select(Prediction).where(Prediction.ticker == push.ticker))
    assert pushed.prediction_outcome == "push"
    assert pushed.realized_pnl == 0.03

    cancelled_row = db_session.scalar(select(Prediction).where(Prediction.ticker == cancelled.ticker))
    assert cancelled_row.prediction_outcome == "cancelled"
    assert cancelled_row.realized_pnl == 0.0

    unresolved_row = db_session.scalar(select(Prediction).where(Prediction.ticker == unresolved.ticker))
    assert unresolved_row.prediction_outcome == "unresolved"
    assert unresolved_row.settled_at is None

    second_pass = settle_predictions(
        db_session,
        client=FakeSettlementClient({unresolved.ticker: {"ticker": unresolved.ticker, "status": "closed"}}),
        open_market_tickers={pending.ticker},
    )
    assert second_pass["updated"] == 0


def test_settle_predictions_settles_every_stacked_prediction_per_ticker(db_session):
    """Bug #12: settlement used to filter to the latest unresolved
    prediction per ``(ticker, scope, side)`` partition, so older
    stacked rows on the same ticker stayed ``pending`` forever and
    distorted hit-rate / calibration / PnL. Fix: settle every
    unresolved prediction; the Kalshi market payload is cached per
    ticker so the cost is extra DB iteration only."""
    older = _create_prediction(
        db_session,
        ticker="KXNBA-DUPE-SETTLE-1",
        captured_at=datetime(2026, 4, 2, 1, 0, tzinfo=timezone.utc),
    )
    newer = Prediction(
        event_id=older.event_id,
        market_id=older.market_id,
        ticker=older.ticker,
        sport_key=older.sport_key,
        event_name=older.event_name,
        market_title=older.market_title,
        market_family=older.market_family,
        market_kind=older.market_kind,
        stat_key=older.stat_key,
        threshold=older.threshold,
        subject_name=older.subject_name,
        subject_team=older.subject_team,
        side=older.side,
        action=older.action,
        suggested_price=older.suggested_price,
        fair_yes_price=older.fair_yes_price,
        fair_no_price=older.fair_no_price,
        edge=older.edge,
        confidence=older.confidence,
        model_name=older.model_name,
        invalidation=older.invalidation,
        rationale=older.rationale,
        reasons=list(older.reasons or []),
        features=dict(older.features or {}),
        market_status_at_capture=older.market_status_at_capture,
        settlement_status="pending",
        prediction_outcome="pending",
        captured_at=datetime(2026, 4, 2, 2, 0, tzinfo=timezone.utc),
    )
    older.capture_scope = "coverage"
    newer.capture_scope = "coverage"
    db_session.add(newer)
    db_session.commit()

    summary = settle_predictions(
        db_session,
        client=FakeSettlementClient(
            {
                "KXNBA-DUPE-SETTLE-1": {
                    "ticker": "KXNBA-DUPE-SETTLE-1",
                    "status": "settled",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                }
            }
        ),
    )
    db_session.commit()

    db_session.refresh(older)
    db_session.refresh(newer)
    assert summary["processed"] == 2
    assert summary["updated"] == 2
    assert older.prediction_outcome == "won"
    assert newer.prediction_outcome == "won"


class BulkSettlementClient:
    """Fake Kalshi client that exposes both ``list_markets`` and
    ``get_market``, separately counted so the bulk-fetch tests can
    assert exactly how many of each were made.

    ``list_markets_payloads`` seeds the paginated bulk response;
    ``get_market_payloads`` backstops the per-ticker fallback path.

    ``list_markets_delay_seconds`` lets us simulate a slow Kalshi
    listing so the timeout-bounded fallback test can fire without
    actually waiting 15s on the real budget.
    """

    def __init__(
        self,
        *,
        list_markets_payloads: list[dict] | None = None,
        get_market_payloads: dict | None = None,
        list_markets_raises: BaseException | None = None,
        list_markets_delay_seconds: float = 0.0,
    ):
        self.list_markets_payloads = list_markets_payloads or []
        self.get_market_payloads = get_market_payloads or {}
        self.list_markets_raises = list_markets_raises
        self.list_markets_delay_seconds = list_markets_delay_seconds
        self.list_markets_calls = 0
        self.get_market_calls: list[str] = []

    def iter_market_pages(
        self,
        *,
        status="open",
        limit=1000,
        mve_filter="exclude",
        max_pages=50,
        cursor=None,
        wall_clock_budget_seconds=None,
    ):
        self.list_markets_calls += 1
        if self.list_markets_delay_seconds:
            import time as _time

            _time.sleep(self.list_markets_delay_seconds)
        if self.list_markets_raises is not None:
            raise self.list_markets_raises
        if self.list_markets_payloads:
            yield self.list_markets_payloads, None

    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        # Some refresh code calls list_markets directly; settlement
        # bulk path uses iter_market_pages, but the existing
        # FakeSettlementClient API forwarded calls to ``list_markets``.
        # Keep this present for compatibility with any caller that may
        # invoke it; for the settlement code path the iter_market_pages
        # method above is what fires.
        return []

    def get_market(self, ticker):
        self.get_market_calls.append(ticker)
        if ticker not in self.get_market_payloads:
            raise KeyError(ticker)
        return self.get_market_payloads[ticker]


def _settled_yes_payload(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "status": "settled",
        "result": "yes",
        "settlement_value_dollars": "1.0000",
        "settlement_ts": "2026-04-02T03:00:00Z",
    }


def test_settle_predictions_bulk_full_hit_skips_get_market(db_session):
    """Happy path: every ticker the batch needs is returned by
    ``list_markets``. ``get_market`` MUST NOT be called — that's the
    whole point of the bulk pre-pass."""
    tickers = [f"KXNBA-BULK-{i}" for i in range(5)]
    predictions = [
        _create_prediction(db_session, ticker=t, side="yes", suggested_price=0.42)
        for t in tickers
    ]
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_payloads=[_settled_yes_payload(t) for t in tickers]
    )

    summary = settle_predictions(db_session, client=client)
    db_session.commit()

    assert summary["processed"] == len(tickers)
    assert summary["won"] == len(tickers)
    assert client.get_market_calls == []
    assert client.list_markets_calls >= 1
    db_session.expire_all()
    for prediction in predictions:
        row = db_session.scalar(
            select(Prediction).where(Prediction.id == prediction.id)
        )
        assert row.settlement_source == "kalshi_list_markets"


def test_settle_predictions_bulk_partial_hit_falls_back_per_ticker(db_session):
    """80 tickers covered by bulk → only the remaining 20 hit
    ``get_market``. Demonstrates the new code path doesn't regress when
    Kalshi's settled-markets page misses some recently-settled tickers
    (older sports, multi-page coverage, etc.)."""
    total = 30
    covered = 20
    tickers = [f"KXMLB-PARTIAL-{i}" for i in range(total)]
    for t in tickers:
        _create_prediction(db_session, ticker=t, sport_key="MLB", side="yes")
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_payloads=[
            _settled_yes_payload(t) for t in tickers[:covered]
        ],
        get_market_payloads={
            t: _settled_yes_payload(t) for t in tickers[covered:]
        },
    )

    summary = settle_predictions(db_session, client=client)
    db_session.commit()

    assert summary["processed"] == total
    assert summary["won"] == total
    # Exactly the misses fell back to get_market.
    assert set(client.get_market_calls) == set(tickers[covered:])
    assert len(client.get_market_calls) == total - covered


def test_settle_predictions_bulk_empty_falls_back_entirely(db_session):
    """Bulk listing returns []. Every prediction must still settle via
    the per-ticker fallback — parity with the pre-change behavior."""
    tickers = [f"KXNBA-EMPTY-{i}" for i in range(3)]
    for t in tickers:
        _create_prediction(db_session, ticker=t, side="yes")
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_payloads=[],
        get_market_payloads={t: _settled_yes_payload(t) for t in tickers},
    )

    summary = settle_predictions(db_session, client=client)
    db_session.commit()

    assert summary["processed"] == len(tickers)
    assert summary["won"] == len(tickers)
    assert sorted(client.get_market_calls) == sorted(tickers)
    db_session.expire_all()
    for ticker in tickers:
        row = db_session.scalar(select(Prediction).where(Prediction.ticker == ticker))
        assert row.settlement_source == "kalshi_get_market"


def test_settle_predictions_bulk_raises_falls_back_entirely(db_session):
    """Bulk listing raises a transport error mid-fetch. Settlement must
    degrade gracefully — every prediction goes through ``get_market``
    so the batch still drains, never crashes."""
    import httpx

    tickers = [f"KXNBA-FAIL-{i}" for i in range(3)]
    for t in tickers:
        _create_prediction(db_session, ticker=t, side="yes")
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_raises=httpx.TimeoutException("simulated"),
        get_market_payloads={t: _settled_yes_payload(t) for t in tickers},
    )

    summary = settle_predictions(db_session, client=client)
    db_session.commit()

    assert summary["processed"] == len(tickers)
    assert summary["won"] == len(tickers)
    assert sorted(client.get_market_calls) == sorted(tickers)


def test_settle_predictions_bulk_records_settlement_source_telemetry(db_session):
    """Operators distinguish bulk-list hits from per-ticker fallbacks via
    the ``settlement_source`` column. Mixed batch → mixed sources."""
    bulk_ticker = "KXNBA-MIX-BULK"
    fallback_ticker = "KXNBA-MIX-FALLBACK"
    _create_prediction(db_session, ticker=bulk_ticker, side="yes")
    _create_prediction(db_session, ticker=fallback_ticker, side="yes")
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_payloads=[_settled_yes_payload(bulk_ticker)],
        get_market_payloads={fallback_ticker: _settled_yes_payload(fallback_ticker)},
    )

    settle_predictions(db_session, client=client)
    db_session.commit()
    db_session.expire_all()

    bulk_row = db_session.scalar(
        select(Prediction).where(Prediction.ticker == bulk_ticker)
    )
    fallback_row = db_session.scalar(
        select(Prediction).where(Prediction.ticker == fallback_ticker)
    )
    assert bulk_row.settlement_source == "kalshi_list_markets"
    assert fallback_row.settlement_source == "kalshi_get_market"


def test_settle_predictions_bulk_hit_still_captures_closing_yes_price(db_session):
    """Pattern 2 (cross-component data flow): the bulk path must still
    feed the CLV capture step. A bulk-hit prediction whose market has
    snapshot history must end up with ``closing_yes_price`` and
    ``closing_line_value`` populated."""
    ticker = "KXNBA-BULK-CLV-1"
    prediction = _create_prediction(db_session, ticker=ticker, side="yes")
    db_session.flush()
    db_session.add(
        MarketSnapshot(
            market_id=prediction.market_id,
            captured_at=datetime(2026, 4, 1, 23, 50, tzinfo=timezone.utc),
            yes_bid=0.55,
            yes_ask=0.60,
            no_bid=0.40,
            no_ask=0.45,
            last_price=0.58,
        )
    )
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_payloads=[
            {
                **_settled_yes_payload(ticker),
                "close_time": "2026-04-02T00:00:00Z",
            }
        ]
    )
    settle_predictions(db_session, client=client)
    db_session.commit()
    db_session.expire_all()

    row = db_session.scalar(select(Prediction).where(Prediction.ticker == ticker))
    assert row.settlement_source == "kalshi_list_markets"
    assert row.closing_yes_price is not None
    assert row.closing_line_value is not None


def test_settle_predictions_bulk_cross_sport_coverage(db_session):
    """Pattern 9 (cross-scope unaccounted): settlement runs for every
    sport at once. A single bulk page must service mixed
    NBA + MLB tickers without one sport's tickers stealing pages from
    another. With ``ticker -> payload`` intersection, mixed sports
    settle correctly so long as every ticker is on the page."""
    nba_ticker = "KXNBA-CROSS-1"
    mlb_ticker = "KXMLB-CROSS-1"
    _create_prediction(db_session, ticker=nba_ticker, sport_key="NBA")
    _create_prediction(db_session, ticker=mlb_ticker, sport_key="MLB")
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_payloads=[
            _settled_yes_payload(nba_ticker),
            _settled_yes_payload(mlb_ticker),
        ]
    )
    summary = settle_predictions(db_session, client=client)
    db_session.commit()
    db_session.expire_all()

    assert summary["won"] == 2
    assert client.get_market_calls == []
    nba_row = db_session.scalar(select(Prediction).where(Prediction.ticker == nba_ticker))
    mlb_row = db_session.scalar(select(Prediction).where(Prediction.ticker == mlb_ticker))
    assert nba_row.settlement_source == "kalshi_list_markets"
    assert mlb_row.settlement_source == "kalshi_list_markets"


def test_settle_predictions_bulk_skips_open_market_tickers(db_session):
    """Pattern 6 (data-shape assumptions): tickers in
    ``open_market_tickers`` should NOT appear in the bulk-fetch set.
    Settlement of an open market is a no-op (still pending) and we
    shouldn't waste Kalshi quota listing them."""
    settled_ticker = "KXNBA-SKIP-SETTLED"
    open_ticker = "KXNBA-SKIP-OPEN"
    _create_prediction(db_session, ticker=settled_ticker)
    _create_prediction(db_session, ticker=open_ticker)
    db_session.commit()

    client = BulkSettlementClient(
        list_markets_payloads=[_settled_yes_payload(settled_ticker)]
    )
    summary = settle_predictions(
        db_session,
        client=client,
        open_market_tickers={open_ticker},
    )
    db_session.commit()
    db_session.expire_all()

    assert summary["pending"] == 1
    assert summary["won"] == 1
    settled_row = db_session.scalar(
        select(Prediction).where(Prediction.ticker == settled_ticker)
    )
    open_row = db_session.scalar(
        select(Prediction).where(Prediction.ticker == open_ticker)
    )
    assert settled_row.settlement_source == "kalshi_list_markets"
    assert open_row.settlement_status == "pending"
    assert open_row.settlement_source is None
    # And the open ticker was never asked about per-ticker either.
    assert open_ticker not in client.get_market_calls


def test_prediction_history_and_summary_endpoints(client, db_session):
    captured_base = datetime.now(timezone.utc) - timedelta(days=1)
    _create_prediction(
        db_session,
        ticker="KXNBA-API-1",
        sport_key="NBA",
        stat_key="points",
        outcome="won",
        settlement_status="settled",
        captured_at=captured_base,
        confidence=0.72,
    )
    _create_prediction(
        db_session,
        ticker="KXMLB-API-1",
        sport_key="MLB",
        stat_key="hits",
        outcome="lost",
        settlement_status="settled",
        captured_at=captured_base + timedelta(minutes=30),
        confidence=0.61,
    )
    _create_prediction(
        db_session,
        ticker="KXNBA-API-2",
        sport_key="NBA",
        stat_key="points",
        outcome="pending",
        settlement_status="pending",
        captured_at=captured_base + timedelta(hours=1),
        confidence=0.69,
    )
    db_session.commit()

    response = client.get("/predictions?sport=NBA&stat_key=points")
    assert response.status_code == 200
    body = response.json()
    assert [item["ticker"] for item in body] == ["KXNBA-API-2", "KXNBA-API-1"]
    assert all(item["sport_key"] == "NBA" for item in body)

    filtered = client.get("/predictions?outcome=won")
    assert filtered.status_code == 200
    assert [item["ticker"] for item in filtered.json()] == ["KXNBA-API-1"]

    summary = client.get("/predictions/summary?sport=NBA")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["total_predictions"] == 2
    assert payload["won_predictions"] == 1
    assert payload["pending_predictions"] == 1
    assert payload["by_market_family"]["player_prop"] == 2
    assert payload["by_outcome"]["won"] == 1


def test_prediction_summary_is_exact_beyond_previous_five_thousand_row_cap(client, db_session):
    captured_at = datetime.now(timezone.utc) - timedelta(days=1)
    row_count = 5_001
    db_session.add_all(
        [
            Prediction(
                run_id=501,
                market_id=100_000 + index,
                ticker=f"NBA-SUMMARY-EXACT-{index}",
                sport_key="NBA",
                market_title="Exact summary market",
                market_family="winner",
                market_kind="game_winner",
                side="yes",
                suggested_price=0.45,
                fair_yes_price=0.58,
                fair_no_price=0.42,
                edge=0.125,
                confidence=0.75,
                model_name="heuristic-v1",
                rationale="Exact aggregate regression",
                settlement_status="settled",
                prediction_outcome="won",
                settled_at=captured_at + timedelta(hours=2),
                realized_pnl=0.2,
                captured_at=captured_at,
            )
            for index in range(row_count)
        ]
    )
    db_session.commit()

    response = client.get("/predictions/summary?sport=NBA")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_predictions"] == row_count
    assert payload["settled_predictions"] == row_count
    assert payload["won_predictions"] == row_count
    assert payload["average_edge"] == 0.125
    assert payload["average_confidence"] == 0.75
    assert payload["average_realized_pnl"] == 0.2
    assert datetime.fromisoformat(payload["window_start"].replace("Z", "+00:00")) == captured_at

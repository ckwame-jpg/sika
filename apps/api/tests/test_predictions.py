from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import EspnPlayerGamelogCache, EspnPlayerSearchCache, Event, EventParticipant, Market, Participant, Prediction
from app.services.ingestion import refresh_kalshi_markets, run_refresh_cycle
from app.services.predictions import settle_predictions
from app.services.scoring import PropStatsResolver, warm_prop_context_cache


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
    def list_markets(self, status="open", limit=1000, mve_filter="exclude"):
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

    def get_market(self, ticker):
        return self.payloads[ticker]


class FakeKalshiComboClient:
    def list_markets(self, status="open", limit=1000, mve_filter="exclude"):
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
    def list_markets(self, status="open", limit=1000, mve_filter="exclude"):
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

    def search_player(self, query: str, sport_key: str = "NBA"):
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

    def list_markets(self, status="open", limit=1000, mve_filter="exclude"):
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


def test_prediction_model_persists_snapshot_fields(db_session):
    prediction = _create_prediction(db_session, ticker="KXNBA-PERSIST-1")
    db_session.commit()

    stored = db_session.scalar(select(Prediction).where(Prediction.id == prediction.id))
    assert stored is not None
    assert stored.ticker == "KXNBA-PERSIST-1"
    assert stored.subject_name == "Jalen Brunson"
    assert stored.reasons == ["Reason 1", "Reason 2"]
    assert stored.prediction_outcome == "pending"


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
        def search_player(self, query: str, sport_key: str = "NBA"):
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


def test_prediction_history_and_summary_endpoints(client, db_session):
    _create_prediction(
        db_session,
        ticker="KXNBA-API-1",
        sport_key="NBA",
        stat_key="points",
        outcome="won",
        settlement_status="settled",
        captured_at=datetime(2026, 4, 1, 1, 0, tzinfo=timezone.utc),
        confidence=0.72,
    )
    _create_prediction(
        db_session,
        ticker="KXMLB-API-1",
        sport_key="MLB",
        stat_key="hits",
        outcome="lost",
        settlement_status="settled",
        captured_at=datetime(2026, 4, 2, 1, 0, tzinfo=timezone.utc),
        confidence=0.61,
    )
    _create_prediction(
        db_session,
        ticker="KXNBA-API-2",
        sport_key="NBA",
        stat_key="points",
        outcome="pending",
        settlement_status="pending",
        captured_at=datetime(2026, 4, 2, 5, 0, tzinfo=timezone.utc),
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

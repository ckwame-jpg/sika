from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import DemoOrder, Event, PaperPosition, Recommendation
from app.schemas import DemoOrderCreate, PaperPositionCreate, PaperPositionExit
from app.services.ingestion import run_refresh_cycle
from app.services.orders import close_paper_position, create_demo_order, create_paper_position


class FakeSportsProvider:
    def fetch_events_window(self, sport_name, start_day, end_day):
        if sport_name == "Basketball":
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

        return []


class FakeKalshiPublicClient:
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
            },
        ]


class EmptyKalshiPublicClient:
    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        return []


class FakeDemoClient:
    def create_order(self, *, ticker, side, action, quantity, limit_price, time_in_force):
        return {
            "request": {"ticker": ticker, "side": side, "action": action, "quantity": quantity},
            "order": {
                "order_id": "demo-order-1",
                "client_order_id": "demo-client-1",
                "status": "resting",
            },
        }


def test_mixed_sport_refresh_and_trading_flow(db_session, monkeypatch):
    run = run_refresh_cycle(
        db_session,
        provider=FakeSportsProvider(),
        public_client=FakeKalshiPublicClient(),
        sports=["NBA"],
    )
    db_session.commit()

    assert run.status == "completed"
    sport_keys = {item[0] for item in db_session.execute(select(Event.sport_key)).all()}
    assert sport_keys == {"NBA"}

    recommendations = db_session.scalars(select(Recommendation)).all()
    assert len(recommendations) >= 1

    position = create_paper_position(
        db_session,
        PaperPositionCreate(ticker="KXNBAGAME-26MAR30BOSBKN-BOS", side="yes", quantity=2, entry_price=0.42),
    )
    # Bug #31 — Kalshi side effect happens in the outbox drain; monkey-
    # patch the client used by the handler so this e2e test stays
    # network-free.
    monkeypatch.setattr("app.services.orders.KalshiDemoClient", FakeDemoClient)
    order = create_demo_order(
        db_session,
        DemoOrderCreate(ticker="KXNBAGAME-26MAR30BOSBKN-BOS", side="yes", quantity=2, limit_price=0.42, approved=True),
    )
    close_paper_position(db_session, position.id, PaperPositionExit(exit_price=0.58))
    db_session.commit()

    # Drain the outbox so the Kalshi-side reconciliation lands.
    from app.services.outbox import drain_once
    drain_once(db_session)
    db_session.commit()

    stored_position = db_session.get(PaperPosition, position.id)
    stored_order = db_session.get(DemoOrder, order.id)
    assert stored_position.status == "closed"
    assert stored_position.pnl == 0.32
    assert stored_order.status == "resting"


class FlakyEspnProvider:
    def fetch_events_window_with_diagnostics(self, sport_name, start_day, end_day):
        if sport_name != "NBA":
            return [], []
        return (
            [
                {
                    "source": "espn_public",
                    "idEvent": "nba-live-1",
                    "idLeague": "46",
                    "strLeague": "NBA",
                    "strHomeTeam": "Boston Celtics",
                    "strAwayTeam": "Brooklyn Nets",
                    "strHomeTeamShort": "Celtics",
                    "strAwayTeamShort": "Nets",
                    "idHomeTeam": "2",
                    "idAwayTeam": "17",
                    "strEvent": "Brooklyn Nets at Boston Celtics",
                    "strTimestamp": "2026-03-30T23:00:00Z",
                    "dateEvent": "2026-03-30",
                    "strStatus": "scheduled",
                    "intHomeScore": None,
                    "intAwayScore": None,
                }
            ],
            ["2026-03-31: ReadTimeout: The read operation timed out"],
        )


class LiveEspnProvider:
    def fetch_events_window_with_diagnostics(self, sport_name, start_day, end_day):
        if sport_name != "NBA":
            return [], []
        return (
            [
                {
                    "source": "espn_public",
                    "idEvent": "nba-live-2",
                    "idLeague": "46",
                    "strLeague": "NBA",
                    "strHomeTeam": "Oklahoma City Thunder",
                    "strAwayTeam": "Los Angeles Lakers",
                    "strHomeTeamShort": "Thunder",
                    "strAwayTeamShort": "Lakers",
                    "idHomeTeam": "25",
                    "idAwayTeam": "13",
                    "strEvent": "Los Angeles Lakers at Oklahoma City Thunder",
                    "strTimestamp": "2026-04-03T01:30:00Z",
                    "dateEvent": "2026-04-03",
                    "strStatus": "in_progress",
                    "intHomeScore": "58",
                    "intAwayScore": "51",
                }
            ],
            [],
        )


def test_refresh_cycle_records_sports_fetch_errors_without_failing_run(db_session):
    run = run_refresh_cycle(
        db_session,
        major_provider=FlakyEspnProvider(),
        public_client=EmptyKalshiPublicClient(),
        sports=["NBA", "MLB"],
    )
    db_session.commit()

    assert run.status == "completed"
    assert run.details["sports_records_ingested"]["NBA"] == 1
    assert run.details["sports_records_ingested"]["MLB"] == 0
    assert run.details["sports_fetch_errors"] == {"NBA": ["2026-03-31: ReadTimeout: The read operation timed out"]}
    assert db_session.scalar(select(Event).where(Event.external_id == "espn_public:NBA:event:nba-live-1")) is not None


def test_refresh_cycle_persists_live_nba_status_and_scores(db_session):
    run = run_refresh_cycle(
        db_session,
        major_provider=LiveEspnProvider(),
        public_client=EmptyKalshiPublicClient(),
        sports=["NBA"],
    )
    db_session.commit()

    event = db_session.scalar(select(Event).where(Event.external_id == "espn_public:NBA:event:nba-live-2"))
    assert run.status == "completed"
    assert event is not None
    assert event.status == "in_progress"
    assert event.completed_at is None

    participants = {entry.role: entry for entry in event.participants}
    assert participants["home"].score == 58.0
    assert participants["away"].score == 51.0
    assert participants["home"].result is None
    assert participants["away"].result is None


def _mlb_raw_event(home_name, away_name, home_abbr, away_abbr, home_lines, away_lines, home_era, away_era):
    def competitor(team_id, display_name, abbreviation, home_away, linescores, era):
        return {
            "id": team_id,
            "homeAway": home_away,
            "team": {
                "id": team_id,
                "displayName": display_name,
                "shortDisplayName": display_name.split()[-1],
                "abbreviation": abbreviation,
            },
            "linescores": [{"value": float(value), "period": index + 1} for index, value in enumerate(linescores)],
            "probables": [
                {
                    "statistics": [
                        {"abbreviation": "ERA", "displayValue": f"{era:.2f}"},
                    ]
                }
            ],
        }

    return {
        "competitions": [
            {
                "competitors": [
                    competitor("home", home_name, home_abbr, "home", home_lines, home_era),
                    competitor("away", away_name, away_abbr, "away", away_lines, away_era),
                ]
            }
        ]
    }


class FakeMLBSportsProvider:
    def fetch_events_window(self, sport_name, start_day, end_day):
        if sport_name != "Baseball":
            return []
        now = datetime(2026, 3, 30, 18, 0, tzinfo=timezone.utc)
        events = []
        for index in range(4):
            event_time = now - timedelta(days=4 - index)
            events.append(
                {
                    "source": "espn_public",
                    "idEvent": f"mlb-past-sea-{index}",
                    "idLeague": "MLB",
                    "strLeague": "MLB",
                    "strHomeTeam": "Seattle Mariners",
                    "strAwayTeam": "Oakland Athletics",
                    "strHomeTeamShort": "Mariners",
                    "strAwayTeamShort": "Athletics",
                    "idHomeTeam": "sea",
                    "idAwayTeam": "oak",
                    "strEvent": "Oakland Athletics at Seattle Mariners",
                    "strTimestamp": event_time.isoformat().replace("+00:00", "Z"),
                    "dateEvent": event_time.date().isoformat(),
                    "strStatus": "completed",
                    "intHomeScore": "6",
                    "intAwayScore": "2",
                    "raw": _mlb_raw_event("Seattle Mariners", "Oakland Athletics", "SEA", "OAK", [2, 1, 0, 1, 0], [0, 0, 1, 0, 0], 3.05, 4.85),
                }
            )
            away_event_time = now - timedelta(days=index + 5)
            events.append(
                {
                    "source": "espn_public",
                    "idEvent": f"mlb-past-nyy-{index}",
                    "idLeague": "MLB",
                    "strLeague": "MLB",
                    "strHomeTeam": "Boston Red Sox",
                    "strAwayTeam": "New York Yankees",
                    "strHomeTeamShort": "Red Sox",
                    "strAwayTeamShort": "Yankees",
                    "idHomeTeam": "bos",
                    "idAwayTeam": "nyy",
                    "strEvent": "New York Yankees at Boston Red Sox",
                    "strTimestamp": away_event_time.isoformat().replace("+00:00", "Z"),
                    "dateEvent": away_event_time.date().isoformat(),
                    "strStatus": "completed",
                    "intHomeScore": "3",
                    "intAwayScore": "1",
                    "raw": _mlb_raw_event("Boston Red Sox", "New York Yankees", "BOS", "NYY", [2, 0, 1, 0, 0], [0, 0, 0, 1, 0], 3.20, 4.90),
                }
            )

        future_time = now + timedelta(hours=8)
        events.append(
            {
                "source": "espn_public",
                "idEvent": "mlb-future-1",
                "idLeague": "MLB",
                "strLeague": "MLB",
                "strHomeTeam": "Seattle Mariners",
                "strAwayTeam": "New York Yankees",
                "strHomeTeamShort": "Mariners",
                "strAwayTeamShort": "Yankees",
                "idHomeTeam": "sea",
                "idAwayTeam": "nyy",
                "strEvent": "New York Yankees at Seattle Mariners",
                "strTimestamp": future_time.isoformat().replace("+00:00", "Z"),
                "dateEvent": future_time.date().isoformat(),
                "strStatus": "scheduled",
                "intHomeScore": None,
                "intAwayScore": None,
                "raw": _mlb_raw_event("Seattle Mariners", "New York Yankees", "SEA", "NYY", [0, 0, 0, 0, 0], [0, 0, 0, 0, 0], 3.00, 4.95),
            }
        )
        return events


class FakeMLBFirstFiveKalshiPublicClient:
    def list_markets(self, status="open", limit=1000, mve_filter="exclude", **_kwargs):
        return [
            {
                "ticker": "KXMLBF5-26MAR312140NYYSEA-SEA",
                "event_ticker": "KXMLBF5-26MAR312140NYYSEA",
                "title": "New York Y vs Seattle first 5 innings winner?",
                "status": "active",
                "close_time": "2026-04-02T14:00:00Z",
                "expected_expiration_time": "2026-03-31T02:05:00Z",
                "yes_sub_title": "Seattle wins first 5 innings",
                "no_sub_title": "New York Y wins first 5 innings",
                "yes_ask_dollars": "0.47",
                "no_ask_dollars": "0.58",
                "last_price_dollars": "0.46",
            }
        ]


def test_mlb_first_five_refresh_generates_watchlist(db_session):
    run = run_refresh_cycle(
        db_session,
        provider=FakeMLBSportsProvider(),
        public_client=FakeMLBFirstFiveKalshiPublicClient(),
        sports=["MLB"],
    )
    db_session.commit()

    assert run.status == "completed"
    recommendations = db_session.scalars(select(Recommendation)).all()
    assert len(recommendations) == 1
    assert recommendations[0].side == "yes"

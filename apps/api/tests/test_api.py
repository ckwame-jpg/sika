from datetime import datetime, timezone

from app.models import Event, EventParticipant, Market, MarketSnapshot, Participant, Recommendation, Run


def test_watchlist_and_positions_endpoints(client, db_session):
    home = Participant(external_id="home", sport_key="NBA", display_name="Boston Celtics", short_name="Celtics", participant_type="team")
    away = Participant(external_id="away", sport_key="NBA", display_name="Miami Heat", short_name="Heat", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id="evt-1",
        sport_key="NBA",
        name="Miami Heat at Boston Celtics",
        status="scheduled",
        starts_at=datetime(2026, 3, 30, 22, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )
    market = Market(
        ticker="NBA-BOS-MIA",
        sport_key="NBA",
        event_id=event.id,
        title="Jalen Brunson: 30+ points?",
        status="open",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 30.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
        },
    )
    db_session.add(market)
    db_session.flush()
    db_session.add(
        Recommendation(
            event_id=event.id,
            market_id=market.id,
            side="yes",
            action="buy",
            status="active",
            suggested_price=0.54,
            edge=0.08,
            confidence=0.62,
            invalidation="Pull if YES entry moves above 0.5800",
            rationale="Recent form favors Boston",
        )
    )
    db_session.commit()

    watchlist = client.get("/watchlist")
    assert watchlist.status_code == 200
    assert watchlist.json()[0]["ticker"] == "NBA-BOS-MIA"
    assert watchlist.json()[0]["market_family"] == "player_prop"
    assert watchlist.json()[0]["stat_key"] == "points"

    open_position = client.post(
        "/paper-positions",
        json={"ticker": "NBA-BOS-MIA", "side": "yes", "quantity": 3, "entry_price": 0.54},
    )
    assert open_position.status_code == 200

    positions = client.get("/positions")
    assert positions.status_code == 200
    assert len(positions.json()["paper_positions"]) == 1


def test_events_endpoint_serializes_naive_datetimes_as_utc(client, db_session):
    home = Participant(external_id="home-naive", sport_key="NBA", display_name="Charlotte Hornets", short_name="Hornets", participant_type="team")
    away = Participant(external_id="away-naive", sport_key="NBA", display_name="Phoenix Suns", short_name="Suns", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id="evt-naive",
        sport_key="NBA",
        name="Phoenix Suns at Charlotte Hornets",
        status="completed",
        starts_at=datetime(2026, 4, 2, 23, 0),
        completed_at=datetime(2026, 4, 3, 1, 15),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True, score=102, result="loss"),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False, score=108, result="win"),
        ]
    )
    db_session.commit()

    response = client.get("/events")

    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["starts_at"] == "2026-04-02T23:00:00Z"
    assert payload["completed_at"] == "2026-04-03T01:15:00Z"


def test_runs_and_market_history_endpoints(client, db_session):
    market = Market(
        ticker="KXMLBHIT-TEST",
        sport_key="MLB",
        title="Josh Smith: 2+ hits?",
        status="open",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "hits",
            "copilot_threshold": 2.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Josh Smith",
            "copilot_subject_team": "TEX",
        },
    )
    db_session.add(market)
    db_session.flush()
    db_session.add(
        MarketSnapshot(
            market_id=market.id,
            captured_at=datetime.now(timezone.utc),
            yes_bid=0.33,
            yes_ask=0.37,
            no_bid=0.63,
            no_ask=0.67,
            last_price=0.35,
            volume=125.0,
        )
    )
    db_session.add(
        Run(
            kind="refresh",
            status="completed",
            records_processed=42,
            details={
                "total_kalshi_markets_seen": 50,
                "supported_nba_props_seen": 8,
                "supported_mlb_props_seen": 12,
                "recommendations_emitted": 3,
                "watchlist_counts_by_prop_category": {"hits": 2},
            },
        )
    )
    db_session.commit()

    runs = client.get("/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["summary_counts"]["supported_mlb_props_seen"] == 12

    run_detail = client.get(f"/runs/{runs.json()[0]['id']}")
    assert run_detail.status_code == 200
    assert run_detail.json()["details"]["total_kalshi_markets_seen"] == 50

    history = client.get("/markets/KXMLBHIT-TEST/history")
    assert history.status_code == 200
    assert history.json()["ticker"] == "KXMLBHIT-TEST"
    assert history.json()["points"][0]["source"] == "local_snapshot"

    markets = client.get("/markets")
    assert markets.status_code == 200
    assert markets.json()[0]["ticker"] == "KXMLBHIT-TEST"
    assert markets.json()[0]["market_family"] == "player_prop"
    assert markets.json()[0]["latest_snapshot"]["last_price"] == 0.35

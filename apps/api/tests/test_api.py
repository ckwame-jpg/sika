from datetime import datetime, timedelta, timezone

from app.models import Event, EventParticipant, Market, MarketSnapshot, Participant, Prediction, Recommendation, RefreshJob, Run


def _seed_trade_event(
    db_session,
    *,
    prefix: str,
    sport_key: str,
    event_name: str,
    home_name: str,
    home_short: str,
    away_name: str,
    away_short: str,
    starts_at: datetime | None = None,
    status: str = "in_progress",
):
    home = Participant(
        external_id=f"{prefix}-home",
        sport_key=sport_key,
        display_name=home_name,
        short_name=home_short,
        participant_type="team",
    )
    away = Participant(
        external_id=f"{prefix}-away",
        sport_key=sport_key,
        display_name=away_name,
        short_name=away_short,
        participant_type="team",
    )
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id=f"{prefix}-event",
        sport_key=sport_key,
        name=event_name,
        status=status,
        starts_at=starts_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
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


def _add_trade_market(
    db_session,
    *,
    event: Event,
    ticker: str,
    title: str,
    raw_data: dict,
    suggested_price: float,
    edge: float,
    confidence: float,
    selected_side_probability: float,
    side: str = "yes",
    subtitle: str | None = None,
):
    market = Market(
        ticker=ticker,
        sport_key=event.sport_key,
        event_id=event.id,
        title=title,
        subtitle=subtitle,
        status="active",
        raw_data=raw_data,
    )
    db_session.add(market)
    db_session.flush()
    db_session.add(
        Recommendation(
            event_id=event.id,
            market_id=market.id,
            side=side,
            action="buy",
            status="active",
            suggested_price=suggested_price,
            edge=edge,
            confidence=confidence,
            invalidation="Pull if entry drifts too far from model fair value.",
            rationale="Trade desk test recommendation.",
            scoring_diagnostics={
                "selected_side_probability": selected_side_probability,
                "display_market_title": raw_data.get("copilot_display_market_title") or title,
            },
        )
    )
    return market


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
            scoring_diagnostics={
                "selected_side_probability": 0.58,
                "source_type": "combo_derived",
                "source_market_ticker": "KXMVE-NBA-PROPS-TEST",
                "source_market_title": "NBA prop combo",
                "display_market_title": "Jalen Brunson: 30+ points?",
                "source_badge_label": "Combo-derived",
                "context_coverage_score": 0.81,
                "quality_tier": "high",
            },
        )
    )
    db_session.commit()

    watchlist = client.get("/watchlist")
    assert watchlist.status_code == 200
    assert watchlist.json()[0]["ticker"] == "NBA-BOS-MIA"
    assert watchlist.json()[0]["market_family"] == "player_prop"
    assert watchlist.json()[0]["stat_key"] == "points"
    assert watchlist.json()[0]["selected_side_probability"] == 0.58
    assert watchlist.json()[0]["quality_tier"] == "high"
    assert watchlist.json()[0]["source_badge_label"] == "Combo-derived"
    assert watchlist.json()[0]["starts_at"] == "2026-03-30T22:00:00Z"

    open_position = client.post(
        "/paper-positions",
        json={"ticker": "NBA-BOS-MIA", "side": "yes", "quantity": 3, "entry_price": 0.54},
    )
    assert open_position.status_code == 200

    positions = client.get("/positions")
    assert positions.status_code == 200
    assert len(positions.json()["paper_positions"]) == 1


def test_watchlist_diagnostics_endpoint_reports_no_refresh_runs(client):
    diagnostics = client.get("/watchlist/diagnostics")

    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["latest_refresh_run"] is None
    assert payload["latest_refresh_succeeded"] is None
    assert payload["latest_recommendations_emitted"] == 0
    assert payload["current_recommendation_count"] == 0
    assert payload["watchlist_min_edge"] == 0.03
    assert payload["watchlist_min_confidence"] == 0.35
    assert payload["prop_refresh_status"] == "idle"
    assert payload["prop_data_stale"] is True

    watchlist = client.get("/watchlist")
    assert watchlist.status_code == 200
    assert watchlist.json() == []


def test_watchlist_coverage_endpoint_reports_prediction_only_rows(client, db_session):
    now = datetime.now(timezone.utc)
    home = Participant(external_id="coverage-home", sport_key="NBA", display_name="Miami Heat", short_name="Heat", participant_type="team")
    away = Participant(external_id="coverage-away", sport_key="NBA", display_name="Washington Wizards", short_name="Wizards", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    event = Event(
        external_id="coverage-evt",
        sport_key="NBA",
        name="Washington Wizards at Miami Heat",
        status="in_progress",
        starts_at=now + timedelta(hours=1),
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
        ticker="KXNBAPTS-COVERAGE-1",
        sport_key="NBA",
        event_id=event.id,
        title="Bam Adebayo: 15+ points?",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 15.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Bam Adebayo",
            "copilot_subject_team": "MIA",
        },
    )
    db_session.add(market)
    db_session.flush()
    db_session.add(
        MarketSnapshot(
            market_id=market.id,
            captured_at=now,
            yes_ask=0.47,
            no_ask=0.56,
            last_price=0.48,
        )
    )
    db_session.add(
        Prediction(
            event_id=event.id,
            market_id=market.id,
            ticker=market.ticker,
            sport_key="NBA",
            event_name=event.name,
            market_title=market.title,
            market_family="player_prop",
            market_kind="player_prop",
            stat_key="points",
            threshold=15.0,
            subject_name="Bam Adebayo",
            subject_team="MIA",
            capture_scope="coverage",
            side="yes",
            action="buy",
            suggested_price=0.47,
            fair_yes_price=0.58,
            fair_no_price=0.42,
            edge=0.11,
            confidence=0.67,
            model_name="heuristic-v1",
            invalidation="Pull if YES entry moves above 0.6200",
            rationale="Coverage prediction for the current slate.",
            reasons=["Using stale cached prop context while live ESPN refresh catches up."],
            features={"uses_stale_prop_context": True},
            scoring_diagnostics={
                "selected_side_probability": 0.58,
                "display_market_title": market.title,
                "context_coverage_score": 0.82,
                "quality_tier": "high",
            },
            market_status_at_capture="active",
            settlement_status="pending",
            prediction_outcome="pending",
            captured_at=now,
        )
    )
    db_session.commit()

    response = client.get("/watchlist/coverage?sport=NBA")

    assert response.status_code == 200
    row = response.json()[0]
    assert row["ticker"] == market.ticker
    assert row["coverage_status"] == "prediction"
    assert row["prop_context_stale"] is True
    assert row["latest_prediction"]["capture_scope"] == "coverage"


def test_refresh_jobs_enqueues_current_slate_job(client, db_session):
    response = client.post("/jobs/refresh")

    assert response.status_code == 202
    payload = response.json()
    assert payload["kind"] == "refresh"
    assert payload["scope"] == "current_slate"
    assert payload["status"] == "queued"

    job = db_session.get(RefreshJob, payload["job_id"])
    assert job is not None
    assert job.kind == "refresh"
    assert job.scope == "current_slate"
    assert job.reason == "manual"
    assert job.status == "queued"

    detail = client.get(f"/jobs/{job.id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == job.id
    assert detail.json()["status"] == "queued"


def test_watchlist_diagnostics_endpoint_reports_zero_emission_refresh(client, db_session):
    run = Run(
        kind="refresh",
        status="completed",
        started_at=datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 3, 12, 4, tzinfo=timezone.utc),
        records_processed=128,
        details={
            "supported_markets_kept": 64,
            "recommendations_emitted": 0,
            "watchlist_counts_by_sport": {},
        },
    )
    db_session.add(run)
    db_session.commit()

    diagnostics = client.get("/watchlist/diagnostics")

    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["latest_refresh_run"]["id"] == run.id
    assert payload["latest_refresh_succeeded"] is True
    assert payload["latest_supported_markets_kept"] == 64
    assert payload["latest_recommendations_emitted"] == 0
    assert payload["latest_watchlist_counts_by_sport"] == {}


def test_watchlist_diagnostics_endpoint_reports_failed_refresh(client, db_session):
    run = Run(
        kind="refresh",
        status="failed",
        started_at=datetime(2026, 4, 3, 13, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 3, 13, 2, tzinfo=timezone.utc),
        records_processed=41,
        error_message="ESPN upstream timeout",
        details={
            "supported_markets_kept": 17,
            "recommendations_emitted": 0,
            "watchlist_counts_by_sport": {},
        },
    )
    db_session.add(run)
    db_session.commit()

    diagnostics = client.get("/watchlist/diagnostics")

    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["latest_refresh_run"]["id"] == run.id
    assert payload["latest_refresh_run"]["status"] == "failed"
    assert payload["latest_refresh_succeeded"] is False
    assert payload["latest_refresh_run"]["error_message"] == "ESPN upstream timeout"


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


def test_trade_desk_groups_game_lines_props_and_research_rows(client, db_session):
    refresh_finished_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    db_session.add(
        Run(
            kind="refresh",
            status="completed",
            started_at=refresh_finished_at - timedelta(minutes=4),
            finished_at=refresh_finished_at,
            records_processed=17,
        )
    )

    nba_event = _seed_trade_event(
        db_session,
        prefix="trade-nba",
        sport_key="NBA",
        event_name="Miami Heat at Boston Celtics",
        home_name="Boston Celtics",
        home_short="BOS",
        away_name="Miami Heat",
        away_short="MIA",
    )
    _seed_trade_event(
        db_session,
        prefix="trade-nfl",
        sport_key="NFL",
        event_name="Chicago Bears at Green Bay Packers",
        home_name="Green Bay Packers",
        home_short="GB",
        away_name="Chicago Bears",
        away_short="CHI",
    )

    _add_trade_market(
        db_session,
        event=nba_event,
        ticker="KXNBAGAME-TRADE-BOS",
        title="Miami Heat at Boston Celtics Winner?",
        raw_data={
            "event_ticker": "KXNBAGAME-TRADE",
            "yes_sub_title": "Boston",
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_subject_name": "Boston Celtics",
        },
        suggested_price=0.56,
        edge=0.08,
        confidence=0.74,
        selected_side_probability=0.64,
    )
    _add_trade_market(
        db_session,
        event=nba_event,
        ticker="KXNBASPREAD-TRADE-BOS",
        title="Boston Celtics wins by over 4.5 points",
        subtitle=nba_event.name,
        raw_data={
            "event_ticker": "KXNBAGAME-TRADE",
            "copilot_market_family": "game_line",
            "copilot_market_kind": "spread",
            "copilot_subject_name": "Boston Celtics",
            "copilot_threshold": 4.5,
            "copilot_direction": "over",
            "copilot_display_market_title": "Boston Celtics wins by over 4.5 points",
        },
        suggested_price=0.49,
        edge=0.08,
        confidence=0.66,
        selected_side_probability=0.57,
    )
    _add_trade_market(
        db_session,
        event=nba_event,
        ticker="KXNBATOTAL-TRADE-220",
        title="Over 220.5 points scored",
        subtitle=nba_event.name,
        raw_data={
            "event_ticker": "KXNBAGAME-TRADE",
            "copilot_market_family": "game_line",
            "copilot_market_kind": "total",
            "copilot_threshold": 220.5,
            "copilot_direction": "over",
            "copilot_display_market_title": "Over 220.5 points scored",
        },
        suggested_price=0.48,
        edge=0.05,
        confidence=0.61,
        selected_side_probability=0.53,
    )
    _add_trade_market(
        db_session,
        event=nba_event,
        ticker="KXNBAPTS-TRADE-TATUM-25",
        title="Jayson Tatum: 25+ points?",
        raw_data={
            "event_ticker": "KXNBAPTS-TRADE",
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 25.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Jayson Tatum",
            "copilot_subject_team": "BOS",
        },
        suggested_price=0.58,
        edge=0.14,
        confidence=0.78,
        selected_side_probability=0.72,
    )
    _add_trade_market(
        db_session,
        event=nba_event,
        ticker="KXNBAPTS-TRADE-TATUM-30",
        title="Jayson Tatum: 30+ points?",
        raw_data={
            "event_ticker": "KXNBAPTS-TRADE",
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 30.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Jayson Tatum",
            "copilot_subject_team": "BOS",
        },
        suggested_price=0.45,
        edge=0.16,
        confidence=0.74,
        selected_side_probability=0.61,
    )
    db_session.commit()

    response = client.get("/trade-desk")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["events"]) == 1

    event = payload["events"][0]
    assert event["event_name"] == "Miami Heat at Boston Celtics"
    assert [line["market_kind"] for line in event["game_lines"]] == ["game_winner", "spread", "total"]
    assert event["game_lines"][0]["display_label"] == "Boston Celtics to win"
    assert event["game_lines"][0]["projected_side_label"] == "Boston Celtics"
    assert event["game_lines"][1]["projected_side_label"] == "Boston Celtics -4.5"
    assert event["game_lines"][2]["projected_side_label"] == "Over 220.5"

    assert len(event["player_props"]) == 1
    player_prop = event["player_props"][0]
    assert player_prop["subject_name"] == "Jayson Tatum"
    thresholds = player_prop["stat_groups"][0]["thresholds"]
    assert [item["threshold"] for item in thresholds] == [25.0, 30.0]
    assert thresholds[1]["is_best"] is True

    research_rows = {row["sport_key"]: row for row in payload["research_sports"]}
    assert research_rows["NFL"]["availability_mode"] == "research_only"
    assert research_rows["NFL"]["events_count"] == 1
    assert "UFC" not in research_rows


def test_trade_desk_hides_non_monotonic_prop_ladders(client, db_session):
    event = _seed_trade_event(
        db_session,
        prefix="trade-bad-ladder",
        sport_key="NBA",
        event_name="Toronto Raptors at Boston Celtics",
        home_name="Boston Celtics",
        home_short="BOS",
        away_name="Toronto Raptors",
        away_short="TOR",
    )

    _add_trade_market(
        db_session,
        event=event,
        ticker="KXNBAGAME-BAD-LADDER-BOS",
        title="Toronto Raptors at Boston Celtics Winner?",
        raw_data={
            "event_ticker": "KXNBAGAME-BAD-LADDER",
            "yes_sub_title": "Boston",
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_subject_name": "Boston Celtics",
        },
        suggested_price=0.57,
        edge=0.07,
        confidence=0.7,
        selected_side_probability=0.64,
    )
    _add_trade_market(
        db_session,
        event=event,
        ticker="KXNBAREB-BAD-LADDER-8",
        title="Scottie Barnes: 8+ rebounds?",
        raw_data={
            "event_ticker": "KXNBAREB-BAD-LADDER",
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "rebounds",
            "copilot_threshold": 8.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Scottie Barnes",
            "copilot_subject_team": "TOR",
        },
        suggested_price=0.61,
        edge=0.127,
        confidence=0.71,
        selected_side_probability=0.737,
    )
    _add_trade_market(
        db_session,
        event=event,
        ticker="KXNBAREB-BAD-LADDER-10",
        title="Scottie Barnes: 10+ rebounds?",
        raw_data={
            "event_ticker": "KXNBAREB-BAD-LADDER",
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "rebounds",
            "copilot_threshold": 10.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Scottie Barnes",
            "copilot_subject_team": "TOR",
        },
        suggested_price=0.80,
        edge=0.112,
        confidence=0.69,
        selected_side_probability=0.912,
    )
    db_session.commit()

    response = client.get("/trade-desk?sport=NBA")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["events"]) == 1
    assert payload["events"][0]["game_lines"][0]["ticker"] == "KXNBAGAME-BAD-LADDER-BOS"
    assert payload["events"][0]["player_props"] == []


def test_trade_desk_filters_mismatched_event_markets(client, db_session):
    event = _seed_trade_event(
        db_session,
        prefix="trade-mismatch",
        sport_key="NBA",
        event_name="Los Angeles Lakers at Dallas Mavericks",
        home_name="Dallas Mavericks",
        home_short="DAL",
        away_name="Los Angeles Lakers",
        away_short="LAL",
    )

    _add_trade_market(
        db_session,
        event=event,
        ticker="KXNBAGAME-MISMATCH-LAC",
        title="Los Angeles Clippers at Sacramento Kings Winner?",
        raw_data={
            "event_ticker": "KXNBAGAME-MISMATCH",
            "yes_sub_title": "Los Angeles Clippers",
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_subject_name": "Los Angeles Clippers",
        },
        suggested_price=0.44,
        edge=0.09,
        confidence=0.63,
        selected_side_probability=0.53,
    )
    db_session.commit()

    response = client.get("/trade-desk?sport=NBA")

    assert response.status_code == 200
    assert response.json()["events"] == []


def test_sports_availability_reports_live_and_research_only_modes(client, db_session):
    now = datetime.now(timezone.utc)
    db_session.add(
        Run(
            kind="refresh",
            status="completed",
            started_at=now - timedelta(minutes=9),
            finished_at=now - timedelta(minutes=5),
            records_processed=9,
        )
    )
    nba_event = _seed_trade_event(
        db_session,
        prefix="availability-nba",
        sport_key="NBA",
        event_name="Phoenix Suns at Denver Nuggets",
        home_name="Denver Nuggets",
        home_short="DEN",
        away_name="Phoenix Suns",
        away_short="PHX",
        starts_at=now + timedelta(hours=2),
        status="scheduled",
    )
    _seed_trade_event(
        db_session,
        prefix="availability-nfl",
        sport_key="NFL",
        event_name="Minnesota Vikings at Chicago Bears",
        home_name="Chicago Bears",
        home_short="CHI",
        away_name="Minnesota Vikings",
        away_short="MIN",
        starts_at=now + timedelta(days=1),
        status="scheduled",
    )
    _add_trade_market(
        db_session,
        event=nba_event,
        ticker="KXNBAGAME-AVAIL-DEN",
        title="Phoenix Suns at Denver Nuggets Winner?",
        raw_data={
            "event_ticker": "KXNBAGAME-AVAIL",
            "yes_sub_title": "Denver",
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_subject_name": "Denver Nuggets",
        },
        suggested_price=0.55,
        edge=0.06,
        confidence=0.68,
        selected_side_probability=0.61,
    )
    db_session.commit()

    response = client.get("/sports/availability")

    assert response.status_code == 200
    payload = {row["sport_key"]: row for row in response.json()}
    assert list(payload) == ["NBA", "NFL", "MLB", "SOCCER", "TENNIS"]
    assert payload["NBA"]["availability_mode"] == "live"
    assert payload["NBA"]["events_count"] == 1
    assert payload["NBA"]["recommendations_count"] == 1
    assert payload["NFL"]["availability_mode"] == "research_only"
    assert payload["NFL"]["events_count"] == 1
    assert payload["MLB"]["availability_mode"] == "live"
    assert "UFC" not in payload

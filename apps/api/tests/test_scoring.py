from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Event, EventParticipant, Market, MarketSnapshot, Participant, SignalSnapshot
from app.services.scoring import ResolvedPropSubject, score_event


def test_score_event_aligns_yes_price_to_market_target(db_session):
    home = Participant(external_id="home", sport_key="NBA", display_name="Atlanta Hawks", short_name="Atlanta", participant_type="team")
    away = Participant(external_id="away", sport_key="NBA", display_name="Boston Celtics", short_name="Boston", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    for index in range(4):
        past_event = Event(
            external_id=f"past-{index}",
            sport_key="NBA",
            name="Boston Celtics at Atlanta Hawks",
            status="completed",
            starts_at=datetime(2026, 3, 20 + index, 0, 0, tzinfo=timezone.utc),
        )
        db_session.add(past_event)
        db_session.flush()
        db_session.add_all(
            [
                EventParticipant(
                    event_id=past_event.id,
                    participant_id=home.id,
                    role="home",
                    is_home=True,
                    score=118,
                    result="win",
                ),
                EventParticipant(
                    event_id=past_event.id,
                    participant_id=away.id,
                    role="away",
                    is_home=False,
                    score=104,
                    result="loss",
                ),
            ]
        )

    event = Event(
        external_id="future-1",
        sport_key="NBA",
        name="Boston Celtics at Atlanta Hawks",
        status="scheduled",
        starts_at=datetime(2026, 3, 31, 0, 0, tzinfo=timezone.utc),
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
        ticker="KXNBAGAME-26MAR30BOSATL-BOS",
        sport_key="NBA",
        event_id=event.id,
        title="Boston at Atlanta Winner?",
        status="active",
        raw_data={
            "event_ticker": "KXNBAGAME-26MAR30BOSATL",
            "yes_sub_title": "Boston",
            "copilot_market_kind": "game_winner",
        },
    )
    snapshot = MarketSnapshot(
        market=market,
        yes_ask=0.45,
        no_ask=0.56,
        last_price=0.45,
    )
    db_session.add_all([market, snapshot])
    db_session.commit()

    recommendation = score_event(db_session, event, market, snapshot)

    assert recommendation is not None
    assert recommendation.side == "no"
    assert recommendation.edge > 0
    assert "Boston Celtics" in recommendation.rationale


def test_score_event_persists_signal_even_when_no_recommendation_is_emitted(db_session):
    home = Participant(external_id="home-signal", sport_key="NBA", display_name="Atlanta Hawks", short_name="Atlanta", participant_type="team")
    away = Participant(external_id="away-signal", sport_key="NBA", display_name="Boston Celtics", short_name="Boston", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    for index in range(4):
        past_event = Event(
            external_id=f"signal-past-{index}",
            sport_key="NBA",
            name="Boston Celtics at Atlanta Hawks",
            status="completed",
            starts_at=datetime(2026, 3, 20 + index, 0, 0, tzinfo=timezone.utc),
        )
        db_session.add(past_event)
        db_session.flush()
        db_session.add_all(
            [
                EventParticipant(event_id=past_event.id, participant_id=home.id, role="home", is_home=True, score=118, result="win"),
                EventParticipant(event_id=past_event.id, participant_id=away.id, role="away", is_home=False, score=104, result="loss"),
            ]
        )

    event = Event(
        external_id="signal-future-1",
        sport_key="NBA",
        name="Boston Celtics at Atlanta Hawks",
        status="scheduled",
        starts_at=datetime(2026, 3, 31, 0, 0, tzinfo=timezone.utc),
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
        ticker="KXNBAGAME-26MAR30BOSATL-BOS-SIGNAL",
        sport_key="NBA",
        event_id=event.id,
        title="Boston at Atlanta Winner?",
        status="active",
        raw_data={
            "event_ticker": "KXNBAGAME-26MAR30BOSATL",
            "yes_sub_title": "Boston",
            "copilot_market_kind": "game_winner",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.95, no_ask=0.95, last_price=0.95)
    db_session.add_all([market, snapshot])
    db_session.commit()

    recommendation = score_event(db_session, event, market, snapshot)
    db_session.flush()

    assert recommendation is None
    signal = db_session.scalar(select(SignalSnapshot).where(SignalSnapshot.market_id == market.id))
    assert signal is not None
    assert signal.fair_yes_price > 0


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
        "raw": {
            "competitions": [
                {
                    "competitors": [
                        competitor("home", home_name, home_abbr, "home", home_lines, home_era),
                        competitor("away", away_name, away_abbr, "away", away_lines, away_era),
                    ]
                }
            ]
        }
    }


def test_score_event_supports_mlb_first_five_winner_markets(db_session):
    home = Participant(external_id="home-mlb", sport_key="MLB", display_name="Seattle Mariners", short_name="Mariners", participant_type="team")
    away = Participant(external_id="away-mlb", sport_key="MLB", display_name="New York Yankees", short_name="Yankees", participant_type="team")
    athletics = Participant(external_id="oak-mlb", sport_key="MLB", display_name="Oakland Athletics", short_name="Athletics", participant_type="team")
    red_sox = Participant(external_id="bos-mlb", sport_key="MLB", display_name="Boston Red Sox", short_name="Red Sox", participant_type="team")
    db_session.add_all([home, away, athletics, red_sox])
    db_session.flush()

    for index in range(4):
        home_past = Event(
            external_id=f"home-past-{index}",
            sport_key="MLB",
            name="Oakland Athletics at Seattle Mariners",
            status="completed",
            starts_at=datetime(2026, 3, 20 + index, 0, 0, tzinfo=timezone.utc),
            raw_data=_mlb_raw_event("Seattle Mariners", "Oakland Athletics", "SEA", "OAK", [2, 1, 0, 1, 0], [0, 0, 1, 0, 0], 3.10, 4.80),
        )
        db_session.add(home_past)
        db_session.flush()
        db_session.add_all(
            [
                EventParticipant(event_id=home_past.id, participant_id=home.id, role="home", is_home=True, score=6, result="win"),
                EventParticipant(event_id=home_past.id, participant_id=athletics.id, role="away", is_home=False, score=2, result="loss"),
            ]
        )

        away_past = Event(
            external_id=f"away-past-{index}",
            sport_key="MLB",
            name="New York Yankees at Boston Red Sox",
            status="completed",
            starts_at=datetime(2026, 3, 24 + index, 0, 0, tzinfo=timezone.utc),
            raw_data=_mlb_raw_event("Boston Red Sox", "New York Yankees", "BOS", "NYY", [2, 0, 1, 0, 0], [0, 0, 0, 1, 0], 3.20, 4.95),
        )
        db_session.add(away_past)
        db_session.flush()
        db_session.add_all(
            [
                EventParticipant(event_id=away_past.id, participant_id=red_sox.id, role="home", is_home=True, score=3, result="win"),
                EventParticipant(event_id=away_past.id, participant_id=away.id, role="away", is_home=False, score=1, result="loss"),
            ]
        )

    event = Event(
        external_id="future-mlb-1",
        sport_key="MLB",
        name="New York Yankees at Seattle Mariners",
        status="scheduled",
        starts_at=datetime(2026, 3, 31, 2, 10, tzinfo=timezone.utc),
        raw_data=_mlb_raw_event("Seattle Mariners", "New York Yankees", "SEA", "NYY", [0, 0, 0, 0, 0], [0, 0, 0, 0, 0], 3.05, 4.85),
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
        ticker="KXMLBF5-26MAR312140NYYSEA-SEA",
        sport_key="MLB",
        event_id=event.id,
        title="New York Y vs Seattle first 5 innings winner?",
        status="active",
        raw_data={
            "event_ticker": "KXMLBF5-26MAR312140NYYSEA",
            "yes_sub_title": "Seattle wins first 5 innings",
            "copilot_market_kind": "first_five_winner",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.47, no_ask=0.58, last_price=0.46)
    db_session.add_all([market, snapshot])
    db_session.commit()

    recommendation = score_event(db_session, event, market, snapshot)

    assert recommendation is not None
    assert recommendation.side == "yes"
    assert recommendation.edge > 0
    assert "first-5 win rate" in recommendation.rationale


def test_score_event_supports_nba_player_props(db_session):
    knicks = Participant(external_id="nyk", sport_key="NBA", display_name="New York Knicks", short_name="Knicks", participant_type="team")
    celtics = Participant(external_id="bos", sport_key="NBA", display_name="Boston Celtics", short_name="Celtics", participant_type="team")
    db_session.add_all([knicks, celtics])
    db_session.flush()

    event = Event(
        external_id="future-nba-prop",
        sport_key="NBA",
        name="Boston Celtics at New York Knicks",
        status="scheduled",
        starts_at=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=knicks.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=celtics.id, role="away", is_home=False),
        ]
    )

    market = Market(
        ticker="KXNBAPTS-26APR01BOSNYK-NYKJBRUNSON11-30",
        sport_key="NBA",
        event_id=event.id,
        title="Jalen Brunson: 30+ points?",
        status="active",
        raw_data={
            "event_ticker": "KXNBAPTS-26APR01BOSNYK",
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "points",
            "copilot_threshold": 30.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Jalen Brunson",
            "copilot_subject_team": "NYK",
            "copilot_requires_lineup": True,
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.28, no_ask=0.76, last_price=0.27)
    db_session.add_all([market, snapshot])
    db_session.commit()

    recent_logs = []
    for index in range(10):
        opponent = "Boston Celtics" if index < 2 else "Miami Heat"
        recent_logs.append(
            {
                "location": "home" if index % 2 == 0 else "away",
                "opponent": opponent,
                "opponent_abbreviation": "BOS" if opponent == "Boston Celtics" else "MIA",
                "raw_metrics": {
                    "minutes": 35.0,
                    "points": 31.0 if index < 5 else 28.0,
                    "rebounds": 4.0,
                    "assists": 7.0,
                    "steals": 1.0,
                    "blocks": 0.0,
                    "turnovers": 2.0,
                    "field_goals_attempted": 22.0,
                },
            }
        )

    class FakeResolver:
        def resolve(self, sport_key, subject_name, team_hint=None):
            return ResolvedPropSubject(
                sport_key=sport_key,
                athlete_id="3934672",
                display_name=subject_name,
                team_name="New York Knicks",
                season=2026,
                game_logs=recent_logs,
            )

    recommendation = score_event(db_session, event, market, snapshot, resolver=FakeResolver())

    assert recommendation is not None
    assert recommendation.side == "yes"
    assert recommendation.edge > 0
    assert "Jalen Brunson" in recommendation.rationale
    assert "starting lineup" in recommendation.invalidation.lower()

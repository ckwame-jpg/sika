from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.models import Event, EventParticipant, Market, MarketSnapshot, Participant, Recommendation, SignalSnapshot
from app.services.scoring import (
    ResolvedPropSubject,
    ScoredRecommendation,
    _days_since_latest_log,
    _days_since_participant_game,
    _enforce_prop_monotonicity,
    _games_in_recent_window,
    _latest_home_state,
    _recent_first_five_results,
    _recent_participant_results,
    _recent_score_pairs,
    _schedule_context,
    regenerate_watchlist,
    score_event,
)


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


def test_score_event_suppresses_heuristic_winner_longshots_below_probability_floor(db_session):
    home = Participant(external_id="okc-home", sport_key="NBA", display_name="Oklahoma City Thunder", short_name="OKC", participant_type="team")
    away = Participant(external_id="uta-away", sport_key="NBA", display_name="Utah Jazz", short_name="Utah", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    for index in range(8):
        thunder_past = Event(
            external_id=f"okc-past-{index}",
            sport_key="NBA",
            name="Phoenix Suns at Oklahoma City Thunder",
            status="completed",
            starts_at=datetime(2026, 3, 10 + index, 0, 0, tzinfo=timezone.utc),
        )
        jazz_past = Event(
            external_id=f"uta-past-{index}",
            sport_key="NBA",
            name="Utah Jazz at Denver Nuggets",
            status="completed",
            starts_at=datetime(2026, 3, 10 + index, 2, 0, tzinfo=timezone.utc),
        )
        db_session.add_all([thunder_past, jazz_past])
        db_session.flush()
        db_session.add_all(
            [
                EventParticipant(event_id=thunder_past.id, participant_id=home.id, role="home", is_home=True, score=124, result="win"),
                EventParticipant(event_id=thunder_past.id, participant_id=away.id, role="away", is_home=False, score=101, result="loss"),
                EventParticipant(event_id=jazz_past.id, participant_id=away.id, role="away", is_home=False, score=98, result="loss"),
                EventParticipant(event_id=jazz_past.id, participant_id=home.id, role="home", is_home=True, score=118, result="win"),
            ]
        )

    event = Event(
        external_id="future-uta-okc",
        sport_key="NBA",
        name="Utah Jazz at Oklahoma City Thunder",
        status="scheduled",
        starts_at=datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc),
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
        ticker="KXNBAGAME-26APR05UTAOKC-UTA",
        sport_key="NBA",
        event_id=event.id,
        title="Utah at Oklahoma City Winner?",
        status="active",
        raw_data={
            "event_ticker": "KXNBAGAME-26APR05UTAOKC",
            "yes_sub_title": "Utah",
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_subject_name": "Utah Jazz",
        },
    )
    snapshot = MarketSnapshot(market=market, yes_ask=0.05, no_ask=0.97, last_price=0.05)
    db_session.add_all([market, snapshot])
    db_session.commit()

    recommendation = score_event(db_session, event, market, snapshot)
    db_session.flush()

    assert recommendation is None
    signal = db_session.scalar(select(SignalSnapshot).where(SignalSnapshot.market_id == market.id))
    assert signal is not None
    assert signal.scoring_diagnostics["selected_side_probability"] < 0.2
    assert "winner_selected_probability_floor" in signal.scoring_diagnostics["suppression_reasons"]


def test_regenerate_watchlist_collapses_inverse_winner_duplicates(db_session):
    settings = get_settings()
    original_floor = settings.watchlist_min_selected_prob_heuristic_winner
    settings.watchlist_min_selected_prob_heuristic_winner = 0.05

    home = Participant(external_id="okc-home-dedupe", sport_key="NBA", display_name="Oklahoma City Thunder", short_name="OKC", participant_type="team")
    away = Participant(external_id="uta-away-dedupe", sport_key="NBA", display_name="Utah Jazz", short_name="Utah", participant_type="team")
    db_session.add_all([home, away])
    db_session.flush()

    for index in range(4):
        thunder_past = Event(
            external_id=f"dedupe-okc-{index}",
            sport_key="NBA",
            name="Phoenix Suns at Oklahoma City Thunder",
            status="completed",
            starts_at=datetime(2026, 3, 18 + index, 0, 0, tzinfo=timezone.utc),
        )
        jazz_past = Event(
            external_id=f"dedupe-uta-{index}",
            sport_key="NBA",
            name="Utah Jazz at Denver Nuggets",
            status="completed",
            starts_at=datetime(2026, 3, 18 + index, 2, 0, tzinfo=timezone.utc),
        )
        db_session.add_all([thunder_past, jazz_past])
        db_session.flush()
        db_session.add_all(
            [
                EventParticipant(event_id=thunder_past.id, participant_id=home.id, role="home", is_home=True, score=121, result="win"),
                EventParticipant(event_id=thunder_past.id, participant_id=away.id, role="away", is_home=False, score=104, result="loss"),
                EventParticipant(event_id=jazz_past.id, participant_id=away.id, role="away", is_home=False, score=101, result="loss"),
                EventParticipant(event_id=jazz_past.id, participant_id=home.id, role="home", is_home=True, score=119, result="win"),
            ]
        )

    event = Event(
        external_id="future-dedupe-uta-okc",
        sport_key="NBA",
        name="Utah Jazz at Oklahoma City Thunder",
        status="scheduled",
        starts_at=datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=home.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=away.id, role="away", is_home=False),
        ]
    )

    utah_market = Market(
        ticker="KXNBAGAME-26APR05UTAOKC-UTA-DEDUP",
        sport_key="NBA",
        event_id=event.id,
        title="Utah at Oklahoma City Winner?",
        status="active",
        raw_data={
            "event_ticker": "KXNBAGAME-26APR05UTAOKC",
            "yes_sub_title": "Utah",
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_subject_name": "Utah Jazz",
        },
    )
    okc_market = Market(
        ticker="KXNBAGAME-26APR05UTAOKC-OKC-DEDUP",
        sport_key="NBA",
        event_id=event.id,
        title="Utah at Oklahoma City Winner?",
        status="active",
        raw_data={
            "event_ticker": "KXNBAGAME-26APR05UTAOKC",
            "yes_sub_title": "Oklahoma City",
            "copilot_market_family": "winner",
            "copilot_market_kind": "game_winner",
            "copilot_subject_name": "Oklahoma City Thunder",
        },
    )
    db_session.add_all(
        [
            utah_market,
            okc_market,
            MarketSnapshot(market=utah_market, yes_ask=0.08, no_ask=0.95, last_price=0.08),
            MarketSnapshot(market=okc_market, yes_ask=0.92, no_ask=0.10, last_price=0.92),
        ]
    )
    db_session.commit()

    try:
        summary = regenerate_watchlist(db_session)
        db_session.flush()

        recommendations = db_session.scalars(select(Recommendation).order_by(Recommendation.id.asc())).all()
        assert len(recommendations) == 1
        assert summary.inverse_winner_duplicates_collapsed == 1
        assert recommendations[0].scoring_diagnostics["selected_subject_name"] == "Utah Jazz"
    finally:
        settings.watchlist_min_selected_prob_heuristic_winner = original_floor


def test_enforce_prop_monotonicity_preserves_recommendation_with_diagnostic_flag():
    lower_market = Market(
        ticker="KXNBAREB-LOWER",
        sport_key="NBA",
        event_id=7,
        title="Scottie Barnes: 8+ rebounds?",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "rebounds",
            "copilot_threshold": 8.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Scottie Barnes",
            "copilot_subject_team": "TOR",
        },
    )
    higher_market = Market(
        ticker="KXNBAREB-HIGHER",
        sport_key="NBA",
        event_id=7,
        title="Scottie Barnes: 10+ rebounds?",
        status="active",
        raw_data={
            "copilot_market_family": "player_prop",
            "copilot_market_kind": "player_prop",
            "copilot_stat_key": "rebounds",
            "copilot_threshold": 10.0,
            "copilot_direction": "over",
            "copilot_subject_name": "Scottie Barnes",
            "copilot_subject_team": "TOR",
        },
    )

    lower_scored = ScoredRecommendation(
        recommendation=Recommendation(
            event_id=7,
            market_id=1,
            side="yes",
            action="buy",
            status="active",
            suggested_price=0.61,
            edge=0.127,
            confidence=0.71,
            invalidation="Pull if YES entry moves above 0.6500",
            rationale="Lower ladder rung remains favored.",
            scoring_diagnostics={"selected_side_probability": 0.737},
        ),
        signal=SignalSnapshot(
            event_id=7,
            market_id=1,
            confidence=0.71,
            fair_yes_price=0.737,
            fair_no_price=0.263,
            edge=0.127,
            reasons=[],
            features={},
            scoring_diagnostics={},
        ),
        metadata=lower_market.raw_data or {},
    )
    higher_scored = ScoredRecommendation(
        recommendation=Recommendation(
            event_id=7,
            market_id=2,
            side="yes",
            action="buy",
            status="active",
            suggested_price=0.80,
            edge=0.112,
            confidence=0.69,
            invalidation="Pull if YES entry moves above 0.8400",
            rationale="Higher ladder rung is too aggressive here.",
            scoring_diagnostics={"selected_side_probability": 0.912},
        ),
        signal=SignalSnapshot(
            event_id=7,
            market_id=2,
            confidence=0.69,
            fair_yes_price=0.912,
            fair_no_price=0.088,
            edge=0.112,
            reasons=[],
            features={},
            scoring_diagnostics={},
        ),
        metadata=higher_market.raw_data or {},
    )

    scored_recommendations = [
        (lower_market, lower_scored),
        (higher_market, higher_scored),
    ]

    _enforce_prop_monotonicity(scored_recommendations)

    assert higher_scored.signal.fair_yes_price == 0.737
    assert higher_scored.signal.fair_no_price == 0.263
    assert higher_scored.recommendation is not None
    assert higher_scored.recommendation.edge == round(0.737 - 0.80, 4)
    assert higher_scored.signal.scoring_diagnostics["monotonicity_adjusted"] is True
    assert higher_scored.signal.scoring_diagnostics.get("monotonicity_edge_below_min") is True


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


def test_thin_sample_penalty_lowers_confidence_and_selection_score_without_changing_edge(db_session):
    def seed(prefix: str, *, past_games: int):
        home = Participant(
            external_id=f"{prefix}-home",
            sport_key="NBA",
            display_name=f"{prefix} Home",
            short_name=f"{prefix}H",
            participant_type="team",
        )
        away = Participant(
            external_id=f"{prefix}-away",
            sport_key="NBA",
            display_name=f"{prefix} Away",
            short_name=f"{prefix}A",
            participant_type="team",
        )
        db_session.add_all([home, away])
        db_session.flush()

        for index in range(past_games):
            past_event = Event(
                external_id=f"{prefix}-past-{index}",
                sport_key="NBA",
                name=f"{prefix} Away at {prefix} Home",
                status="completed",
                starts_at=datetime(2026, 3, 1 + index, 0, 0, tzinfo=timezone.utc),
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
            external_id=f"{prefix}-future",
            sport_key="NBA",
            name=f"{prefix} Away at {prefix} Home",
            status="scheduled",
            starts_at=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
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
            ticker=f"{prefix}-market",
            sport_key="NBA",
            event_id=event.id,
            title=f"{prefix} winner?",
            status="active",
            raw_data={
                "yes_sub_title": f"{prefix} Home",
                "copilot_market_kind": "game_winner",
            },
        )
        snapshot = MarketSnapshot(market=market, yes_ask=0.46, no_ask=0.58, last_price=0.46)
        db_session.add_all([market, snapshot])
        db_session.commit()
        recommendation = score_event(db_session, event, market, snapshot)
        db_session.flush()
        signal = db_session.scalar(select(SignalSnapshot).where(SignalSnapshot.market_id == market.id))
        return recommendation, signal

    thin_recommendation, thin_signal = seed("thin", past_games=4)
    deep_recommendation, deep_signal = seed("deep", past_games=10)

    assert thin_recommendation is not None and deep_recommendation is not None
    assert thin_signal is not None and deep_signal is not None
    assert round(thin_recommendation.edge, 4) == round(deep_recommendation.edge, 4)
    assert round(thin_signal.edge, 4) == round(deep_signal.edge, 4)
    assert thin_recommendation.confidence < deep_recommendation.confidence
    assert thin_recommendation.selection_score < deep_recommendation.selection_score


def test_missing_context_penalty_lowers_prop_confidence_and_selection_score(db_session):
    knicks = Participant(external_id="ctx-nyk", sport_key="NBA", display_name="New York Knicks", short_name="Knicks", participant_type="team")
    celtics = Participant(external_id="ctx-bos", sport_key="NBA", display_name="Boston Celtics", short_name="Celtics", participant_type="team")
    db_session.add_all([knicks, celtics])
    db_session.flush()

    recent_logs = [
        {
            "game_date": datetime(2026, 3, 30 - index, 0, 0, tzinfo=timezone.utc),
            "location": "home" if index % 2 == 0 else "away",
            "opponent": "Boston Celtics",
            "opponent_abbreviation": "BOS",
            "raw_metrics": {
                "minutes": 35.0,
                "points": 30.0 if index < 5 else 28.0,
                "rebounds": 4.0,
                "assists": 7.0,
                "steals": 1.0,
                "blocks": 0.0,
                "turnovers": 2.0,
                "field_goals_attempted": 21.0,
            },
        }
        for index in range(10)
    ]

    class MatchingResolver:
        def resolve(self, sport_key, subject_name, team_hint=None):
            return ResolvedPropSubject(
                sport_key=sport_key,
                athlete_id="ctx-player",
                display_name=subject_name,
                team_name="New York Knicks",
                season=2026,
                game_logs=recent_logs,
            )

    class MissingContextResolver:
        def resolve(self, sport_key, subject_name, team_hint=None):
            return ResolvedPropSubject(
                sport_key=sport_key,
                athlete_id="ctx-player",
                display_name=subject_name,
                team_name=None,
                season=2026,
                game_logs=recent_logs,
            )

    def seed(prefix: str, *, subject_team: str, resolver):
        event = Event(
            external_id=f"{prefix}-future",
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
            ticker=f"{prefix}-market",
            sport_key="NBA",
            event_id=event.id,
            title="Jalen Brunson: 30+ points?",
            status="active",
            raw_data={
                "copilot_market_family": "player_prop",
                "copilot_market_kind": "player_prop",
                "copilot_stat_key": "points",
                "copilot_threshold": 30.0,
                "copilot_direction": "over",
                "copilot_subject_name": "Jalen Brunson",
                "copilot_subject_team": subject_team,
                "copilot_requires_lineup": True,
            },
        )
        snapshot = MarketSnapshot(market=market, yes_ask=0.28, no_ask=0.76, last_price=0.27)
        db_session.add_all([market, snapshot])
        db_session.commit()
        recommendation = score_event(db_session, event, market, snapshot, resolver=resolver)
        db_session.flush()
        signal = db_session.scalar(select(SignalSnapshot).where(SignalSnapshot.market_id == market.id))
        return recommendation, signal

    matched_recommendation, matched_signal = seed("ctx-match", subject_team="NYK", resolver=MatchingResolver())
    missing_recommendation, missing_signal = seed("ctx-missing", subject_team="UNK", resolver=MissingContextResolver())

    assert matched_recommendation is not None and missing_recommendation is not None
    assert matched_signal is not None and missing_signal is not None
    assert missing_signal.confidence < matched_signal.confidence
    assert missing_recommendation.selection_score < matched_recommendation.selection_score


def test_scoring_functions_handle_none_starts_at(db_session):
    """All datetime helpers must tolerate before=None without crashing."""
    participant = Participant(
        external_id="none-dt-player",
        sport_key="NBA",
        display_name="Test Player",
        short_name="TST",
        participant_type="team",
    )
    db_session.add(participant)
    db_session.flush()

    assert _days_since_participant_game(db_session, participant.id, None) is None
    assert _days_since_latest_log([{"game_date": datetime(2026, 3, 1, tzinfo=timezone.utc)}], None) is None
    assert _days_since_latest_log([], None) is None
    assert _recent_participant_results(db_session, participant.id, None) == []
    assert _recent_first_five_results(db_session, participant.id, None) == []
    assert _games_in_recent_window(db_session, participant.id, None, days=7) == 0
    assert _latest_home_state(db_session, participant.id, None) is None
    assert _recent_score_pairs(db_session, participant.id, None) == []

    context = _schedule_context(db_session, participant.id, None)
    assert context["days_rest"] is None
    assert context["games_last_4"] == 0
    assert context["games_last_7"] == 0
    assert context["back_to_back"] is False
    assert context["last_home_state"] is None

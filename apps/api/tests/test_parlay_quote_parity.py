"""Parity coverage for the canonical auto/paper/tray quote engine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Event,
    EventParticipant,
    Market,
    OperatorSetting,
    ParlayPrediction,
    Participant,
    Prediction,
    Recommendation,
    SignalSnapshot,
)
from app.schemas import PaperParlayCreate, PaperParlayLegCreate
from app.services.parlay_correlation_cache import CACHE_KEY
from app.services.paper_parlays import create_paper_parlay
from app.services.parlay_quotes import load_quote_empirical_correlations
from app.services import parlays as parlays_module
from app.services.parlays import (
    ParlayCandidateInput,
    _correlation_adjusted_joint_probability,
    _count_correlation_pairs,
    capture_parlay_artifacts,
)


def _add_event(
    db: Session,
    *,
    prefix: str,
    sport: str,
    home: str,
    away: str,
) -> Event:
    participants = [
        Participant(
            external_id=f"{prefix}-{short.lower()}",
            sport_key=sport,
            display_name=short,
            short_name=short,
            participant_type="team",
        )
        for short in (home, away)
    ]
    db.add_all(participants)
    db.flush()
    event = Event(
        external_id=f"{prefix}-event",
        sport_key=sport,
        name=f"{away} at {home}",
        status="scheduled",
        starts_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    db.add(event)
    db.flush()
    db.add_all(
        [
            EventParticipant(
                event_id=event.id,
                participant_id=participant.id,
                role="home" if index == 0 else "away",
                is_home=index == 0,
            )
            for index, participant in enumerate(participants)
        ]
    )
    db.flush()
    return event


def _add_leg(
    db: Session,
    *,
    event: Event,
    ticker: str,
    subject: str,
    team: str,
    stat_key: str,
    probability: float,
    market_family: str = "player_prop",
) -> tuple[Market, Prediction]:
    market = Market(
        ticker=ticker,
        sport_key=event.sport_key,
        event_id=event.id,
        title=ticker,
        status="active",
        raw_data={
            "copilot_subject_name": subject,
            "copilot_subject_team": team,
            "copilot_stat_key": stat_key,
            "copilot_market_family": market_family,
            "copilot_market_kind": market_family,
        },
    )
    db.add(market)
    db.flush()
    prediction = Prediction(
        event_id=event.id,
        market_id=market.id,
        ticker=ticker,
        market_title=ticker,
        side="yes",
        action="buy",
        suggested_price=0.5,
        fair_yes_price=probability,
        fair_no_price=1.0 - probability,
        edge=probability - 0.5,
        confidence=0.75,
        rationale="parity",
        captured_at=datetime.now(timezone.utc),
    )
    db.add(prediction)
    db.flush()
    return market, prediction


def _auto_candidate(market: Market, prediction: Prediction) -> SimpleNamespace:
    return SimpleNamespace(
        event=market.event,
        market=market,
        recommendation=SimpleNamespace(side="yes", confidence=0.75),
        signal=SimpleNamespace(
            fair_yes_price=prediction.fair_yes_price,
            fair_no_price=prediction.fair_no_price,
        ),
        prediction=prediction,
        metadata=dict(market.raw_data or {}),
    )


def _workflow_candidate(
    db: Session,
    *,
    market: Market,
    prediction: Prediction,
    suggested_price: float,
) -> ParlayCandidateInput:
    captured_at = datetime.now(timezone.utc)
    probability = float(prediction.fair_yes_price)
    recommendation = Recommendation(
        event_id=market.event_id,
        market_id=market.id,
        side="yes",
        action="buy",
        status="active",
        suggested_price=suggested_price,
        edge=probability - suggested_price,
        confidence=0.8,
        selection_score=1.0,
        invalidation="test",
        rationale="canonical quote parity",
        scoring_diagnostics={},
        captured_at=captured_at,
    )
    signal = SignalSnapshot(
        event_id=market.event_id,
        market_id=market.id,
        captured_at=captured_at,
        model_name="heuristic-v1",
        confidence=0.8,
        fair_yes_price=probability,
        fair_no_price=1.0 - probability,
        edge=probability - suggested_price,
        reasons=["test"],
        features={},
    )
    db.add_all([recommendation, signal])
    db.flush()
    return ParlayCandidateInput(
        event=market.event,
        market=market,
        recommendation=recommendation,
        signal=signal,
        prediction=prediction,
        metadata=dict(market.raw_data or {}),
    )


def _seed_empirical_cache(db: Session) -> None:
    db.add(
        OperatorSetting(
            key=CACHE_KEY,
            value=json.dumps(
                {
                    "computed_at": datetime.now(timezone.utc).isoformat(),
                    "lookback_days": 90,
                    "min_sample": 30,
                    "estimates": {
                        "shared_subject": {
                            "coefficient": 0.9,
                            "sample_size": 500,
                        },
                        "same_team": {
                            "coefficient": 0.8,
                            "sample_size": 500,
                        },
                        "shared_opponent": {
                            "coefficient": 0.6,
                            "sample_size": 500,
                        },
                    },
                }
            ),
        )
    )
    db.flush()


@pytest.mark.parametrize(
    ("case", "left", "right", "expected_category", "expected_factor"),
    [
        (
            "nba_same_team",
            ("NBA", "NBA-A", "BOS", "NYK", "Player A", "BOS", "points"),
            ("NBA", "NBA-B", "BOS", "NYK", "Player B", "BOS", "rebounds"),
            "same_team",
            0.8,
        ),
        (
            "mlb_cross_event",
            ("MLB", "MLB-A", "NYY", "BOS", "Hitter A", "NYY", "hits"),
            ("MLB", "MLB-B", "LAD", "SD", "Hitter B", "LAD", "hits"),
            None,
            0.0,
        ),
        (
            "wnba_shared_subject",
            ("WNBA", "WNBA-A", "NYL", "CHI", "Player W", "NYL", "points"),
            ("WNBA", "WNBA-B", "NYL", "CHI", "Player W", "NYL", "assists"),
            "shared_subject",
            0.85,
        ),
        (
            "nfl_same_event_stack",
            (
                "NFL",
                "NFL-A",
                "PHI",
                "DAL",
                "Quarterback",
                "PHI",
                "passing_yards",
            ),
            (
                "NFL",
                "NFL-A",
                "PHI",
                "DAL",
                "Receiver",
                "PHI",
                "receiving_yards",
            ),
            "qb_receiver_stack",
            0.55,
        ),
        (
            "nfl_cross_event_stack_shape",
            (
                "NFL",
                "NFL-A",
                "PHI",
                "DAL",
                "Quarterback",
                "PHI",
                "passing_yards",
            ),
            (
                "NFL",
                "NFL-B",
                "PHI",
                "NYG",
                "Receiver",
                "PHI",
                "receiving_yards",
            ),
            "same_team",
            0.8,
        ),
        (
            "mixed_sports",
            ("NBA", "MIX-A", "CLE", "DET", "NBA Player", "CLE", "points"),
            ("MLB", "MIX-B", "NYY", "BOS", "MLB Player", "NYY", "hits"),
            None,
            0.0,
        ),
    ],
)
def test_auto_create_and_quote_endpoint_share_identical_joint(
    case: str,
    left: tuple[str, str, str, str, str, str, str],
    right: tuple[str, str, str, str, str, str, str],
    expected_category: str | None,
    expected_factor: float,
    client: TestClient,
    db_session: Session,
) -> None:
    event_cache: dict[tuple[str, str], Event] = {}
    seeded: list[tuple[Market, Prediction]] = []
    for index, values in enumerate((left, right)):
        sport, event_key, home, away, subject, team, stat_key = values
        cache_key = (sport, event_key)
        event = event_cache.get(cache_key)
        if event is None:
            event = _add_event(
                db_session,
                prefix=f"{case}-{event_key}",
                sport=sport,
                home=home,
                away=away,
            )
            event_cache[cache_key] = event
        seeded.append(
            _add_leg(
                db_session,
                event=event,
                ticker=f"{case}-{index}",
                subject=subject,
                team=team,
                stat_key=stat_key,
                probability=0.6 if index == 0 else 0.5,
            )
        )
    _seed_empirical_cache(db_session)
    db_session.commit()

    combo = tuple(_auto_candidate(*item) for item in seeded)
    pair_counts = _count_correlation_pairs(combo)
    empirical = load_quote_empirical_correlations(db_session)
    assert empirical is not None
    assert empirical["same_team"] is not None
    auto_joint = _correlation_adjusted_joint_probability(
        combo,
        pair_counts,
        empirical_correlations=empirical,
    )
    if expected_category is None:
        assert sum(pair_counts.values()) == 0
    else:
        assert pair_counts[expected_category] == 1
        assert sum(pair_counts.values()) == 1

    legs = [
        PaperParlayLegCreate(
            ticker=market.ticker,
            side="yes",
            suggested_price=0.5 if index == 0 else 0.4,
        )
        for index, (market, _prediction) in enumerate(seeded)
    ]
    created = create_paper_parlay(
        db_session,
        PaperParlayCreate(legs=legs, stake=10.0),
    )
    endpoint = client.post(
        "/paper-parlays/quote",
        json={"legs": [leg.model_dump() for leg in legs]},
    )
    assert endpoint.status_code == 200, endpoint.text
    quoted = endpoint.json()

    assert created.combined_model_probability == pytest.approx(auto_joint, abs=1e-9)
    assert quoted["joint_probability"] == pytest.approx(auto_joint, abs=1e-9)
    assert quoted["joint_probability"] == pytest.approx(
        created.combined_model_probability, abs=1e-9
    )
    assert quoted["pair_counts"] == pair_counts
    assert quoted["correlation_factor"] == pytest.approx(
        expected_factor, abs=1e-9
    )
    assert quoted["edge"] == pytest.approx(
        quoted["joint_probability"] - quoted["combined_market_price"],
        abs=1e-9,
    )


def test_persisted_auto_paper_and_endpoint_quotes_match_at_six_decimals(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices = (0.10, 0.15, 0.15, 0.25)
    probabilities = (0.70, 0.68, 0.66, 0.64)
    candidates: list[ParlayCandidateInput] = []
    request_legs: list[PaperParlayLegCreate] = []
    for index, (price, probability) in enumerate(
        zip(prices, probabilities, strict=True)
    ):
        event = _add_event(
            db_session,
            prefix=f"persisted-parity-{index}",
            sport="NBA",
            home=f"H{index}",
            away=f"A{index}",
        )
        market, prediction = _add_leg(
            db_session,
            event=event,
            ticker=f"PERSISTED-{index}",
            subject=f"Player {index}",
            team=f"H{index}",
            stat_key="points",
            probability=probability,
        )
        candidates.append(
            _workflow_candidate(
                db_session,
                market=market,
                prediction=prediction,
                suggested_price=price,
            )
        )
        request_legs.append(
            PaperParlayLegCreate(
                ticker=market.ticker,
                side="yes",
                suggested_price=price,
            )
        )
    _seed_empirical_cache(db_session)
    db_session.commit()

    # The auto workflow can deliberately serve a promoted ML parlay artifact;
    # this regression pins the canonical heuristic mode shared with paper/tray.
    monkeypatch.setattr(
        parlays_module,
        "run_serving_inference",
        lambda *_args, **_kwargs: (None, None),
    )
    _recommendations, predictions = capture_parlay_artifacts(
        db_session,
        run_id=601,
        candidates=candidates,
    )
    assert predictions > 0
    auto = db_session.query(ParlayPrediction).filter_by(
        run_id=601, leg_count=4
    ).one()

    paper = create_paper_parlay(
        db_session,
        PaperParlayCreate(legs=request_legs, stake=10.0),
    )
    response = client.post(
        "/paper-parlays/quote",
        json={"legs": [leg.model_dump() for leg in request_legs]},
    )
    assert response.status_code == 200, response.text
    endpoint = response.json()

    assert endpoint["combined_market_price"] == 0.000562
    assert auto.combined_market_price == paper.combined_market_price
    assert auto.combined_market_price == endpoint["combined_market_price"]
    assert auto.combined_model_probability == paper.combined_model_probability
    assert auto.combined_model_probability == endpoint["joint_probability"]
    assert auto.edge == paper.edge == endpoint["edge"]

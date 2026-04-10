from datetime import datetime, timezone

from app.models import Event, EventParticipant, Market, Participant, Recommendation
from app.schemas import TradeDeskThresholdRead
from app.services.trade_desk import build_trade_desk_response, thresholds_are_monotonic


def test_thresholds_are_monotonic_utility():
    def make(prob: float) -> TradeDeskThresholdRead:
        return TradeDeskThresholdRead(
            ticker="T", threshold=0, probability_yes=prob,
            selected_side="yes", edge=0.01, confidence=0.5,
        )

    assert thresholds_are_monotonic([make(0.80), make(0.60), make(0.40)]) is True
    assert thresholds_are_monotonic([make(0.80), make(0.80), make(0.40)]) is True
    assert thresholds_are_monotonic([make(0.80), make(0.85), make(0.40)]) is False
    assert thresholds_are_monotonic([make(0.50)]) is True
    assert thresholds_are_monotonic([]) is True


def test_build_trade_desk_clamps_rather_than_drops_non_monotonic_stat_group(db_session):
    """Stat groups with non-monotonic probabilities should be clamped at display,
    not silently dropped. This ensures multi-threshold ladders are preserved."""
    player = Participant(
        external_id="clamp-player",
        sport_key="NBA",
        display_name="Scottie Barnes",
        short_name="Barnes",
        participant_type="player",
    )
    db_session.add(player)
    db_session.flush()

    event = Event(
        external_id="clamp-event",
        sport_key="NBA",
        name="Toronto Raptors at Boston Celtics",
        status="in_progress",
        starts_at=datetime(2026, 4, 9, 19, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        EventParticipant(
            event_id=event.id,
            participant_id=player.id,
            role="home",
            is_home=True,
        )
    )
    db_session.flush()

    thresholds = [
        (20.0, "KXPROP-20", 0.80, 0.10),
        (25.0, "KXPROP-25", 0.85, -0.05),  # non-monotonic: higher than 20+
        (30.0, "KXPROP-30", 0.40, 0.08),
    ]
    for threshold_val, ticker, prob, edge in thresholds:
        market = Market(
            ticker=ticker,
            sport_key="NBA",
            event_id=event.id,
            title=f"Scottie Barnes: {int(threshold_val)}+ points?",
            status="active",
            raw_data={
                "copilot_market_family": "player_prop",
                "copilot_market_kind": "player_prop",
                "copilot_stat_key": "points",
                "copilot_threshold": threshold_val,
                "copilot_direction": "over",
                "copilot_subject_name": "Scottie Barnes",
                "copilot_subject_team": "TOR",
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
                suggested_price=round(prob - edge, 4),
                edge=edge,
                confidence=0.70,
                invalidation="test",
                rationale="test",
                scoring_diagnostics={
                    "selected_side_probability": prob,
                    "monotonicity_adjusted": edge <= 0,
                },
            )
        )
    db_session.commit()

    response = build_trade_desk_response(db_session, sport="NBA")

    assert len(response.events) == 1
    nba_event = response.events[0]
    assert len(nba_event.player_props) == 1

    prop = nba_event.player_props[0]
    assert prop.subject_name == "Scottie Barnes"
    assert len(prop.stat_groups) == 1

    stat_group = prop.stat_groups[0]
    assert stat_group.stat_key == "points"
    assert len(stat_group.thresholds) == 3

    probs = [t.probability_yes for t in stat_group.thresholds]
    # After clamping, 25+ should be clamped to 20+'s probability (0.80)
    assert probs[0] == 0.80
    assert probs[1] == 0.80  # clamped from 0.85
    assert probs[2] == 0.40

    # Verify monotonicity holds after clamping
    for i in range(1, len(probs)):
        assert probs[i] <= probs[i - 1]

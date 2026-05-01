from datetime import datetime, timezone

from app.models import Event, EventParticipant, Market, Participant
from app.services.market_mapping import map_markets_to_events


def test_map_markets_to_events_links_on_token_overlap(db_session):
    lakers = Participant(external_id="p1", sport_key="NBA", display_name="Los Angeles Lakers", short_name="Lakers", participant_type="team")
    warriors = Participant(external_id="p2", sport_key="NBA", display_name="Golden State Warriors", short_name="Warriors", participant_type="team")
    db_session.add_all([lakers, warriors])
    db_session.flush()

    event = Event(
        external_id="evt1",
        sport_key="NBA",
        name="Golden State Warriors at Los Angeles Lakers",
        status="scheduled",
        starts_at=datetime(2026, 3, 31, 1, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=event.id, participant_id=lakers.id, role="home", is_home=True),
            EventParticipant(event_id=event.id, participant_id=warriors.id, role="away", is_home=False),
        ]
    )
    market = Market(
        ticker="KXNBAGAME-26MAR31LALGSW-LAL",
        title="Golden State at Los Angeles Winner?",
        subtitle="NBA regular season",
        status="active",
        close_time=datetime(2026, 4, 14, 0, 55, tzinfo=timezone.utc),
        raw_data={
            "event_ticker": "KXNBAGAME-26MAR31LALGSW",
            "expected_expiration_time": "2026-03-31T00:55:00Z",
            "yes_sub_title": "Los Angeles",
            "copilot_market_kind": "game_winner",
        },
    )
    db_session.add(market)
    db_session.commit()

    updated = map_markets_to_events(db_session)
    db_session.commit()

    assert updated == 1
    assert market.event_id == event.id
    assert market.sport_key == "NBA"


def test_map_markets_picks_event_closest_to_anchor_when_teams_repeat(db_session):
    knicks = Participant(
        external_id="p1",
        sport_key="NBA",
        display_name="New York Knicks",
        short_name="Knicks",
        participant_type="team",
    )
    hawks = Participant(
        external_id="p2",
        sport_key="NBA",
        display_name="Atlanta Hawks",
        short_name="Hawks",
        participant_type="team",
    )
    db_session.add_all([knicks, hawks])
    db_session.flush()

    earlier_event = Event(
        external_id="evt-earlier",
        sport_key="NBA",
        name="Atlanta Hawks at New York Knicks",
        status="scheduled",
        starts_at=datetime(2026, 4, 29, 0, 0, tzinfo=timezone.utc),
    )
    later_event = Event(
        external_id="evt-later",
        sport_key="NBA",
        name="New York Knicks at Atlanta Hawks",
        status="scheduled",
        starts_at=datetime(2026, 4, 30, 23, 0, tzinfo=timezone.utc),
    )
    db_session.add_all([earlier_event, later_event])
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=earlier_event.id, participant_id=knicks.id, role="home", is_home=True),
            EventParticipant(event_id=earlier_event.id, participant_id=hawks.id, role="away", is_home=False),
            EventParticipant(event_id=later_event.id, participant_id=hawks.id, role="home", is_home=True),
            EventParticipant(event_id=later_event.id, participant_id=knicks.id, role="away", is_home=False),
        ]
    )
    market = Market(
        ticker="KXNBAPTS-26APR30NYKATL-NYKJBRUNSON11-22",
        title="Jalen Brunson: 22+ points",
        subtitle="New York Knicks at Atlanta Hawks",
        status="active",
        close_time=datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc),
        raw_data={
            "event_ticker": "KXNBAPTS-26APR30NYKATL",
            "expected_expiration_time": "2026-05-01T02:00:00Z",
        },
    )
    db_session.add(market)
    db_session.commit()

    updated = map_markets_to_events(db_session)
    db_session.commit()

    assert updated == 1
    assert market.event_id == later_event.id


def test_map_markets_skips_events_outside_anchor_window(db_session):
    lakers = Participant(
        external_id="p1",
        sport_key="NBA",
        display_name="Los Angeles Lakers",
        short_name="Lakers",
        participant_type="team",
    )
    warriors = Participant(
        external_id="p2",
        sport_key="NBA",
        display_name="Golden State Warriors",
        short_name="Warriors",
        participant_type="team",
    )
    db_session.add_all([lakers, warriors])
    db_session.flush()

    far_event = Event(
        external_id="evt-far",
        sport_key="NBA",
        name="Golden State Warriors at Los Angeles Lakers",
        status="scheduled",
        starts_at=datetime(2026, 3, 25, 1, 0, tzinfo=timezone.utc),
    )
    db_session.add(far_event)
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=far_event.id, participant_id=lakers.id, role="home", is_home=True),
            EventParticipant(event_id=far_event.id, participant_id=warriors.id, role="away", is_home=False),
        ]
    )
    market = Market(
        ticker="KXNBAGAME-26MAR31LALGSW-LAL",
        title="Golden State at Los Angeles Winner?",
        status="active",
        close_time=datetime(2026, 4, 14, 0, 55, tzinfo=timezone.utc),
        raw_data={
            "event_ticker": "KXNBAGAME-26MAR31LALGSW",
            "expected_expiration_time": "2026-03-31T00:55:00Z",
        },
    )
    db_session.add(market)
    db_session.commit()

    updated = map_markets_to_events(db_session)
    db_session.commit()

    assert updated == 0
    assert market.event_id is None

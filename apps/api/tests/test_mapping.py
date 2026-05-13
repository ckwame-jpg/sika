from datetime import datetime, timezone

import pytest

from app.models import Event, EventParticipant, Market, Participant
from app.services.market_mapping import map_markets_to_events, override_market_mapping


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


# -----------------------------------------------------------------------------
# Bug #17 — confidence + candidates + manual override
# -----------------------------------------------------------------------------


def _seed_nba_event_pair(db_session, *, far_event: bool = False) -> tuple[Event, Event, list[Participant]]:
    """Seed two NBA Lakers-vs-Warriors events (today + later) plus
    participants. Used by the bug-#17 tests for ambiguity coverage."""
    lakers = Participant(
        external_id="p-lal",
        sport_key="NBA",
        display_name="Los Angeles Lakers",
        short_name="Lakers",
        participant_type="team",
    )
    warriors = Participant(
        external_id="p-gsw",
        sport_key="NBA",
        display_name="Golden State Warriors",
        short_name="Warriors",
        participant_type="team",
    )
    db_session.add_all([lakers, warriors])
    db_session.flush()

    primary = Event(
        external_id="evt-primary",
        sport_key="NBA",
        name="Golden State Warriors at Los Angeles Lakers",
        status="scheduled",
        starts_at=datetime(2026, 3, 31, 1, 0, tzinfo=timezone.utc),
    )
    # Place the secondary inside the 36-hour anchor window (the
    # mapper drops anything beyond) so it shows up in the persisted
    # candidate list. ``far_event=True`` opts into a wider gap when
    # the test specifically wants the secondary filtered out.
    secondary_starts = (
        datetime(2026, 3, 31, 13, 0, tzinfo=timezone.utc)
        if not far_event
        else datetime(2026, 4, 12, 1, 0, tzinfo=timezone.utc)
    )
    secondary = Event(
        external_id="evt-secondary",
        sport_key="NBA",
        name="Golden State Warriors at Los Angeles Lakers",
        status="scheduled",
        starts_at=secondary_starts,
    )
    db_session.add_all([primary, secondary])
    db_session.flush()
    db_session.add_all(
        [
            EventParticipant(event_id=primary.id, participant_id=lakers.id, role="home", is_home=True),
            EventParticipant(event_id=primary.id, participant_id=warriors.id, role="away", is_home=False),
            EventParticipant(event_id=secondary.id, participant_id=lakers.id, role="home", is_home=True),
            EventParticipant(event_id=secondary.id, participant_id=warriors.id, role="away", is_home=False),
        ]
    )
    return primary, secondary, [lakers, warriors]


def test_map_markets_persists_confidence_and_candidates(db_session):
    """Bug #17: every auto-map must persist the winning score AND the
    top-K candidates the mapper considered, so ops can review
    ambiguous cases instead of trusting a silent best-match."""
    primary, secondary, _ = _seed_nba_event_pair(db_session)
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

    map_markets_to_events(db_session)
    db_session.commit()
    db_session.refresh(market)

    assert market.event_id == primary.id  # anchor-time tiebreak picks the closer event
    # Confidence is the top candidate's score.
    assert market.mapping_confidence is not None
    assert market.mapping_confidence > 0.35
    # Candidates is a list of dicts with both events scored.
    candidate_event_ids = {entry["event_id"] for entry in (market.mapping_candidates or [])}
    assert primary.id in candidate_event_ids
    assert secondary.id in candidate_event_ids
    # Top candidate matches the chosen event.
    assert market.mapping_candidates[0]["event_id"] == primary.id


def test_map_markets_below_threshold_records_ambiguity_without_mapping(db_session):
    """Bug #17: when no candidate clears the confidence threshold,
    the market stays unmapped BUT we still persist what the mapper
    saw so ops can decide whether to override manually."""
    lakers = Participant(
        external_id="p-lal-only",
        sport_key="NBA",
        display_name="Los Angeles Lakers",
        short_name="Lakers",
        participant_type="team",
    )
    db_session.add(lakers)
    db_session.flush()
    event = Event(
        external_id="evt-orphan",
        sport_key="NBA",
        name="Lakers vs Mystery Opponent",
        status="scheduled",
        starts_at=datetime(2026, 3, 31, 1, 0, tzinfo=timezone.utc),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        EventParticipant(event_id=event.id, participant_id=lakers.id, role="home", is_home=True)
    )
    # A market whose tokens don't strongly overlap with the event.
    market = Market(
        ticker="KXNBAGAME-CRYPTIC",
        title="Cryptic title with no clear teams",
        status="active",
        close_time=datetime(2026, 3, 31, 1, 0, tzinfo=timezone.utc),
        raw_data={"expected_expiration_time": "2026-03-31T00:55:00Z"},
    )
    db_session.add(market)
    db_session.commit()

    map_markets_to_events(db_session)
    db_session.commit()
    db_session.refresh(market)

    assert market.event_id is None  # below threshold → no mapping
    # The mapper still recorded its observation (0.0 if no tokens
    # overlapped — the point is the column is no longer NULL after
    # an attempt was made).
    assert market.mapping_confidence is not None


def test_override_market_mapping_persists_event_and_stamp(db_session):
    """Bug #17: ``override_market_mapping`` links the market to the
    chosen event, stamps ``mapping_overridden_at`` so the auto-mapper
    skips it on the next cycle, and stores the supplied reason."""
    primary, secondary, _ = _seed_nba_event_pair(db_session)
    market = Market(
        ticker="KXNBAGAME-OVERRIDE-1",
        title="Golden State at Los Angeles Winner?",
        status="active",
        close_time=datetime(2026, 4, 14, 0, 55, tzinfo=timezone.utc),
        raw_data={"expected_expiration_time": "2026-03-31T00:55:00Z"},
    )
    db_session.add(market)
    db_session.commit()

    override_market_mapping(
        db_session,
        ticker=market.ticker,
        event_id=secondary.id,
        reason="auto-map picked the wrong slate; doubleheader collision",
    )
    db_session.commit()
    db_session.refresh(market)

    assert market.event_id == secondary.id
    assert market.sport_key == secondary.sport_key
    assert market.mapping_overridden_at is not None
    assert "doubleheader" in (market.mapping_overridden_reason or "")


def test_override_market_mapping_blocks_subsequent_auto_remap(db_session):
    """Bug #17: once a manual override is recorded, the next
    ``map_markets_to_events`` call must NOT clobber the choice — even
    when the market's id is in ``candidate_market_ids``."""
    primary, secondary, _ = _seed_nba_event_pair(db_session)
    market = Market(
        ticker="KXNBAGAME-STICKY-1",
        title="Golden State at Los Angeles Winner?",
        status="active",
        close_time=datetime(2026, 4, 14, 0, 55, tzinfo=timezone.utc),
        raw_data={"expected_expiration_time": "2026-03-31T00:55:00Z"},
    )
    db_session.add(market)
    db_session.commit()

    override_market_mapping(
        db_session,
        ticker=market.ticker,
        event_id=secondary.id,
        reason="sticky override",
    )
    db_session.commit()

    # Even forced re-evaluation via candidate_market_ids must respect
    # the override stamp.
    map_markets_to_events(db_session, candidate_market_ids={market.id})
    db_session.commit()
    db_session.refresh(market)

    assert market.event_id == secondary.id


def test_override_market_mapping_raises_for_unknown_ticker(db_session):
    with pytest.raises(LookupError):
        override_market_mapping(db_session, ticker="KXNBAGAME-DOES-NOT-EXIST", event_id=None)


def test_override_market_mapping_can_clear_event(db_session):
    """Passing ``event_id=None`` clears the link (but keeps the
    override stamp so the auto-mapper still skips the row)."""
    primary, _, _ = _seed_nba_event_pair(db_session)
    market = Market(
        ticker="KXNBAGAME-CLEAR-1",
        title="Some market",
        status="active",
        close_time=datetime(2026, 4, 14, 0, 55, tzinfo=timezone.utc),
        event_id=primary.id,
        sport_key="NBA",
        raw_data={"expected_expiration_time": "2026-03-31T00:55:00Z"},
    )
    db_session.add(market)
    db_session.commit()

    override_market_mapping(
        db_session,
        ticker=market.ticker,
        event_id=None,
        reason="market belongs to a different event we haven't ingested yet",
    )
    db_session.commit()
    db_session.refresh(market)

    assert market.event_id is None
    assert market.mapping_overridden_at is not None


def test_ops_market_mapping_get_and_override_round_trip(client, db_session):
    """End-to-end: ``GET /ops/market-mapping/{ticker}`` returns the
    stored state, and ``POST`` to the same path applies the override
    and reflects it in the response."""
    primary, secondary, _ = _seed_nba_event_pair(db_session)
    market = Market(
        ticker="KXNBAGAME-OPS-1",
        title="Golden State at Los Angeles Winner?",
        status="active",
        close_time=datetime(2026, 4, 14, 0, 55, tzinfo=timezone.utc),
        raw_data={
            "event_ticker": "KXNBAGAME-26MAR31LALGSW",
            "expected_expiration_time": "2026-03-31T00:55:00Z",
            "yes_sub_title": "Los Angeles",
        },
    )
    db_session.add(market)
    db_session.commit()

    map_markets_to_events(db_session)
    db_session.commit()

    # GET — confirm the auto-map persisted the confidence + candidates.
    response = client.get(f"/ops/market-mapping/{market.ticker}")
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == market.ticker
    assert body["event_id"] == primary.id
    assert body["mapping_confidence"] is not None
    assert any(entry["event_id"] == secondary.id for entry in body["mapping_candidates"])
    assert body["mapping_overridden_at"] is None

    # POST — override to the secondary event.
    response = client.post(
        f"/ops/market-mapping/{market.ticker}",
        json={"event_id": secondary.id, "reason": "ops correction"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event_id"] == secondary.id
    assert body["mapping_overridden_at"] is not None
    assert body["mapping_overridden_reason"] == "ops correction"


def test_ops_market_mapping_returns_404_for_unknown_ticker(client):
    response = client.get("/ops/market-mapping/KX-NOPE")
    assert response.status_code == 404
    response = client.post("/ops/market-mapping/KX-NOPE", json={"event_id": None})
    assert response.status_code == 404

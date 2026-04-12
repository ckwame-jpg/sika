from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import CurrentSlateSnapshot, Event, EventParticipant, Market, Participant, Recommendation
from app.schemas import TradeDeskThresholdRead
from app.services.maintenance import prune_runtime_artifacts
from app.services.trade_desk import (
    build_trade_desk_response,
    load_trade_desk_snapshot,
    persist_current_slate_snapshots,
    thresholds_are_monotonic,
)


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

    # Use a wall-clock-relative tip-off so the current-watchlist window
    # filter (which is "now"-relative via ``is_current_watchlist_status``)
    # keeps this event eligible regardless of when the test runs. A previous
    # hardcoded ``datetime(2026, 4, 9, ...)`` drifted past the 18-hour
    # in-progress cutoff and started failing the next day the suite ran.
    event = Event(
        external_id="clamp-event",
        sport_key="NBA",
        name="Toronto Raptors at Boston Celtics",
        status="in_progress",
        starts_at=datetime.now(timezone.utc) - timedelta(hours=1),
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


def test_load_trade_desk_snapshot_returns_stale_payload_with_flag_when_events_are_stale(db_session):
    """Regression: a snapshot whose events have aged past the current-slate
    window must still be served, with ``freshness_status="stale"``. Previously
    this returned ``None`` which silently triggered a live-table fallback in
    the route handler — exactly the failure mode we want to eliminate."""
    persisted_at = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
    snapshot = CurrentSlateSnapshot(
        scope="NBA",
        generated_at=persisted_at,
        payload={
            "events": [
                {
                    "event_id": 336,
                    "event_name": "New York Knicks at Atlanta Hawks",
                    "event_status": "in_progress",
                    "starts_at": "2026-04-06T23:00:00Z",
                    "sport_key": "NBA",
                    "game_lines": [],
                    "player_props": [],
                }
            ],
            "research_sports": [],
        },
    )
    db_session.add(snapshot)
    db_session.commit()

    response = load_trade_desk_snapshot(db_session, sport="NBA")

    assert response is not None
    assert response.freshness_status == "stale"
    assert len(response.events) == 1
    assert response.events[0].event_name == "New York Knicks at Atlanta Hawks"
    # generated_at is back-filled from the DB row when the payload predates the field.
    assert response.generated_at == persisted_at


def test_load_trade_desk_snapshot_marks_fully_fresh_payload_as_fresh(db_session):
    """A snapshot whose events are all within the current-slate window must be
    returned with ``freshness_status="fresh"``."""
    persisted_at = datetime.now(timezone.utc)
    # ``is_current_watchlist_status`` compares ``event_local_date`` to
    # ``current_local_date`` in the coverage timezone (``America/Chicago``).
    # Using the exact same instant as ``persisted_at`` guarantees the two
    # local dates match without timezone math, regardless of what hour the
    # test runs at. Picking ``persisted_at + timedelta(hours=23)`` for
    # example would silently drift past midnight CT for tests that run
    # after ~17:00 CT and re-introduce the same flake this comment exists
    # to prevent.
    starts_at = persisted_at
    snapshot = CurrentSlateSnapshot(
        scope="NBA",
        generated_at=persisted_at,
        payload={
            "events": [
                {
                    "event_id": 337,
                    "event_name": "Boston Celtics at Toronto Raptors",
                    "event_status": "scheduled",
                    "starts_at": starts_at.isoformat().replace("+00:00", "Z"),
                    "sport_key": "NBA",
                    "game_lines": [],
                    "player_props": [],
                }
            ],
            "research_sports": [],
            "generated_at": persisted_at.isoformat().replace("+00:00", "Z"),
            "freshness_status": "fresh",
        },
    )
    db_session.add(snapshot)
    db_session.commit()

    response = load_trade_desk_snapshot(db_session, sport="NBA")

    assert response is not None
    assert response.freshness_status == "fresh"
    assert response.generated_at == persisted_at
    assert len(response.events) == 1


def test_load_trade_desk_snapshot_returns_latest_when_multiple_rows_exist(db_session):
    """Slice 2: the snapshot store is versioned/append-only per scope. When
    multiple rows exist for the same scope, the loader must return the one
    with the greatest ``generated_at`` — never the oldest, never a random one.
    This is the regression guard for the 'UPDATE-in-place then crash' race
    that the old unique-on-scope schema had."""
    older_ts = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    newer_ts = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
    db_session.add(
        CurrentSlateSnapshot(
            scope="NBA",
            generated_at=older_ts,
            payload={
                "events": [],
                "research_sports": [],
                "generated_at": older_ts.isoformat().replace("+00:00", "Z"),
                "freshness_status": "fresh",
            },
        )
    )
    db_session.add(
        CurrentSlateSnapshot(
            scope="NBA",
            generated_at=newer_ts,
            payload={
                "events": [],
                "research_sports": [],
                "generated_at": newer_ts.isoformat().replace("+00:00", "Z"),
                "freshness_status": "fresh",
            },
        )
    )
    db_session.commit()

    response = load_trade_desk_snapshot(db_session, sport="NBA")

    assert response is not None
    assert response.generated_at == newer_ts


def test_persist_current_slate_snapshots_appends_new_rows_per_call(db_session):
    """Slice 2: each call to ``persist_current_slate_snapshots`` must INSERT
    a new row per scope rather than UPDATE-in-place. Two sequential calls
    must therefore leave *two* rows per scope in the DB. This is the write
    side of the versioning guarantee — combined with the ORDER BY generated_at
    loader, a partial/failed write leaves the previous snapshot intact."""
    persist_current_slate_snapshots(
        db_session,
        source_run_id=None,
        generated_at=datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc),
    )
    persist_current_slate_snapshots(
        db_session,
        source_run_id=None,
        generated_at=datetime(2026, 4, 11, 12, 5, tzinfo=timezone.utc),
    )

    rows_per_scope: dict[str, int] = {}
    for row in db_session.scalars(select(CurrentSlateSnapshot)).all():
        rows_per_scope[row.scope] = rows_per_scope.get(row.scope, 0) + 1
    # Expect 2 rows per scope (2 calls × {all, NBA, MLB} = 6 total rows)
    assert rows_per_scope.get("all") == 2
    assert rows_per_scope.get("NBA") == 2
    assert rows_per_scope.get("MLB") == 2


def test_prune_runtime_artifacts_keeps_last_n_snapshots_per_scope(db_session):
    """Slice 2: retention for the versioned snapshot store. Insert more rows
    per scope than the keep-threshold and verify the pruner keeps only the
    newest N per scope, preserving the latest (so reads never regress)."""
    base = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
    # 12 rows for NBA, 3 rows for MLB. Keep threshold is 10 — NBA should be
    # trimmed to 10, MLB left untouched.
    for i in range(12):
        db_session.add(
            CurrentSlateSnapshot(
                scope="NBA",
                generated_at=base + timedelta(minutes=i),
                payload={"events": [], "research_sports": []},
            )
        )
    for i in range(3):
        db_session.add(
            CurrentSlateSnapshot(
                scope="MLB",
                generated_at=base + timedelta(minutes=i),
                payload={"events": [], "research_sports": []},
            )
        )
    db_session.commit()

    result = prune_runtime_artifacts(db_session)
    db_session.commit()

    assert result["current_slate_snapshots_deleted"] == 2

    def _to_utc(dt: datetime) -> datetime:
        # SQLite returns naive datetimes on round-trip; normalize so equality
        # checks against the tz-aware ``base`` succeed.
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    nba_rows = db_session.scalars(
        select(CurrentSlateSnapshot)
        .where(CurrentSlateSnapshot.scope == "NBA")
        .order_by(CurrentSlateSnapshot.generated_at.desc())
    ).all()
    assert len(nba_rows) == 10
    # The survivors must be the 10 most recent — the oldest two (minute 0, 1)
    # are the ones that should have been dropped.
    assert _to_utc(nba_rows[0].generated_at) == base + timedelta(minutes=11)
    assert _to_utc(nba_rows[-1].generated_at) == base + timedelta(minutes=2)

    mlb_rows = db_session.scalars(
        select(CurrentSlateSnapshot).where(CurrentSlateSnapshot.scope == "MLB")
    ).all()
    assert len(mlb_rows) == 3

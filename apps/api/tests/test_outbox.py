"""Bug #31 — transactional outbox contract tests.

The promotion gate of these tests is: the outbox keeps the local DB
and an external system (here, Kalshi via ``KalshiDemoClient``) from
diverging silently. Specifically:

  - ``enqueue`` writes a pending row that participates in the
    enclosing transaction.
  - ``drain_once`` invokes the registered handler exactly once per
    successful attempt, marks the entry ``done``, and updates
    ``completed_at``.
  - A handler that raises bumps ``attempts``, applies exponential
    backoff via ``next_attempt_at``, and stops being retried once
    ``max_attempts`` is reached (``dead_lettered``).
  - Backoff is respected: a row whose ``next_attempt_at`` is in the
    future is skipped by ``drain_once``.
  - Idempotency: a re-drain after a handler succeeds doesn't re-invoke
    the handler (the row is ``done``, not eligible for pickup).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import OutboxEntry
from app.services.outbox import (
    DEFAULT_MAX_ATTEMPTS,
    INITIAL_BACKOFF_SECONDS,
    STATUS_DEAD_LETTERED,
    STATUS_DONE,
    STATUS_PENDING,
    _HANDLERS,
    drain_once,
    enqueue,
    list_pending,
    register_intent_handler,
)


TEST_INTENT = "test_intent"


@pytest.fixture(autouse=True)
def reset_outbox_handlers():
    """Restore the handler registry around each test so handler
    registration in one test doesn't leak into another."""
    saved = dict(_HANDLERS)
    yield
    _HANDLERS.clear()
    _HANDLERS.update(saved)


def test_enqueue_persists_pending_entry(db_session):
    entry = enqueue(
        db_session,
        intent_kind=TEST_INTENT,
        payload={"key": "value"},
        target_kind="thing",
        target_id=42,
    )
    db_session.commit()

    stored = db_session.scalars(select(OutboxEntry).where(OutboxEntry.id == entry.id)).one()
    assert stored.intent_kind == TEST_INTENT
    assert stored.status == STATUS_PENDING
    assert stored.payload == {"key": "value"}
    assert stored.target_kind == "thing"
    assert stored.target_id == 42
    assert stored.attempts == 0
    assert stored.next_attempt_at is None


def test_drain_invokes_registered_handler_and_marks_done(db_session):
    calls: list[OutboxEntry] = []

    def handler(db, entry):
        calls.append(entry)

    register_intent_handler(TEST_INTENT, handler)

    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={"n": 1})
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["succeeded"] == 1
    assert counts["failed"] == 0
    assert counts["processed"] == 1

    assert len(calls) == 1
    db_session.refresh(entry)
    assert entry.status == STATUS_DONE
    assert entry.completed_at is not None


def test_drain_failure_increments_attempts_and_schedules_backoff(db_session):
    def failing_handler(db, entry):
        raise RuntimeError("boom")

    register_intent_handler(TEST_INTENT, failing_handler)

    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={})
    db_session.commit()

    drain_once(db_session)

    db_session.refresh(entry)
    assert entry.status == STATUS_PENDING
    assert entry.attempts == 1
    assert entry.last_error == "RuntimeError('boom')"
    assert entry.next_attempt_at is not None
    # First failure → backoff starts at INITIAL_BACKOFF_SECONDS.
    delta = entry.next_attempt_at - entry.last_error_at
    assert delta.total_seconds() == pytest.approx(INITIAL_BACKOFF_SECONDS, abs=2)


def test_drain_respects_next_attempt_at_backoff_window(db_session):
    def failing_handler(db, entry):
        raise RuntimeError("still failing")

    register_intent_handler(TEST_INTENT, failing_handler)

    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={})
    db_session.commit()

    # First drain — bumps attempts to 1 and schedules backoff.
    drain_once(db_session)
    db_session.refresh(entry)
    first_attempt_count = entry.attempts

    # Immediately drain again at the same wall clock — backoff window
    # not yet elapsed, so the entry should be skipped, attempts stay
    # the same.
    counts = drain_once(db_session, now=datetime.now(timezone.utc))
    assert counts["processed"] == 0
    db_session.refresh(entry)
    assert entry.attempts == first_attempt_count

    # Fast-forward past the backoff: the next drain picks it up.
    future = entry.next_attempt_at + timedelta(seconds=1)
    drain_once(db_session, now=future)
    db_session.refresh(entry)
    assert entry.attempts == first_attempt_count + 1


def test_drain_dead_letters_after_max_attempts(db_session):
    def always_fails(db, entry):
        raise RuntimeError("permanent")

    register_intent_handler(TEST_INTENT, always_fails)

    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={}, max_attempts=3)
    db_session.commit()

    # Walk past each backoff window so the entry actually gets retried
    # rather than being skipped by the next-attempt filter.
    now = datetime.now(timezone.utc)
    for _ in range(3):
        drain_once(db_session, now=now)
        db_session.refresh(entry)
        # Step ahead of whatever next_attempt_at landed at so the next
        # drain treats the row as eligible.
        if entry.next_attempt_at is not None:
            now = entry.next_attempt_at + timedelta(seconds=1)

    db_session.refresh(entry)
    assert entry.attempts == 3
    assert entry.status == STATUS_DEAD_LETTERED
    assert entry.next_attempt_at is None

    # Subsequent drains ignore dead-lettered rows.
    counts = drain_once(db_session, now=now + timedelta(hours=1))
    assert counts["processed"] == 0


def test_drain_handler_missing_marks_failed_and_eventually_dead_letters(db_session):
    """Defensive path: an intent kind with no registered handler should
    fail (and back off), not crash the drain loop. Three drain cycles
    later (default max_attempts=5; we use 2 here for speed) the entry
    moves to dead_lettered."""
    entry = enqueue(db_session, intent_kind="never_registered", payload={}, max_attempts=2)
    db_session.commit()

    now = datetime.now(timezone.utc)
    for _ in range(2):
        drain_once(db_session, now=now)
        db_session.refresh(entry)
        if entry.next_attempt_at is not None:
            now = entry.next_attempt_at + timedelta(seconds=1)
    db_session.refresh(entry)
    assert entry.status == STATUS_DEAD_LETTERED
    assert "no handler" in (entry.last_error or "").lower()


def test_drain_idempotent_after_success(db_session):
    """Once an entry is done, re-running drain doesn't re-invoke the
    handler. Catches a regression where the drain forgets to filter
    out completed rows and double-submits."""
    call_count = 0

    def counting_handler(db, entry):
        nonlocal call_count
        call_count += 1

    register_intent_handler(TEST_INTENT, counting_handler)

    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={})
    db_session.commit()

    drain_once(db_session)
    drain_once(db_session)
    drain_once(db_session)

    assert call_count == 1
    db_session.refresh(entry)
    assert entry.status == STATUS_DONE


def test_one_handler_failure_does_not_block_other_entries(db_session):
    """Bug-#31 fault isolation — a handler raising on entry N must not
    prevent entries N+1..M from running."""
    successes: list[int] = []

    def handler(db, entry):
        if entry.payload.get("explode"):
            raise RuntimeError("kaboom")
        successes.append(int(entry.payload.get("id")))

    register_intent_handler(TEST_INTENT, handler)

    enqueue(db_session, intent_kind=TEST_INTENT, payload={"id": 1})
    enqueue(db_session, intent_kind=TEST_INTENT, payload={"id": 2, "explode": True})
    enqueue(db_session, intent_kind=TEST_INTENT, payload={"id": 3})
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["processed"] == 3
    assert counts["succeeded"] == 2
    assert counts["failed"] == 1
    assert sorted(successes) == [1, 3]


def test_list_pending_skips_future_next_attempt_at(db_session):
    """Backoff filter respects time. Entries with ``next_attempt_at >
    now`` are not returned even if status is pending."""
    now = datetime.now(timezone.utc)
    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={})
    entry.next_attempt_at = now + timedelta(minutes=10)
    db_session.flush()
    db_session.commit()

    pending = list_pending(db_session, now=now)
    assert entry.id not in {row.id for row in pending}

    pending_later = list_pending(db_session, now=now + timedelta(minutes=11))
    assert entry.id in {row.id for row in pending_later}


def test_drain_reclaims_stale_in_flight_entries(db_session):
    """Bug #31 / codex-pattern 5: a crash mid-handler leaves an entry
    in ``in_flight``. On a subsequent drain pass (after the reclaim
    window elapses) the entry must reset to ``pending`` so the same
    intent can retry — without this, a single crash blocks future
    retries of that intent forever.
    """
    from app.services.outbox import IN_FLIGHT_RECLAIM_SECONDS, STATUS_IN_FLIGHT

    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={"foo": "bar"})
    db_session.commit()

    # Simulate a crash mid-handler: claim the row + stamp it stale.
    entry.status = STATUS_IN_FLIGHT
    entry.updated_at = datetime.now(timezone.utc) - timedelta(seconds=IN_FLIGHT_RECLAIM_SECONDS + 10)
    db_session.flush()
    db_session.commit()

    # Register a handler so the post-reclaim retry has somewhere to go.
    register_intent_handler(TEST_INTENT, lambda db, e: None)

    counts = drain_once(db_session)
    assert counts["reclaimed"] == 1
    # The reclaim bumped attempts and set status back to pending; the
    # entry doesn't run again in this drain because it's now under
    # backoff. After the backoff elapses it would retry.
    db_session.refresh(entry)
    assert entry.status == STATUS_PENDING
    assert entry.attempts >= 1
    assert entry.next_attempt_at is not None


def test_drain_does_not_reclaim_recent_in_flight_entries(db_session):
    """Healthy entries (recently claimed, handler still executing) must
    NOT be reclaimed mid-handler. Only entries older than the reclaim
    window are eligible."""
    from app.services.outbox import STATUS_IN_FLIGHT

    entry = enqueue(db_session, intent_kind=TEST_INTENT, payload={})
    db_session.commit()

    entry.status = STATUS_IN_FLIGHT
    entry.updated_at = datetime.now(timezone.utc)  # fresh
    db_session.flush()
    db_session.commit()

    counts = drain_once(db_session)
    assert counts["reclaimed"] == 0
    db_session.refresh(entry)
    assert entry.status == STATUS_IN_FLIGHT


def test_default_max_attempts_constant_is_reasonable():
    """Lightweight regression — if someone bumps ``DEFAULT_MAX_ATTEMPTS``
    way up, an unattended faulty intent could pile up retries forever.
    Pin the default to a small, intentional value."""
    assert 3 <= DEFAULT_MAX_ATTEMPTS <= 10

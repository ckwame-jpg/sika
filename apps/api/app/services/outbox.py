"""Transactional outbox for cross-system writes (bug #31).

The submit_demo_order path used to do this:

    1. db.add(DemoOrder); db.flush()
    2. kalshi.create_order(...)
    3. db.flush() with the Kalshi response

If the API process crashed (or the network glitched) between (1) and (3),
the local DB and Kalshi could diverge silently — a DemoOrder stuck in
"submitting" while Kalshi had/hadn't accepted the request. The outbox
pattern decouples the local write from the external side effect:

    1. db.add(DemoOrder, status="pending_submission")
       db.add(OutboxEntry, intent="kalshi_order_submit", payload={...})
       request commits — both rows persist atomically (or neither does)
    2. A background drain reads pending OutboxEntry rows, claims each
       with status="in_flight", invokes the registered handler, and
       updates both rows transactionally on each step.
    3. Failures bump ``attempts`` + apply exponential backoff via
       ``next_attempt_at``; after ``max_attempts`` the row is moved to
       ``status="dead_lettered"`` so it stops being polled and surfaces
       to ops.

Today only ``kalshi_order_submit`` and ``kalshi_order_cancel`` are
wired; the dispatcher is keyed off ``intent_kind`` so additional
intents (e.g., ``paper_position_open`` if paper positions ever mirror
to a remote system) plug in by registering a handler with
``register_intent_handler``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OutboxEntry

logger = logging.getLogger(__name__)


# Intent kinds — keep as plain strings so adding a new intent doesn't
# require an enum migration. The handler registry is the source of truth.
INTENT_KALSHI_ORDER_SUBMIT = "kalshi_order_submit"
INTENT_KALSHI_ORDER_CANCEL = "kalshi_order_cancel"


# Outbox-entry status values. ``pending`` rows are eligible for drain;
# ``in_flight`` rows are claimed by a worker; ``done`` rows are kept for
# audit and skipped by the poll; ``dead_lettered`` rows are kept for
# ops review.
STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_DONE = "done"
STATUS_DEAD_LETTERED = "dead_lettered"

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_IN_FLIGHT)

DEFAULT_MAX_ATTEMPTS = 5
# Bug #22 used the same backoff shape for prop-refresh retries;
# reusing it here for consistency.
INITIAL_BACKOFF_SECONDS = 30
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 30 * 60  # 30 minutes
# How long an entry can sit in ``in_flight`` before it's eligible for
# reclamation. Real Kalshi submits land in well under a second; five
# minutes is generous slack for a crashed worker that grabbed an entry
# and never finished. Without this, a single ``in_flight`` straggler
# blocks future retries of that intent forever.
IN_FLIGHT_RECLAIM_SECONDS = 300


# Handler signature: (db, entry) -> None; raise on failure (caller
# applies retry/backoff). Handlers MUST be idempotent — the drainer
# may invoke the same intent multiple times if a transient failure
# occurred AFTER the external side effect but BEFORE the entry was
# marked ``done`` (e.g., Kalshi accepted the order but the local
# commit raced a crash).
IntentHandler = Callable[[Session, "OutboxEntry"], None]

_HANDLERS: dict[str, IntentHandler] = {}


def register_intent_handler(intent_kind: str, handler: IntentHandler) -> None:
    """Register a handler for ``intent_kind``. Subsequent calls
    overwrite — useful for tests and for the dispatcher wired in
    ``services/orders.py``."""
    _HANDLERS[intent_kind] = handler


def get_intent_handler(intent_kind: str) -> IntentHandler | None:
    return _HANDLERS.get(intent_kind)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def enqueue(
    db: Session,
    *,
    intent_kind: str,
    payload: dict[str, Any],
    target_kind: str | None = None,
    target_id: int | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> OutboxEntry:
    """Append a pending outbox entry. Caller is responsible for the
    commit boundary — typically the same request handler that wrote the
    target row will flush this and let the FastAPI session commit at
    request end."""
    entry = OutboxEntry(
        intent_kind=intent_kind,
        target_kind=target_kind,
        target_id=target_id,
        payload=dict(payload or {}),
        status=STATUS_PENDING,
        attempts=0,
        max_attempts=max_attempts,
    )
    db.add(entry)
    db.flush()
    return entry


def _backoff_seconds(attempts: int) -> int:
    seconds = INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** max(attempts - 1, 0))
    return int(min(seconds, MAX_BACKOFF_SECONDS))


def _mark_failed(entry: OutboxEntry, *, error: str, now: datetime) -> None:
    entry.attempts = int(entry.attempts or 0) + 1
    entry.last_error = error
    entry.last_error_at = now
    if entry.attempts >= int(entry.max_attempts or DEFAULT_MAX_ATTEMPTS):
        entry.status = STATUS_DEAD_LETTERED
        entry.next_attempt_at = None
        logger.error(
            "Outbox entry %s dead-lettered after %s attempts (intent=%s, target=%s/%s): %s",
            entry.id,
            entry.attempts,
            entry.intent_kind,
            entry.target_kind,
            entry.target_id,
            error,
        )
    else:
        entry.status = STATUS_PENDING
        entry.next_attempt_at = now + timedelta(seconds=_backoff_seconds(entry.attempts))
        logger.warning(
            "Outbox entry %s failed attempt %s/%s (intent=%s, target=%s/%s); next attempt at %s: %s",
            entry.id,
            entry.attempts,
            entry.max_attempts,
            entry.intent_kind,
            entry.target_kind,
            entry.target_id,
            entry.next_attempt_at.isoformat(),
            error,
        )


def _mark_done(entry: OutboxEntry, *, now: datetime) -> None:
    entry.status = STATUS_DONE
    entry.completed_at = now
    entry.next_attempt_at = None
    entry.last_error = None


def _claim(db: Session, entry: OutboxEntry, *, now: datetime) -> bool:
    """Best-effort claim. Returns True if this caller transitioned the
    entry from ``pending`` to ``in_flight``. Re-reads the row with a
    refresh to guard against another worker racing on the same id.

    Postgres + ``FOR UPDATE SKIP LOCKED`` would be the strict-correct
    primitive (see bug #11 — refresh-job singleton race); the demo
    deployment is single-process so we accept the soft-claim today and
    rely on the row-level UPDATE atomicity.
    """
    db.refresh(entry)
    if entry.status != STATUS_PENDING:
        return False
    entry.status = STATUS_IN_FLIGHT
    entry.next_attempt_at = None
    db.flush()
    return True


def reclaim_stale_in_flight(db: Session, *, now: datetime | None = None) -> int:
    """Recover entries that got stuck in ``in_flight`` (worker crashed
    mid-handler, process killed during drain, etc.) by flipping them
    back to ``pending`` once they exceed ``IN_FLIGHT_RECLAIM_SECONDS``.
    The reclaim counts as a failed attempt so the entry still respects
    its max-attempts budget. Returns the count reclaimed.

    Called both at the top of every drain (catches anything stuck
    while the API was up) and could be called at API startup if we
    wanted faster recovery from a crash — for now the drain runs
    every 5s so the latency is already small.
    """
    reference_now = now or _now_utc()
    cutoff = reference_now - timedelta(seconds=IN_FLIGHT_RECLAIM_SECONDS)
    rows = db.scalars(
        select(OutboxEntry)
        .where(OutboxEntry.status == STATUS_IN_FLIGHT)
        .where(OutboxEntry.updated_at <= cutoff)
    ).all()
    for entry in rows:
        _mark_failed(
            entry,
            error=f"reclaimed from in_flight after {IN_FLIGHT_RECLAIM_SECONDS}s timeout",
            now=reference_now,
        )
    if rows:
        db.flush()
    return len(rows)


def list_pending(db: Session, *, now: datetime | None = None, limit: int = 50) -> list[OutboxEntry]:
    """Pending entries ready to drain, ordered oldest-first. Entries
    whose ``next_attempt_at`` is still in the future are filtered out
    so backoff is respected."""
    reference_now = now or _now_utc()
    statement = (
        select(OutboxEntry)
        .where(OutboxEntry.status == STATUS_PENDING)
        .where(
            (OutboxEntry.next_attempt_at.is_(None))
            | (OutboxEntry.next_attempt_at <= reference_now)
        )
        .order_by(OutboxEntry.created_at.asc(), OutboxEntry.id.asc())
        .limit(limit)
    )
    return list(db.scalars(statement).all())


def drain_once(db: Session, *, now: datetime | None = None, limit: int = 50) -> dict[str, int]:
    """Process up to ``limit`` pending outbox entries. Returns a counts
    dict for observability (``processed``, ``succeeded``, ``failed``,
    ``dead_lettered``, ``skipped``).

    Each entry runs in isolation: a handler exception only fails that
    entry, the rest proceed. After each entry the session is flushed so
    a subsequent crash doesn't lose accumulated bookkeeping.
    """
    reference_now = now or _now_utc()
    counts = {"processed": 0, "succeeded": 0, "failed": 0, "dead_lettered": 0, "skipped": 0, "reclaimed": 0}
    # Recover from any prior-run stragglers BEFORE picking up new work
    # — otherwise a crashed worker's entry sits in ``in_flight``
    # forever even though its row-level lock is gone.
    counts["reclaimed"] = reclaim_stale_in_flight(db, now=reference_now)
    for entry in list_pending(db, now=reference_now, limit=limit):
        if not _claim(db, entry, now=reference_now):
            counts["skipped"] += 1
            continue
        handler = get_intent_handler(entry.intent_kind)
        if handler is None:
            _mark_failed(entry, error=f"No handler registered for intent_kind={entry.intent_kind!r}", now=reference_now)
            counts["failed"] += 1
            if entry.status == STATUS_DEAD_LETTERED:
                counts["dead_lettered"] += 1
            db.flush()
            counts["processed"] += 1
            continue
        try:
            handler(db, entry)
        except Exception as exc:  # noqa: BLE001 — handlers may raise anything; we adapt
            _mark_failed(entry, error=repr(exc), now=reference_now)
            counts["failed"] += 1
            if entry.status == STATUS_DEAD_LETTERED:
                counts["dead_lettered"] += 1
        else:
            _mark_done(entry, now=reference_now)
            counts["succeeded"] += 1
        db.flush()
        counts["processed"] += 1
    return counts

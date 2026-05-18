"""Multi-user identity seeding + lookup (multi-user batch PR 1).

``seed_users_from_settings`` runs on every API startup. It compares the
``SIKA_USERS`` env var against the ``users`` table and:

  - Inserts a row for every name in ``SIKA_USERS`` that doesn't exist
  - Marks the ``SIKA_KALSHI_OWNER`` user with ``is_kalshi_owner=True``
    (and clears the flag on everyone else)
  - Ensures the synthetic ``legacy`` user exists if there are any users
    configured at all (PR 3's backfill target)
  - Does NOT delete users dropped from ``SIKA_USERS`` — their rows
    persist so historical paper data stays tagged correctly. If the
    operator wants to free up the username, they can drop the row
    manually; the existing rows will surface as "orphaned user".

Codex pattern 5 (reset edge cases): the seeding is idempotent — running
it twice with the same input is a no-op. The ``is_kalshi_owner`` toggle
is a SYNC, not an additive write, so demoting one user and promoting
another in the env var produces the correct flag on both rows.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DemoOrder, PaperParlay, PaperPosition, User


LEGACY_USERNAME = "legacy"


def seed_users_from_settings(db: Session, settings: Settings) -> dict[str, object]:
    """Sync the ``users`` table with ``SIKA_USERS`` + ``SIKA_KALSHI_OWNER``.

    Returns a small summary dict for the startup log:
    ``{"inserted": N, "owner_set": "username" or "", "legacy_ensured": 0|1}``.

    Safe to call when ``SIKA_USERS`` is empty (single-tenant mode): no
    rows are inserted and the function returns zeros.
    """
    summary: dict[str, object] = {
        "inserted": 0,
        "owner_set": "",
        "legacy_ensured": 0,
        # PR 3: per-table backfill counts (NULL user_id rows pointed at
        # the legacy bucket). Reported in the startup log so the
        # operator sees the one-time backfill happen.
        "backfilled_paper_positions": 0,
        "backfilled_paper_parlays": 0,
        "backfilled_demo_orders": 0,
    }
    requested = settings.users
    if not requested:
        # Single-tenant mode — nothing to seed. Existing rows (if any)
        # are left alone.
        return summary

    existing_by_name = {
        row.username: row
        for row in db.scalars(select(User).where(User.username.in_(requested))).all()
    }
    for username in requested:
        if username in existing_by_name:
            continue
        user = User(username=username, display_name=username)
        db.add(user)
        summary["inserted"] += 1
    db.flush()

    # Ensure the legacy bucket exists once any real users are configured.
    legacy = db.scalar(select(User).where(User.username == LEGACY_USERNAME))
    if legacy is None:
        legacy = User(
            username=LEGACY_USERNAME,
            display_name="Legacy (pre-multi-user)",
            is_legacy_bucket=True,
        )
        db.add(legacy)
        summary["legacy_ensured"] = 1
        db.flush()

    # PR 3 — one-time backfill of NULL user_id rows to the legacy
    # bucket. Subsequent runs are no-ops (no rows match the WHERE).
    # Codex pattern 8 (migration compat): this is the only place that
    # writes to the legacy user_id; new rows always carry a real user.
    summary["backfilled_paper_positions"] = db.execute(
        update(PaperPosition)
        .where(PaperPosition.user_id.is_(None))
        .values(user_id=legacy.id)
    ).rowcount or 0
    summary["backfilled_paper_parlays"] = db.execute(
        update(PaperParlay)
        .where(PaperParlay.user_id.is_(None))
        .values(user_id=legacy.id)
    ).rowcount or 0
    summary["backfilled_demo_orders"] = db.execute(
        update(DemoOrder)
        .where(DemoOrder.user_id.is_(None))
        .values(user_id=legacy.id)
    ).rowcount or 0
    db.flush()

    # Sync the kalshi-owner flag — set on the named user, clear on
    # everyone else. Codex pattern 1 (state-machine compat): a previous
    # operator might have been the owner; demoting them is an explicit
    # write rather than something that requires manual DB surgery.
    requested_owner = settings.kalshi_owner.strip()
    if requested_owner:
        for user in db.scalars(select(User)).all():
            should_own = user.username == requested_owner
            if user.is_kalshi_owner != should_own:
                user.is_kalshi_owner = should_own
        if requested_owner in existing_by_name or any(
            u.username == requested_owner for u in db.scalars(select(User)).all()
        ):
            summary["owner_set"] = requested_owner
        db.flush()

    return summary


def get_user_by_username(db: Session, username: str) -> User | None:
    """Look up a user by username. Returns None if absent or empty.

    Used by the session middleware to validate the ``sika.userId``
    cookie on every request.
    """
    if not username:
        return None
    return db.scalar(select(User).where(User.username == username))


def list_active_users(db: Session) -> list[User]:
    """Users available in the topbar dropdown. Excludes the legacy
    bucket — operators can't impersonate the historical-data shared
    user."""
    return list(
        db.scalars(
            select(User)
            .where(User.is_legacy_bucket == False)  # noqa: E712 — SQLAlchemy boolean compare
            .order_by(User.username.asc())
        ).all()
    )

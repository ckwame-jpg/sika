"""Per-user Kalshi credential storage + client factory (multi-user PR 4).

The operator (chris) connects via the env var (legacy single-tenant
behavior) or via the /settings/kalshi page (PR 5). Other users
(canaan) connect their own Kalshi via the same UI; their credentials
are stored as a row in ``user_kalshi_credentials`` and read on demand
when constructing a per-user KalshiClient.

The env-var migration runs at startup (PR 1 + PR 4 combined): if a
user with ``is_kalshi_owner=True`` exists and they don't have a
credentials row yet, sika copies the env-var creds into the table
for them. After that, the env vars are still consulted as a
fallback (single-tenant safety), but the table is the source of
truth for the owner.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.kalshi import KalshiAccountClient, KalshiAuthenticatedClient, KalshiDemoClient
from app.config import Settings, get_settings
from app.models import User, UserKalshiCredentials


def get_user_credentials(db: Session, user_id: int) -> UserKalshiCredentials | None:
    """Look up the Kalshi credentials row for a user.
    Returns None if the user hasn't connected their account yet."""
    return db.scalar(
        select(UserKalshiCredentials).where(UserKalshiCredentials.user_id == user_id)
    )


def upsert_user_credentials(
    db: Session,
    *,
    user_id: int,
    key_id: str,
    private_key_pem: str,
    base_url: str,
) -> UserKalshiCredentials:
    """Insert or update the Kalshi credentials for a user.

    Idempotent: re-calling with the same inputs updates the
    ``updated_at`` timestamp and is otherwise a no-op. PR 5's
    /settings/kalshi page POSTs here on form submit.
    """
    row = get_user_credentials(db, user_id)
    if row is None:
        row = UserKalshiCredentials(
            user_id=user_id,
            key_id=key_id,
            private_key_pem=private_key_pem,
            base_url=base_url,
        )
        db.add(row)
    else:
        row.key_id = key_id
        row.private_key_pem = private_key_pem
        row.base_url = base_url
    db.flush()
    return row


def delete_user_credentials(db: Session, user_id: int) -> bool:
    """Remove a user's Kalshi credentials. Returns True if a row was
    deleted, False if there was nothing to delete (idempotent)."""
    row = get_user_credentials(db, user_id)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def build_account_client_for_user(
    db: Session, user_id: int | None
) -> KalshiAccountClient | None:
    """Per-user KalshiAccountClient factory.

    Returns None when:
      - ``user_id`` is None (single-tenant fallback handled by callers)
      - The user has no credentials row AND isn't the kalshi_owner
        (in which case the env-var creds would apply)

    Codex pattern 9 (cross-scope): per-user credentials take
    precedence over env-var creds even for the owner — once the
    owner connects via the UI, the DB row wins. Operators that never
    touch the UI keep using the env var (single-tenant compat).
    """
    if user_id is None:
        return _env_var_account_client_or_none()
    row = get_user_credentials(db, user_id)
    if row is None:
        user = db.get(User, user_id)
        if user is not None and user.is_kalshi_owner:
            return _env_var_account_client_or_none()
        return None
    return KalshiAccountClient(
        key_id=row.key_id,
        private_key_pem=row.private_key_pem.encode("utf-8"),
        base_url=row.base_url,
    )


def build_demo_client_for_user(
    db: Session, user_id: int | None
) -> KalshiDemoClient | None:
    """Per-user KalshiDemoClient factory. Same precedence rules as
    ``build_account_client_for_user``."""
    if user_id is None:
        return _env_var_demo_client_or_none()
    row = get_user_credentials(db, user_id)
    if row is None:
        user = db.get(User, user_id)
        if user is not None and user.is_kalshi_owner:
            return _env_var_demo_client_or_none()
        return None
    settings = get_settings()
    return KalshiDemoClient(
        key_id=row.key_id,
        private_key_pem=row.private_key_pem.encode("utf-8"),
        # Demo client always uses the demo base URL; the row's
        # base_url is for the account client (prod vs sandbox).
        base_url=settings.kalshi_demo_base_url,
    )


def _env_var_account_client_or_none() -> KalshiAccountClient | None:
    client = KalshiAccountClient()
    return client if client.is_configured() else None


def _env_var_demo_client_or_none() -> KalshiDemoClient | None:
    client = KalshiDemoClient()
    return client if client.is_configured() else None


def migrate_env_var_credentials_to_owner(db: Session, settings: Settings) -> bool:
    """One-time migration: if SIKA_KALSHI_OWNER is set and the env var
    has real credentials, insert them into the
    ``user_kalshi_credentials`` table for that user.

    Idempotent: skips when the owner already has a row, and bails
    when the env vars are empty. Codex pattern 8 (migration / legacy
    compat): the env vars stay as a fallback after migration so the
    config files stay self-documenting, but the DB row wins.
    """
    if not settings.kalshi_owner.strip():
        return False
    if not settings.kalshi_key_id.strip():
        return False
    owner = db.scalar(select(User).where(User.username == settings.kalshi_owner.strip()))
    if owner is None:
        return False
    if get_user_credentials(db, owner.id) is not None:
        return False
    key_path = settings.kalshi_private_key_path
    if not key_path.exists():
        return False
    pem = key_path.read_text(encoding="utf-8")
    upsert_user_credentials(
        db,
        user_id=owner.id,
        key_id=settings.kalshi_key_id,
        private_key_pem=pem,
        base_url=settings.kalshi_public_base_url,
    )
    return True

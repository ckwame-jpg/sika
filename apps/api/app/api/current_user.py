"""Session middleware + current-user dependency (multi-user batch PR 1).

The session model is intentionally light: a plain ``sika.userId`` cookie
holds the chosen username. The middleware here just stashes the cookie
value on ``request.state``; the actual DB lookup happens in the
``get_current_user_optional`` / ``require_current_user`` dependencies
so they use the same ``get_db`` session as every other endpoint (and
so the test client's ``dependency_overrides[get_db]`` is honored —
otherwise the middleware would spin up its own session bound to the
prod engine and miss the test's in-memory fixture data).

No PINs, no signed tokens, no session store. Threat model: Tailscale is
the perimeter, users are trusted not to impersonate each other.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.database import get_db
from app.models import User
from app.services.users import get_user_by_username


CURRENT_USER_COOKIE = "sika.userId"
# Long max-age so a switch persists across sessions / browser restarts.
# 30 days; the operator can re-switch any time.
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


class CurrentUserMiddleware(BaseHTTPMiddleware):
    """Extract the ``sika.userId`` cookie value and stash it on
    ``request.state.current_username``.

    Deliberately does NOT touch the DB — see the module docstring.
    The dependency functions below do the lookup using the request's
    real DB session.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request.state.current_username = request.cookies.get(CURRENT_USER_COOKIE, "")
        return await call_next(request)


def get_current_user_optional(
    request: Request, db: Session = Depends(get_db)
) -> User | None:
    """Dependency: returns the current user or None.

    Use this for endpoints that work for both authenticated and
    anonymous callers (e.g. ``/users`` itself, which the dropdown
    needs to populate before the user has picked anyone).
    """
    username = getattr(request.state, "current_username", "") or ""
    if not username:
        return None
    user = get_user_by_username(db, username)
    if user is None or user.is_legacy_bucket:
        # Stale cookie (user dropped from SIKA_USERS) or someone
        # forging the legacy username — fall back to anonymous.
        return None
    return user


def require_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """Dependency: returns the current user, 401s if absent.

    Use this for endpoints that operate on per-user data (paper trades,
    parlays, demo orders) and don't have a sensible 'no user' fallback.
    """
    user = get_current_user_optional(request, db)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "No user selected. Pick a user from the topbar dropdown "
                "before continuing."
            ),
        )
    return user

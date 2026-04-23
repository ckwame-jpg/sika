import secrets

from fastapi import Header, HTTPException

from app.config import get_settings


def require_owner_admin_token(x_sika_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().sika_owner_admin_token
    if not expected:
        raise HTTPException(status_code=503, detail="Owner admin token is not configured")
    if not x_sika_admin_token or not secrets.compare_digest(x_sika_admin_token, expected):
        raise HTTPException(status_code=401, detail="Owner admin token is required")

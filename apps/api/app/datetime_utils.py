from datetime import datetime, timezone


def ensure_utc_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat(value: datetime) -> str:
    normalized = ensure_utc_datetime(value)
    if normalized is None:
        raise ValueError("Cannot serialize a null datetime to UTC ISO format")
    return normalized.isoformat().replace("+00:00", "Z")

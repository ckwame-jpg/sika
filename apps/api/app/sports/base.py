import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.datetime_utils import ensure_utc_datetime


@dataclass
class NormalizedParticipant:
    external_id: str
    display_name: str
    short_name: str | None
    role: str
    is_home: bool
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedEvent:
    external_id: str
    sport_key: str
    league_external_id: str | None
    league_name: str
    name: str
    status: str
    starts_at: datetime
    completed_at: datetime | None
    participants: list[NormalizedParticipant]
    raw_data: dict[str, Any] = field(default_factory=dict)


TERMINAL_EVENT_STATUSES = frozenset({"completed", "cancelled"})


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = ensure_utc_datetime(value)
    return parsed if parsed is not None else datetime.now(timezone.utc)


def normalize_event_status(value: str | None, default: str = "scheduled") -> str:
    if not value:
        return default

    normalized = re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()
    if not normalized:
        return default
    if "cancel" in normalized:
        return "cancelled"
    if "postpon" in normalized:
        return "postponed"
    if normalized in {"completed", "complete"} or any(term in normalized for term in ("final", "full time", "fulltime")):
        return "completed"
    if normalized in {"in progress", "live"} or any(
        term in normalized for term in ("progress", "halftime", "quarter", "period", "intermission", "inning", "overtime", "extra time")
    ):
        return "in_progress"
    if normalized in {"scheduled", "pre"} or any(term in normalized for term in ("schedule", "not started", "time tbd", "pregame")):
        return "scheduled"
    if normalized in {"cancelled", "postponed"}:
        return normalized
    return default


def alias_tokens(*values: str | None) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        lowered = value.lower().replace("&", " and ")
        cleaned = re.sub(r"[^a-z0-9 ]+", " ", lowered)
        words = [word for word in cleaned.split() if len(word) > 1]
        tokens.update(words)
        if words:
            tokens.add("".join(words))
            if len(words) > 1:
                tokens.add(" ".join(words))
                acronym = "".join(word[0] for word in words)
                if len(acronym) > 1:
                    tokens.add(acronym)
    return tokens


def scoped_external_id(source: str, sport_key: str, entity_type: str, external_id: str | None) -> str:
    suffix = external_id or "unknown"
    return f"{source}:{sport_key}:{entity_type}:{suffix}"

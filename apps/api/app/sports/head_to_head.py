import re
from typing import Any

from app.sports.base import NormalizedEvent, NormalizedParticipant, alias_tokens, normalize_event_status, parse_datetime, scoped_external_id


class HeadToHeadSportAdapter:
    participant_type = "competitor"

    def __init__(self, sport_key: str, provider_name: str, league_whitelist: list[str] | None = None) -> None:
        self.sport_key = sport_key
        self.provider_name = provider_name
        self.league_whitelist = set(league_whitelist or [])

    def supports_event(self, raw_event: dict[str, Any]) -> bool:
        if not self.league_whitelist:
            return True
        return (raw_event.get("strLeague") or "") in self.league_whitelist

    def _names_from_event(self, raw_event: dict[str, Any]) -> tuple[str, str] | None:
        first = raw_event.get("strHomeTeam") or raw_event.get("strPlayer")
        second = raw_event.get("strAwayTeam") or raw_event.get("strAwayPlayer")
        if first and second:
            return first, second

        raw_name = raw_event.get("strEvent") or ""
        match = re.split(r"\s+vs\.?\s+|\s+v\s+", raw_name, maxsplit=1, flags=re.IGNORECASE)
        if len(match) == 2:
            return match[0].strip(), match[1].strip()
        return None

    def normalize_event(self, raw_event: dict[str, Any]) -> NormalizedEvent | None:
        if not self.supports_event(raw_event):
            return None
        source = str(raw_event.get("source") or "sportsdb")

        names = self._names_from_event(raw_event)
        if not names:
            return None

        first_name, second_name = names
        participants = [
            NormalizedParticipant(
                external_id=scoped_external_id(
                    source,
                    self.sport_key,
                    "participant",
                    str(raw_event.get("idHomeTeam") or raw_event.get("idPlayer") or f"{raw_event.get('idEvent')}:1"),
                ),
                display_name=first_name,
                short_name=None,
                role="competitor_1",
                is_home=True,
            ),
            NormalizedParticipant(
                external_id=scoped_external_id(
                    source,
                    self.sport_key,
                    "participant",
                    str(raw_event.get("idAwayTeam") or raw_event.get("idAwayPlayer") or f"{raw_event.get('idEvent')}:2"),
                ),
                display_name=second_name,
                short_name=None,
                role="competitor_2",
                is_home=False,
            ),
        ]
        status = normalize_event_status(raw_event.get("strStatus"))
        completed_at = parse_datetime(raw_event.get("dateEvent") and f"{raw_event.get('dateEvent')}T23:59:59Z") if status == "completed" else None
        return NormalizedEvent(
            external_id=scoped_external_id(source, self.sport_key, "event", str(raw_event["idEvent"])),
            sport_key=self.sport_key,
            league_external_id=scoped_external_id(source, self.sport_key, "league", str(raw_event.get("idLeague") or self.provider_name)),
            league_name=raw_event.get("strLeague") or self.provider_name,
            name=raw_event.get("strEvent") or f"{first_name} vs {second_name}",
            status=status,
            starts_at=parse_datetime(raw_event.get("strTimestamp")),
            completed_at=completed_at,
            participants=participants,
            raw_data=raw_event,
        )

    def participant_aliases(self, participant_name: str, short_name: str | None = None) -> set[str]:
        return alias_tokens(participant_name, short_name)

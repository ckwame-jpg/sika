from typing import Any

from app.sports.base import NormalizedEvent, NormalizedParticipant, alias_tokens, normalize_event_status, parse_datetime, scoped_external_id


class TeamSportAdapter:
    participant_type = "team"

    def __init__(self, sport_key: str, provider_name: str, league_whitelist: list[str] | None = None) -> None:
        self.sport_key = sport_key
        self.provider_name = provider_name
        self.league_whitelist = set(league_whitelist or [])

    def supports_event(self, raw_event: dict[str, Any]) -> bool:
        if not self.league_whitelist:
            return True
        return (raw_event.get("strLeague") or "") in self.league_whitelist

    def normalize_event(self, raw_event: dict[str, Any]) -> NormalizedEvent | None:
        if not self.supports_event(raw_event):
            return None
        source = str(raw_event.get("source") or "sportsdb")

        home_name = raw_event.get("strHomeTeam") or ""
        away_name = raw_event.get("strAwayTeam") or ""
        if not home_name or not away_name:
            return None

        participants = [
            NormalizedParticipant(
                external_id=scoped_external_id(source, self.sport_key, "participant", str(raw_event.get("idHomeTeam") or f"{raw_event.get('idEvent')}:home")),
                display_name=home_name,
                short_name=raw_event.get("strHomeTeamShort"),
                role="home",
                is_home=True,
                raw_data={"badge": raw_event.get("strHomeTeamBadge")},
            ),
            NormalizedParticipant(
                external_id=scoped_external_id(source, self.sport_key, "participant", str(raw_event.get("idAwayTeam") or f"{raw_event.get('idEvent')}:away")),
                display_name=away_name,
                short_name=raw_event.get("strAwayTeamShort"),
                role="away",
                is_home=False,
                raw_data={"badge": raw_event.get("strAwayTeamBadge")},
            ),
        ]
        status = normalize_event_status(raw_event.get("strStatus"))
        completed_at = parse_datetime(raw_event.get("dateEvent") and f"{raw_event.get('dateEvent')}T23:59:59Z") if status == "completed" else None
        return NormalizedEvent(
            external_id=scoped_external_id(source, self.sport_key, "event", str(raw_event["idEvent"])),
            sport_key=self.sport_key,
            league_external_id=scoped_external_id(source, self.sport_key, "league", str(raw_event.get("idLeague") or self.provider_name)),
            league_name=raw_event.get("strLeague") or self.provider_name,
            name=raw_event.get("strEvent") or f"{away_name} at {home_name}",
            status=status,
            starts_at=parse_datetime(raw_event.get("strTimestamp")),
            completed_at=completed_at,
            participants=participants,
            raw_data=raw_event,
        )

    def participant_aliases(self, participant_name: str, short_name: str | None = None) -> set[str]:
        return alias_tokens(participant_name, short_name)

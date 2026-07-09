from datetime import date, timedelta
import logging
from typing import Any

import httpx

from app.config import get_settings


logger = logging.getLogger(__name__)


class TheSportsDBClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.sports_api_base_url).rstrip("/")
        self.api_key = api_key or settings.sports_api_key or "123"

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}/{self.api_key}/{endpoint}"

    def fetch_events_for_day(self, sport_name: str, target_day: date) -> list[dict[str, Any]]:
        response = httpx.get(
            self._url("eventsday.php"),
            params={"d": target_day.isoformat(), "s": sport_name},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("events") or []

    def fetch_events_window_with_diagnostics(
        self,
        sport_name: str,
        start_day: date,
        end_day: date,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        current = start_day
        while current <= end_day:
            try:
                events.extend(self.fetch_events_for_day(sport_name, current))
            except (httpx.HTTPError, ValueError) as exc:
                # ValueError catches json.JSONDecodeError: a 200 HTML/non-JSON
                # body would otherwise escape this per-day guard and abort the
                # whole multi-sport refresh.
                message = str(exc).strip() or exc.__class__.__name__
                errors.append(f"{current.isoformat()}: {exc.__class__.__name__}: {message}")
                logger.warning("TheSportsDB fetch failed for %s on %s: %s", sport_name, current.isoformat(), message)
            current += timedelta(days=1)
        return events, errors

    def fetch_events_window(self, sport_name: str, start_day: date, end_day: date) -> list[dict[str, Any]]:
        events, _ = self.fetch_events_window_with_diagnostics(sport_name, start_day, end_day)
        return events

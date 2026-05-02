"""Weather client with OpenWeatherMap (free tier) primary and NWS fallback.

Behavior:
  - If ``OPENWEATHER_API_KEY`` is configured, use OpenWeather's One Call API.
  - Otherwise fall back to api.weather.gov (NWS). NWS requires a User-Agent
    identifying the consumer. NWS uses a two-call pattern: first GET
    ``/points/{lat,lon}`` to discover the forecast URL, then GET that URL.

Both code paths normalize their response into a single shape:
    {
      "temp_f": float,
      "wind_speed_mph": float,
      "wind_dir_deg": float,
      "precip_pct": float,    # 0-100
      "humidity_pct": float,  # 0-100
      "is_dome": bool,        # always False here; resolved by caller from venue metadata
      "source": "openweather" | "nws" | None,
    }
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.clients._rate_limit import shared_bucket
from app.config import get_settings


logger = logging.getLogger(__name__)


_OPENWEATHER_URL = "https://api.openweathermap.org/data/3.0/onecall"
_NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
_RATE_LIMIT_RPS = 1.0
_RATE_LIMIT_BURST = 2.0


def _nws_user_agent() -> str:
    """NWS asks consumers to identify themselves in the User-Agent header so
    they can contact the operator if traffic looks abusive. We use a plain
    product token sourced from settings — no email baked into source so
    nothing has to be redacted on a fork. Operators wanting the contactable
    form per NWS guidance can set ``NWS_USER_AGENT="myorg (ops@example.com)"``
    via env."""
    return (get_settings().nws_user_agent or "sika-sports-copilot").strip()


class WeatherClient:
    _MAX_ATTEMPTS = 3
    _BACKOFF_SECONDS: tuple[float, ...] = (1.0, 3.0, 10.0)

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http_client = http_client
        self._bucket = shared_bucket("weather", _RATE_LIMIT_RPS, _RATE_LIMIT_BURST)

    def fetch_game_weather(
        self,
        *,
        lat: float,
        lon: float,
        game_time_utc: datetime,
    ) -> dict[str, Any]:
        api_key = (get_settings().openweather_api_key or "").strip()
        if api_key:
            try:
                return self._fetch_openweather(lat=lat, lon=lon, game_time_utc=game_time_utc, api_key=api_key)
            except Exception as exc:  # noqa: BLE001 — fall through to NWS on any failure
                logger.warning("OpenWeather fetch failed (%s); falling back to NWS", exc)
        return self._fetch_nws(lat=lat, lon=lon, game_time_utc=game_time_utc)

    # ------------------------------------------------------------------
    # OpenWeather

    def _fetch_openweather(
        self,
        *,
        lat: float,
        lon: float,
        game_time_utc: datetime,
        api_key: str,
    ) -> dict[str, Any]:
        response = self._get(
            _OPENWEATHER_URL,
            params={
                "lat": f"{lat:.4f}",
                "lon": f"{lon:.4f}",
                "exclude": "minutely,daily,alerts",
                "units": "imperial",
                "appid": api_key,
            },
            headers=None,
        )
        response.raise_for_status()
        payload = response.json()
        target_ts = int(game_time_utc.replace(tzinfo=timezone.utc).timestamp())
        hourly = payload.get("hourly") or []
        chosen = min(hourly, key=lambda h: abs(int(h.get("dt") or 0) - target_ts), default=None)
        source = chosen or payload.get("current") or {}

        return {
            "temp_f": _safe_float(source.get("temp")),
            "wind_speed_mph": _safe_float(source.get("wind_speed")),
            "wind_dir_deg": _safe_float(source.get("wind_deg")),
            "precip_pct": _safe_float(source.get("pop", 0)) * 100.0 if source.get("pop") is not None else 0.0,
            "humidity_pct": _safe_float(source.get("humidity")),
            "is_dome": False,
            "source": "openweather",
        }

    # ------------------------------------------------------------------
    # NWS fallback

    def _fetch_nws(
        self,
        *,
        lat: float,
        lon: float,
        game_time_utc: datetime,
    ) -> dict[str, Any]:
        headers = {"User-Agent": _nws_user_agent(), "Accept": "application/geo+json"}
        points_response = self._get(
            _NWS_POINTS_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"),
            params={},
            headers=headers,
        )
        points_response.raise_for_status()
        forecast_url = (points_response.json().get("properties") or {}).get("forecastHourly")
        if not forecast_url:
            raise httpx.HTTPError("NWS forecast hourly URL missing from points response")

        forecast_response = self._get(forecast_url, params={}, headers=headers)
        forecast_response.raise_for_status()
        periods = ((forecast_response.json().get("properties") or {}).get("periods")) or []
        if not periods:
            raise httpx.HTTPError("NWS forecast hourly returned no periods")
        target_iso = game_time_utc.replace(tzinfo=timezone.utc).isoformat()
        chosen = min(periods, key=lambda p: abs(_parse_iso(p.get("startTime")) - _parse_iso(target_iso)))

        return {
            "temp_f": _safe_float(chosen.get("temperature")),
            "wind_speed_mph": _wind_speed_from_string(chosen.get("windSpeed")),
            "wind_dir_deg": _direction_to_deg(chosen.get("windDirection")),
            "precip_pct": _safe_float(((chosen.get("probabilityOfPrecipitation") or {}).get("value")) or 0.0),
            "humidity_pct": _safe_float(((chosen.get("relativeHumidity") or {}).get("value")) or None),
            "is_dome": False,
            "source": "nws",
        }

    # ------------------------------------------------------------------
    # Internal

    def _do_get(
        self,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str] | None,
        timeout: float,
    ) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.get(url, params=params, headers=headers, timeout=timeout)
        return httpx.get(url, params=params, headers=headers, timeout=timeout)

    def _get(
        self,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            self._bucket.acquire()
            try:
                response = self._do_get(url, params, headers, timeout=20.0)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self._MAX_ATTEMPTS:
                    raise
                time.sleep(1.0 * attempt)
                continue

            if response.status_code == 429 and attempt < self._MAX_ATTEMPTS:
                time.sleep(self._BACKOFF_SECONDS[min(attempt - 1, len(self._BACKOFF_SECONDS) - 1)])
                continue

            return response

        assert last_error is not None
        raise last_error


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _wind_speed_from_string(raw: Any) -> float | None:
    """NWS wind speed comes as 'N mph' or 'N to M mph'. Take the upper bound."""
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("mph", "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split("to")]
    candidate = parts[-1] if parts else text
    try:
        return float(candidate.split()[0])
    except (ValueError, IndexError):
        return None


_DIRECTION_DEG = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def _direction_to_deg(raw: Any) -> float | None:
    if raw is None:
        return None
    key = str(raw).strip().upper()
    return float(_DIRECTION_DEG[key]) if key in _DIRECTION_DEG else None

import base64
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import get_settings


# Process-level token bucket for Kalshi public API calls. One limiter per
# process serializes read traffic across all KalshiPublicClient instances,
# which matters because the refresh worker makes bursts of market fetches and
# Kalshi will 429 aggressively otherwise. Tunables:
#   _RATE_LIMIT_RPS    — steady-state request rate
#   _RATE_LIMIT_BURST  — instantaneous burst size (tokens)
_RATE_LIMIT_RPS = 5.0
_RATE_LIMIT_BURST = 10.0


class _TokenBucket:
    def __init__(self, rate_per_second: float, burst: float) -> None:
        self._rate = float(rate_per_second)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate if self._rate > 0 else 0.1
            time.sleep(max(wait, 0.0))


_KALSHI_RATE_LIMITER = _TokenBucket(_RATE_LIMIT_RPS, _RATE_LIMIT_BURST)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value into seconds, supporting both the
    integer-seconds form (``"5"``) and a bounded fallback for unparseable
    values (returns ``None`` so the caller can pick its own backoff).
    HTTP-date form is deliberately unsupported because Kalshi sends seconds.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds


def parse_price_dollars(raw_value: Any) -> float | None:
    if raw_value in (None, ""):
        return None
    return float(raw_value)


class KalshiPublicClient:
    _MAX_ATTEMPTS = 4
    _BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
    _MAX_BACKOFF_SECONDS = 4.0

    def __init__(self, base_url: str | None = None, http_client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.kalshi_public_base_url).rstrip("/")
        self._http_client = http_client

    def _get(self, path: str, **kwargs):
        """Fetch a Kalshi public endpoint with retries.

        Retries on two conditions, with independent attempt counts bounded by
        ``_MAX_ATTEMPTS``:
          1. ``httpx.TransportError`` — transient network failure.
          2. HTTP ``429 Too Many Requests`` — rate-limited by Kalshi. Honors
             ``Retry-After`` when present, otherwise uses an exponential
             backoff schedule.

        All other HTTP status codes are returned unraised so callers retain
        control of ``raise_for_status()`` semantics at the call-site.

        Note: previously this method only caught ``httpx.TransportError``, and
        ``raise_for_status()`` was called OUTSIDE this method by every
        call-site — so 429 responses bypassed the retry entirely, which was
        the root cause of the "Kalshi 429" maintenance stalls.
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            _KALSHI_RATE_LIMITER.acquire()
            try:
                if self._http_client is not None:
                    response = self._http_client.get(url, **kwargs)
                else:
                    response = httpx.get(url, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self._MAX_ATTEMPTS:
                    raise
                time.sleep(0.25 * attempt)
                continue
            except httpx.HTTPError as exc:
                # Non-transport HTTPError (e.g. InvalidURL) — do not retry.
                raise exc

            if response.status_code == 429 and attempt < self._MAX_ATTEMPTS:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is None:
                    retry_after = self._BACKOFF_SCHEDULE_SECONDS[
                        min(attempt - 1, len(self._BACKOFF_SCHEDULE_SECONDS) - 1)
                    ]
                time.sleep(min(retry_after, self._MAX_BACKOFF_SECONDS))
                continue

            return response

        assert last_error is not None
        raise last_error

    def list_markets_page(
        self,
        *,
        status: str = "open",
        limit: int = 1000,
        mve_filter: str = "exclude",
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        params: dict[str, Any] = {
            "status": status,
            "limit": min(limit, 1000),
            "mve_filter": mve_filter,
        }
        if cursor:
            params["cursor"] = cursor

        response = self._get(
            "/markets",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return list(payload.get("markets") or []), payload.get("cursor")

    def iter_market_pages(
        self,
        *,
        status: str = "open",
        limit: int = 1000,
        mve_filter: str = "exclude",
        max_pages: int = 5,
        cursor: str | None = None,
    ):
        """Iterate Kalshi market pages, ``limit`` markets per page (≤1000),
        up to ``max_pages`` pages. Total cap is ``limit * max_pages``.

        Previously ``limit`` was treated as a global cap which made
        ``max_pages > 1`` meaningless — the caller would only ever see one
        page worth of markets. Now ``limit`` is per-page (clamped to
        Kalshi's 1000 max) and ``max_pages`` controls depth, so a caller
        asking for ``limit=1000, max_pages=50`` gets up to 50K markets.
        """
        per_page = max(1, min(int(limit), 1000))
        next_cursor = cursor
        for _ in range(max_pages):
            page_markets, next_cursor = self.list_markets_page(
                status=status,
                limit=per_page,
                mve_filter=mve_filter,
                cursor=next_cursor,
            )
            if not page_markets:
                break
            yield page_markets, next_cursor
            if not next_cursor:
                break

    def list_markets(
        self,
        status: str = "open",
        limit: int = 1000,
        mve_filter: str = "exclude",
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit * max_pages`` markets across paginated calls.

        ``limit`` is the per-page cap (Kalshi's max is 1000); ``max_pages``
        is the total depth. Defaults of ``(1000, 5)`` give up to 5,000
        markets — enough to surface NBA/MLB game-winner tickers that get
        buried behind tens of thousands of music-streaming and prop
        tickers in Kalshi's default ordering.
        """
        markets: list[dict[str, Any]] = []
        for page_markets, _cursor in self.iter_market_pages(
            status=status,
            limit=limit,
            mve_filter=mve_filter,
            max_pages=max_pages,
        ):
            markets.extend(page_markets)
        return markets

    def get_market(self, ticker: str) -> dict[str, Any]:
        response = self._get(f"/markets/{ticker}", timeout=20)
        response.raise_for_status()
        return response.json().get("market") or {}

    def get_market_candlesticks(
        self,
        *,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int,
        include_latest_before_start: bool = True,
    ) -> dict[str, Any]:
        response = self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
                "include_latest_before_start": str(include_latest_before_start).lower(),
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def get_historical_market_candlesticks(
        self,
        *,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int,
    ) -> dict[str, Any]:
        response = self._get(
            f"/historical/markets/{ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()


class KalshiAuthenticatedClient:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        base_url: str | None = None,
    ) -> None:
        settings = get_settings()
        self.key_id = key_id or settings.kalshi_key_id
        self.private_key_path = Path(private_key_path or settings.kalshi_private_key_path)
        self.base_url = (base_url or settings.kalshi_public_base_url).rstrip("/")

    def is_configured(self) -> bool:
        return bool(self.key_id.strip()) and self.private_key_path.exists()

    def _load_private_key(self):
        if not self.private_key_path.exists():
            raise FileNotFoundError(f"Kalshi private key not found at {self.private_key_path}")
        return serialization.load_pem_private_key(self.private_key_path.read_bytes(), password=None)

    def sign_request(self, method: str, path: str, timestamp_ms: str) -> str:
        payload = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = self._load_private_key().sign(
            payload,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, url: str) -> dict[str, str]:
        parsed = urlparse(url)
        path = parsed.path
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self.sign_request(method, path, timestamp_ms),
        }

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        response = httpx.request(
            method,
            url,
            headers=self._headers(method, url),
            json=json_body,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()


class KalshiAccountClient(KalshiAuthenticatedClient):
    def get_balance(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/balance", timeout=8)

    def list_markets_by_tickers(self, tickers: list[str]) -> list[dict[str, Any]]:
        if not tickers:
            return []
        payload = self._request(
            "GET",
            "/markets",
            params={
                "tickers": ",".join(tickers),
                "limit": min(len(tickers), 1000),
                "mve_filter": "include",
            },
            timeout=8,
        )
        return list(payload.get("markets") or [])

    def list_positions(
        self,
        *,
        count_filter: str = "position",
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "count_filter": count_filter,
            "limit": min(max(limit, 1), 1000),
        }
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/portfolio/positions", params=params, timeout=8)

    def list_fills(self, *, limit: int = 25, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": min(max(limit, 1), 200)}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/portfolio/fills", params=params, timeout=8)


class KalshiDemoClient(KalshiAuthenticatedClient):
    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        base_url: str | None = None,
    ) -> None:
        settings = get_settings()
        super().__init__(
            key_id=key_id,
            private_key_path=private_key_path,
            base_url=base_url or settings.kalshi_demo_base_url,
        )

    def create_order(self, *, ticker: str, side: str, action: str, quantity: int, limit_price: float, time_in_force: str) -> dict[str, Any]:
        client_order_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "client_order_id": client_order_id,
            "count": quantity,
            "count_fp": f"{quantity:.2f}",
            "time_in_force": time_in_force,
        }
        price_field = "yes_price_dollars" if side.lower() == "yes" else "no_price_dollars"
        payload[price_field] = f"{limit_price:.4f}"
        response = self._request("POST", "/portfolio/orders", json_body=payload)
        response.setdefault("request", payload)
        return response

    def cancel_order(self, kalshi_order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/portfolio/orders/{kalshi_order_id}")

    def list_orders(self) -> list[dict[str, Any]]:
        return self._request("GET", "/portfolio/orders").get("orders") or []

    def list_fills(self) -> list[dict[str, Any]]:
        return self._request("GET", "/portfolio/fills").get("fills") or []


def snapshot_from_market_payload(market: dict[str, Any]) -> dict[str, float | None]:
    return {
        "yes_bid": parse_price_dollars(market.get("yes_bid_dollars")),
        "yes_ask": parse_price_dollars(market.get("yes_ask_dollars")),
        "no_bid": parse_price_dollars(market.get("no_bid_dollars")),
        "no_ask": parse_price_dollars(market.get("no_ask_dollars")),
        "last_price": parse_price_dollars(market.get("last_price_dollars")),
        "volume": parse_price_dollars(market.get("volume_dollars") or market.get("volume")),
        "open_interest": parse_price_dollars(market.get("open_interest_fp") or market.get("open_interest")),
    }

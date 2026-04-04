import base64
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import get_settings


def parse_price_dollars(raw_value: Any) -> float | None:
    if raw_value in (None, ""):
        return None
    return float(raw_value)


class KalshiPublicClient:
    def __init__(self, base_url: str | None = None, http_client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.kalshi_public_base_url).rstrip("/")
        self._http_client = http_client

    def _get(self, path: str, **kwargs):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        if self._http_client is not None:
            return self._http_client.get(url, **kwargs)
        return httpx.get(url, **kwargs)

    def list_markets(
        self,
        status: str = "open",
        limit: int = 1000,
        mve_filter: str = "exclude",
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor: str | None = None

        for _ in range(max_pages):
            remaining = limit - len(markets)
            if remaining <= 0:
                break
            params: dict[str, Any] = {
                "status": status,
                "limit": min(remaining, 1000),
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
            page_markets = payload.get("markets") or []
            markets.extend(page_markets)
            cursor = payload.get("cursor")
            if not page_markets or not cursor:
                break

        return markets[:limit]

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


class KalshiDemoClient:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_path: str | Path | None = None,
        base_url: str | None = None,
    ) -> None:
        settings = get_settings()
        self.key_id = key_id or settings.kalshi_key_id
        self.private_key_path = Path(private_key_path or settings.kalshi_private_key_path)
        self.base_url = (base_url or settings.kalshi_demo_base_url).rstrip("/")

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

    def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        response = httpx.request(
            method,
            url,
            headers=self._headers(method, url),
            json=json_body,
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

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

import httpx
import pytest

from app.clients import kalshi as kalshi_module
from app.clients.kalshi import KalshiPublicClient


@pytest.fixture(autouse=True)
def _disable_rate_limiter_sleep(monkeypatch):
    """Drop the token-bucket wait time to zero so retry tests don't sit on
    real sleeps. The bucket is still exercised (acquire() runs), just without
    introducing per-test wall-clock latency."""
    import time as _time

    real_sleep = _time.sleep
    monkeypatch.setattr(kalshi_module.time, "sleep", lambda _s: real_sleep(0))


def test_list_markets_page_retries_transient_transport_error():
    request = httpx.Request("GET", "https://example.test/markets")

    class FakeHttpClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ReadError("connection reset by peer", request=request)
            return httpx.Response(
                200,
                request=request,
                json={"markets": [{"ticker": "KXTEST"}], "cursor": "next-cursor"},
            )

    fake = FakeHttpClient()
    client = KalshiPublicClient(base_url="https://example.test", http_client=fake)

    markets, cursor = client.list_markets_page(status="open", limit=10, mve_filter="include")

    assert fake.calls == 3
    assert markets == [{"ticker": "KXTEST"}]
    assert cursor == "next-cursor"


def test_list_markets_page_retries_on_429_with_retry_after_header():
    """Regression: 429 responses must be retried. Previously only
    httpx.TransportError was caught, and ``response.raise_for_status()`` was
    called outside ``_get()``, so 429s bypassed the retry entirely."""
    request = httpx.Request("GET", "https://example.test/markets")
    sleeps: list[float] = []

    class FakeHttpClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if self.calls < 3:
                return httpx.Response(
                    429,
                    request=request,
                    headers={"Retry-After": "2"},
                    json={"error": "rate_limited"},
                )
            return httpx.Response(
                200,
                request=request,
                json={"markets": [{"ticker": "KXTEST"}], "cursor": None},
            )

    fake = FakeHttpClient()

    # Capture the sleeps the client makes between retries so we can assert it
    # actually honored the Retry-After header.
    real_sleep = kalshi_module.time.sleep

    def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        real_sleep(0)

    kalshi_module.time.sleep = _capture_sleep
    try:
        client = KalshiPublicClient(base_url="https://example.test", http_client=fake)
        markets, _ = client.list_markets_page(status="open", limit=10, mve_filter="include")
    finally:
        kalshi_module.time.sleep = real_sleep

    assert fake.calls == 3
    assert markets == [{"ticker": "KXTEST"}]
    # At least two sleeps of ~2s (the Retry-After value) must appear — one
    # per 429 response. Other sleeps (rate limiter, final success) may also
    # be present; we only assert the retry waits are honored.
    assert sleeps.count(2.0) >= 2


def test_list_markets_page_retries_on_429_without_retry_after_uses_backoff_schedule():
    """When a 429 response has no Retry-After header the client should fall
    back to an exponential backoff schedule (0.5s, 1s, 2s, ...) capped by
    ``_MAX_BACKOFF_SECONDS``."""
    request = httpx.Request("GET", "https://example.test/markets")
    sleeps: list[float] = []

    class FakeHttpClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if self.calls < 2:
                return httpx.Response(429, request=request, json={"error": "rate_limited"})
            return httpx.Response(
                200,
                request=request,
                json={"markets": [], "cursor": None},
            )

    fake = FakeHttpClient()

    real_sleep = kalshi_module.time.sleep

    def _capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        real_sleep(0)

    kalshi_module.time.sleep = _capture_sleep
    try:
        client = KalshiPublicClient(base_url="https://example.test", http_client=fake)
        markets, _ = client.list_markets_page(status="open", limit=10, mve_filter="include")
    finally:
        kalshi_module.time.sleep = real_sleep

    assert fake.calls == 2
    assert markets == []
    # The first backoff slot is 0.5s per ``_BACKOFF_SCHEDULE_SECONDS``.
    assert 0.5 in sleeps


def test_list_markets_page_returns_non_429_errors_without_retry():
    """400-range and 500-range responses other than 429 should be returned
    unraised to the caller, which is responsible for ``raise_for_status()``.
    Critically, they must NOT be retried — the current ``_get()`` contract is
    that only transport errors and 429s are retried."""
    request = httpx.Request("GET", "https://example.test/markets")

    class FakeHttpClient:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            return httpx.Response(500, request=request, json={"error": "boom"})

    fake = FakeHttpClient()
    client = KalshiPublicClient(base_url="https://example.test", http_client=fake)

    with pytest.raises(httpx.HTTPStatusError):
        client.list_markets_page(status="open", limit=10, mve_filter="include")

    assert fake.calls == 1

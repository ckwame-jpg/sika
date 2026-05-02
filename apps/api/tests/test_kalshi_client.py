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


def test_list_markets_walks_max_pages_with_per_page_limit():
    """``list_markets`` previously clipped at ``markets[:limit]`` after
    paginating, so a caller asking for 5 pages of 1000 markets would only
    see 1000 markets total — masking standalone game-winner tickers
    buried behind tens of thousands of prop tickers in Kalshi's default
    ordering. This regression test pins the new behavior: ``limit`` is the
    per-page size and ``max_pages`` controls depth, so the caller gets
    up to ``limit * max_pages`` markets across all pages."""
    request = httpx.Request("GET", "https://example.test/markets")

    class FakeHttpClient:
        def __init__(self) -> None:
            self.call_count = 0

        def get(self, url, **kwargs):
            self.call_count += 1
            params = kwargs.get("params") or {}
            assert int(params["limit"]) == 1000
            payload = {
                "markets": [{"ticker": f"KX-page{self.call_count}-{i}"} for i in range(1000)],
                "cursor": "next" if self.call_count < 3 else "",
            }
            return httpx.Response(200, request=request, json=payload)

    fake = FakeHttpClient()
    client = KalshiPublicClient(base_url="https://example.test", http_client=fake)
    markets = client.list_markets(status="open", limit=1000, mve_filter="include", max_pages=3)

    assert fake.call_count == 3, "expected three paginated requests"
    assert len(markets) == 3000, "expected 3 pages × 1000 markets, not the old 1000-cap"
    # Markers from each page must all be present so we know none of the
    # later pages got dropped by a trailing slice.
    assert markets[0]["ticker"] == "KX-page1-0"
    assert markets[1500]["ticker"].startswith("KX-page2-")
    assert markets[2999]["ticker"] == "KX-page3-999"


def test_list_markets_stops_when_cursor_empties():
    """If Kalshi runs out of markets before ``max_pages``, pagination
    stops and we return everything fetched so far — without retrying."""
    request = httpx.Request("GET", "https://example.test/markets")

    class FakeHttpClient:
        def __init__(self) -> None:
            self.call_count = 0

        def get(self, url, **kwargs):
            self.call_count += 1
            payload = {
                "markets": [{"ticker": f"KX-{self.call_count}"}],
                "cursor": "",
            }
            return httpx.Response(200, request=request, json=payload)

    fake = FakeHttpClient()
    client = KalshiPublicClient(base_url="https://example.test", http_client=fake)
    markets = client.list_markets(status="open", limit=1000, mve_filter="include", max_pages=10)

    assert fake.call_count == 1
    assert len(markets) == 1

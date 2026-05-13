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


# -----------------------------------------------------------------------------
# Bug #18 — pagination drains the cursor; wall-clock budget bounds runtime
# -----------------------------------------------------------------------------


def test_list_markets_drains_cursor_past_legacy_5k_default():
    """Bug #18: the previous ``max_pages=5`` default capped discovery
    at 5,000 markets. Now ``max_pages=50`` is the safety bound and
    pagination keeps going as long as the upstream returns a non-empty
    cursor — confirm that 10 pages of 1,000 markets each (10K total)
    actually come back when Kalshi keeps handing out cursors."""
    request = httpx.Request("GET", "https://example.test/markets")

    class FakeHttpClient:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs):
            self.calls += 1
            # Hand out 10 pages of 1000 markets, each with a cursor
            # pointing to the next page; the 11th page returns no cursor.
            page_index = self.calls
            return httpx.Response(
                200,
                request=request,
                json={
                    "markets": [
                        {"ticker": f"KXTEST-{page_index}-{i}"}
                        for i in range(1000)
                    ],
                    "cursor": f"cursor-{page_index}" if page_index < 10 else "",
                },
            )

    fake = FakeHttpClient()
    client = KalshiPublicClient(base_url="https://example.test", http_client=fake)
    markets = client.list_markets(status="open", limit=1000, mve_filter="exclude")

    # 10 pages of 1000 each → 10K markets. Previously capped at 5K
    # by the old ``max_pages=5`` default.
    assert fake.calls == 10
    assert len(markets) == 10_000


def test_list_markets_respects_wall_clock_budget(monkeypatch):
    """Bug #18: ``wall_clock_budget_seconds`` is the soft cap callers
    use instead of guessing a page count. Pagination must stop as
    soon as the cumulative elapsed time exceeds the budget.

    The shared module-level rate-limiter polls ``time.monotonic`` in a
    while loop to refill tokens; under a faked clock that loop sees no
    elapsed time and spins forever. Stub the limiter to a no-op so
    the test isolates ``iter_market_pages``'s budget check.
    """
    request = httpx.Request("GET", "https://example.test/markets")

    # No-op the shared rate limiter for this test only.
    monkeypatch.setattr(
        kalshi_module._KALSHI_RATE_LIMITER, "acquire", lambda: None
    )

    # Fake clock that ONLY advances when ``iter_market_pages`` reads
    # it. The function reads ``time.monotonic`` once before the loop
    # (started) and once per iteration (budget check). Drive it from a
    # counter so the elapsed value is deterministic.
    monotonic_calls = {"count": 0}

    def _fake_monotonic() -> float:
        monotonic_calls["count"] += 1
        # Sequence: 0 (started), then 1, 2, 3, … per iteration.
        return float(monotonic_calls["count"] - 1)

    monkeypatch.setattr(kalshi_module.time, "monotonic", _fake_monotonic)

    class FakeHttpClient:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, _url: str, **_kwargs):
            self.calls += 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "markets": [{"ticker": f"KX-{self.calls}"}],
                    # Always hand out a next-cursor so the only stop
                    # signal is the wall-clock budget.
                    "cursor": f"cursor-{self.calls}",
                },
            )

    fake = FakeHttpClient()
    client = KalshiPublicClient(base_url="https://example.test", http_client=fake)
    # ``time.monotonic`` returns 0 (started), then 1, 2, 3, …
    # With budget=2.5 the iterations see elapsed = 1, 2, 3; the
    # iter-3 check (elapsed=3 > 2.5) breaks before that page lands,
    # so we get pages from iterations 1 and 2 only.
    markets = client.list_markets(
        status="open",
        limit=1000,
        mve_filter="exclude",
        wall_clock_budget_seconds=2.5,
    )

    assert fake.calls == 2, (
        f"wall-clock budget should stop pagination once elapsed > budget; "
        f"got {fake.calls} pages"
    )
    assert len(markets) == 2

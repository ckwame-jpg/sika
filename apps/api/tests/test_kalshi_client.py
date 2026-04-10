import httpx

from app.clients.kalshi import KalshiPublicClient


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

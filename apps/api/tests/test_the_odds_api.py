"""Tests for Smarter #18 Phase 1 — The Odds API client + vig-removal.

The HTTP client is exercised against a stub so no network is touched.
Vig-removal math is unit-tested directly against known American /
decimal-odds values. Phase 2 (cache + scoring integration) will land
in a follow-up PR.
"""

from typing import Any

import httpx
import pytest

from app.clients.the_odds_api import (
    MissingApiKeyError,
    TheOddsApiClient,
    consensus_yes_probability,
    decimal_to_implied_probability,
    devig_two_way_market,
    odds_api_sport_key,
)


# -- sport-key mapping ---------------------------------------------------


def test_odds_api_sport_key_maps_known_sports() -> None:
    assert odds_api_sport_key("NBA") == "basketball_nba"
    assert odds_api_sport_key("MLB") == "baseball_mlb"
    assert odds_api_sport_key("NFL") == "americanfootball_nfl"


def test_odds_api_sport_key_is_case_insensitive() -> None:
    assert odds_api_sport_key("nba") == "basketball_nba"
    assert odds_api_sport_key("mlb") == "baseball_mlb"


def test_odds_api_sport_key_returns_none_for_unsupported_sport() -> None:
    assert odds_api_sport_key("CRICKET") is None
    assert odds_api_sport_key("") is None


# -- decimal_to_implied_probability --------------------------------------


def test_decimal_odds_2_implies_50_percent() -> None:
    assert decimal_to_implied_probability(2.0) == 0.5


def test_decimal_odds_191_implies_minus_110_american() -> None:
    # -110 American = 1.909... decimal = 52.38% implied.
    assert decimal_to_implied_probability(1.91) == pytest.approx(0.5236, abs=1e-3)


def test_decimal_odds_3_implies_33_percent() -> None:
    assert decimal_to_implied_probability(3.0) == pytest.approx(0.3333, abs=1e-3)


def test_decimal_to_implied_returns_none_for_non_positive() -> None:
    assert decimal_to_implied_probability(0.0) is None
    assert decimal_to_implied_probability(-1.5) is None


def test_decimal_to_implied_rejects_non_numeric() -> None:
    assert decimal_to_implied_probability("2.0") is None  # type: ignore[arg-type]
    assert decimal_to_implied_probability(None) is None  # type: ignore[arg-type]


def test_decimal_to_implied_rejects_bool() -> None:
    # ``bool`` is a subclass of int in Python — reject explicitly so
    # True/False don't accidentally produce 1.0/inf-style outputs.
    assert decimal_to_implied_probability(True) is None  # type: ignore[arg-type]
    assert decimal_to_implied_probability(False) is None  # type: ignore[arg-type]


# -- devig_two_way_market ------------------------------------------------


def test_devig_pick_em_market_sums_to_unity() -> None:
    # Both sides at -110 (1.91): raw probs ~ 0.5236 each, sum ~ 1.0472.
    # De-vigged: 0.5 / 0.5 — fair pick'em.
    devigged = devig_two_way_market(1.91, 1.91)
    assert devigged is not None
    yes_prob, no_prob = devigged
    assert yes_prob == pytest.approx(0.5, abs=1e-4)
    assert no_prob == pytest.approx(0.5, abs=1e-4)


def test_devig_favorite_underdog_market_sums_to_unity() -> None:
    # Favorite at 1.5, underdog at 2.7. Raw: 0.6667 + 0.3704 = 1.0371.
    # De-vigged: ~0.643 / ~0.357.
    devigged = devig_two_way_market(1.5, 2.7)
    assert devigged is not None
    yes_prob, no_prob = devigged
    assert yes_prob == pytest.approx(0.6429, abs=1e-3)
    assert no_prob == pytest.approx(0.3571, abs=1e-3)
    assert yes_prob + no_prob == pytest.approx(1.0, abs=1e-6)


def test_devig_returns_none_when_either_side_unusable() -> None:
    assert devig_two_way_market(0.0, 2.0) is None
    assert devig_two_way_market(2.0, -1.0) is None
    assert devig_two_way_market("1.91", 1.91) is None  # type: ignore[arg-type]


# -- consensus_yes_probability ------------------------------------------


def _bookmaker(name: str, yes_team: str, no_team: str, yes_price: float, no_price: float) -> dict[str, Any]:
    return {
        "key": name.lower().replace(" ", "_"),
        "title": name,
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": yes_team, "price": yes_price},
                    {"name": no_team, "price": no_price},
                ],
            }
        ],
    }


def test_consensus_averages_devigged_probabilities_across_books() -> None:
    books = [
        _bookmaker("DraftKings", "Celtics", "Nets", 1.5, 2.7),
        _bookmaker("FanDuel", "Celtics", "Nets", 1.53, 2.6),
        _bookmaker("BetMGM", "Celtics", "Nets", 1.48, 2.75),
    ]
    consensus = consensus_yes_probability(books, yes_team_name="Celtics", no_team_name="Nets")
    assert consensus is not None
    yes_prob, book_count = consensus
    assert book_count == 3
    # Each book devigs to roughly 0.64 → consensus around 0.64.
    assert yes_prob == pytest.approx(0.645, abs=0.01)


def test_consensus_is_case_insensitive_on_team_names() -> None:
    books = [_bookmaker("DraftKings", "Boston Celtics", "Brooklyn Nets", 1.5, 2.7)]
    consensus = consensus_yes_probability(
        books,
        yes_team_name="boston celtics",
        no_team_name="brooklyn nets",
    )
    assert consensus is not None


def test_consensus_skips_books_missing_h2h_market() -> None:
    books = [
        _bookmaker("DraftKings", "Celtics", "Nets", 1.5, 2.7),
        # FanDuel has a different market (totals) — should be skipped.
        {
            "key": "fanduel",
            "title": "FanDuel",
            "markets": [{"key": "totals", "outcomes": [{"name": "Over", "price": 1.9}]}],
        },
    ]
    consensus = consensus_yes_probability(books, yes_team_name="Celtics", no_team_name="Nets")
    assert consensus is not None
    _yes, book_count = consensus
    assert book_count == 1


def test_consensus_skips_books_with_unparseable_outcomes() -> None:
    books = [
        _bookmaker("DraftKings", "Celtics", "Nets", 1.5, 2.7),
        # Malformed: missing prices.
        {
            "key": "fanduel",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [{"name": "Celtics"}, {"name": "Nets"}],
                }
            ],
        },
    ]
    consensus = consensus_yes_probability(books, yes_team_name="Celtics", no_team_name="Nets")
    assert consensus is not None
    _yes, book_count = consensus
    assert book_count == 1


def test_consensus_returns_none_when_no_book_has_usable_quote() -> None:
    books = [
        {"key": "fanduel", "markets": [{"key": "totals", "outcomes": []}]},
    ]
    assert consensus_yes_probability(books, yes_team_name="Celtics", no_team_name="Nets") is None


def test_consensus_returns_none_when_team_names_missing() -> None:
    books = [_bookmaker("DraftKings", "Celtics", "Nets", 1.5, 2.7)]
    assert consensus_yes_probability(books, yes_team_name="", no_team_name="Nets") is None
    assert consensus_yes_probability(books, yes_team_name="Celtics", no_team_name="") is None


def test_consensus_returns_none_when_bookmakers_is_not_a_list() -> None:
    assert consensus_yes_probability(None, yes_team_name="A", no_team_name="B") is None  # type: ignore[arg-type]
    assert consensus_yes_probability("string", yes_team_name="A", no_team_name="B") is None  # type: ignore[arg-type]


# -- TheOddsApiClient ----------------------------------------------------


class _StubHttpClient:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else []
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, "params": kwargs.get("params", {})})
        return httpx.Response(
            status_code=self.status_code,
            json=self.payload,
            request=httpx.Request("GET", url),
        )


def test_client_raises_missing_api_key_when_setting_empty(monkeypatch) -> None:
    from app.config import get_settings as _get_settings

    _get_settings.cache_clear()
    monkeypatch.setenv("THE_ODDS_API_KEY", "")
    _get_settings.cache_clear()
    client = TheOddsApiClient(http_client=_StubHttpClient())
    with pytest.raises(MissingApiKeyError):
        client.fetch_h2h_odds("NBA")


def test_client_returns_empty_list_for_unsupported_sport(monkeypatch) -> None:
    from app.config import get_settings as _get_settings

    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    _get_settings.cache_clear()
    client = TheOddsApiClient(http_client=_StubHttpClient(payload=[]))
    assert client.fetch_h2h_odds("CRICKET") == []


def test_client_sends_apikey_and_market_parameters(monkeypatch) -> None:
    from app.config import get_settings as _get_settings

    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    _get_settings.cache_clear()
    stub = _StubHttpClient(
        payload=[
            {
                "id": "evt-1",
                "sport_key": "basketball_nba",
                "home_team": "Celtics",
                "away_team": "Nets",
                "bookmakers": [],
            }
        ]
    )
    client = TheOddsApiClient(http_client=stub)
    events = client.fetch_h2h_odds("NBA")
    assert len(events) == 1
    assert events[0]["home_team"] == "Celtics"
    assert stub.calls[0]["url"].endswith("/sports/basketball_nba/odds")
    assert stub.calls[0]["params"]["apiKey"] == "test-key"
    assert stub.calls[0]["params"]["markets"] == "h2h"
    assert stub.calls[0]["params"]["regions"] == "us"
    assert stub.calls[0]["params"]["oddsFormat"] == "decimal"


def test_client_returns_empty_list_when_payload_is_not_a_list(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.config import get_settings as _get_settings

    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    _get_settings.cache_clear()
    stub = _StubHttpClient(payload={"error": "Invalid sport key"})
    client = TheOddsApiClient(http_client=stub)
    with caplog.at_level("WARNING", logger="app.clients.the_odds_api"):
        result = client.fetch_h2h_odds("NBA")
    assert result == []
    # The unexpected shape is logged for operator visibility.
    assert any("non-list payload" in r.getMessage() for r in caplog.records)


def test_client_raises_on_4xx(monkeypatch) -> None:
    from app.config import get_settings as _get_settings

    monkeypatch.setenv("THE_ODDS_API_KEY", "test-key")
    _get_settings.cache_clear()
    stub = _StubHttpClient(status_code=401, payload={"message": "Invalid API key"})
    client = TheOddsApiClient(http_client=stub)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_h2h_odds("NBA")

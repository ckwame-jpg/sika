"""HTTP client for The Odds API (https://the-odds-api.com).

Smarter #18 Phase 1: thin wrapper around the H2H (head-to-head /
moneyline) odds endpoint. Vig-removal and the consumer-side scoring
diagnostic ship in a follow-up PR; this layer is the foundation.

Operator setup: drop a key into ``the_odds_api_key`` (env or settings).
An empty key short-circuits — the client raises ``MissingApiKeyError``
which the calling service catches and returns ``None``. No fallout to
the existing scoring pipeline when the key is unset.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings


logger = logging.getLogger(__name__)


# Map sika's sport keys to The Odds API's sport identifiers. ESPN uses
# "NBA"/"MLB"/etc.; The Odds API uses "basketball_nba"/"baseball_mlb".
# Limited intentionally to the sports sika currently scores — adding
# a new entry requires confirming the upstream slug.
_SPORT_KEY_TO_ODDS_API_KEY: dict[str, str] = {
    "NBA": "basketball_nba",
    "MLB": "baseball_mlb",
    "NFL": "americanfootball_nfl",
    "TENNIS": "tennis_atp_french_open",  # placeholder; tour-aware is a follow-up
    # Smarter WNBA PR 6 — The Odds API supports WNBA h2h / spreads /
    # totals / player props on the free tier with the ``basketball_wnba``
    # slug (prep doc §3). Sport-consensus path picks this up
    # automatically once the slug resolves.
    "WNBA": "basketball_wnba",
}


class MissingApiKeyError(RuntimeError):
    """Raised when ``the_odds_api_key`` is empty.

    Callers should catch this and return ``None`` / ``{}`` so the
    scoring path skips the sportsbook prior without erroring.
    """


def odds_api_sport_key(sika_sport_key: str) -> str | None:
    """Translate a sika sport_key to The Odds API's slug.

    Returns ``None`` for sports not yet mapped — callers should
    gracefully skip rather than raise. Adding a new mapping requires
    confirming the upstream slug in The Odds API's ``/sports`` endpoint.
    """
    return _SPORT_KEY_TO_ODDS_API_KEY.get(sika_sport_key.upper())


class TheOddsApiClient:
    """Thin HTTP wrapper around The Odds API's H2H odds endpoint.

    Single-purpose for Phase 1 — only ``fetch_h2h_odds`` exposed.
    Spread / totals / props markets are deferred to follow-up PRs.
    """

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._http_client = http_client

    def _api_key(self) -> str:
        key = (get_settings().the_odds_api_key or "").strip()
        if not key:
            raise MissingApiKeyError("the_odds_api_key is not configured")
        return key

    def _base_url(self) -> str:
        return get_settings().the_odds_api_base_url.rstrip("/")

    def _timeout(self) -> float:
        return float(get_settings().the_odds_api_request_timeout_seconds)

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        if self._http_client is not None:
            return self._http_client.get(url, **kwargs)
        return httpx.get(url, **kwargs)

    def fetch_odds_with_quota(
        self,
        sika_sport_key: str,
        *,
        markets: str = "h2h",
        regions: str = "us",
        odds_format: str = "decimal",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch odds for a sport and return ``(events, quota)``.

        Smarter NFL PR 4 — generalizes the original h2h-only fetch to
        arbitrary market lists. IMPORTANT for budgeting: The Odds API
        charges ONE credit per market per region, so
        ``markets="h2h,spreads,totals"`` costs 3 credits per call —
        the caller-side cache/gating (``odds_api_cache``) is what keeps
        the free tier viable.

        ``quota`` carries the response's ``x-requests-remaining`` /
        ``x-requests-used`` headers (ints when present) so the cache
        layer can surface budget burn on the upstream-health board.

        Raises ``MissingApiKeyError`` when the key is unset and
        ``httpx.HTTPStatusError`` on 4xx/5xx, matching the original
        fetch semantics.
        """
        slug = odds_api_sport_key(sika_sport_key)
        if slug is None:
            return [], {}
        url = f"{self._base_url()}/sports/{slug}/odds"
        params = {
            "apiKey": self._api_key(),
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
        }
        response = self._get(url, params=params, timeout=self._timeout())
        response.raise_for_status()
        quota: dict[str, Any] = {}
        for header, key in (
            ("x-requests-remaining", "requests_remaining"),
            ("x-requests-used", "requests_used"),
        ):
            raw = response.headers.get(header)
            if raw is not None:
                try:
                    quota[key] = int(float(raw))
                except (TypeError, ValueError):
                    pass
        if quota.get("requests_remaining") is not None:
            logger.info(
                "The Odds API quota after %s fetch (markets=%s): %s remaining",
                sika_sport_key,
                markets,
                quota["requests_remaining"],
            )
        payload = response.json()
        if not isinstance(payload, list):
            logger.warning(
                "The Odds API returned non-list payload for %s: %r",
                sika_sport_key,
                type(payload).__name__,
            )
            return [], quota
        return payload, quota

    def fetch_h2h_odds(
        self,
        sika_sport_key: str,
        *,
        regions: str = "us",
        odds_format: str = "decimal",
    ) -> list[dict[str, Any]]:
        """Return the raw H2H quote list for a sport.

        Each event in the returned list has the shape::

            {
                "id": "...",
                "sport_key": "basketball_nba",
                "commence_time": "2026-05-14T23:00:00Z",
                "home_team": "Boston Celtics",
                "away_team": "Brooklyn Nets",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "last_update": "2026-05-14T22:30:00Z",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Boston Celtics", "price": 1.4},
                                    {"name": "Brooklyn Nets", "price": 3.0},
                                ],
                            }
                        ],
                    },
                    ...
                ],
            }

        Raises ``MissingApiKeyError`` when the key is unset (caller
        treats as "skip sportsbook prior for this scoring pass").
        Raises ``httpx.HTTPStatusError`` on 4xx/5xx so callers can
        record per-source failure on the upstream-health board.
        """
        events, _quota = self.fetch_odds_with_quota(
            sika_sport_key, markets="h2h", regions=regions, odds_format=odds_format,
        )
        return events


# -----------------------------------------------------------------------------
# Vig-removal helpers
#
# Sportsbook quotes are over-round: the implied probabilities across all
# outcomes sum to more than 1.0 — the excess is the book's margin / "vig".
# To use the quote as a probability prior we need to strip the vig.
#
# The standard de-vig approach for a two-outcome market: divide each raw
# implied probability by the sum. Result sums to 1.0 and is a fair
# probability estimate under the book's pricing.


def decimal_to_implied_probability(decimal_price: float) -> float | None:
    """Convert decimal odds to raw implied probability (with vig).

    1.91 → 0.5236  (American -110)
    2.00 → 0.5
    3.00 → 0.3333

    Returns ``None`` when ``decimal_price`` is non-positive — those are
    nonsensical and shouldn't propagate as a probability.
    """
    if not isinstance(decimal_price, (int, float)) or isinstance(decimal_price, bool):
        return None
    if decimal_price <= 0:
        return None
    return round(1.0 / float(decimal_price), 6)


def devig_two_way_market(
    yes_price: float,
    no_price: float,
) -> tuple[float, float] | None:
    """De-vig a two-outcome quote, returning ``(yes_prob, no_prob)``
    that sum to 1.0.

    Returns ``None`` when either price is unusable (non-positive,
    non-numeric) or both prices imply zero — the caller treats this as
    a "no usable quote" signal.
    """
    raw_yes = decimal_to_implied_probability(yes_price)
    raw_no = decimal_to_implied_probability(no_price)
    if raw_yes is None or raw_no is None:
        return None
    total = raw_yes + raw_no
    if total <= 0:
        return None
    return round(raw_yes / total, 6), round(raw_no / total, 6)


def consensus_yes_probability(
    bookmakers: list[dict[str, Any]],
    *,
    yes_team_name: str,
    no_team_name: str,
) -> tuple[float, int] | None:
    """Return ``(consensus_yes_probability, book_count)`` across the
    supplied bookmakers, or ``None`` when no usable book is found.

    Each bookmaker's H2H market is de-vigged independently, then the
    de-vigged YES probabilities are simple-averaged across books. The
    book count surfaces how broad the consensus is — a 1-book consensus
    is far weaker signal than an 8-book consensus.
    """
    if not isinstance(bookmakers, list):
        return None
    yes_probs: list[float] = []
    yes_target = (yes_team_name or "").strip().lower()
    no_target = (no_team_name or "").strip().lower()
    if not yes_target or not no_target:
        return None
    for book in bookmakers:
        if not isinstance(book, dict):
            continue
        markets = book.get("markets") or []
        if not isinstance(markets, list):
            continue
        h2h_market = next(
            (m for m in markets if isinstance(m, dict) and m.get("key") == "h2h"),
            None,
        )
        if h2h_market is None:
            continue
        outcomes = h2h_market.get("outcomes") or []
        if not isinstance(outcomes, list):
            continue
        yes_price: float | None = None
        no_price: float | None = None
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            name = str(outcome.get("name") or "").strip().lower()
            price = outcome.get("price")
            if name == yes_target:
                yes_price = price if isinstance(price, (int, float)) else None
            elif name == no_target:
                no_price = price if isinstance(price, (int, float)) else None
        if yes_price is None or no_price is None:
            continue
        devigged = devig_two_way_market(yes_price, no_price)
        if devigged is None:
            continue
        yes_probs.append(devigged[0])
    if not yes_probs:
        return None
    return round(sum(yes_probs) / len(yes_probs), 6), len(yes_probs)


# -----------------------------------------------------------------------------
# Line consensus (Smarter NFL PR 4)
#
# ``spreads`` and ``totals`` markets quote a POINT per outcome in
# addition to the price. The consensus signal sika wants from them is
# the line itself (the market's margin / total expectation) — the
# median across books is robust to a single stale or outlier book.


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def consensus_spread_point(
    bookmakers: list[dict[str, Any]],
    *,
    team_name: str,
) -> tuple[float, int] | None:
    """Return ``(median_spread_point, book_count)`` for ``team_name``
    across books' ``spreads`` markets, or ``None`` when no book quotes
    it. The point is oriented to the named team: -3.5 means that team
    is favored by 3.5."""
    if not isinstance(bookmakers, list):
        return None
    target = (team_name or "").strip().lower()
    if not target:
        return None
    points: list[float] = []
    for book in bookmakers:
        if not isinstance(book, dict):
            continue
        markets = book.get("markets") or []
        if not isinstance(markets, list):
            continue
        spread_market = next(
            (m for m in markets if isinstance(m, dict) and m.get("key") == "spreads"),
            None,
        )
        if spread_market is None:
            continue
        for outcome in spread_market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            if str(outcome.get("name") or "").strip().lower() != target:
                continue
            point = outcome.get("point")
            if isinstance(point, (int, float)) and not isinstance(point, bool):
                points.append(float(point))
            break
    median_point = _median(points)
    if median_point is None:
        return None
    return round(median_point, 2), len(points)


def consensus_total_point(
    bookmakers: list[dict[str, Any]],
) -> tuple[float, int] | None:
    """Return ``(median_total_point, book_count)`` across books'
    ``totals`` markets (the Over outcome's point), or ``None``."""
    if not isinstance(bookmakers, list):
        return None
    points: list[float] = []
    for book in bookmakers:
        if not isinstance(book, dict):
            continue
        markets = book.get("markets") or []
        if not isinstance(markets, list):
            continue
        totals_market = next(
            (m for m in markets if isinstance(m, dict) and m.get("key") == "totals"),
            None,
        )
        if totals_market is None:
            continue
        for outcome in totals_market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            if str(outcome.get("name") or "").strip().lower() != "over":
                continue
            point = outcome.get("point")
            if isinstance(point, (int, float)) and not isinstance(point, bool):
                points.append(float(point))
            break
    median_point = _median(points)
    if median_point is None:
        return None
    return round(median_point, 2), len(points)

from app.models import Market
from app.services.kalshi_account import build_kalshi_account_snapshot


class FakeConfiguredKalshiAccountClient:
    def is_configured(self):
        return True

    def get_balance(self):
        return {"balance": 12550, "portfolio_value": 17125, "updated_ts": 1711814400}

    def list_positions(self, *, count_filter, limit):
        return {
            "market_positions": [
                {
                    "ticker": "NBA-TEST",
                    "position_fp": "3.00",
                    "total_traded_dollars": "1.6500",
                    "market_exposure_dollars": "1.3500",
                    "realized_pnl_dollars": "0.2400",
                    "fees_paid_dollars": "0.0100",
                    "resting_orders_count": 1,
                    "last_updated_ts": "2026-04-29T12:00:00Z",
                }
            ]
        }

    def list_markets_by_tickers(self, tickers):
        return [
            {
                "ticker": "NBA-TEST",
                "title": "Celtics to win?",
                "subtitle": "NBA regular season",
                "yes_sub_title": "Boston Celtics",
                "no_sub_title": "Brooklyn Nets",
            }
        ]

    def list_fills(self, *, limit):
        return {
            "fills": [
                {
                    "fill_id": "fill-1",
                    "trade_id": "trade-1",
                    "order_id": "order-1",
                    "ticker": "NBA-TEST",
                    "side": "yes",
                    "action": "buy",
                    "count_fp": "3.00",
                    "yes_price_dollars": "0.5500",
                    "fee_cost": "0.0100",
                    "created_time": "2026-04-29T12:01:00Z",
                }
            ]
        }


class FakeUnknownTickerKalshiAccountClient(FakeConfiguredKalshiAccountClient):
    def list_positions(self, *, count_filter, limit):
        return {
            "market_positions": [
                {
                    "ticker": "KXSWIFTKELCEWEDDINGLOCATION-30-PEN",
                    "position_fp": "117.00",
                    "market_exposure_dollars": "4.6800",
                    "realized_pnl_dollars": "0.0000",
                    "resting_orders_count": 0,
                }
            ]
        }

    def list_fills(self, *, limit):
        return {"fills": []}

    def list_markets_by_tickers(self, tickers):
        return [
            {
                "ticker": "KXSWIFTKELCEWEDDINGLOCATION-30-PEN",
                "title": "Where will Taylor Swift and Travis Kelce's Wedding occur?",
                "yes_sub_title": "Pennsylvania",
            }
        ]


class FakeMultilegKalshiAccountClient(FakeUnknownTickerKalshiAccountClient):
    def list_positions(self, *, count_filter, limit):
        return {
            "market_positions": [
                {
                    "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-TEST",
                    "position_fp": "5.00",
                    "market_exposure_dollars": "1.0000",
                    "realized_pnl_dollars": "0.0000",
                    "resting_orders_count": 0,
                }
            ]
        }

    def list_markets_by_tickers(self, tickers):
        return [
            {
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-TEST",
                "title": "yes Boston,yes Bam Adebayo: 15+",
                "subtitle": "NBA combo",
                "mve_collection_ticker": "KXMVE-NBA-MIXED-COLLECTION",
            }
        ]


class FakeMetadataFailureKalshiAccountClient(FakeUnknownTickerKalshiAccountClient):
    def list_markets_by_tickers(self, tickers):
        raise RuntimeError("metadata unavailable")


class FakeMissingKalshiAccountClient:
    def is_configured(self):
        return False


def test_kalshi_account_snapshot_maps_live_positions_and_fills(db_session):
    db_session.add(
        Market(
            ticker="NBA-TEST",
            sport_key="NBA",
            title="Celtics to win?",
            status="open",
        )
    )
    db_session.commit()

    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeConfiguredKalshiAccountClient(),
    )

    assert snapshot.status == "connected"
    assert snapshot.balance.cash_balance_dollars == 125.5
    assert snapshot.balance.portfolio_value_dollars == 171.25
    assert snapshot.market_positions[0].market_title == "Celtics to win?"
    assert snapshot.market_positions[0].market_subtitle == "NBA regular season"
    assert snapshot.market_positions[0].bet_label == "YES Boston Celtics"
    assert snapshot.market_positions[0].bet_subtitle == "Celtics to win?"
    assert snapshot.market_positions[0].position == 3
    assert snapshot.market_positions[0].realized_pnl_dollars == 0.24
    assert snapshot.recent_fills[0].ticker == "NBA-TEST"
    assert snapshot.recent_fills[0].bet_label == "YES Boston Celtics"
    assert snapshot.recent_fills[0].yes_price_dollars == 0.55


def test_kalshi_account_snapshot_enriches_unknown_tickers_from_kalshi_metadata(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeUnknownTickerKalshiAccountClient(),
    )

    assert snapshot.status == "connected"
    assert snapshot.market_positions[0].bet_label == "YES Pennsylvania"
    assert (
        snapshot.market_positions[0].bet_subtitle
        == "Where will Taylor Swift and Travis Kelce's Wedding occur?"
    )


def test_kalshi_account_snapshot_uses_compact_multileg_labels(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeMultilegKalshiAccountClient(),
    )

    assert snapshot.market_positions[0].bet_label == "Boston + Bam Adebayo: 15+"
    assert snapshot.market_positions[0].bet_subtitle == "NBA combo"


def test_kalshi_account_snapshot_falls_back_to_ticker_when_metadata_lookup_fails(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeMetadataFailureKalshiAccountClient(),
    )

    assert snapshot.status == "connected"
    assert snapshot.market_positions[0].bet_label == "KXSWIFTKELCEWEDDINGLOCATION-30-PEN"
    assert snapshot.market_positions[0].bet_subtitle is None


def test_kalshi_account_snapshot_reports_missing_credentials(db_session):
    snapshot = build_kalshi_account_snapshot(
        db_session,
        client=FakeMissingKalshiAccountClient(),
    )

    assert snapshot.configured is False
    assert snapshot.status == "not_configured"
    assert snapshot.market_positions == []
    assert snapshot.recent_fills == []

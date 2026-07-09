"""Smarter NFL PR 2 — market classification for NFL Kalshi markets.

Everything ticker-shaped here is grounded in Kalshi's live series
inventory (fetched 2026-07-09):

- Game lines: KXNFLGAME (winner), KXNFLSPREAD, KXNFLTOTAL, plus the
  1H/2H/1Q-4Q variants and KXNFLTEAMTOTAL (team totals — deliberately
  rejected, see the TEAMTOTAL guard).
- Player props: KXNFLPASSYDS, KXNFLPASSTDS, KXNFLRSHYDS (Kalshi's
  abbreviation — NOT "RUSHYDS"), KXNFLRECYDS, KXNFLREC.
- Winner titles use a phrasing no other sport does: "Will Seattle win
  the Dallas vs Seattle Pro Football game?" (live-verified against
  preseason markets). The classifier's new branch requires the " vs "
  matchup AND trailing "game?" so season futures ("Will Buffalo win
  the AFC East?") can't classify as game winners.

NFL stays research_only after this PR — classification makes NFL
markets persist + carry metadata; scoring/watchlist gates flip in
PR 10.
"""

from __future__ import annotations

from app.services.market_support import (
    classify_market_payload,
    combo_leg_metadata_prefilter,
)


def _payload(**overrides) -> dict:
    base = {
        "ticker": "KXNFLGAME-26SEP13DALPHI-PHI",
        "event_ticker": "KXNFLGAME-26SEP13DALPHI",
        "series_ticker": "KXNFLGAME",
        "title": "Will Philadelphia win the Dallas vs Philadelphia Pro Football game?",
        "yes_sub_title": "Philadelphia",
    }
    base.update(overrides)
    return base


# --- Winner ---------------------------------------------------------------

def test_nfl_game_winner_classifies_from_live_phrasing() -> None:
    result = classify_market_payload(_payload())
    assert result["supported"] is True
    metadata = result["metadata"]
    assert metadata["copilot_market_family"] == "winner"
    assert metadata["copilot_market_kind"] == "game_winner"
    assert metadata["copilot_subject_name"] == "Philadelphia"


def test_nfl_season_futures_do_not_classify_as_game_winner() -> None:
    result = classify_market_payload(_payload(
        ticker="KXNFLAFCEAST-26-BUF",
        event_ticker="KXNFLAFCEAST-26",
        series_ticker="KXNFLAFCEAST",
        title="Will Buffalo win the AFC East?",
        yes_sub_title="Buffalo",
    ))
    assert result["supported"] is False


def test_nfl_quarter_winner_stays_unsupported() -> None:
    result = classify_market_payload(_payload(
        ticker="KXNFL1QWINNER-26SEP13DALPHI-PHI",
        event_ticker="KXNFL1QWINNER-26SEP13DALPHI",
        series_ticker="KXNFL1QWINNER",
        title="Dallas vs Philadelphia: 1st Quarter Winner?",
        yes_sub_title="Philadelphia",
    ))
    assert result["supported"] is False


# --- Game lines -----------------------------------------------------------

def test_nfl_spread_classifies() -> None:
    result = classify_market_payload(_payload(
        ticker="KXNFLSPREAD-26SEP13DALPHI-PHI3",
        event_ticker="KXNFLSPREAD-26SEP13DALPHI",
        series_ticker="KXNFLSPREAD",
        title="Philadelphia wins by over 3.5 points?",
        yes_sub_title=None,
    ))
    assert result["supported"] is True
    metadata = result["metadata"]
    assert metadata["copilot_market_family"] == "game_line"
    assert metadata["copilot_market_kind"] == "spread"
    assert metadata["copilot_stat_key"] == "margin_points"
    assert metadata["copilot_threshold"] == 3.5
    assert metadata["copilot_subject_name"] == "Philadelphia"


def test_nfl_total_classifies() -> None:
    result = classify_market_payload(_payload(
        ticker="KXNFLTOTAL-26SEP13DALPHI-47",
        event_ticker="KXNFLTOTAL-26SEP13DALPHI",
        series_ticker="KXNFLTOTAL",
        title="Over 47.5 points scored?",
        yes_sub_title=None,
    ))
    assert result["supported"] is True
    metadata = result["metadata"]
    assert metadata["copilot_market_kind"] == "total"
    assert metadata["copilot_stat_key"] == "total_points"
    assert metadata["copilot_threshold"] == 47.5


def test_nfl_team_total_rejected_despite_total_shaped_title() -> None:
    """KXNFLTEAMTOTAL could title exactly like a game total; pricing a
    team total with the game-total model would be badly wrong, so the
    ticker guard rejects it before the title regex can match."""
    result = classify_market_payload(_payload(
        ticker="KXNFLTEAMTOTAL-26SEP13DALPHI-PHI24",
        event_ticker="KXNFLTEAMTOTAL-26SEP13DALPHI",
        series_ticker="KXNFLTEAMTOTAL",
        title="Over 24.5 points scored?",
        yes_sub_title=None,
    ))
    assert result["supported"] is False


# --- Player props ----------------------------------------------------------

def test_nfl_passing_yards_prop_classifies() -> None:
    result = classify_market_payload(_payload(
        ticker="KXNFLPASSYDS-26SEP13DALPHI-JHURTS-250",
        event_ticker="KXNFLPASSYDS-26SEP13DALPHI",
        series_ticker="KXNFLPASSYDS",
        title="Jalen Hurts: 250+ passing yards?",
        yes_sub_title=None,
    ))
    assert result["supported"] is True
    metadata = result["metadata"]
    assert metadata["copilot_market_family"] == "player_prop"
    assert metadata["copilot_stat_key"] == "passing_yards"
    assert metadata["copilot_component_stat_keys"] == ["passing_yards"]
    assert metadata["copilot_threshold"] == 250.0
    assert metadata["copilot_subject_name"] == "Jalen Hurts"


def test_nfl_receptions_prop_classifies() -> None:
    result = classify_market_payload(_payload(
        ticker="KXNFLREC-26SEP13DALPHI-CLAMB-6",
        event_ticker="KXNFLREC-26SEP13DALPHI",
        series_ticker="KXNFLREC",
        title="CeeDee Lamb: 6+ receptions?",
        yes_sub_title=None,
    ))
    assert result["supported"] is True
    assert result["metadata"]["copilot_stat_key"] == "receptions"


def test_nfl_combined_rush_rec_prop_expands_shorthand() -> None:
    """"rush + rec yards" must expand BEFORE the "+" split — otherwise
    the fragments "rush" / "rec yards" match no alias and the market is
    wrongly rejected. Combined key joins with "_" (note for PR 7: NFL
    component keys contain underscores, so split("_") can NOT recover
    the components — use copilot_component_stat_keys)."""
    result = classify_market_payload(_payload(
        ticker="KXNFLRUSHRECYDS-26SEP13DALPHI-SBARKLEY-99",
        event_ticker="KXNFLRUSHRECYDS-26SEP13DALPHI",
        series_ticker="KXNFLRUSHRECYDS",
        title="Saquon Barkley: 99+ rush + rec yards?",
        yes_sub_title=None,
    ))
    assert result["supported"] is True
    metadata = result["metadata"]
    assert metadata["copilot_stat_key"] == "rushing_yards_receiving_yards"
    assert metadata["copilot_component_stat_keys"] == ["rushing_yards", "receiving_yards"]


def test_nfl_unsupported_prop_category_reports_reason() -> None:
    result = classify_market_payload(_payload(
        ticker="KXNFLSACKS-26SEP13DALPHI-MPARSONS-1",
        event_ticker="KXNFLSACKS-26SEP13DALPHI",
        series_ticker="KXNFLSACKS",
        title="Micah Parsons: 1+ sacks?",
        yes_sub_title=None,
    ))
    assert result["supported"] is False
    assert result["reason"] == "unsupported_prop_category"
    assert result["prop_category"] == "sacks"


# --- Combo-leg prefilter ----------------------------------------------------

def test_combo_prefilter_accepts_live_verified_nfl_prop_families() -> None:
    for family in ("PASSYDS", "PASSTDS", "RSHYDS", "RECYDS", "REC"):
        result = combo_leg_metadata_prefilter({
            "market_ticker": f"KXNFL{family}-26SEP13DALPHI-PLAYER-100",
            "event_ticker": f"KXNFL{family}-26SEP13DALPHI",
        })
        assert result["supported"] is True, f"family {family} should be supported"
        assert result["sport_key"] == "NFL"
        assert result["market_family_code"] == family


def test_combo_prefilter_blocks_nfl_game_structure_families() -> None:
    cases = {
        "KXNFLGAME-26SEP13DALPHI-PHI": "GAME",
        "KXNFLSPREAD-26SEP13DALPHI-PHI3": "SPREAD",
        "KXNFLTOTAL-26SEP13DALPHI-47": "TOTAL",
        "KXNFLTEAMTOTAL-26SEP13DALPHI-PHI24": "TEAMTOTAL",
        "KXNFL1HSPREAD-26SEP13DALPHI-PHI3": "1HSPREAD",
        "KXNFLWINMARGIN-26SEP13DALPHI-PHI7": "WINMARGIN",
        "KXNFLWINS-ARI-26-9": "WINS",
    }
    for ticker, family in cases.items():
        result = combo_leg_metadata_prefilter({
            "market_ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
        })
        assert result["supported"] is False, f"{ticker} should be blocked"
        assert result["reason"] == "unsupported_market_family"


def test_combo_prefilter_rejects_nfl_td_scorer_and_exotics() -> None:
    for ticker in (
        "KXNFLANYTD-26SEP13DALPHI-JGIBBS-1",
        "KXNFLFIRSTTD-26SEP13DALPHI-JGIBBS-1",
        "KXNFL2TD-26SEP13DALPHI-JGIBBS-1",
    ):
        result = combo_leg_metadata_prefilter({
            "market_ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
        })
        assert result["supported"] is False
        assert result["reason"] == "unsupported_prop_category"


# --- Kalshi deep-link constants ---------------------------------------------

def test_kalshi_constants_include_nfl_in_routes_and_trade_desk() -> None:
    """Both copies (Bug #30 duplication) must carry the NFL entries."""
    from app.api.routes import (
        KALSHI_EVENT_SERIES as ROUTES_KALSHI_EVENT_SERIES,
        KALSHI_PROP_CATEGORY_SLUGS as ROUTES_KALSHI_PROP_CATEGORY_SLUGS,
        KALSHI_SPORT_CATEGORY_ROOTS as ROUTES_KALSHI_SPORT_CATEGORY_ROOTS,
    )
    from app.services.trade_desk import (
        KALSHI_EVENT_SERIES as TD_KALSHI_EVENT_SERIES,
        KALSHI_PROP_CATEGORY_SLUGS as TD_KALSHI_PROP_CATEGORY_SLUGS,
        KALSHI_SPORT_CATEGORY_ROOTS as TD_KALSHI_SPORT_CATEGORY_ROOTS,
    )

    for category_root in (ROUTES_KALSHI_SPORT_CATEGORY_ROOTS, TD_KALSHI_SPORT_CATEGORY_ROOTS):
        nfl_root = category_root.get("NFL")
        assert nfl_root and "football/pro-football" in nfl_root

    for event_series in (ROUTES_KALSHI_EVENT_SERIES, TD_KALSHI_EVENT_SERIES):
        nfl_series = event_series.get("NFL")
        assert nfl_series, "NFL missing from KALSHI_EVENT_SERIES"
        series_ticker, series_slug = nfl_series
        # Live-verified series ticker (kxnbagame / kxmlbgame → kxnflgame).
        assert series_ticker == "kxnflgame"
        assert "football" in series_slug

    for prop_slugs in (ROUTES_KALSHI_PROP_CATEGORY_SLUGS, TD_KALSHI_PROP_CATEGORY_SLUGS):
        nfl_slugs = prop_slugs.get("NFL")
        assert nfl_slugs is not None, "NFL missing from KALSHI_PROP_CATEGORY_SLUGS"
        for stat_key in ("passing_yards", "passing_touchdowns", "rushing_yards",
                         "receiving_yards", "receptions"):
            assert stat_key in nfl_slugs, f"Missing NFL stat slug for {stat_key}"

from app.services.ingestion import is_supported_market_payload
from app.services.market_support import classify_market_payload, market_metadata


def test_market_filter_rejects_cross_category_combos():
    payload = {
        "ticker": "KXMVECROSSCATEGORY-S2026",
        "title": "yes Toronto,yes Atlanta,yes New York",
    }

    assert is_supported_market_payload(payload) is False


def test_market_filter_accepts_full_game_winner_market():
    payload = {
        "ticker": "KXNBAGAME-26MAR31NYKHOU-NYK",
        "event_ticker": "KXNBAGAME-26MAR31NYKHOU",
        "title": "New York at Houston Winner?",
        "yes_sub_title": "New York",
    }

    assert is_supported_market_payload(payload) is True


def test_market_filter_accepts_mlb_first_five_winner_market():
    payload = {
        "ticker": "KXMLBF5-26MAR312140NYYSEA-SEA",
        "event_ticker": "KXMLBF5-26MAR312140NYYSEA",
        "title": "New York Y vs Seattle first 5 innings winner?",
        "yes_sub_title": "Seattle wins first 5 innings",
    }

    assert is_supported_market_payload(payload) is True


def test_market_filter_accepts_supported_nba_and_mlb_player_props():
    nba_prop = {
        "ticker": "KXNBAPRA-26APR01BOSNYK-NYKJBRUNSON11-33",
        "event_ticker": "KXNBAPRA-26APR01BOSNYK",
        "title": "Jalen Brunson: 33+ points + rebounds + assists?",
        "yes_sub_title": "Jalen Brunson: 33+",
        "rules_primary": "If Jalen Brunson records 33+ total points + rebounds + assists in the Boston vs New York professional basketball game, then the market resolves to Yes.",
        "primary_participant_key": "basketball_player",
    }
    mlb_prop = {
        "ticker": "KXMLBHRR-26MAR302210DETAZ-DETPMEADOWS22-2",
        "event_ticker": "KXMLBHRR-26MAR302210DETAZ",
        "title": "Parker Meadows: 2+ hits + runs + RBIs?",
        "yes_sub_title": "Parker Meadows: 2+",
        "rules_primary": "If Parker Meadows records 2+ total hits + runs + rbis in the Detroit vs Arizona professional baseball game, then the market resolves to Yes.",
        "rules_secondary": "If Parker Meadows is scratched or not included in the starting lineup, the market will resolve to the fair market price. If Parker Meadows starts the game but does not record a plate appearance, the market will resolve to the fair market price.",
        "primary_participant_key": "baseball_player",
    }

    assert is_supported_market_payload(nba_prop) is True
    assert market_metadata(nba_prop)["copilot_stat_key"] == "points_rebounds_assists"
    assert is_supported_market_payload(mlb_prop) is True
    assert market_metadata(mlb_prop)["copilot_stat_key"] == "hits_runs_rbis"


def test_market_filter_rejects_split_winner_and_unsupported_pitcher_props():
    split_market = {
        "ticker": "KXNBA2HWINNER-26MAR31NYKHOU-NYK",
        "event_ticker": "KXNBA2HWINNER-26MAR31NYKHOU",
        "title": "New York vs Houston: Second Half Winner?",
        "yes_sub_title": "New York wins 2nd half",
    }
    pitcher_prop = {
        "ticker": "KXMLBSO-26MAR311835TEXBAL-BALBURNES39-9",
        "event_ticker": "KXMLBSO-26MAR311835TEXBAL",
        "title": "Corbin Burnes: 9+ strikeouts?",
        "yes_sub_title": "Corbin Burnes: 9+",
        "rules_secondary": "If Corbin Burnes does not start the game, the market will resolve to the fair market price. If Corbin Burnes records at least one out, the market will settle based on strikeouts recorded.",
        "primary_participant_key": "baseball_player",
    }

    assert is_supported_market_payload(split_market) is False
    classification = classify_market_payload(pitcher_prop)
    assert classification["supported"] is False
    assert classification["reason"] == "unsupported_prop_category"


def test_market_filter_rejects_unsupported_leagues_even_if_they_are_winner_markets():
    payload = {
        "ticker": "KXBALLERLEAGUEGAME-26APR02MID876-MID",
        "event_ticker": "KXBALLERLEAGUEGAME-26APR02MID876",
        "title": "Midnight Wizards vs 876 United winner?",
        "yes_sub_title": "Midnight Wizards",
    }

    assert is_supported_market_payload(payload) is False

from app.services.ingestion import is_supported_market_payload
from app.services.market_support import classify_market_payload, combo_leg_metadata_prefilter, market_metadata


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


def test_market_filter_accepts_player_prop_without_trailing_question_mark():
    payload = {
        "ticker": "KXNBAPTS-26APR04WASMIA-MIABADEBAYO13-15",
        "event_ticker": "KXNBAPTS-26APR04WASMIA",
        "title": "Bam Adebayo: 15+ points",
        "yes_sub_title": "Bam Adebayo: 15+",
        "rules_primary": "If Bam Adebayo records 15+ Points in the Washington at Miami professional basketball game, then the market resolves to Yes.",
        "primary_participant_key": "basketball_player",
    }

    classification = classify_market_payload(payload)

    assert classification["supported"] is True
    assert classification["metadata"]["copilot_market_family"] == "player_prop"
    assert classification["metadata"]["copilot_stat_key"] == "points"


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


def test_market_filter_accepts_supported_spread_and_total_game_lines():
    spread_payload = {
        "ticker": "KXNBASPREAD-26APR05MIABOS-BOS-4_5",
        "event_ticker": "KXNBAGAME-26APR05MIABOS",
        "title": "Boston wins by over 4.5 points",
        "yes_sub_title": "Boston wins by over 4.5 points",
    }
    total_payload = {
        "ticker": "KXNBATOTAL-26APR05MIABOS-220_5",
        "event_ticker": "KXNBAGAME-26APR05MIABOS",
        "title": "Over 220.5 points scored",
        "yes_sub_title": "Over 220.5 points scored",
    }

    spread = classify_market_payload(spread_payload)
    total = classify_market_payload(total_payload)

    assert spread["supported"] is True
    assert spread["metadata"]["copilot_market_family"] == "game_line"
    assert spread["metadata"]["copilot_market_kind"] == "spread"
    assert spread["metadata"]["copilot_subject_name"] == "Boston"
    assert spread["metadata"]["copilot_threshold"] == 4.5

    assert total["supported"] is True
    assert total["metadata"]["copilot_market_family"] == "game_line"
    assert total["metadata"]["copilot_market_kind"] == "total"
    assert total["metadata"]["copilot_direction"] == "over"
    assert total["metadata"]["copilot_threshold"] == 220.5


def test_combo_leg_prefilter_accepts_supported_nba_prop_ticker_metadata():
    classification = combo_leg_metadata_prefilter(
        {
            "event_ticker": "KXNBAPTS-26APR05NYKBOS",
            "market_ticker": "KXNBAPTS-26APR05NYKBOS-NYKJBRUNSON11-30",
        }
    )

    assert classification["supported"] is True
    assert classification["sport_key"] == "NBA"


def test_combo_leg_prefilter_rejects_non_target_combo_leg_ticker_metadata():
    classification = combo_leg_metadata_prefilter(
        {
            "market_ticker": "KXEPLGAME-26APR11ARSBOU-ARS",
        }
    )

    assert classification["supported"] is False
    assert classification["reason"] == "unsupported_sport"

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


# -- Smarter WNBA PR 2 ------------------------------------------------------


def test_market_filter_accepts_wnba_player_prop():
    """WNBA props share NBA's title regex + alias dict; classification
    should produce the same shape as an NBA prop with sport_key=WNBA.
    """
    payload = {
        "ticker": "KXWNBAPTS-26MAY15INDNYL-INDCCLARK22-22",
        "event_ticker": "KXWNBAPTS-26MAY15INDNYL",
        "title": "Caitlin Clark: 22+ points?",
        "yes_sub_title": "Caitlin Clark: 22+",
        "rules_primary": "If Caitlin Clark records 22+ Points in the Indiana at New York professional basketball game, then the market resolves to Yes.",
        "primary_participant_key": "basketball_player",
    }

    classification = classify_market_payload(payload)

    assert classification["supported"] is True
    assert classification["sport_key"] == "WNBA"
    metadata = classification["metadata"]
    assert metadata["copilot_market_family"] == "player_prop"
    assert metadata["copilot_stat_key"] == "points"
    assert metadata["copilot_threshold"] == 22.0
    assert metadata["copilot_subject_name"] == "Caitlin Clark"


def test_market_filter_accepts_wnba_combo_prop():
    """Combo props sum components via PROP_COMPONENT_ORDER; WNBA's
    component order mirrors NBA's so points + rebounds + assists
    must resolve to the canonical ``points_rebounds_assists`` stat key.
    """
    payload = {
        "ticker": "KXWNBAPRA-26MAY15INDNYL-NYLBSTEWART11-35",
        "event_ticker": "KXWNBAPRA-26MAY15INDNYL",
        "title": "Breanna Stewart: 35+ points + rebounds + assists?",
        "yes_sub_title": "Breanna Stewart: 35+",
        "rules_primary": "If Breanna Stewart records 35+ total points + rebounds + assists in the Indiana vs New York professional basketball game, then the market resolves to Yes.",
        "primary_participant_key": "basketball_player",
    }

    classification = classify_market_payload(payload)

    assert classification["supported"] is True
    assert classification["sport_key"] == "WNBA"
    assert classification["metadata"]["copilot_stat_key"] == "points_rebounds_assists"


def test_market_filter_accepts_wnba_made_threes_prop():
    """Pin the alias resolution path — "made threes" / "3 pointers"
    phrasing must resolve to the canonical ``made_threes`` stat key.
    """
    payload = {
        "ticker": "KXWNBA3PM-26MAY16LVPHX-PHXBGRINER13-3",
        "event_ticker": "KXWNBA3PM-26MAY16LVPHX",
        "title": "Diana Taurasi: 3+ made threes?",
        "yes_sub_title": "Diana Taurasi: 3+",
        "rules_primary": "If Diana Taurasi records 3+ Three Pointers in the Las Vegas at Phoenix professional basketball game, then the market resolves to Yes.",
        "primary_participant_key": "basketball_player",
    }

    classification = classify_market_payload(payload)

    assert classification["supported"] is True
    assert classification["metadata"]["copilot_stat_key"] == "made_threes"


def test_market_filter_accepts_wnba_game_winner():
    """Game-winner markets should classify with copilot_market_kind
    == 'game_winner' for WNBA the same way they do for NBA / MLB.
    """
    payload = {
        "ticker": "KXWNBAGAME-26MAY15INDNYL-NYL",
        "event_ticker": "KXWNBAGAME-26MAY15INDNYL",
        "title": "Indiana Fever at New York Liberty winner?",
        "yes_sub_title": "New York Liberty",
    }

    classification = classify_market_payload(payload)

    assert classification["supported"] is True
    assert classification["sport_key"] == "WNBA"
    assert classification["metadata"]["copilot_market_family"] == "winner"
    assert classification["metadata"]["copilot_market_kind"] == "game_winner"


def test_market_filter_accepts_wnba_spread_and_total_game_lines():
    """Spread + total game-line title regex must accept WNBA markets.
    Pre-PR 2 the dispatcher gated game_line metadata to
    {NBA, NFL, MLB, SOCCER}; WNBA was added with this PR.
    """
    spread_payload = {
        "ticker": "KXWNBASPREAD-26MAY15INDNYL-NYL-5_5",
        "event_ticker": "KXWNBAGAME-26MAY15INDNYL",
        "title": "New York Liberty wins by over 5.5 points",
        "yes_sub_title": "New York Liberty wins by over 5.5 points",
    }
    total_payload = {
        "ticker": "KXWNBATOTAL-26MAY15INDNYL-160_5",
        "event_ticker": "KXWNBAGAME-26MAY15INDNYL",
        "title": "Over 160.5 points scored",
        "yes_sub_title": "Over 160.5 points scored",
    }

    spread = classify_market_payload(spread_payload)
    total = classify_market_payload(total_payload)

    assert spread["supported"] is True
    assert spread["metadata"]["copilot_market_kind"] == "spread"
    assert spread["metadata"]["copilot_threshold"] == 5.5

    assert total["supported"] is True
    assert total["metadata"]["copilot_market_kind"] == "total"
    assert total["metadata"]["copilot_threshold"] == 160.5


def test_combo_leg_prefilter_accepts_supported_wnba_prop_ticker():
    """KXWNBA-prefixed combo legs were unsupported pre-PR 2
    (combo_leg_metadata_prefilter gated to {NBA, MLB}). Now WNBA is in
    the allowlist and the family code dispatcher recognizes the
    ``KXWNBA`` prefix.
    """
    classification = combo_leg_metadata_prefilter(
        {
            "event_ticker": "KXWNBAPTS-26MAY16LVPHX",
            "market_ticker": "KXWNBAPTS-26MAY16LVPHX-LVAJWILSON09-26",
        }
    )

    assert classification["supported"] is True
    assert classification["sport_key"] == "WNBA"
    assert classification["market_family_code"] == "PTS"


def test_combo_leg_prefilter_blocks_wnba_winner_family():
    """Same BLOCKED prefixes as NBA (GAME / WINNER / SPREAD / TOTAL /
    1H..4Q) — a KXWNBAGAME or KXWNBAWINNER leg must not get treated
    as a prop combo leg.
    """
    classification = combo_leg_metadata_prefilter(
        {
            "market_ticker": "KXWNBAGAME-26MAY16LVPHX-PHX",
        }
    )

    assert classification["supported"] is False
    assert classification["reason"] == "unsupported_market_family"


def test_persist_market_payload_records_skips_wnba_when_not_in_enabled_sports(
    db_session, monkeypatch
):
    """The ``enabled_sports`` gate at ``_persist_market_payload_records``
    is defense-in-depth: even when the classifier correctly recognizes
    a WNBA market, the operator-controlled feature flag must still gate
    persistence. PR 6 flipped the default to include WNBA, so this
    regression test forces ``enabled_sports`` back to the pre-PR-6
    sport set inside the test scope to keep the gate semantics
    exercised. If a future change removes the gate, this test will
    catch it.
    """
    from app.config import get_settings
    from app.models import Market
    from app.services.ingestion import _persist_market_payload_records

    settings = get_settings()
    monkeypatch.setattr(settings, "enabled_sports", ["NBA", "NFL", "MLB", "SOCCER", "TENNIS"])

    wnba_prop = {
        "ticker": "KXWNBAPTS-26MAY15INDNYL-INDCCLARK22-22",
        "event_ticker": "KXWNBAPTS-26MAY15INDNYL",
        "title": "Caitlin Clark: 22+ points?",
        "yes_sub_title": "Caitlin Clark: 22+",
        "rules_primary": "If Caitlin Clark records 22+ Points in the Indiana at New York professional basketball game, then the market resolves to Yes.",
        "primary_participant_key": "basketball_player",
        "status": "active",
    }

    summary = _persist_market_payload_records(
        db_session,
        [
            {
                "payload": wnba_prop,
                "source_type": "standalone",
                "source_payload": None,
                "classification_override": None,
            }
        ],
    )
    db_session.commit()

    # No Market row should have been created for the WNBA ticker.
    market = db_session.query(Market).filter(Market.ticker == wnba_prop["ticker"]).one_or_none()
    assert market is None, "WNBA market persisted while WNBA is not in enabled_sports"
    # The summary's NBA / MLB counters should be unchanged.
    assert summary.get("supported_nba_props", 0) == 0
    assert summary.get("supported_mlb_props", 0) == 0

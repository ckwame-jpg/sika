"""WNBA PR 1 scaffolding pin — apps/ml side.

The handoff doc requires WNBA to surface as a first-class family in
``ml.dataset`` so the training pipeline picks up settled WNBA rows the
same way it does NBA / MLB. These tests pin the minimum shape:

- ``_family_key`` derives ``wnba_props`` / ``wnba_singles`` correctly.
- ``_enrich_prediction_features`` adds the ``sport_is_wnba`` one-hot
  the trainer reads.
- The ``cli._family_key_for_row`` mirror agrees with ``dataset._family_key``
  (already enforced for NBA / MLB; PR 1 extends the contract to WNBA).

End-to-end training behavior lands in later PRs once WNBA settled rows
exist. The drift-guard test in ``test_interval_dataset.py``
(``test_team_abbreviation_map_matches_apps_api_canonical_source``)
implicitly pins that the WNBA team map was added to both apps/api and
apps/ml — it parses the canonical apps/api source via AST and asserts
the apps/ml copy agrees.
"""

from __future__ import annotations

from ml.cli import _family_key_for_row
from ml.dataset import _family_key, _enrich_prediction_features


def test_family_key_wnba_player_prop() -> None:
    assert _family_key("WNBA", "player_prop") == "wnba_props"


def test_family_key_wnba_non_prop_falls_back_to_singles() -> None:
    assert _family_key("WNBA", "winner") == "wnba_singles"
    assert _family_key("WNBA", None) == "wnba_singles"


def test_family_key_for_row_mirror_agrees_with_dataset_for_wnba() -> None:
    """Drift guard — ``cli._family_key_for_row`` exists because
    apps/ml's recalibrate path runs a query that doesn't import
    ``dataset._family_key`` directly. The two must agree for every
    (sport, family) combination training touches; pin WNBA explicitly
    so a future divergence is caught.
    """
    pairs = [
        ("WNBA", "player_prop"),
        ("WNBA", "winner"),
        ("WNBA", None),
        ("WNBA", ""),
    ]
    for sport_key, market_family in pairs:
        assert _family_key(sport_key, market_family) == _family_key_for_row(
            sport_key, market_family,
        ), f"divergence at ({sport_key!r}, {market_family!r})"


def test_enrich_prediction_features_emits_sport_is_wnba_one_hot() -> None:
    """The trainer feature vector includes a per-sport one-hot. WNBA
    rows must set ``sport_is_wnba=1.0`` (and NBA / MLB to 0.0); NBA
    rows must leave ``sport_is_wnba`` at 0.0 so the trainer's
    coefficients don't cross-contaminate across sports.
    """
    wnba_row = {
        "sport_key": "WNBA",
        "market_family": "player_prop",
        "suggested_price": 0.5, "fair_yes_price": 0.5,
        "edge": 0.02, "confidence": 0.65, "selection_score": 0.1,
        "threshold": 20.0,
    }
    wnba_features = _enrich_prediction_features(wnba_row, {})
    assert wnba_features["family_key"] == "wnba_props"
    assert wnba_features["sport_is_wnba"] == 1.0
    assert wnba_features["sport_is_nba"] == 0.0
    assert wnba_features["sport_is_mlb"] == 0.0

    nba_row = dict(wnba_row, sport_key="NBA")
    nba_features = _enrich_prediction_features(nba_row, {})
    assert nba_features["family_key"] == "nba_props"
    assert nba_features["sport_is_nba"] == 1.0
    assert nba_features["sport_is_wnba"] == 0.0

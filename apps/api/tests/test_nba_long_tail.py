from typing import Any

import pytest

from app.config import get_settings
from app.models import (
    NbaClutchPlayerCache,
    NbaHustlePlayerCache,
    NbaPlayerDefenseCache,
    NbaTrackingCache,
)
from app.services import nba_long_tail


class _StubNbaStatsClient:
    def __init__(
        self,
        *,
        hustle: dict[str, Any] | None = None,
        tracking: dict[str, Any] | None = None,
        clutch: dict[str, Any] | None = None,
        defense: dict[str, Any] | None = None,
        raise_with: Exception | None = None,
    ) -> None:
        self.hustle = hustle
        self.tracking = tracking
        self.clutch = clutch
        self.defense = defense
        self.raise_with = raise_with
        self.calls: list[str] = []

    def fetch_hustle_stats_player(self, season, season_type="Regular Season"):
        self.calls.append(f"hustle:{season}")
        if self.raise_with is not None:
            raise self.raise_with
        return self.hustle or {"resultSets": []}

    def fetch_player_tracking(self, season, pt_measure_type, season_type="Regular Season"):
        self.calls.append(f"tracking:{pt_measure_type}:{season}")
        if self.raise_with is not None:
            raise self.raise_with
        return self.tracking or {"resultSets": []}

    def fetch_player_clutch(self, season, **kwargs):
        self.calls.append(f"clutch:{season}")
        if self.raise_with is not None:
            raise self.raise_with
        return self.clutch or {"resultSets": []}

    def fetch_player_defense_dashboard(self, season, defense_category="Overall", **kwargs):
        self.calls.append(f"defense:{defense_category}:{season}")
        if self.raise_with is not None:
            raise self.raise_with
        return self.defense or {"resultSets": []}


def _result_set(headers: list[str], rows: list[list[Any]], name: str = "Stats") -> dict[str, Any]:
    return {"resultSets": [{"name": name, "headers": headers, "rowSet": rows}]}


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_load_nba_hustle_player_persists_keyed_by_player_id(db_session):
    payload = _result_set(
        ["PLAYER_ID", "CONTESTED_SHOTS", "DEFLECTIONS", "SCREEN_ASSISTS", "BOX_OUTS",
         "CHARGES_DRAWN", "LOOSE_BALLS_RECOVERED", "OFF_LOOSE_BALLS_RECOVERED",
         "DEF_LOOSE_BALLS_RECOVERED", "OFF_BOX_OUTS", "DEF_BOX_OUTS",
         "CONTESTED_SHOTS_2PT", "CONTESTED_SHOTS_3PT", "SCREEN_AST_PTS"],
        [[2544, 8.5, 1.2, 1.0, 5.0, 0.2, 0.8, 0.4, 0.4, 1.5, 3.5, 6.0, 2.5, 2.5]],
    )
    stub = _StubNbaStatsClient(hustle=payload)
    result = nba_long_tail.load_nba_hustle_player(
        db_session, season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(NbaHustlePlayerCache).one()
    assert cached.payload["players"]["2544"]["contested_shots"] == 8.5
    assert cached.payload["players"]["2544"]["screen_assists"] == 1.0


def test_load_nba_tracking_drives_persists_payload(db_session):
    payload = _result_set(
        ["PLAYER_ID", "DRIVES", "DRIVE_FGA", "DRIVE_PTS", "DRIVE_PASSES_PCT",
         "DRIVE_AST_PCT", "DRIVE_TOV_PCT", "DRIVE_FG_PCT"],
        [[2544, 14.5, 7.0, 9.0, 0.45, 0.20, 0.10, 0.51]],
    )
    stub = _StubNbaStatsClient(tracking=payload)
    result = nba_long_tail.load_nba_tracking(
        db_session, season=2024, pt_measure_type="Drives", client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(NbaTrackingCache).one()
    assert cached.payload["pt_measure_type"] == "Drives"
    assert "2544" in cached.payload["players"]


def test_load_nba_clutch_player_persists_payload(db_session):
    payload = _result_set(
        ["PLAYER_ID", "MIN", "PTS", "FG_PCT", "FG3_PCT", "FT_PCT", "PLUS_MINUS",
         "AST", "TOV", "STL", "BLK"],
        [[2544, 2.5, 4.0, 0.55, 0.42, 0.85, 6.0, 0.9, 0.3, 0.4, 0.2]],
    )
    stub = _StubNbaStatsClient(clutch=payload)
    result = nba_long_tail.load_nba_clutch_player(
        db_session, season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(NbaClutchPlayerCache).one()
    assert cached.payload["players"]["2544"]["fg_pct"] == 0.55


def test_load_nba_player_defense_keys_by_close_def_person(db_session):
    payload = _result_set(
        ["CLOSE_DEF_PERSON_ID", "D_FGA", "D_FGM", "D_FG_PCT", "NORMAL_FG_PCT", "PCT_PLUSMINUS"],
        [[202695, 8.0, 3.4, 0.425, 0.470, -0.045]],
    )
    stub = _StubNbaStatsClient(defense=payload)
    result = nba_long_tail.load_nba_player_defense(
        db_session, season=2024, defense_category="Overall", client=stub, allow_network=True
    )
    assert result.cache_status == "miss"
    cached = db_session.query(NbaPlayerDefenseCache).one()
    assert cached.payload["players"]["202695"]["defended_fg_pct"] == 0.425
    assert cached.payload["players"]["202695"]["fg_pct_diff"] == pytest.approx(-0.045)


def test_emit_hustle_features_returns_keys_for_known_player():
    hustle_payload = {
        "players": {
            "2544": {
                "contested_shots": 8.5, "deflections": 1.2, "screen_assists": 1.0,
                "box_outs": 5.0, "charges_drawn": 0.2, "loose_balls_recovered": 0.8,
                "contested_shots_2pt": 6.0, "contested_shots_3pt": 2.5,
            }
        }
    }
    out = nba_long_tail.emit_nba_hustle_features(hustle_payload, "2544")
    assert out["hustle_contested_shots"] == 8.5
    assert out["hustle_screen_assists"] == 1.0
    assert out["hustle_data_complete"] == 1.0


def test_emit_hustle_features_empty_for_unknown_player():
    payload = {"players": {"2544": {"contested_shots": 8.5}}}
    assert nba_long_tail.emit_nba_hustle_features(payload, "999") == {}
    assert nba_long_tail.emit_nba_hustle_features(payload, None) == {}
    assert nba_long_tail.emit_nba_hustle_features(None, "2544") == {}


def test_emit_drives_features_extracts_tracking_columns():
    drives_payload = {
        "players": {
            "2544": {
                "DRIVES": 14.5, "DRIVE_FGA": 7.0, "DRIVE_FG_PCT": 0.51,
                "DRIVE_PTS": 9.0, "DRIVE_PASSES_PCT": 0.45,
                "DRIVE_AST_PCT": 0.20, "DRIVE_TOV_PCT": 0.10, "DRIVE_FT_PCT": 0.30,
            }
        }
    }
    out = nba_long_tail.emit_nba_drives_features(drives_payload, "2544")
    assert out["drives_per_game"] == 14.5
    assert out["drives_pass_pct"] == 0.45
    assert out["drives_data_complete"] == 1.0


def test_emit_clutch_features_returns_clutch_subset():
    payload = {
        "players": {
            "2544": {"min": 2.5, "pts": 4.0, "fg_pct": 0.55, "fg3_pct": 0.42, "ft_pct": 0.85,
                     "plus_minus": 6.0, "ast": 0.9, "tov": 0.3, "stl": 0.4, "blk": 0.2}
        }
    }
    out = nba_long_tail.emit_nba_clutch_features(payload, "2544")
    assert out["clutch_pts"] == 4.0
    assert out["clutch_plus_minus"] == 6.0
    assert out["clutch_data_complete"] == 1.0


def test_emit_player_defense_features_targets_defender_id():
    payload = {
        "players": {
            "202695": {
                "defended_fga": 8.0, "defended_fg_pct": 0.425,
                "normal_fg_pct": 0.470, "fg_pct_diff": -0.045,
            }
        }
    }
    out = nba_long_tail.emit_nba_player_defense_features(payload, "202695")
    assert out["opponent_defender_defended_fg_pct"] == 0.425
    assert out["opponent_defender_data_complete"] == 1.0
    assert nba_long_tail.emit_nba_player_defense_features(payload, None) == {}


def test_load_nba_hustle_skips_network_when_not_allowed(db_session):
    stub = _StubNbaStatsClient()
    result = nba_long_tail.load_nba_hustle_player(
        db_session, season=2024, client=stub, allow_network=False
    )
    assert result.cache_status == "miss"
    assert stub.calls == []


def test_load_nba_long_tail_falls_back_to_stale_on_failure(db_session):
    from datetime import timedelta
    from app.models import utcnow

    payload = {"players": {"x": {"contested_shots": 5.0}}}
    db_session.add(
        NbaHustlePlayerCache(
            season=2024,
            payload=payload,
            cached_at=utcnow() - timedelta(hours=10),
            expires_at=utcnow() - timedelta(hours=1),
        )
    )
    db_session.flush()
    stub = _StubNbaStatsClient(raise_with=RuntimeError("boom"))
    result = nba_long_tail.load_nba_hustle_player(
        db_session, season=2024, client=stub, allow_network=True
    )
    assert result.cache_status == "stale"
    assert result.payload == payload

"""Smarter NFL PR 5 — game-line pricing + scoring model regression.

Two layers:
1. Pure math (``ml_features.nfl_pricing``): key-number behavior of the
   conditional margin grid, win-prob sanity vs standard moneyline
   conversions, blend weights, symmetry.
2. Model integration (``scoring.nfl_game_model``): ratings-driven
   winner, QB-out margin shift, spread subject orientation, weather on
   totals, consensus blending, feature groups.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ml_features.nfl_pricing import (
    blend_probability,
    nfl_margin_yes_probability,
    nfl_total_yes_probability,
    nfl_win_probability,
    normal_tail_yes_probability,
)

from app.models import (
    Event,
    EventParticipant,
    Market,
    NflDepthChartCache,
    NflOfficialInjuryCache,
    NflScheduleCache,
    NflTeamRatingCache,
    NflWeatherCache,
    Participant,
)
from app.services.scoring.nfl_game_model import (
    score_nfl_game_line,
    score_nfl_team_winner,
)


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
KICKOFF = NOW + timedelta(hours=28)


# -- Pure pricing math ---------------------------------------------------------

def test_margin_grid_prices_key_number_three() -> None:
    """The whole reason for the empirical grid: at a 3-point projection,
    the 2.5 → 3.5 threshold gap must reflect the ~10% of games landing
    exactly on 3 — far beyond what any Normal(σ≈13) can produce (~3%)."""
    gap_at_three = nfl_margin_yes_probability(3.0, 2.5) - nfl_margin_yes_probability(3.0, 3.5)
    assert gap_at_three > 0.06, f"key-number gap too small: {gap_at_three:.4f}"
    # Within the same projection, the 3 rung carries far more mass than
    # a non-key rung equally far from the mean (10 is a weaker key).
    gap_at_ten_rung = nfl_margin_yes_probability(3.0, 9.5) - nfl_margin_yes_probability(3.0, 10.5)
    assert gap_at_three > gap_at_ten_rung * 1.3


def test_win_probability_matches_moneyline_conversions() -> None:
    assert abs(nfl_win_probability(0.0) - 0.5) < 0.01  # symmetrized grid
    p3 = nfl_win_probability(3.0)
    assert 0.56 < p3 < 0.63  # -3 favorites ≈ 59%
    p7 = nfl_win_probability(7.0)
    assert 0.68 < p7 < 0.75  # -7 favorites ≈ 71%
    assert nfl_win_probability(-3.0) < 0.45
    # Monotonic in mu.
    assert p7 > p3 > nfl_win_probability(1.0)


def test_margin_grid_home_away_mirror() -> None:
    """P(away wins by > t | mu) == P(home wins by > t | -mu) — the
    symmetrized grid identity the spread scorer relies on."""
    lhs = nfl_margin_yes_probability(-4.0, 3.5)
    rhs = 1.0 - nfl_margin_yes_probability(4.0, -3.5)
    assert abs(lhs - rhs) < 0.005


def test_total_pricing_directions() -> None:
    over = nfl_total_yes_probability(48.0, 44.5, direction="over")
    under = nfl_total_yes_probability(48.0, 44.5, direction="under")
    assert abs(over + under - 1.0) < 1e-9
    assert over > 0.5


def test_blend_probability_weights() -> None:
    strong, w_strong = blend_probability(0.50, 0.60, 5)
    assert w_strong == 0.70 and abs(strong - 0.57) < 1e-9
    thin, w_thin = blend_probability(0.50, 0.60, 1)
    assert w_thin == 0.50 and abs(thin - 0.55) < 1e-9
    none_case, w_none = blend_probability(0.50, None, 0)
    assert w_none == 0.0 and none_case == 0.50


def test_normal_tail_yardage_sanity() -> None:
    """The Poisson-pathology fix: a 260-yd passer at a 250.5 threshold
    should be near a coin flip with sd≈70, not the ~0.73 Poisson gives."""
    p = normal_tail_yes_probability(260.0, 70.0, 250.5)
    assert 0.50 < p < 0.60


# -- Model integration ---------------------------------------------------------

def _seed_event(db) -> tuple[Event, EventParticipant, EventParticipant]:
    event = Event(
        external_id="espn:nfl:401555",
        sport_key="NFL",
        name="Dallas Cowboys at Philadelphia Eagles",
        status="scheduled",
        starts_at=KICKOFF,
    )
    db.add(event)
    db.flush()
    entries = []
    for name, is_home in (("Philadelphia Eagles", True), ("Dallas Cowboys", False)):
        participant = Participant(
            external_id=f"espn:nfl:t:{name}", sport_key="NFL", display_name=name,
        )
        db.add(participant)
        db.flush()
        entry = EventParticipant(
            event_id=event.id, participant_id=participant.id,
            role="competitor", is_home=is_home,
        )
        db.add(entry)
        entries.append(entry)
    db.flush()
    db.refresh(event)
    home, away = entries
    return event, home, away


def _seed_ratings(db, *, home_net=0.10, away_net=-0.05) -> None:
    db.add(NflTeamRatingCache(
        season=2026,
        payload={"season": 2026, "through_week": 6, "teams": {
            "PHI": {"games": 6, "off_epa_per_play": home_net, "def_epa_per_play_allowed": 0.0,
                    "net_epa_per_play": home_net, "plays_per_game": 62.0,
                    "points_for_per_game": 27.0, "points_against_per_game": 20.0},
            "DAL": {"games": 6, "off_epa_per_play": away_net, "def_epa_per_play_allowed": 0.0,
                    "net_epa_per_play": away_net, "plays_per_game": 62.0,
                    "points_for_per_game": 21.0, "points_against_per_game": 24.0},
        }},
        cached_at=NOW, expires_at=NOW + timedelta(days=1),
    ))
    db.flush()


def test_winner_pure_internal_favors_stronger_home_team(db_session) -> None:
    event, home, away = _seed_event(db_session)
    _seed_ratings(db_session)
    prob_left, confidence, reasons, features, groups = score_nfl_team_winner(
        db_session, event, home, away,
    )
    # PHI: (0.10 - (-0.05)) * 62 ≈ 9.3 pts + 1.7 HFA → strong favorite.
    assert prob_left > 0.65
    assert features["nfl_consensus_data_complete"] == 0.0
    assert features["nfl_ratings_data_complete"] == 1.0
    assert "nfl_team_ratings" in groups and "nfl_consensus" in groups
    assert 0.2 <= confidence <= 0.92
    assert any("EPA power ratings" in reason for reason in reasons)


def test_winner_orientation_flips_for_away_left(db_session) -> None:
    event, home, away = _seed_event(db_session)
    _seed_ratings(db_session)
    prob_home_first, *_ = score_nfl_team_winner(db_session, event, home, away)
    prob_away_first, *_ = score_nfl_team_winner(db_session, event, away, home)
    assert abs(prob_home_first + prob_away_first - 1.0) < 0.01


def test_qb_out_drags_margin(db_session) -> None:
    event, home, away = _seed_event(db_session)
    _seed_ratings(db_session)
    baseline_prob, *_ = score_nfl_team_winner(db_session, event, home, away)
    db_session.add(NflDepthChartCache(
        season=2026, team="PHI",
        payload={"rows": [{"team": "PHI", "player_name": "Jalen Hurts",
                           "gsis_id": "00-0036389", "pos_abb": "QB", "pos_rank": "1"}]},
        cached_at=NOW, expires_at=NOW + timedelta(days=1),
    ))
    db_session.add(NflOfficialInjuryCache(
        season=2026, week=6,
        payload={"rows": [{"gsis_id": "00-0036389", "full_name": "Jalen Hurts",
                           "team": "PHI", "position": "QB", "report_status": "Out"}]},
        cached_at=NOW, expires_at=NOW + timedelta(days=1),
    ))
    db_session.flush()
    prob_with_qb_out, _, reasons, features, _ = score_nfl_team_winner(
        db_session, event, home, away,
    )
    assert features["nfl_home_qb1_out"] == 1.0
    assert prob_with_qb_out < baseline_prob - 0.08  # ≈4.5 pts ≈ 10+ prob pts
    assert any("Starting QB" in reason for reason in reasons)


def _spread_market(threshold: float, subject: str) -> Market:
    return Market(
        ticker=f"KXNFLSPREAD-26SEP13DALPHI-{subject[:3].upper()}{threshold:g}",
        title=f"{subject} wins by over {threshold:g} points?",
        sport_key="NFL",
        raw_data={
            "copilot_market_family": "game_line",
            "copilot_market_kind": "spread",
            "copilot_stat_key": "margin_points",
            "copilot_threshold": threshold,
            "copilot_direction": "over",
            "copilot_subject_name": subject,
        },
    )


def test_spread_scoring_home_and_away_subjects(db_session) -> None:
    event, home, away = _seed_event(db_session)
    _seed_ratings(db_session)
    home_market = _spread_market(3.5, "Philadelphia Eagles")
    db_session.add(home_market)
    away_market = _spread_market(3.5, "Dallas Cowboys")
    db_session.add(away_market)
    db_session.flush()

    home_score = score_nfl_game_line(db_session, event, home_market, home, away)
    away_score = score_nfl_game_line(db_session, event, away_market, home, away)
    assert home_score is not None and away_score is not None
    p_home_cover, *_ = home_score
    p_away_cover, *_ = away_score
    # PHI projected ~+11 → covering -3.5 is likely; DAL +3.5 the mirror.
    assert p_home_cover > 0.6
    assert p_away_cover < 0.4
    assert home_score[3]["nfl_spread_signed_mu"] > 0
    assert away_score[3]["nfl_spread_signed_mu"] < 0


def test_total_scoring_applies_weather_to_internal(db_session) -> None:
    event, home, away = _seed_event(db_session)
    _seed_ratings(db_session)
    market = Market(
        ticker="KXNFLTOTAL-26SEP13DALPHI-46",
        title="Over 46.5 points scored?",
        sport_key="NFL",
        raw_data={
            "copilot_market_family": "game_line",
            "copilot_market_kind": "total",
            "copilot_stat_key": "total_points",
            "copilot_threshold": 46.5,
            "copilot_direction": "over",
        },
    )
    db_session.add(market)
    db_session.flush()
    calm = score_nfl_game_line(db_session, event, market, home, away)
    assert calm is not None
    # Gale-force wind at an outdoor stadium (PHI = Lincoln Financial).
    db_session.add(NflWeatherCache(
        event_id=str(event.id),
        payload={"wind_speed_mph": 30.0, "precip_pct": 80.0, "is_dome": False},
        cached_at=NOW, expires_at=NOW + timedelta(hours=2),
    ))
    db_session.flush()
    windy = score_nfl_game_line(db_session, event, market, home, away)
    assert windy is not None
    assert windy[3]["nfl_weather_total_factor"] < 1.0
    assert windy[0] < calm[0]  # over gets cheaper in a gale


def test_rest_edge_shifts_margin(db_session) -> None:
    event, home, away = _seed_event(db_session)
    _seed_ratings(db_session, home_net=0.0, away_net=0.0)
    db_session.add(NflScheduleCache(
        season=2026,
        payload={"games": [{
            "home_team": "PHI", "away_team": "DAL",
            "gameday": KICKOFF.date().isoformat(),
            "home_rest": "14", "away_rest": "6",
        }]},
        cached_at=NOW, expires_at=NOW + timedelta(days=7),
    ))
    db_session.flush()
    prob_left, _, _, features, _ = score_nfl_team_winner(db_session, event, home, away)
    assert features["nfl_rest_adjustment"] == 1.0  # off a bye
    assert features["nfl_home_rest_days"] == 14.0


def test_feature_group_policies_registered() -> None:
    from app.services.scoring.feature_groups import (
        FEATURE_GROUP_POLICIES,
        FeatureGroupSeverity,
    )

    for key in ("nfl_consensus", "nfl_weather", "nfl_team_ratings"):
        policy = FEATURE_GROUP_POLICIES.get(key)
        assert policy is not None, f"missing policy for {key}"
        assert policy.severity is FeatureGroupSeverity.PENALIZE
        assert policy.penalty_confidence_delta < 0

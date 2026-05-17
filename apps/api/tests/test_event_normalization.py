from app.sports.head_to_head import HeadToHeadSportAdapter
from app.sports.team import TeamSportAdapter


def test_team_adapter_preserves_in_progress_status_without_finalizing_event():
    adapter = TeamSportAdapter("NBA", "Basketball")

    event = adapter.normalize_event(
        {
            "source": "espn_public",
            "idEvent": "nba-live-1",
            "idLeague": "46",
            "strLeague": "NBA",
            "strHomeTeam": "Charlotte Hornets",
            "strAwayTeam": "Phoenix Suns",
            "strHomeTeamShort": "Hornets",
            "strAwayTeamShort": "Suns",
            "idHomeTeam": "30",
            "idAwayTeam": "21",
            "strEvent": "Phoenix Suns at Charlotte Hornets",
            "strTimestamp": "2026-04-02T23:00:00Z",
            "dateEvent": "2026-04-02",
            "strStatus": "in_progress",
            "intHomeScore": "94",
            "intAwayScore": "101",
        }
    )

    assert event is not None
    assert event.status == "in_progress"
    assert event.completed_at is None


def test_head_to_head_adapter_uses_upstream_status_instead_of_score_inference():
    adapter = HeadToHeadSportAdapter("TENNIS", "Tennis")

    event = adapter.normalize_event(
        {
            "source": "sportsdb",
            "idEvent": "tennis-1",
            "idLeague": "atp",
            "strLeague": "ATP Tour",
            "strEvent": "Novak Djokovic vs Carlos Alcaraz",
            "strTimestamp": "2026-04-01T03:00:00Z",
            "dateEvent": "2026-04-01",
            "strStatus": "scheduled",
            "intHomeScore": "1",
            "intAwayScore": "0",
        }
    )

    assert event is not None
    assert event.status == "scheduled"
    assert event.completed_at is None

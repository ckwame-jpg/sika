from sqlalchemy import select

from app.models import Participant
from app.services.ingestion import _upsert_event
from app.sports.team import TeamSportAdapter


def test_participants_are_scoped_by_source_and_sport(db_session):
    nba_adapter = TeamSportAdapter("NBA", "Basketball")
    mlb_adapter = TeamSportAdapter("MLB", "Baseball")

    nba_event = nba_adapter.normalize_event(
        {
            "source": "espn_public",
            "idEvent": "game-1",
            "idLeague": "46",
            "strLeague": "NBA",
            "strHomeTeam": "Atlanta Hawks",
            "strAwayTeam": "Orlando Magic",
            "idHomeTeam": "1",
            "idAwayTeam": "19",
            "strEvent": "Orlando Magic at Atlanta Hawks",
            "strTimestamp": "2026-03-30T23:00:00Z",
            "dateEvent": "2026-03-30",
        }
    )
    mlb_event = mlb_adapter.normalize_event(
        {
            "source": "sportsdb",
            "idEvent": "game-2",
            "idLeague": "4424",
            "strLeague": "MLB",
            "strHomeTeam": "Baltimore Orioles",
            "strAwayTeam": "Los Angeles Dodgers",
            "idHomeTeam": "1",
            "idAwayTeam": "19",
            "strEvent": "Los Angeles Dodgers at Baltimore Orioles",
            "strTimestamp": "2026-03-30T23:30:00Z",
            "dateEvent": "2026-03-30",
        }
    )

    _upsert_event(db_session, nba_event)
    _upsert_event(db_session, mlb_event)
    db_session.commit()

    participants = db_session.scalars(select(Participant).order_by(Participant.sport_key, Participant.display_name)).all()
    assert len(participants) == 4
    assert {item.sport_key for item in participants} == {"NBA", "MLB"}

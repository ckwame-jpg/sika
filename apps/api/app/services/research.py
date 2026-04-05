"""Daily research cycle: pre-fetches ESPN gamelogs for historical seasons.

Ensures the ML model has deep training data for all ESPN-supported sports
(NBA, NFL, MLB). Runs as a daily scheduled job via the refresh queue.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.clients.espn import ESPN_GAMELOG_URLS, EspnPublicClient
from app.config import get_settings
from app.models import EspnPlayerGamelogCache, EspnPlayerSearchCache, Run
from app.services.stats_query import default_season_for_sport

logger = logging.getLogger(__name__)

_RESEARCH_CACHE_TTL_DAYS = 7
_API_SLEEP_SECONDS = 0.5


def run_research_cycle(db: Session) -> Run:
    """Fetch historical ESPN gamelogs for all configured research sports."""
    settings = get_settings()
    research_sports = settings.research_sports
    seasons_to_collect = settings.historical_seasons_to_collect

    run = Run(kind="research", status="running", details={"sports": research_sports})
    db.add(run)
    db.flush()

    total_fetched = 0
    total_skipped = 0
    sport_summaries: dict[str, dict] = {}

    try:
        espn_client = EspnPublicClient()

        for sport_key in research_sports:
            if sport_key.upper() not in ESPN_GAMELOG_URLS:
                logger.warning("Research: skipping %s — no ESPN gamelog URL configured", sport_key)
                sport_summaries[sport_key] = {"status": "skipped", "reason": "no_espn_gamelog_support"}
                continue

            athlete_ids = _discover_athlete_ids(db, sport_key)
            if not athlete_ids:
                logger.info("Research: no known athletes for %s, skipping", sport_key)
                sport_summaries[sport_key] = {"status": "skipped", "reason": "no_athletes", "athletes": 0}
                continue

            current_season = default_season_for_sport(sport_key)
            seasons = list(range(current_season, current_season - seasons_to_collect, -1))

            sport_fetched = 0
            sport_skipped = 0
            sport_errors = 0

            for athlete_id in athlete_ids:
                for season in seasons:
                    if _has_valid_cache(db, sport_key, athlete_id, season):
                        sport_skipped += 1
                        continue

                    try:
                        payload = espn_client.fetch_player_gamelog(sport_key, athlete_id, season)
                        _upsert_gamelog_cache(db, sport_key, athlete_id, season, payload)
                        sport_fetched += 1
                    except Exception:
                        logger.debug(
                            "Research: failed to fetch %s/%s season %d",
                            sport_key, athlete_id, season,
                            exc_info=True,
                        )
                        sport_errors += 1

                    time.sleep(_API_SLEEP_SECONDS)

            db.commit()
            total_fetched += sport_fetched
            total_skipped += sport_skipped
            sport_summaries[sport_key] = {
                "status": "completed",
                "athletes": len(athlete_ids),
                "seasons": seasons,
                "fetched": sport_fetched,
                "skipped_cached": sport_skipped,
                "errors": sport_errors,
            }
            logger.info(
                "Research: %s — %d athletes, %d fetched, %d cached, %d errors",
                sport_key, len(athlete_ids), sport_fetched, sport_skipped, sport_errors,
            )

        run.status = "completed"
        run.records_processed = total_fetched
        run.finished_at = datetime.now(timezone.utc)
        run.details = {
            "sports": research_sports,
            "total_fetched": total_fetched,
            "total_skipped_cached": total_skipped,
            "sport_summaries": sport_summaries,
        }
        db.flush()
        return run

    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        run.details = {
            "sports": research_sports,
            "total_fetched": total_fetched,
            "sport_summaries": sport_summaries,
        }
        db.flush()
        raise


def _discover_athlete_ids(db: Session, sport_key: str) -> list[str]:
    """Find all known ESPN athlete IDs for a sport from cache tables."""
    ids: set[str] = set()

    # From gamelog cache
    gamelog_ids = db.scalars(
        select(EspnPlayerGamelogCache.athlete_id)
        .where(EspnPlayerGamelogCache.sport_key == sport_key)
        .distinct()
    ).all()
    ids.update(gamelog_ids)

    # From player search cache — extract athlete_id from payload JSON
    search_rows = db.execute(
        select(EspnPlayerSearchCache.payload)
        .where(EspnPlayerSearchCache.sport_key == sport_key)
    ).all()
    for (payload,) in search_rows:
        if isinstance(payload, dict) and payload.get("athlete_id"):
            ids.add(str(payload["athlete_id"]))

    return sorted(ids)


def _has_valid_cache(db: Session, sport_key: str, athlete_id: str, season: int) -> bool:
    """Check if we already have a non-expired cache entry."""
    now = datetime.now(timezone.utc)
    count = db.scalar(
        select(func.count(EspnPlayerGamelogCache.id))
        .where(
            EspnPlayerGamelogCache.sport_key == sport_key,
            EspnPlayerGamelogCache.athlete_id == athlete_id,
            EspnPlayerGamelogCache.season == season,
            EspnPlayerGamelogCache.expires_at > now,
        )
    )
    return (count or 0) > 0


def _upsert_gamelog_cache(
    db: Session,
    sport_key: str,
    athlete_id: str,
    season: int,
    payload: dict,
) -> None:
    """Insert or update a gamelog cache entry with a long TTL."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=_RESEARCH_CACHE_TTL_DAYS)

    existing = db.scalar(
        select(EspnPlayerGamelogCache)
        .where(
            EspnPlayerGamelogCache.sport_key == sport_key,
            EspnPlayerGamelogCache.athlete_id == athlete_id,
            EspnPlayerGamelogCache.season == season,
        )
    )
    if existing:
        existing.payload = payload
        existing.cached_at = now
        existing.expires_at = expires_at
    else:
        db.add(EspnPlayerGamelogCache(
            sport_key=sport_key,
            athlete_id=athlete_id,
            season=season,
            payload=payload,
            cached_at=now,
            expires_at=expires_at,
        ))
    db.flush()

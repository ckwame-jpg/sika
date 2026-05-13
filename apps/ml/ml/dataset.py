from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text


SETTLED_OUTCOMES = {"won", "lost", "push", "cancelled"}


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+"):
        return database_url
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def _family_key(sport_key: str | None, market_family: str | None) -> str:
    sport = (sport_key or "").upper()
    family = (market_family or "").lower()
    if family == "player_prop":
        if sport == "NBA":
            return "nba_props"
        if sport == "MLB":
            return "mlb_props"
    if sport == "NBA":
        return "nba_singles"
    if sport == "MLB":
        return "mlb_singles"
    return f"{sport.lower()}_singles" if sport else "unknown_singles"


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _prepare_frame(rows: pd.DataFrame, *, drop_pushes: bool, dedupe_markets: bool) -> pd.DataFrame:
    if rows.empty:
        return rows
    frame = rows.copy()
    frame["prediction_outcome"] = frame["prediction_outcome"].astype(str).str.lower()
    frame = frame[frame["prediction_outcome"].isin(SETTLED_OUTCOMES)]
    if drop_pushes:
        frame = frame[frame["prediction_outcome"].isin({"won", "lost"})]
    frame = frame[frame["sport_key"].astype(str).str.upper().isin({"NBA", "MLB"})]
    # Target derivation below relies on side being "yes" or "no" — drop anything
    # else (null, empty, unknown values) before computing, so we never silently
    # mislabel a row whose side was missing.
    frame = frame[frame["side"].astype(str).str.lower().isin({"yes", "no"})]
    if "capture_scope" in frame.columns:
        frame = frame[(frame["capture_scope"].isna()) | (frame["capture_scope"] != "coverage")]
    # Bug #20 walk-forward folds use captured_at to assign rows to weekly
    # buckets. A row with an unparseable / null captured_at would coerce
    # to NaT and silently land in a spurious bucket; ``errors='coerce'``
    # plus ``dropna`` filters those rows out so the fold builder only
    # sees real timestamps.
    frame["captured_at"] = pd.to_datetime(frame["captured_at"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["captured_at"])
    frame = frame.sort_values(["captured_at", "id"], ascending=[True, True])
    if dedupe_markets:
        frame = frame.drop_duplicates(subset=["market_id"], keep="first")

    normalized_features = []
    for row in frame.to_dict(orient="records"):
        features = _as_dict(row.get("features"))
        family_key = str(features.get("family_key") or _family_key(row.get("sport_key"), row.get("market_family")))
        features.update(
            {
                "family_key": family_key,
                "sport_is_nba": 1.0 if str(row.get("sport_key") or "").upper() == "NBA" else 0.0,
                "sport_is_mlb": 1.0 if str(row.get("sport_key") or "").upper() == "MLB" else 0.0,
                "suggested_price": row.get("suggested_price"),
                "heuristic_fair_yes_price": row.get("fair_yes_price"),
                "heuristic_edge": row.get("edge"),
                "heuristic_confidence": row.get("confidence"),
                "heuristic_selection_score": row.get("selection_score"),
                "threshold": row.get("threshold"),
            }
        )
        normalized_features.append(features)
    frame["features"] = normalized_features
    frame["family_key"] = [features["family_key"] for features in normalized_features]
    # target = 1 iff YES wins. Without this, NO-side rows would be labeled by
    # whether the user's pick won, but serving reads predict_proba[:,1] as
    # P(YES) — see bug #2 in SIKA_PUNCH_LIST.md.
    side_yes = frame["side"].astype(str).str.lower() == "yes"
    outcome_won = frame["prediction_outcome"] == "won"
    frame["target"] = (side_yes == outcome_won).astype(int)
    frame["player_group"] = frame["subject_name"].fillna(frame["ticker"]).astype(str)
    frame["event_group"] = frame["event_id"].fillna(frame["event_name"]).fillna(frame["ticker"]).astype(str)
    return frame.reset_index(drop=True)


def load_settled_predictions(
    database_url: str | None = None,
    *,
    drop_pushes: bool = True,
    dedupe_markets: bool = True,
) -> pd.DataFrame:
    resolved_url = normalize_database_url(
        database_url or os.environ.get("DATABASE_URL") or "sqlite:///../api/kalshi_sports_copilot.db"
    )
    engine = create_engine(resolved_url, future=True)
    query = text(
        """
        SELECT
            id, market_id, event_id, ticker, sport_key, event_name, market_family,
            market_kind, stat_key, threshold, subject_name, subject_team, capture_scope,
            side, suggested_price, fair_yes_price, edge, confidence, selection_score,
            features, scoring_diagnostics, market_status_at_capture, prediction_outcome,
            settled_at, realized_pnl, captured_at
        FROM predictions
        WHERE prediction_outcome IN ('won', 'lost', 'push', 'cancelled')
        """
    )
    with engine.connect() as connection:
        rows = pd.read_sql_query(query, connection)
    return _prepare_frame(rows, drop_pushes=drop_pushes, dedupe_markets=dedupe_markets)


def settled_predictions_from_records(
    records: list[dict[str, Any]],
    *,
    drop_pushes: bool = True,
    dedupe_markets: bool = True,
) -> pd.DataFrame:
    return _prepare_frame(pd.DataFrame.from_records(records), drop_pushes=drop_pushes, dedupe_markets=dedupe_markets)

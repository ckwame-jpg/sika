# sika ML Workspace

Offline training, calibration, backtests, and artifact packaging for the public-data ML rollout.

## Scope

- Singles:
  - `nba_singles_v1`
  - `mlb_singles_v1`
  - `nba_props_v1`
  - `mlb_props_v1`
- Direct parlays:
  - `nba_parlay_2leg_v1`
  - `nba_parlay_3leg_v1`
  - `mlb_parlay_2leg_v1`
  - `mlb_parlay_3leg_v1`
  - `mixed_parlay_2leg_v1`
  - `mixed_parlay_3leg_v1`
- 4-leg through 6-leg parlays stay on a calibrated combiner until there is enough historical density for direct models.

## Public Data Sources

- Existing Kalshi market and snapshot data from the API database
- ESPN public schedules, scoreboards, player game logs, and public status context
- TheSportsDB for schedule and event-gap backfill
- MLB Stats API for rosters, probable starters, bullpen context, and player/team logs
- Public NBA stats endpoints for opponent-defense and player/team rolling form
- Open-Meteo archive and forecast weather
- Versioned static park-factor tables
- Offline-derived travel/rest lookup tables from public schedules

## Workflow

1. Build canonical pregame examples from the API database and public feature enrichments.
2. Train time-split models by family.
3. Calibrate each family.
4. Run holdout and shadow evaluation.
5. Package artifacts into a manifest consumed by the API.

## Artifact Contract

The live API stays inference-only and reads a manifest shaped like [`manifests/public-shadow.example.json`](./manifests/public-shadow.example.json).
Use `serves_family_key` when the packaged artifact keeps a versioned family name such as `nba_props_v1` but should serve the live API family key such as `nba_props`.

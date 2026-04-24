# sika ML Workspace

Offline training, calibration, backtests, and artifact packaging for the public-data ML rollout.

## Scope

- Active study track:
  - `nba_singles_v1`
  - `mlb_singles_v1`
  - `nba_props_v1`
  - `mlb_props_v1`
  - `nba_parlay_2leg_v1`
  - `mlb_parlay_2leg_v1`
  - `mixed_parlay_2leg_v1`
- Heuristic lane for now:
  - `nba_parlay_3leg_v1`
  - `mlb_parlay_3leg_v1`
  - `mixed_parlay_3leg_v1`
  - `4-leg` through `6-leg` parlays stay on a calibrated combiner until there is enough historical density for direct models.

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

`artifact_path` points at an artifact directory, not an individual model file:

```text
artifacts/global_v1_20260424/
├── model.joblib
├── feature_spec.json
└── training_metadata.json
```

The API dispatches directory artifacts with `metadata.behavior = "sklearn_predict_proba"`. Static JSON probability artifacts remain supported for tests and fallbacks.
Because manifests live in `apps/ml/manifests/`, committed manifest entries should use paths like `../artifacts/global_v1_20260424`.

## Training

Run training from this workspace:

```bash
python -m ml.cli train --artifact-root artifacts --manifest-out manifests/current.json
```

The default data source is `DATABASE_URL`, falling back to `../api/kalshi_sports_copilot.db`. Pushes are dropped, coverage captures are excluded, and the first v1 artifact is a global residual-calibration model that can serve a live family through `serves_family_key`.

API shadow activation is explicit:

```bash
ML_SERVING_MODE=shadow
ML_MANIFEST_PATH=/absolute/path/to/apps/ml/manifests/current.json
```

Promotion to live ML still requires `ML_SERVING_MODE=ml`. The API promotion evaluator only promotes after enough settled shadow samples beat the heuristic on Brier, top-decile ROI, and three consecutive daily evaluations; the kill switch demotes back to shadow on rolling Brier regression or sustained runtime failure.

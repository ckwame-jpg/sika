# Codex Review Notes

Target: `claude/compassionate-keller-63679f` / PR `sika#11`  
Basis: targeted repo inspection plus a few live MLB Stats API shape checks. This is not a full line-by-line PR review yet.

## Headline Verdicts

- **PR 1, NBA foundation: ship.** The rate limiter uses a real lock around token accounting, the circuit-breaker state is persisted through `OperatorSetting`, and `season_param(1999) == "1999-00"` is covered. No blocker found in the quick pass.
- **PR 2a, NBA team + ID resolution: ship with monitoring.** The main residual risk is player-ID resolution accuracy for ambiguous names and the fact that only one `EspnPlayerSearchCache` row gets updated on first match.
- **PR 2b, MLB stack: needs changes.** The MLB client/cache layer is broad, but the real scoring path does not yet consume several features the heuristic expects, and the lineup parser appears to target the wrong schedule schema.
- **PR 2c, NBA long-tail: ship with cleanup.** The private helper imports from `advanced_stats.py` are a design smell, but not a functional blocker if tests are green.
- **PR 3, advanced heuristic + v2 feature label: needs changes.** There is one concrete typo in `_mlb_xstats_anchor_factor`, and multiple MLB advanced factors are effectively dead because scoring never emits their input features.
- **PR 4, UI panels: ship as fallback UI.** The UI gracefully hides advanced metric surfaces when optional backend fields are absent. It does not prove backend advanced metric exposure yet.
- **PR 5, market discovery: ship with operational caution.** The deep pagination fix addresses the winner-market discovery issue. Watch the long DB transaction around market refresh plus mapping.

## Actual Bugs / Behavioral Gaps

1. **MLB lineup feature parsing likely does not work with real schedule responses.**
   - Code: `apps/api/app/services/mlb_advanced.py:957-964`
   - The code looks for `game.teams.{home,away}.probableLineup` or `battingOrder`.
   - A live MLB schedule response for `hydrate=lineups,probablePitcher,weather,venue` returned top-level `game.lineups.homePlayers` / `game.lineups.awayPlayers` arrays instead.
   - Impact: `emit_lineup_features()` returns no `batting_order_position`, so `lineup_factor` will not fire from real cached lineups.

2. **MLB opposing-starter, lineup, and bullpen factors are not wired into player-prop scoring.**
   - Code: `apps/api/app/services/scoring.py:1539-1569`
   - The MLB branch emits batter features, park factors, and cached weather only.
   - It does not call `emit_mlb_opposing_pitcher_features()`, `emit_lineup_features()`, bullpen emitters, or opponent-team emitters.
   - Impact: heuristic factors tested in `test_heuristic_factors.py` for `opposing_starter_*` and `batting_order_position` will not affect real predictions unless another path injects those keys.

3. **`_mlb_xstats_anchor_factor` reads recent xBA from the season key.**
   - Code: `apps/api/app/services/heuristic_factors.py:130-142`
   - `recent_xba = features.get("season_xba")` and `season_xba = features.get("season_xba")`, so the expected-stat half of the blend is always `1.0`.
   - Impact: the factor only moves on actual recent AVG vs season AVG, not recent xBA vs season xBA. If no rolling xBA exists yet, make this explicit and do not pretend it is using recent expected stats.

4. **Game-winner scoring does not appear to consume advanced team context.**
   - Code: `apps/api/app/services/scoring.py`, `_score_team_winner`
   - The winner model still uses win rate, average score, home, rest, workload, and venue context.
   - Impact: PR 5 makes game-winner markets flow, but it does not make game-winner predictions use NBA/MLB advanced team stats.

5. **The daily `advanced_stats_warm` scheduler does not discover current prop athletes.**
   - Code: `apps/api/app/services/scheduler.py:257-264`, `apps/api/app/services/refresh_jobs.py:745-755`
   - The cron queues a job without `nba_stats_player_ids` or `mlb_stats_player_ids`; the job defaults those lists to empty.
   - NBA warm still refreshes broad team/percentile and long-tail leaderboards, and MLB warm loads the roster, but per-player MLB/NBA advanced caches depend on IDs being supplied elsewhere or lazy scoring with `allow_network=True`.

## Scrutinize Items That Look Fine

- **Token bucket concurrency:** `TokenBucket.acquire()` holds a lock during token update/decrement, so two threads should not race past one token.
- **Circuit breaker persistence:** breaker and consecutive failures are persisted in `OperatorSetting`. After the disabled window expires, the next successful fetch resets the breaker.
- **OperatorSetting helpers:** `_operator_get` / `_operator_set` use the caller-owned SQLAlchemy session and do not open independent sessions, so I do not see a session leak there.
- **NBA season formatting:** `season_param()` handles century rollover via `(year + 1) % 100`, and tests cover `2024-25` and `1999-00`.
- **Statcast barrel classification:** using `launch_speed_angle == "6"` is plausible for Baseball Savant's barrel classification and is covered by local tests.
- **PR 4 graceful fallback:** `StatsSummaryRead.percentiles` and `metric_categories` are optional; `stats-workspace.tsx` treats missing categories as legacy/basic metrics and hides the advanced grid when there are no advanced metrics.
- **Feature-set label without family-key bump:** in this repo, serving routes through unversioned runtime family keys like `nba_props` / `mlb_props`; `feature_set_version` is checked against the artifact spec but does not by itself select the family. The bump is safe as a label, but it is not the full v2 rollout story.

## Deferred Follow-Up Guidance

- **Family-key v2 rollout:** keep runtime keys (`nba_props`, `mlb_props`) stable unless `model_families.py`, manifest serving, readiness, promotion, shadow capture, and fallback logic are all migrated together. If adding explicit v2 families, register v1 and v2 side-by-side and make fallback selection explicit in `ModelFamilyRuntimeHealth`.
- **Median imputation:** store medians in `FeatureSpec.default_values` for advanced keys. That already survives artifact reload because both training and API runtime vectorizers read `feature_spec.json`. Keep `advanced_data_complete` as a separate indicator.
- **Sample-weighted training:** thread `sample_weight` through `_fit_estimator()` and `_evaluate_candidates()`. For calibrated models, pass weights into `CalibratedClassifierCV.fit(..., sample_weight=weights)` as well as direct estimator fits. Use the same weights for candidate evaluation and final fit.
- **V2-only retrain trigger:** count settled predictions by runtime family key and `features.advanced_data_complete == 1.0`. When the threshold is reached, build the dataset with only complete rows, set advanced-key defaults from that dataset, and disable the 3-5x weighting.
- **Driver attribution:** decide whether PR 4 should render raw multipliers (`advanced_factors`) or true marginal drivers. The current UI renders multipliers, not replayed marginal contribution. If marginal attribution is still desired, add `feature_attribution.py` and write a structured `_drivers` array rather than overloading `advanced_factors`.
- **Stats Assistant advanced metrics:** PR 4 UI is ready, but `stats_query.py` still returns only ESPN gamelog-derived metrics. The backend needs to merge cached advanced metrics and percentile ranks before the advanced grid can appear in real use.

## Suggested Fix Order

1. Fix MLB lineup parser to read `game.lineups.homePlayers` / `awayPlayers`; add a fixture from a real schedule response.
2. Wire MLB opposing pitcher, lineup, bullpen, and opponent-team emitters into `_score_player_prop`, or remove those factors from active gating until their inputs are present.
3. Fix or rename `_mlb_xstats_anchor_factor` so it does not claim to use recent xBA unless rolling xBA is actually emitted.
4. Make `advanced_stats_warm` derive athlete IDs from the current slate / prop context, or document that per-player caches are populated only by prop-refresh scoring.
5. Add a focused integration test that scores an MLB prop and asserts at least one opposing-starter factor and one lineup factor can appear when cached payloads are present.


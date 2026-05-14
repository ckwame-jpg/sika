# Smarter NBA Batch — Session Handoff

**Items in scope:** Smarter #10, #11, #12, #29 from `SIKA_PUNCH_LIST.md`.

This is the heaviest sprint stretch left in the punch list — all four items touch the same scoring surfaces (NBA player props + game lines), share data sources (NBA Stats API, ESPN injury report), and depend on infrastructure that doesn't fully exist yet. The PR sequence in this doc isolates the foundational pieces so each shipment is testable on its own.

---

## How to start the session

### 1. Resume context

```bash
cd /Users/chris/Workspace/locked-in/github/sika
git switch main
git pull --ff-only origin main
git log --oneline -10   # confirm last PR is Smarter #6 (PR #78) or later
```

Recent merged PRs in the smarter roadmap:
- PR #71 — Smarter #1 (reliability-curve buckets)
- PR #72 — Smarter #3 (closing-line value)
- PR #73 — Smarter #24 (time-to-close badge)
- PR #74 — refresh-jobs timeout flake fix
- PR #75 — Smarter #16 (lineup-confirmation suppress)
- PR #76 — Smarter #15 (MLB weather pre-warm)
- PR #77 — Smarter #5 (MLB platoon splits)
- PR #78 — Smarter #6 phase 1 (MLB bullpen rest)

### 2. Verify the environment

```bash
# Python 3.12 venv (NOT 3.14 — the lock pins psycopg-binary==3.2.9 which 3.14 can't resolve)
/Users/chris/Workspace/locked-in/github/sika/.venv/bin/python --version  # 3.12.x

# Sanity run (SQLite override because the local .env points at postgres that may not be running)
cd apps/api && DATABASE_URL='sqlite:///./test_run.db' \
    /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python -m pytest -q --tb=line
# Expect: ~782 passed, 2 skipped
```

### 3. Codex-review-patterns discipline

Codex is rate-limited; the established workflow is:
1. Walk the 9-point checklist from `/Users/chris/.claude/skills/codex-review-patterns/SKILL.md` against the diff before push.
2. Spawn `code-reviewer` subagent for an independent pass on any PR touching gates, persisted state, aggregations, or cross-module contracts.
3. Self-merge via `gh pr merge {N} --squash --delete-branch` since codex is unavailable.

Skip the `code-reviewer` spawn only if the change is a pure additive refactor with no behavior change.

### 4. Workflow per PR

Each item below produces its own PR:

```bash
git switch -c claude/smarter-{N}-{short-topic} origin/main
# ... implement + tests ...
DATABASE_URL='sqlite:///./test_run.db' \
    /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python -m pytest -q --tb=line
# Apply codex-review-patterns
# Spawn code-reviewer agent if applicable
git add <files>
git commit -m "feat(api): {summary} (Smarter #{N})"
git push -u origin claude/smarter-{N}-{short-topic}
gh pr create --title "..." --body "..."
gh pr merge {pr#} --squash --delete-branch
git switch main && git pull --ff-only
```

Update `SIKA_PUNCH_LIST.md` in-PR — mark the item `[shipped]` and add a row to the shipped roll-up table at the bottom of the file.

---

## Item #29 first — small, unblocks #11

**Item:** *NBA pre-game lineup-confirmation reliability tuning.*

**Punch-list framing:**
> NBA lineup confirmation arrives ~10–15 min before tip; injury report ~30–90 min before. Make `nba_injury_report_cache_minutes` 15 during the hour before tip.

**Effort:** S. **Recommended order:** ship FIRST. Frees the cache-freshness assumption that #11 depends on.

### Existing infrastructure

- Config: `apps/api/app/config.py:106` — `nba_injury_report_cache_minutes: int = 60`.
- Cache lookup callers: grep `nba_injury_report_cache_minutes` to find the use sites. Probably one or two loaders in `nba_long_tail.py` / `advanced_stats.py`.

### Plan

1. **Add a dynamic TTL helper** in `apps/api/app/services/nba_long_tail.py` (or wherever the loader lives):
   ```python
   def _effective_injury_report_ttl_minutes(*, now: datetime, event_start: datetime | None) -> int:
       """Per Smarter #29: tighten the cache TTL to 15 min when an NBA
       event is within the next hour; otherwise the default 60-min TTL
       is fine. Returns the minute count for the active call site."""
       settings = get_settings()
       default = int(settings.nba_injury_report_cache_minutes)
       if event_start is None or now is None:
           return default
       seconds_until_tip = (event_start - now).total_seconds()
       if 0 <= seconds_until_tip <= 3600:
           return 15
       return default
   ```
2. Each call site that currently uses `settings.nba_injury_report_cache_minutes` directly must be passed the `event_start` (or `now + event.starts_at`) so the helper can decide. Often this is already in scope.
3. **Pattern 1 catch to avoid:** the TTL value is used both to write `expires_at` on cache row INSERT and to gate "is this cache row still valid?" on READ. If only one side changes, you get a flapping cache.
   - Both sides must call `_effective_injury_report_ttl_minutes`.
   - On write: `expires_at = now + timedelta(minutes=ttl)`.
   - On read: trust the persisted `expires_at` — don't recompute from settings.

### Tests

Add `apps/api/tests/test_nba_injury_ttl.py`:
- Pre-game hour: returns 15.
- 2h pre-game: returns 60.
- Post-tip: returns 60 (don't keep tightening forever).
- Missing `event_start`: returns 60 (no information → safe default).
- Edge: exactly 3600s out → returns 15 (inclusive on the hour boundary so reload is timely).
- Edge: 0s out (kickoff): returns 15.

### Codex review

- Pattern 1 (state-machine): WRITE-side and READ-side must agree on TTL.
- Pattern 5 (reset edge cases): the cache row's `expires_at` is set at write time; later read just compares. If `event_start` changes between write and read (unlikely but possible — postponements), the stale row stays until natural expiry.
- Pattern 7 (UX surface lag): nothing operator-facing. Skip.

---

## Item #11 second — depends on #29, prereq for #10

**Item:** *NBA load-management / workload heuristic.*

**Punch-list framing:**
> A star at 22% chance of "rest day" is the single biggest source of large NBA prop misses. Today only `back_to_back_edge` exists. New `recent_workload_minutes_per_game`, `consecutive_games_played` features in `advanced_stats.py`; require *more than* lineup confirmation when workload is top-quartile.

**Effort:** M.

### Existing infrastructure

- Schedule context already exists at `apps/api/app/services/scoring.py:840` — `_schedule_context()` returns `days_rest`, `games_last_4`, `games_last_7`, `back_to_back`, `last_home_state`.
- Game-log cache: `EspnPlayerGamelogCache` model — caches per-game minutes/stats per player. Find it via `grep -rn EspnPlayerGamelogCache apps/api/app/services`.
- The lineup-suppression infrastructure from Smarter #16 is **NBA-applicable but not yet emitting** — see "Cross-scope note" below.

### Plan — Phase 1 (this PR)

1. **Add player-level workload features** in `apps/api/app/services/advanced_stats.py`:
   ```python
   def emit_nba_workload_features(
       gamelog_payload: dict | None,
       *,
       window_games: int = 5,
   ) -> dict[str, float]:
       """Emit per-player workload features for Smarter #11.

       Reads the recent N games from the gamelog payload and emits:
       - ``recent_workload_minutes_per_game`` — mean MIN over last N
       - ``consecutive_games_played`` — count of consecutive games
         (DNP / DNP-CD breaks the streak, INACTIVE keeps it intact —
         intentional: a one-night rest is the signal we want)
       - ``workload_data_complete`` — 1.0 if at least one game in window
       """
       # ...
   ```
2. **Add a heuristic factor** in `apps/api/app/services/heuristic_factors.py`:
   ```python
   def _nba_workload_factor(features: dict[str, Any]) -> float:
       """Top-quartile recent minutes loaded a player's prop slightly
       downward (fatigue suppresses output). Below-median minutes load
       slightly upward (rested star is dangerous). Conservative ±5%
       envelope."""
       mpg = features.get("recent_workload_minutes_per_game")
       if not isinstance(mpg, (int, float)):
           return 1.0
       # League-wide average MPG for rotation players is ~28.
       # Top quartile starts around 34.
       if mpg >= 34.0:
           return 0.96
       if mpg <= 22.0:
           return 1.03
       return 1.0
   ```
   Gate on **points, rebounds, assists, made_threes, points_rebounds_assists** in `_NBA_FACTORS_BY_STAT`.
3. **Tighten the "requires_lineup" gate** for NBA — phase 1 implementation. The punch list says:
   > require *more than* lineup confirmation when workload is top-quartile

   This is the Smarter #16 pattern adapted: when a player's workload is in the top quartile AND the prop's market metadata has `copilot_requires_lineup`, the existing 0.025 penalty AND a new "workload uncertainty" missing-context entry both apply. Don't suppress (that's an injury-report signal, not a workload one).

   In `_single_scoring_adjustments`, inside the `family_key.endswith("_props")` + `copilot_requires_lineup` branch:
   ```python
   if (
       lineup_data_complete
       and player_in_starting_lineup
       and float(features.get("recent_workload_minutes_per_game") or 0) >= 34.0
   ):
       missing_context.append("workload_top_quartile_uncertainty")
   ```

### Plan — Phase 2 (separate follow-up PR)

- Inject a NEW feature `player_load_management_risk_index` derived from MPG × consecutive games × b2b status × age tier. Suppress recommendations when the risk index exceeds a threshold AND it's a "high-leverage" pick (top quality_tier).
- This requires the more sophisticated workload signal that boxscore archives can provide. Defer until the data plumbing exists.

### Tests

Create `apps/api/tests/test_nba_workload.py`:
- `emit_nba_workload_features` — empty payload returns `{}`; payload with 5 games returns mean MPG + consecutive count + data_complete; payload where 3rd game is DNP returns consecutive=2.
- `_nba_workload_factor` — `<= 22 MPG` → 1.03; `>= 34 MPG` → 0.96; missing → 1.0; `27 MPG` → 1.0 (in the deadband).
- Per-stat gating: points/rebounds/assists/made_threes/PRA include the factor; turnovers/blocks/steals exclude it.
- Drift guard: `workload_factor` in both `_NBA_FACTORS_BY_STAT` and `_NBA_FACTOR_FNS`.
- Scoring integration test: with `recent_workload_minutes_per_game=36` and `copilot_requires_lineup=True` and lineup confirmed, `missing_context` includes `workload_top_quartile_uncertainty`.

### Codex review

- Pattern 1 (state-machine): workload features are computed live, not persisted — no state-shift risk.
- Pattern 2 (cross-component): `recent_workload_minutes_per_game` becomes a new feature key consumed by `_nba_workload_factor`. ML training corpus needs to handle the new feature gracefully (it should — feature spec handles unknown keys via median imputation).
- Pattern 6 (data shape): DNP rows, INACTIVE rows, mid-season player traded (gamelog has multiple teams), rookies with <5 games, all need explicit test cases.
- Pattern 9 (cross-scope): NBA only. Confirm scoring path for MLB/other sports doesn't unintentionally read these features (they shouldn't be set).

### Cross-scope note about Smarter #16

Smarter #16 shipped lineup-suppression for MLB (`emit_lineup_features` distinguishes scratch/confirmed/pre-lineup). NBA has no per-player lineup-confirmation emitter today — that's a known downscope. Phase 2 of #11 OR a separate small follow-up should add an NBA equivalent: parse the injury report for `OUT` / `DOUBTFUL` / `QUESTIONABLE` and emit `player_in_starting_lineup: 0.0` when the player is `OUT`. Then suppression fires the same way MLB does.

If you want a quick win on this during #11: do it. The scoring kernel's suppression block is already wired (`player_not_in_starting_lineup` in `suppression_reasons`). You just need the NBA-side emitter.

---

## Item #10 third — depends on #11 for the workload-aware DNP signal

**Item:** *NBA rest, travel, back-to-backs, schedule density.*

**Punch-list framing:**
> NBA props and game outcomes are strongly affected by fatigue, travel, B2B. WHERE: event ingestion, participant features, `scoring.py`, ML feature emitters.

**Effort:** M.

### Existing infrastructure

- `_schedule_context()` at `apps/api/app/services/scoring.py:840` — already gives `days_rest`, `games_last_4`, `games_last_7`, `back_to_back`, `last_home_state`. **You'll extend this — don't rewrite it.**
- The current `back_to_back_edge` at line 1075 is a binary +/- 0.03 in winner-market expected probability. Phase 1 keeps that and ADDS more granular rest factors.

### Plan

1. **Extend `_schedule_context`** to also return:
   - `games_last_3` (3-day window — matches the MLB bullpen-rest envelope from Smarter #6)
   - `is_third_game_in_four_nights` (boolean — the famous NBA fatigue trigger)
   - `is_fourth_game_in_six_nights` (rarer but punishing)
   - `last_game_away` (proxy for travel: true if last game was an away game; further refinement could compute miles from a venue-coord lookup like MLB Smarter #15 did)

2. **Add a `_nba_rest_factor`** in `heuristic_factors.py`:
   - Reads `team_days_rest`, `team_games_last_3`, `team_is_third_in_four`.
   - 3rd-in-4: 0.96 multiplier (suppression).
   - 4th-in-6: 0.94 multiplier (stronger suppression).
   - 3+ days rest: 1.02 multiplier (slight boost).
   - Combine via min/max so the most extreme case wins; don't multiply two suppressors.
   - Conservative envelope ±6%.

3. **Add to per-stat gating:** points, rebounds, assists, PRA. Made threes are particularly sensitive to fatigue (shot percentage tanks).

4. **Travel proxy (Phase 1 simple version):** `last_game_away` boolean. If today is home AND last was away → no travel today. If today is away AND last was away → travel. Multiplier ±2% suppression for travel cases. This is a placeholder until #15-style venue coords land for NBA arenas (Phase 2 enhancement).

### Tests

Create `apps/api/tests/test_nba_schedule_density.py`:
- Schedule helpers: 3rd-in-4 detection (true / false / edge with exactly 4 days apart).
- 4th-in-6 detection.
- Travel proxy with home → home, home → away, away → home, away → away.
- `_nba_rest_factor` reads each input and produces the expected multiplier.
- Per-stat gating: points/rebounds/assists/PRA in; steals/blocks out (less fatigue-sensitive).
- Drift guard.

### Codex review

- Pattern 3 (granularity): per-stat gating must NOT add the rest factor to stats where the multiplier doesn't make sense.
- Pattern 4 (reduction): the rest-factor takes min of multiple sub-multipliers — verify the reduction is across-the-suppression-pool only, not the boost pool.
- Pattern 9 (cross-scope): NBA only. Verify game-line scoring (winner market) still uses the existing binary `back_to_back_edge` until a separate decision integrates this richer signal there.

---

## Item #12 last — the most speculative, gated on #10 + #11

**Item:** *NBA usage-rate × pace × opponent-defense interaction feature.*

**Punch-list framing:**
> Today these are independent multipliers capped at 0.85–1.15 — understates extreme combinations. Let the ML model learn the shape. WHERE: `heuristic_factors.py` add `_nba_interaction_factor`; emit a single uncapped feature.

**Effort:** M.

### Existing infrastructure

- `_nba_pace_factor_advanced` at `heuristic_factors.py:123` and `_nba_usage_factor_advanced` at `:130` already emit individual factors capped at `_clamp` (±15%).
- The features they read: `recent_usage_pct`, `season_usage_pct`, `opponent_pace_recent_5`, `opponent_pace_season`, `opponent_defensive_rating_*`.

### Plan

1. **Emit a NEW raw feature** `nba_offense_interaction_term`:
   ```python
   def emit_nba_interaction_term(
       *,
       usage_pct: float | None,
       opponent_pace: float | None,
       opponent_drtg: float | None,
   ) -> dict[str, float]:
       """Smarter #12: the multiplicative interaction of usage × pace ×
       (1 / DRtg). Emitted UNCAPPED — the ML model captures the shape,
       not the heuristic factor. Returns {} when any input is missing."""
       if not all(isinstance(v, (int, float)) for v in (usage_pct, opponent_pace, opponent_drtg)):
           return {}
       if opponent_drtg <= 0:
           return {}
       # Centered so league-average values produce ~1.0:
       # usage 25% × pace 100 × (110 / opp_drtg) ≈ 1.0 baseline.
       return {
           "nba_offense_interaction_term": round(
               (usage_pct / 0.25) * (opponent_pace / 100.0) * (110.0 / opponent_drtg),
               4,
           ),
       }
   ```
2. **Do NOT add a new factor function** that reads it during heuristic scoring. The point of #12 is the ML model learns the interaction — heuristic stays on independent factors. Just emit the feature so it lands in `Prediction.features` for training.
3. The training pipeline (`apps/ml/ml/dataset.py`) picks up the new feature automatically via the existing flatten-features path.
4. **Add to `ADVANCED_COMPLETENESS_MARKERS`** in `apps/ml/ml/training.py`? **NO** — the interaction term isn't a "completeness" signal. The underlying inputs (usage, pace, DRtg) already have their own completeness markers.

### Tests

Create `apps/api/tests/test_nba_interaction_term.py`:
- League-average inputs → ~1.0.
- High usage (0.30) + slow opponent (95) + great opp DRtg (102) → some value (computed by hand in the test).
- Each missing input returns `{}`.
- Zero / negative DRtg returns `{}` (defensive).
- Feature key present in emitted dict matches the consumer expectation in the dataset flatten.

### Codex review

- Pattern 2 (cross-component): the ML training pipeline must see the new feature. Verify `dataset.py`'s flatten doesn't have an allowlist that excludes new keys.
- Pattern 4 (reduction): no aggregation; not applicable.
- Pattern 9 (cross-scope): NBA only. Verify feature emission is gated to NBA scoring path.

### Important: this is the only item where you DON'T add a heuristic factor

#12 explicitly says "let the ML model learn the shape". Don't reflexively add a `_nba_interaction_factor` to `_NBA_FACTORS_BY_STAT`. Just emit the feature.

---

## Cross-cutting concerns

### NBA Stats API rate limits + circuit breaker

`apps/api/app/services/advanced_stats.py:380` already gates loaders on `nba_circuit_breaker_open` and `nba_daily_cap_reached`. Any new loader you add must respect both. Tests should monkey-patch the circuit-breaker check.

### Test fixtures for NBA gamelogs

Look in `apps/api/tests/fixtures/` for existing NBA gamelog fixtures. Reuse them; don't invent new shapes. If you need a new fixture, mirror the existing `EspnPlayerGamelogCache.payload` shape exactly.

### NBA arena coords

If you want to do the venue-distance travel proxy properly (#10 Phase 2), follow the Smarter #15 pattern:
- Hardcode `_NBA_ARENA_COORDS` in a new file or extend an existing constants module.
- 30 arenas × (lat, lon).
- All NBA arenas are indoor — no `is_dome` flag needed.

### `_install_threaded_session_factory` is NOT needed

Smarter #6 + Smarter #15 had to use a shared-session fixture for refresh-job tests. NBA factor work won't need it — these are pure-function or single-session DB tests.

### Test commands

```bash
# Full suite from apps/api
cd apps/api && DATABASE_URL='sqlite:///./test_run.db' \
    /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python -m pytest -q --tb=line

# Just the new test file you're working on
DATABASE_URL='sqlite:///./test_run.db' \
    /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python -m pytest tests/test_nba_workload.py -q

# ML tests separately (training markers may need updating)
cd ../ml && DATABASE_URL='sqlite:///./test_run.db' \
    /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python -m pytest -q
```

---

## Order of operations

| Order | PR | Item | Effort | Why this order |
|---|---|---|---|---|
| 1 | Smarter #29 | NBA injury TTL tuning | S | No dependencies; small win; unblocks #11 |
| 2 | Smarter #11 | NBA workload heuristic | M | Depends on #29's fresher injury cache; introduces workload features that #10 can reference |
| 3 | Smarter #10 | NBA rest / travel / B2B | M | Uses workload features from #11 for the "too tired" rest-day predictor |
| 4 | Smarter #12 | NBA interaction term | M | Last because it's the most speculative; better verified via #1's reliability buckets once the simpler factors are in |

Each PR should:
1. Update the relevant `SIKA_PUNCH_LIST.md` entry with `[shipped]` + a one-paragraph "Shipped:" summary mirroring the established format.
2. Add a row to the shipped roll-up table at the bottom.
3. Reference the codex-review-patterns skill in the PR body even if you self-reviewed (the note that codex was rate-limited and you walked the 9-point checklist by hand is standard).

---

## Known traps from the previous sprint

These bit the MLB feature batch and will bite NBA the same way:

1. **`UnboundLocalError` on conditional vars.** Smarter #5 had `starter_id` defined only inside `if starter_name:` then referenced unconditionally below. Initialize to `None` upfront. The same pattern will hit any conditional NBA player-resolution.

2. **Test session vs handler session.** Anything calling into `refresh_jobs._execute_claimed_job` opens its own `SessionLocal()`. Use the `_share_session_local` fixture pattern from `test_weather_prewarm.py` if you need cross-session visibility.

3. **`Participant` schema** uses `display_name` and `participant_type`, NOT `name` and `role`. The Smarter #6 bullpen tests got this wrong on first try; reuse the corrected helper in `test_mlb_bullpen_rest.py`.

4. **`Event` schema** doesn't have `league_key` — it has `league_id` (FK). Both helpers in `test_weather_prewarm.py` and `test_mlb_bullpen_rest.py` show the correct construction.

5. **`event.starts_at` may be naive.** Always coerce to UTC:
   ```python
   if starts_at.tzinfo is None:
       starts_at = starts_at.replace(tzinfo=timezone.utc)
   ```

6. **`ADVANCED_COMPLETENESS_MARKERS` symmetry test** at `apps/ml/tests/test_pr3d_training_v2.py` scans `apps/api/app/services` for `*_data_complete` literals. If you add a new completeness marker, the symmetry test will fail unless the constant in `training.py` is updated AND the marker follows the naming convention. (Smarter #16 had to special-case `player_in_starting_lineup`.)

7. **Drift guard pattern:** every PR that adds a factor function should include a test asserting the factor name appears in BOTH `_*_FACTORS_BY_STAT` AND `_*_FACTOR_FNS`. Otherwise a rename silently no-ops the factor. See `test_platoon_factor_factor_fns_wired` for the canonical shape.

8. **Self-merge requires `--delete-branch` AND syncing main locally.** Pattern:
   ```bash
   gh pr merge {N} --squash --delete-branch
   git fetch origin main --quiet
   git switch main
   git pull --ff-only
   ```
   If the `--delete-branch` flag is omitted, the remote branch lingers and the next PR creation may complain.

---

## What "done" looks like

All four items shipped means:

- 4 PRs merged to main, each on its own `claude/smarter-{N}-{topic}` branch.
- `SIKA_PUNCH_LIST.md` has `[shipped]` markers on items #10, #11, #12, #29.
- Shipped roll-up table at the bottom of the punch list lists all 4 PRs.
- Test count moves up materially — expect ~50-80 new tests across the batch.
- No regressions on the full pytest suite; `pytest apps/api` runs green.
- NBA player-prop scoring now exercises workload, rest, lineup-confirmation, and the new interaction feature in the heuristic path; the ML feature pipeline picks up the new keys for the next retrain.

Good luck. The MLB batch was scoped right at "infrastructure that fires through to a real feature output" — keep that bar for NBA and the batch should land cleanly.

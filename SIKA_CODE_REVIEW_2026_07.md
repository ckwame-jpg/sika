# SIKA Code Review — July 2026

A multi-agent review (picks math, ML pipeline, ingestion/concurrency, data clients,
frontend) plus a live Playwright walk of the dashboard. **47 findings** surfaced;
**25 confirmed** by adversarial verification, **3 refuted**, and the live UI walk
found the runtime bug that mattered most. This doc records what shipped and the
prioritized backlog of what remains.

---

## Shipped — merged to `main` (PRs #252–#257)

| PR | Area | Fixes |
|----|------|-------|
| [#252](https://github.com/ckwame-jpg/sika/pull/252) | refresh | SQLite `too many SQL variables` crash-loop (chunked `IN` queries) → **markets discovered, picks produced again**; un-masked naive/aware datetime bug in `trade_desk._time_to_close_minutes`; pagination truncation now warns instead of silently dropping markets (`max_pages` 50→100). |
| [#253](https://github.com/ckwame-jpg/sika/pull/253) | picks/money | NO-side `suggested_price` double-inversion — Kelly sizing (`scoring/persistence.py`), paper-trade dialog quantity (`trade-dialog.tsx`), and Kalshi NO-fill price display (`kalshi-account-panel.tsx`). |
| [#254](https://github.com/ckwame-jpg/sika/pull/254) | settlement | Parlay cancel-on-void while legs pending (`parlays.py`, `paper_parlays.py`); ZeroDivision poison row from price underflow; settlement-aging badge now counts `unresolved`. |
| [#255](https://github.com/ckwame-jpg/sika/pull/255) | signal | CLV anchored to the **pre-game** close (`predictions.py`) instead of the converged in-game price; coverage-scope picks excluded from the drawdown brake (`kelly_sizing_db.py`). |
| [#256](https://github.com/ckwame-jpg/sika/pull/256) | ingestion | Postgres-invalid `DATETIME` DDL in `SCHEMA_PATCHES`; job-failure handler now rolls back a poisoned session; candlestick prices read the nested `price` object; unbounded `Retry-After` clamped; per-day fetch catches `JSONDecodeError`. |
| [#257](https://github.com/ckwame-jpg/sika/pull/257) | frontend | Negative-PnL sign; panels no longer blank valid cached data on a transient poll failure; trade dialog no longer wipes in-progress input on the 30s poll; hero empty-timestamp. |

Backend suite **1862 passing**, frontend **256 passing** at each merge.

---

## Backlog — deferred, prioritized

Each item is grouped into a suggested follow-up PR. Severity is the reviewer's
confirmed rating. `file:line` is approximate (pre-fix `main`).

### B1 — ML pipeline integrity  *(blocked on ML test env — do first)*
> These change model training/serving behavior and can't be safely validated
> until the ML suite runs. **Prerequisite:** `pip install pandas` into the venv
> and `pip install -e apps/ml` (10 of 14 ML test modules fail collection today
> on `import pandas`, and two backend `test_walk_forward_db.py` cases skip).

- **[HIGH] Train/serve feature skew** — `apps/ml/ml/dataset.py:67`, `training.py:904`. Training + the walk-forward promotion gate consume `suggested_price` / `heuristic_*` features that serving never emits (`runtime._TRAINING_ONLY_FEATURE_KEYS` confirms it), so served vectors freeze those strong signals to the dataset median and the promotion Brier measures a config serving can't reproduce. *Fix:* strip training-only keys from the residual `FeatureSpec` (mirror `independent` mode's `HEURISTIC_DERIVED_KEYS`), or emit them at serve time; vectorize walk-forward test rows with the exact serving key set.
- **[HIGH] Isotonic recalibration "no-improvement" gate is vacuous** — `apps/ml/ml/recalibration.py:378`. `metrics_after` is computed in-sample on the fit window, so improvement is always ≥ 0 and the CLI skip-gate never fires; noisy calibrators ship and can serve exactly `1.0`. *Fix:* evaluate out-of-sample (held-out tail / K-fold); clip recalibrated output to `[0.01, 0.99]`.
- **[MED] Kill switch never protects manifest-promoted families** — `apps/api/app/services/ml/kill_switch.py:84`. Gated on `promotion_mode == "ml"`, which only the API paired-shadow path sets; manifest-promoted families never get rolling-Brier demotion. *Fix:* gate on effective serving mode; derive a baseline from the manifest promotion decision.
- **[MED] Interval coverage is in-sample + triangular CDF serves exact 0/1** — `apps/ml/ml/interval_training.py:232`, `apps/api/app/services/scoring/interval_consumer.py:100`. Coverage gate uses in-sample stats; thresholds outside `[p10,p90]` serve probability 0 or 1 → spurious max edges on alt lines. *Fix:* held-out coverage; clip the CDF off `{0,1}`.
- **[LOW] Artifact loader unpickles any sidecar with no integrity check** — `apps/api/app/services/ml/artifact_loader.py:236`. Local-write → RCE. *Fix:* record SHA-256 of artifacts in the manifest; refuse files whose digest doesn't match.

### B2 — Parlay correlation  *(cross-surface — one coherent change)*
- **[HIGH] Correlation lift is side/direction-blind** — `apps/api/app/services/paper_parlays.py:350` (+ `parlays.py`, `apps/web/components/parlays/paper-parlay-quote.ts`). Mutually-exclusive same-subject opposite-side legs (e.g. "LeBron 30+ YES" + "25+ NO") get a positive lift and a fake edge. *Fix:* make pair-counting side-aware (lift only same-direction legs; opposite-direction → product or reject). Note: a blunt "reject same-event legs" breaks legitimate same-game parlays — don't.
- **[MED] Three divergent joint-probability formulas** — `parlays.py:164` (multi-count `if`, 0.85 cap) vs `paper_parlays.py:386` (exclusive `elif`, 0.7) vs the frontend; plus the empirical trainer classifies a third way and is consumed additively. Same legs → different edge per surface. *Fix:* one shared exclusive-priority pair-classifier consumed by all three.
- **[LOW] American-odds clamp misquotes sub-1¢ parlays** — `apps/api/app/services/parlays.py:35`. Clamping combined price to 0.01 shows `+9900` where the true payout is much larger. *Fix:* clamp only to avoid div-by-zero (`max(price, 1e-6)`).

### B3 — Drawdown brake dedupe + fees
- **[HIGH, partial] Dedupe stacked captures in the brake** — `apps/api/app/services/kelly_sizing_db.py:147`. The coverage-scope filter shipped in #255, but a pick re-scored every 5 min still settles as dozens of rows, multiplying its PnL by the capture count. *Fix:* windowed "latest per (market_id, side)" sum; **requires a test rewrite** — the current tests seed same-market rows as separate PnL.
- **[MED] Kalshi trading fees modeled nowhere** — `apps/api/app/services/kelly_sizing.py:157`, edge gate `scoring/__init__.py:2285`, settlement `predictions.py:445`. Fee `ceil_to_cent(0.07·P·(1-P))` is ~half the 0.03 min-edge at mid prices; stakes overstated ~2x and paper PnL biased optimistic. *Fix:* fold the fee into the effective price before the edge gate, Kelly, and realized PnL.

### B4 — Scoring kernel  *(large, complex file — careful)*
- **[HIGH] Spread game-line markets can never be scored** — `apps/api/app/services/scoring/__init__.py:720`, `heuristics.py:225`. `_market_yes_entry`'s kind gate excludes `"spread"`, making the whole spread scorer dead code (markets ingested, always dropped as `scoring_returned_none`). *Fix:* resolve the spread team from metadata or extend the gate; **validate the currently-unrun spread scorer** before enabling.
- **[MED] ML-served picks discard the freshness confidence penalty** — `scoring/__init__.py:2408`. The Bug-#2 side-probability overwrite in ML mode drops the per-group freshness delta applied ~130 lines earlier, defeating the min-confidence gate/quality tier for prop families served by ML. *Fix:* apply the side-probability conversion then the freshness delta.
- **[MED] Force-YES suppressed props persist mispriced coverage predictions** — `scoring/__init__.py:2349`. The early-return branch books coverage predictions at the model's own fair value with a side-mismatched edge. The brake pollution is fixed by #255's scope filter, but the diagnostics are still wrong. *Fix:* populate `selected_side`/`suggested_price`/side-consistent edge in that branch.
- **[LOW] Consensus/disagreement gates use the pre-ML probability** — `scoring/__init__.py:2157`.
- **[LOW] Poisson banker's rounding on even-floor half-point lines** — `scoring/__init__.py:884` (dormant — prop titles currently parse as integer thresholds).

### B5 — Ingestion / concurrency  *(mostly Postgres-focused)*
- **[HIGH] Retention deletes Runs still referenced by FKs** — `apps/api/app/services/maintenance.py:246`, `database.py`. On Postgres nearly every 6-hourly cleanup aborts on `refresh_jobs.run_id` / `current_slate_snapshots.source_run_id`; a ≥14-day idle gap wedges it permanently. *Fix:* null those FKs before the runs delete (or reorder + exclude referenced runs); enable `PRAGMA foreign_keys=ON` so SQLite surfaces it in tests.
- **[MED] SQLite claim check-then-act isn't atomic** — `refresh_jobs.py:590`. API + worker (both default schedulers) can double-claim the same job. *Fix:* compare-and-swap `UPDATE ... WHERE id=:id AND status='queued' AND NOT EXISTS(running)`.
- **[MED] Kalshi account snapshot ignores the positions cursor** — `kalshi_account.py:584`. Accounts with >100 positions silently truncated. *Fix:* drain the cursor (bounded).
- **[MED] MLB game-day schedule anchored to server-local `date.today()`** — `refresh_jobs.py:1415`. UTC deployment queries the wrong slate every evening. *Fix:* derive the slate date in a fixed US timezone.
- **[LOW] `reconcile_stale_jobs` unguarded read-then-write** — `refresh_jobs.py:402`. Can clobber a just-requeued job as `failed`. *Fix:* guarded bulk UPDATE mirroring the watchdog.

### B6 — Security  *(pre-hosting — low risk on today's localhost single-operator install)*
- **[HIGH-when-hosted] Unsigned identity cookie + unauthenticated `/users/switch`** — `apps/api/app/api/current_user.py:46`, `routes.py`. Anyone reaching the port can impersonate any user. *Fix:* sign the cookie / session token; gate mutating user + ops endpoints behind an operator secret. **Do before any non-localhost exposure.**
- **[MED] Kalshi RSA private keys stored plaintext** — `models.py:64`, `user_kalshi.py`. *Fix:* encrypt at rest (Fernet/keyring); decrypt only in the client factory.
- **[LOW] Unvalidated per-user `base_url` → limited SSRF** — `user_kalshi.py:100`, `schemas.py:1607`. *Fix:* allowlist Kalshi hosts; don't echo upstream error bodies.
- **[LOW] Demo-order "manual approval" is a client-supplied boolean** — `orders.py:173`. *Fix:* server-side confirmation step.

### B7 — Frontend / misc  *(low)*
- **[LOW] Three negative-currency formats on the positions page** — unify into one helper.
- **[LOW] Raw `404 {json}` shown on trade errors** — `trade-dialog.tsx` — map to a human message.
- **[LOW] Parlay dialog "potential payout" shows profit** — `paper-parlay-dialog.tsx:159` — relabel or show gross.
- **[LOW] Kalshi-cents input can't express 1¢** — `lib/price-display.tsx:79`.
- **[LOW] ruff: 113 lint errors** in `apps/api/app` (98 unused imports, 15 import-position) — `ruff check app --fix` + manual.
- **[investigate] Player-stats data ~6 weeks stale** relative to the events feed — a data-pipeline gap surfaced by the UI walk, not a code defect per se.

---

## Refuted — verified NOT issues (do not fix)

- **No-cookie requests bypass per-user scoping** — a guard prevents it; not reachable.
- **Rolling-PnL notional unit "error"** — the notional assumption is defensible.
- **`MarketSnapshot.volume` always NULL (`volume_fp`)** — the field is actually populated.

---

*Generated from the July 2026 review. Fixes #252–#257; backlog items above are unstarted.*

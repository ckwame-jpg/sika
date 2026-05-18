# Paper Parlay Builder — Scope

**Author**: drafted 2026-05-17 (Batch B of the trade-desk UX session).
**Status**: scope only — not yet started.
**Estimated effort**: ~half a day if no surprises.

## Problem

Operators today can only **observe** model-recommended parlays via the read-only
`Parlay Predictions` ledger. There is no UI to:

1. Hand-pick legs from the trade desk (e.g. "Duren 10+ pts AND Mitchell 25+ pts")
2. See the combined model probability + correlation lift in real time
3. Save the combination as a paper parlay and watch it settle in the portfolio

Singles already have this loop: pick → paper trade button → portfolio settles
the outcome. Parlays do not. This blocks the operator from testing parlay
strategies the same way they test single picks.

## Out of scope (deliberately)

- **Demo parlays via Kalshi sandbox** — Kalshi's API doesn't expose a parlay
  endpoint; sandbox orders are per-ticker. Paper-only for v1.
- **"Trade on Kalshi" for parlays** — same reason; would need a guided "place
  each leg yourself" flow. Defer to v2 if ever.
- **Parlay editing after creation** — paper parlay is immutable once saved
  (matches paper-position semantics).
- **Cross-event parlays with > 6 legs** — backend `parlay_max_output` caps the
  recommendation combiner at 4–5 legs today; the builder mirrors that cap.
- **Real-time recalculation across stale cards** — if a leg's underlying market
  closes mid-build, the build fails on submit rather than silently dropping a
  leg. (Out of scope to live-recompute as cards age.)

## UX flow

1. **Entry point**: a new "Add to parlay" button next to "paper trade" on
   the trade-ticket card.
2. **Tray**: clicking the button adds the current selection to a persistent
   tray that docks at the bottom of the trade-desk page. Tray shows each leg
   as a chip (player + threshold + side + entry price).
3. **Live joint math**: as legs are added, the tray header recomputes:
   - Combined market price (product of entry prices)
   - Combined model probability (correlation-adjusted; reuse `_compute_joint_probability` from `parlays.py:232`)
   - Implied edge
   - American odds string (reuse the existing formatter)
4. **Submit**: a "Save paper parlay" button on the tray. Opens a small modal
   with: stake amount, optional notes, confirm. Submits to a new POST
   `/paper-parlays` endpoint.
5. **Portfolio surface**: paper parlays render in a new collapsible section in
   `/positions`, below the existing paper positions table. Same column shape as
   the predictions ledger (leg count, sport scope, joint price, joint prob,
   odds, edge, status, realized pnl) plus the operator's stake + notes.
6. **Settlement**: when the underlying single predictions for every leg
   settle, the paper parlay's outcome is rolled up using the same logic as
   `_settle_parlay_rows` in `parlays.py:657` (any loss → lost; all won → won;
   any push/cancelled → cancelled).

## Data model

**New table**: `paper_parlay` (mirrors `parlay_prediction` shape minus the
recommendation lineage):

| column                    | type           | notes                                      |
|---------------------------|----------------|--------------------------------------------|
| `id`                      | int PK         |                                            |
| `created_at`              | timestamp      | when the operator hit save                 |
| `stake`                   | numeric        | operator-input fake-money stake            |
| `leg_count`               | int            | denormalized for filtering                 |
| `sport_scope`             | str            | single sport key or `"MIXED"`              |
| `participating_sports`    | json           | sorted list                                |
| `combined_market_price`   | numeric        | snapshot at save time                      |
| `combined_model_probability` | numeric     | snapshot, correlation-adjusted             |
| `american_odds`           | str            | snapshot                                   |
| `edge`                    | numeric        | snapshot                                   |
| `notes`                   | str nullable   | operator-supplied                          |
| `settlement_status`       | enum           | pending / settled                          |
| `outcome`                 | enum           | pending / won / lost / push / cancelled    |
| `realized_pnl`            | numeric        | `stake * (1/combined_market_price - 1)` on win, `-stake` on loss |
| `settled_at`              | timestamp      |                                            |

**New table**: `paper_parlay_leg` (mirrors `parlay_prediction_leg`):

| column                    | type            | notes                                                                |
|---------------------------|-----------------|----------------------------------------------------------------------|
| `id`                      | int PK          |                                                                      |
| `paper_parlay_id`         | int FK          |                                                                      |
| `leg_index`               | int             |                                                                      |
| `source_prediction_id`    | int FK nullable | FK to `predictions` so settlement can reuse `_settle_parlay_rows`    |
| `ticker`                  | str             | denormalized for display when source prediction is missing/replaced  |
| `side`                    | str             | yes / no                                                             |
| `suggested_price`         | numeric         | leg entry price at save time                                         |
| `fair_yes_price`          | numeric         | snapshot                                                             |
| `fair_no_price`           | numeric         | snapshot                                                             |
| `subject_name`            | str nullable    | denormalized for display                                             |
| `subject_team`            | str nullable    | for correlation math                                                 |
| `stat_key`                | str nullable    |                                                                      |
| `threshold`               | numeric nullable |                                                                     |
| `market_title`            | str             | display fallback                                                     |
| `event_name`              | str             |                                                                      |
| `sport_key`               | str             |                                                                      |

**Schema deploy**: this project uses SQLAlchemy `Base.metadata.create_all`
(see `apps/api/app/database.py:134`) rather than alembic. Adding new tables
to `models.py` is sufficient — they're created automatically on next API
start. Composite indexes (`ix_paper_parlays_status_created`,
`ix_paper_parlay_legs_parlay_index`) are declared inline next to the
models so they ship as part of the same create_all sweep.

## Backend changes

1. **Schema** (`apps/api/app/schemas.py`):
   - `PaperParlayCreate` — list of leg specs (ticker, side, entry_price snapshot) + stake + notes
   - `PaperParlayLegRead` — wire shape of one leg
   - `PaperParlayRead` — wire shape of full parlay
2. **Service** (`apps/api/app/services/paper_parlays.py` — new):
   - `create_paper_parlay(db, payload)` — validates each leg's ticker against active markets, resolves `source_prediction_id` from the latest unsettled prediction for that ticker+side, computes joint prob via the existing `_compute_joint_probability`, persists to `paper_parlay` + `paper_parlay_leg` atomically
   - `settle_paper_parlays(db)` — mirrors `settle_parlay_predictions` but on the new tables; runs from the same cron entry that does prediction + parlay-prediction settlement
3. **Endpoints** (`apps/api/app/api/routes.py`):
   - `POST /paper-parlays` — create
   - `GET /paper-parlays` — list (filter by status)
   - The existing `GET /positions` aggregator returns paper parlays in a new `paper_parlays` field alongside `paper_positions`
4. **Settlement wiring**: add `settle_paper_parlays` to the post-settlement
   chain in `refresh_jobs.py` so paper parlays settle on the same tick as
   prediction parlays.

## Frontend changes

1. **Tray** (`apps/web/components/trade/parlay-tray.tsx` — new):
   - Zustand store (lightweight) holding `legs: TradeSelection[]`
   - Render docked at bottom of trade-desk page when length > 0
   - Live joint math via a `usePaperParlayQuote(legs)` hook that posts to a
     new `POST /paper-parlays/quote` endpoint (or computes client-side
     using a published `correlationFactors` constant — backend call is more
     authoritative)
2. **Trade-ticket button** (`apps/web/components/trade/trade-ticket.tsx`):
   - "Add to parlay" pill next to "paper trade"; disabled when the current
     selection is already in the tray or when the leg count would exceed 5
3. **Save dialog** (extend `apps/web/components/positions/trade-dialog.tsx`
   or add a parallel `paper-parlay-dialog.tsx`):
   - Stake input, notes textarea, confirm → POST `/paper-parlays` → clear tray
     → push toast → invalidate `/positions` SWR key
4. **Portfolio section** (`apps/web/components/positions/paper-parlays-table.tsx` — new):
   - Mirror `paper-positions-table.tsx` shape; expandable rows show each leg
5. **Routing**: no new route — paper parlays live on `/positions`

## Test plan

**Backend (pytest)**:
- `test_create_paper_parlay_resolves_source_predictions` — given valid leg tickers, the created parlay's legs link to the latest unsettled prediction per leg
- `test_create_paper_parlay_rejects_closed_markets` — at least one leg's market is closed → 400
- `test_create_paper_parlay_joint_probability_matches_combiner` — same input as the predictions combiner produces same joint prob (reuse fixture)
- `test_settle_paper_parlays_wins_when_all_legs_won` — three settled legs all WIN → outcome=won, realized_pnl computed
- `test_settle_paper_parlays_loses_on_any_loss` — three legs, one LOST → outcome=lost
- `test_settle_paper_parlays_cancels_on_push` — any leg PUSH/CANCELLED → outcome=cancelled (matches existing parlay-prediction semantics)
- `test_settle_paper_parlays_skips_pending` — any leg still pending → outcome stays pending

**Frontend (vitest)**:
- `parlay-tray.test.tsx` — adding/removing legs updates the tray; joint math
  fetches and displays; cap at 5 legs is enforced
- `paper-parlay-dialog.test.tsx` — stake validation, submit fires correct
  POST body, clear-tray-on-success
- `paper-parlays-table.test.tsx` — renders settled/pending parlays with
  correct color coding; expand toggle shows legs

**E2E (playwright)** — one happy path:
- Operator picks 2 player props → adds both to tray → joint math appears →
  saves parlay → navigates to /positions → sees the row → simulates leg
  settlement → row updates to "won" with realized_pnl.

## Operator decisions (locked 2026-05-17)

1. **Stake denomination**: **dollar amount** (freeform). Payout on win =
   `stake * (1/combined_market_price - 1)`; loss = `-stake`. Matches
   standard sportsbook UX. Slight semantic divergence from paper
   positions (which use `quantity * entry_price`) is acceptable — parlay
   UX is its own mental model.
2. **Tray persistence**: **persist to localStorage**. Refreshing a tab
   shouldn't nuke 4 legs of work. Implementation note: cap staleness at
   ~30 minutes — drop tray entirely on load if `tray.savedAt` is older
   than that, so the operator doesn't resume a half-built parlay from
   yesterday with stale market prices.
3. **Mid-build price drift**: **save with the original snapshot**. When
   the operator hits save, the parlay is stored at the entry prices
   they saw when each leg was added to the tray, NOT the current
   market price. This is the "what you saw is what you wagered"
   guarantee — paper parlay is its own snapshot, decoupled from live
   market quotes after creation.
4. **Correlation lift breakdown**: **v1 without**. Joint probability
   number is rendered but no "why" tooltip. Add the breakdown in v2 if
   operators ask why a parlay's joint prob differs from the product of
   leg probabilities.

## Phasing

**Phase 1 (MVP, ship together)** — everything in this scope.
**Phase 2 (deferred)** — correlation lift breakdown, parlay-builder via API
(no UI), Kalshi sandbox parlay route once Kalshi exposes one.

## Implementation order (when you're ready to build)

1. Backend models + alembic migration (no service code yet)
2. Backend service `create_paper_parlay` + tests (no endpoints)
3. Backend POST endpoint + tests
4. Backend settlement extension + tests
5. Frontend tray store + render + tests
6. Frontend dialog + POST integration + tests
7. Frontend portfolio table + tests
8. E2E happy path
9. Wire into refresh_jobs.py cron chain

Each step should be a separately-mergeable commit so codex can review each
piece in isolation per the standard sika review pattern.

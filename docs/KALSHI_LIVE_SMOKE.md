# Kalshi live-order smoke test (user-run)

Real orders route to whichever environment you pick on **settings →
kalshi** — the env select is the live/sandbox switch. This checklist
walks the sandbox first, then prod. It is deliberately user-run: no
automation places real orders.

## 0. One-time setup — enable per-user credentials

Real orders require a **per-user credentials row** (they never fall
back to env-var creds; that fallback is pinned to the sandbox by
design). The dev stack currently runs single-tenant, so first enable
multi-user mode:

1. Add to `apps/api/.env` (or the shell env the API starts with):
   ```
   SIKA_USERS=chris
   SIKA_KALSHI_OWNER=chris
   ```
   On next API start, the user `chris` is seeded and — if your env-var
   Kalshi creds are configured — they migrate into chris's credentials
   row automatically (prod base URL).
2. Restart the stack (`stop-sika.sh` → `start-sika.sh`).
3. Pick **chris** in the topbar user dropdown.

## 1. Sandbox pass (fake money)

You need demo-environment API credentials from
[demo.kalshi.co](https://demo.kalshi.co) (its own account + API key).

> **Key permissions matter:** when creating any Kalshi API key (demo or
> prod), grant it **full trading permissions**. A read-only key can see
> your portfolio but order placement fails with
> `insufficient_scope` — and combo minting additionally requires write
> access. The orders panel surfaces this with a fix hint if it happens.

1. **settings → kalshi**: paste the demo key id + PEM, Environment =
   **Demo / sandbox**, save. The header chip should flip to
   `kalshi demo`.
2. Optionally set the **trading guardrails** cap (default $25).
3. **trade** page: the ticket shows the amber `place on kalshi · demo`
   button. Pick any market, stake ~$1–2, and set a **non-crossing
   limit price** (a few cents below the current yes price) so the
   order rests instead of filling.
4. Review → confirm. The order appears in **portfolio → kalshi
   orders** as `submitting`, then `resting` within ~10s (outbox
   drains every 5s).
5. Hit **sync** — status/fills refresh from the sandbox.
6. **cancel** the resting order; status walks `cancelling → cancelled`.
7. Combo: add 2+ legs via `+ parlay`, watch the tray's
   `combinable on kalshi ✓` row, then `place combo on kalshi · demo`
   with a small stake. Expect it to rest (fresh combo books are thin);
   cancel it after.

## 2. Prod pass (real money)

1. **settings → kalshi**: switch Environment to **Production**, paste
   your prod key id + PEM, save. Header chip flips to `live · kalshi`.
2. Confirm the guardrail cap is where you want it — every order is
   rejected server-side above it.
3. Repeat step 1.3–1.6 with a 1-contract, non-crossing limit order.
   The confirm dialog now reads `live · real money`.
4. The order should also appear in the Kalshi app/site under your
   account — that's the "reflects on Kalshi" proof.

## Notes

- Orders stamp their environment at creation: flipping the settings
  env later never re-routes an existing order's cancel/reconcile.
- `submission_failed` / `mint_failed` rows keep the exchange's error
  inline in the panel — nothing fails silently.
- Fees: the dialog shows the taker estimate; resting fills are charged
  the lower maker rate. Actual fees land on fills after reconcile.

# Spawn prompt — Session pickup after 2026-05-17

Copy this whole prompt into a fresh Claude Code session.

---

You are picking up a sika sports trading copilot session that ended at 2026-05-17. The prior session shipped 8 PRs closing out Smarter #21 phase 2d, Smarter #22 PR A + the empirical audit panel, and the design system foundation. Code is at `/Users/chris/Workspace/locked-in/github/sika/`.

Read first, code second (in this exact order):

1. **`SIKA_SESSION_RULES.md`** — durable patterns from prior sessions. Most important: "research, don't fabricate" — look things up via WebSearch / WebFetch / `gh` CLI before writing them. Also documents the BR 403 reality, codex hang fallback, worktree-vs-repo-root contracts package quirk, branch-fresh-from-origin/main pattern.

2. **`SESSION_HANDOFF_2026_05_17.md`** — the session brief. Has the 8 PRs from last session, the current truly-open list, the 5 ranked next-work options (with my recommendation as option 1), workflow notes new from last session, and test baselines.

3. **`apps/web/DESIGN_SYSTEM.md`** — **new last session.** Read this BEFORE any UI PR. Documents 13 patterns + tokens + naming conventions + 6 drift categories + a decision tree for new components.

4. **`PUNCH_LIST_STATE_2026_05_17.md`** — authoritative open-items list as of the last reconciliation.

Skim each. They have every PR reference, every gating rationale, every file:line. Don't re-derive.

## What to do

The handoff's "What's queued (pick from this list)" section lists 5 options. **Default to option 1 unless the user says otherwise:**

- **Option 1 (recommended)** — Retro-redesign `apps/web/components/trade/freshness-badge.tsx` + `apps/web/components/trade/prediction-interval-band.tsx` via `/frontend-design`. Both were built ad-hoc before `/frontend-design` was on the table; the design system doc explicitly names them as drift. The newly-shipped `apps/web/components/predictions/freshness-audit-panel.tsx` is the quality bar. One PR redesigning both. The test contracts (testid + role + data attributes) pin behavior — only visuals change.

If the user asks for a different option (2 = tier-1 surface redesign, 3 = drift-fix, 4 = net-new, 5 = stop), defer to them. Each option is well-scoped in the handoff.

## Workflow requirements (non-negotiable, all in `SIKA_SESSION_RULES.md`)

- **Worktree, not main.** Branch off `origin/main` per PR (`git fetch origin main && git checkout -b claude/<topic> origin/main`).
- **TDD ordering.** Failing test first, then implementation. The test contract for option 1 is the existing test file (`freshness-badge.test.tsx`, `prediction-interval-band.test.tsx`) — don't change the assertions; the visuals are the only thing that should change.
- **Use the design skills proactively.** `/frontend-design` for new component builds, `/design-system` if you discover an undocumented pattern, `/design-critique` if you want feedback before shipping. The 2026-05-15 session and the first half of 2026-05-17 BOTH shipped UI PRs without these skills; the visual quality was meaningfully lower. Don't repeat that.
- **Self-review with the 9-point checklist before push** (in the prior handoff or in `SMARTER_WNBA_HANDOFF.md`).
- **Codex review** via `codex exec --skip-git-repo-check --sandbox read-only "<focused prompt with 9-point list>"`. Default model `gpt-5.5` — `gpt-5-codex` errors on ChatGPT accounts. **Codex hung 2× last session** — fall back to `python-reviewer` / `typescript-reviewer` subagents when it does. Both fallback agents catch real P1s.
- **Address every P1 before merging.** Reply to P2s in the PR description if you choose not to fix them.
- **Admin-merge** with `gh pr merge <N> --squash --admin --body ""`.

Baseline tests must stay green: **1,689 apps/api · 187 apps/web** (as of session end 2026-05-17).

## Cross-package drift guards already in place

- `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` in both `apps/api/app/clients/espn.py` and `apps/ml/ml/interval_dataset.py`.
- `INTERVAL_COVERAGE_*` constants in both `apps/api/app/services/ml/interval_status.py` and `apps/ml/ml/cli.py`.
- Pre-commit `contracts:check` hook (sika#161) verifies the regenerated contracts match the Pydantic schema.

Don't add a third copy if you find yourself duplicating a constant — extend the drift guard pattern.

## What NOT to do

- **Don't open Smarter #22 PR B (policy registry expansion) yet.** It's gated on operator observation of the audit panel for ~1-2 weeks of game cycles. Settled-history rows need ≥20 samples per bucket AND positive calibration delta before any group is promoted. Discipline in `SMARTER_22_TUNING_PLAYBOOK.md`.
- **Don't touch WNBA work.** It's in another session (5 PRs remaining per `SMARTER_WNBA_PREP.md`).
- **Don't ship UI PRs without `/frontend-design`** unless the change is purely structural (e.g. plumbing a typed field through, not designing a new visual). The redesigns in option 1 specifically exist because three UI PRs last session shipped without it.
- **Don't bundle option 1's redesign with any other change** (option 2, option 3, etc.). They're independent surfaces; reviewing them separately is easier and the per-PR scope is already well-bounded.

Start by reading `SIKA_SESSION_RULES.md`, then `SESSION_HANDOFF_2026_05_17.md`, then `apps/web/DESIGN_SYSTEM.md`. Then ask the user which option, defaulting to option 1.

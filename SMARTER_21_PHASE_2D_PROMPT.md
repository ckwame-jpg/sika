# Smarter #21 phase 2d — Claude session spawn prompt

Copy-paste this into a fresh Claude session.

---

You are picking up Smarter #21 phase 2d for the sika sports trading copilot — the scoring kernel consumer + trade-ticket UI band that consume the prediction-interval artifacts phase 2b shipped. Code is at `/Users/chris/Workspace/locked-in/github/sika/`.

Read first, code second (in this exact order):

1. **`SIKA_SESSION_RULES.md`** — durable patterns from prior sessions. Most important: "research, don't fabricate" — if you need a URL / value / fact from outside the codebase, look it up via WebSearch / WebFetch / `gh` CLI before writing it. Also documents the BR 403 reality, codex hang fallback, and worktree-vs-repo-root contracts package quirk.
2. **`SMARTER_21_PHASE_2D_HANDOFF.md`** — the execution brief. Has the 2-PR sequence, the on-disk contract, the load-bearing gating-design decision, real coverage evidence from the 2026-05-16 inspect-intervals run, and what NOT to do.
3. **`PUNCH_LIST_STATE_2026_05_16.md`** (or newer) — confirms phase 2d is one of 3 truly-open items; nothing else blocks it.

Skim each. They have every PR reference, every gating rationale, every file:line. Don't re-derive.

**Before writing any code:** run `python -m ml.cli inspect-intervals --manifest-path manifests/current.json` (from `apps/ml`) to see the current per-stat-key coverage status. The handoff doc has the 2026-05-16 snapshot baked in (2/7 ok, 1/7 warn, 4/7 bad), but coverage may have improved as more games settled. The gating-policy design in PR 3 depends on what you see.

Workflow requirements (non-negotiable, all spelled out in the handoff):

- **Worktree, not main.** Branch off `origin/main` per PR.
- **Two PRs:** PR 3 (scoring kernel consumer) and PR 4 (trade-ticket UI band). Don't bundle.
- **TDD ordering.** Failing test first, then implementation.
- **Self-review with the 9-point checklist before push** (see handoff §"9-point self-review").
- **Codex review** via `codex exec --skip-git-repo-check --sandbox read-only "<focused prompt with 9-point list>"`. Default model `gpt-5.5` — `gpt-5-codex` errors on ChatGPT accounts. If codex hangs (it did 4× in the prior session), fall back to `python-reviewer` / `typescript-reviewer` subagents.
- **Frontend changes use the `/frontend-design` skill family** — PR 4's component work.
- **Address every P1 before merging.** Reply to P2s in the PR description if you choose not to fix them.
- **Admin-merge** with `gh pr merge <N> --squash --admin --body ""`.

Baseline tests must stay green: **1,560 apps/api · 247 apps/ml · 153 apps/web.**

Cross-package drift guards already in place (CI fails if you forget one side):
- `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` in BOTH `apps/api/app/clients/espn.py` AND `apps/ml/ml/interval_dataset.py` (added PR #165).
- `INTERVAL_COVERAGE_*` constants in BOTH `apps/api/app/services/ml/interval_status.py` AND `apps/ml/ml/cli.py` (added PR #164 + drift-guarded in #163). **Phase 2d's consumer should READ these constants from `apps/api/app/services/ml/interval_status.py` rather than duplicate them a third time.**

The load-bearing design decision for PR 3 (do this before writing tests):

**Coverage-status gating policy.** The 2026-05-16 demo proved 4/7 stat keys land in `bad` coverage. Naively consuming all intervals = worse than Poisson for those keys. Three options (handoff §PR 3 §Gating explains in depth):
- **Strict (recommended for first ship):** consume only when `coverage_status == "ok"`.
- **Lenient:** consume `ok` + `warn`.
- **Weighted:** blend interval-derived YES probability with Poisson, weighted by coverage proximity to 0.80.

Pick one (recommend strict), state it in the PR description, then code.

The 2-PR sequence (estimated 1-2 sessions total):

| PR | Scope | Effort |
|---|---|---|
| 3 | Scoring kernel consumer — `_score_player_prop` branch, triangular CDF math, coverage-status gating, `scoring_diagnostics.prediction_interval` surface, ~10-15 tests | 1 day |
| 4 | Trade-ticket UI band — `prediction-interval-band.tsx` + `trade-ticket.tsx` mount + `lib/types.ts` `PredictionInterval` interface + contracts regen + ~5-8 vitest tests | ½ day |

Start by reading `SIKA_SESSION_RULES.md`, then `SMARTER_21_PHASE_2D_HANDOFF.md`, then run `inspect-intervals` to see live coverage.

---

## How to use this prompt

In a fresh Claude session, paste the section between the `---` markers above. The new session will read SIKA_SESSION_RULES first (avoiding the fabrication trap the previous session hit), then the handoff doc, then inspect live coverage state before starting PR 3.

If the new session reports something missing or unclear, that's a signal to update one of the two source docs (`SIKA_SESSION_RULES.md` or `SMARTER_21_PHASE_2D_HANDOFF.md`) and re-spawn — not to inline-clarify in chat.

## Estimated session count

- **1-2 sessions** to ship PRs 3 + 4.
- **+1 session of follow-up** if you decide to add a "data committed snapshot" for the monthly digest routine (out of scope for phase 2d itself; covered by the `sika monthly operator digest` routine `trig_01CiYygd95WDdjupTfc5zCah`).

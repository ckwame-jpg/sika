# Smarter WNBA PR 6-8 — Claude session spawn prompt

Copy-paste the section between the `---` markers below into a fresh Claude session.

---

You are picking up the Smarter WNBA work for the sika sports trading copilot. The 2026 WNBA season is live (started 2026-05-08, peak runs through Sept) — every week without coverage is missed picks. Code is at `/Users/chris/Workspace/locked-in/github/sika/`.

**5 of 8 WNBA PRs are already merged** (#177 sport scaffolding, #178 market mapping, #181 gamelog + stats query, #183 scoring kernel, #184 training pipeline registration). The remaining work is **PR 6 (critical path) + PRs 7-8 (MVP+1)**.

Read first, code second:

1. **`SMARTER_WNBA_PR6_8_HANDOFF.md`** at the repo root — the detailed scope + workflow for PRs 6-8 written by the previous session right after #184 merged. Has per-file touchpoints, the in-flight PR 6 state, codex-prompt template, and what NOT to do.
2. **`SMARTER_WNBA_PREP.md`** — deep research backing the handoff (every URL, every gotcha, every Kalshi-coverage caveat). Don't re-derive.
3. **`SMARTER_WNBA_HANDOFF.md`** — the original 8-PR sequence and workflow doc. PRs 1-5 are done; the workflow requirements still apply.

Workflow requirements (non-negotiable, full list in the handoff):

- **Worktree, not main.** Branch `claude/<topic>` off `origin/main` per PR.
- **One PR per scope.** PR 6 is multi-file (sport adapter + Kalshi constants + flip `enabled_sports` + tests). PRs 7-8 are independent.
- **TDD-ish ordering.** Failing test first; then implement; verify green.
- **9-point self-review** before push.
- **Codex review** via `codex exec --skip-git-repo-check --sandbox read-only "<focused prompt>"` (default model — `gpt-5-codex` errors on ChatGPT accounts). Address every P1 + the meaningful Mediums.
- **Admin-merge** via `gh pr merge <N> --squash --admin --body ""`.

Baseline tests must stay green (post-#184):
- **apps/api 1666** (4 skipped), **apps/ml 255**, **apps/web 153** (1 pre-existing tsc error from sika#180 unrelated to WNBA).

PR 6 is the only critical-path PR remaining. After it lands, `enabled_sports` includes WNBA and `KXWNBAGAME` markets actually persist + score. PRs 7-8 are MVP+1 — ship them if there's session budget, defer if not.

Two things to verify before assuming day-1 parity (both flagged in the prep doc + handoff):

1. **Kalshi WNBA per-game player props were not yet broadly live as of mid-May 2026.** Only milestones (`kxwnba40pts`) + futures (`kxwnbamvp`, `kxwnbaseries`) were confirmed. Hit Kalshi's events API at first WNBA slate refresh after PR 6 ships to confirm per-game prop coverage. If empty, PR 6 still ships value — infrastructure is ready — but the day-1 watchlist won't carry WNBA props.

2. **WNBA doesn't mandate pre-tip lineup confirmation.** RotoWire often confirms starters post-tipoff. Smarter #16's "suppress, don't penalize" policy still works correctly but fires more often on WNBA. Document for operators.

The previous session left two uncommitted edits on `claude/smarter-wnba-pr-6-kalshi-adapter` (sport adapter + PUBLIC_MAJOR_SPORTS). The handoff doc recommends discarding them and branching fresh — those 10 lines are easier to re-apply with full focus than to inherit.

Start by reading `SMARTER_WNBA_PR6_8_HANDOFF.md`, then branch fresh and tackle PR 6 first.

---

## How to use this prompt

In a fresh Claude session, paste the section between the `---` markers above. The new session will read the handoff doc, which references the prep + original handoff, and start with PR 6.

If the new session asks clarifying questions, the answers are in the handoff's `## PR 6 — Kalshi WNBA discovery + sport adapter` section or the prep doc's §6 (recommended PR sequence) and §7 (risks).

If the new session reports something is unclear or missing, that's a signal to update one of these docs and re-spawn, not to inline-clarify in chat.

## Estimated session count

- **1 session** for PR 6 (critical path). ~30-60 min if Kalshi WNBA per-game props are live; ~30 min if they're not yet (you still ship the infrastructure).
- **+0.5 session** for PR 7 (WNBA injury endpoint).
- **+0.5 session** for PR 8 (operator UX polish).

So 1-2 sessions to complete the WNBA expansion.

## What's done at handoff time

- 5 of 8 WNBA PRs merged: #177, #178, #181, #183, #184.
- Architecture #5 + 2 follow-ups merged: #169, #173, #175.
- Smarter #21 phase 2d work (#179, #180) merged by another session — unrelated to WNBA but landed in the same window.
- 1666 apps/api / 255 apps/ml / 153 apps/web tests green.

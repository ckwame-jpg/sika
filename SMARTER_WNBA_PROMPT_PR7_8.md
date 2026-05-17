# Smarter WNBA PR 7-8 — Claude session spawn prompt

Copy-paste the section between the `---` markers below into a fresh Claude session.

---

You are picking up the Smarter WNBA work for the sika sports trading copilot. The 2026 WNBA season is live (started 2026-05-08, peak runs through Sept). Code is at `/Users/chris/Workspace/locked-in/github/sika/`.

**6 of 8 WNBA PRs are merged.** PR 6 ([sika#188](https://github.com/ckwame-jpg/sika/pull/188), commit `72d1821`) flipped `enabled_sports` to include WNBA + wired every cross-component surface (sport adapter, Kalshi constants, Odds API mapping, `CURRENT_WATCHLIST_SPORTS`, refresh-job defaults, ml/promotion gate). After PR 6, a fresh sika deployment running the default refresh cycle fetches WNBA events from ESPN, persists KXWNBA Kalshi markets, scores them via the WNBA branch, and surfaces them in the trade desk + `/product/freshness`.

The remaining work is **PR 7 (WNBA injury endpoint, ~half-day LOW risk)** and **PR 8 (operator UX polish, ~half-day NONE risk)**. Both are MVP+1 — operator-experience improvements, not blockers.

Read first, code second:

1. **`SMARTER_WNBA_PR7_8_HANDOFF.md`** at the repo root — detailed scope + workflow for PRs 7 and 8 written by the session that finished PR 6. Has per-file touchpoints, reviewer-prompt focus, baselines, and a "What NOT to do" section.
2. **`SMARTER_WNBA_PREP.md`** — deep research backing the handoff (every URL, every gotcha, every Kalshi caveat). Don't re-derive.
3. **`SMARTER_WNBA_HANDOFF.md`** — the original 8-PR sequence and workflow doc. PRs 1-6 are done; the workflow requirements (worktree-per-PR, 9-point self-review, admin-merge) still apply.

Workflow requirements (non-negotiable, full list in the handoff):

- **Worktree, not main.** Branch `claude/<topic>` off `origin/main` per PR.
- **One PR per scope.** PRs 7 and 8 are independent — don't bundle.
- **TDD-ish ordering.** Failing test first; then implement; verify green.
- **9-point self-review** before push.
- **Reviewer subagent (python-reviewer / typescript-reviewer) in preference to codex.** Codex has hung 5× across the last two sessions. The reviewer subagent is responsive and caught a real Medium on PR 6 (dead `_parlay_examples` WNBA gate).
- **Admin-merge** via `gh pr merge <N> --squash --admin --body ""`.
- **Rebase if main moves mid-session.** Re-run all three suites post-rebase before push.

Baseline tests must stay green (post-#188):
- **apps/api 1680** (4 skipped), **apps/ml 255**, **apps/web 176** (4 pre-existing tsc errors from sika#180 unrelated to WNBA — vitest itself green).

Three things from PR 6 to carry forward (full list in handoff §"Cross-PR learnings"):

1. **Flipping a global gate has wider test blast radius than the code change itself.** PR 6's `CURRENT_WATCHLIST_SPORTS` expansion required updating 5+ test fixtures across `test_api.py` + `test_trade_desk.py`. When PR 7 adds WNBA to any new gate, grep for sibling pinning tests BEFORE running the full suite.
2. **Codex hangs on large diffs — skip it.** Go straight to the `python-reviewer` agent (or `typescript-reviewer` for apps/web). On PR 6 the reviewer caught a dead-code Medium that codex would have caught too.
3. **Latent bug worth knowing but NOT fixing in PR 7 or 8:** `apps/api/app/services/ml/walk_forward.py:358` (`_build_parlay_predicate`) gates only `{"NBA", "MLB"}` — a future `wnba_parlay_*` family with `sport_scope="WNBA"` would fall through to the bare `leg_filter` and silently over-include mixed-sport rows. Fix in the WNBA parlay PR (Smarter #28 follow-up), not as a drive-by here.

PR 7 is the meatier one (new `WnbaInjuryReportCache` model + cross-PR `family_key` gate widening + scoring kernel emit). PR 8 is largely operator-facing string updates + readiness-panel verification (uses the `/frontend-design` skill family). They're independent — start with whichever fits the session budget best.

Two things to verify at first WNBA slate refresh (carryover from PR 6, not blocking these PRs):

1. **Kalshi `kxwnbagame` series slug.** PR 6 used the NBA naming pattern. Hit Kalshi's events API to confirm. If wrong, update both `apps/api/app/api/routes.py:KALSHI_EVENT_SERIES` and `apps/api/app/services/trade_desk.py:KALSHI_EVENT_SERIES`.
2. **WNBA prop stat slugs.** Mirrored NBA's `player-points` / `player-rebounds` / etc. — verify at first live KXWNBA prop.

Start by reading `SMARTER_WNBA_PR7_8_HANDOFF.md`, then branch fresh and tackle whichever PR fits the session budget.

---

## How to use this prompt

In a fresh Claude session, paste the section between the `---` markers above. The new session will read the handoff doc, which references the prep + original handoff, and start with PR 7 or PR 8.

If the new session asks clarifying questions, the answers are in the handoff's `## PR 7` / `## PR 8` sections or the prep doc's §6 (recommended PR sequence) and §7 (risks).

If the new session reports something is unclear or missing, that's a signal to update one of these docs and re-spawn, not to inline-clarify in chat.

## Estimated session count

- **+0.5-1 session** for PR 7 (WNBA injury endpoint — new cache table + scoring kernel emit + cross-component `family_key` widening).
- **+0.5 session** for PR 8 (operator UX polish — mostly string updates + cascading test fixtures + readiness-panel verification).

A single session should fit both if scoped tightly; a fresh session per PR is safer if the operator wants thorough reviewer rounds.

## What's done at handoff time

- 6 of 8 WNBA PRs merged: #177, #178, #181, #183, #184, #188.
- Architecture #5 + 2 follow-ups merged: #169, #173, #175.
- Smarter #21 phase 2d work (#179, #180) merged by another session.
- Smarter #22 PR A (#186) + tuning playbook (#187) merged in parallel during PR 6's session — rebase landed cleanly, baselines bumped to apps/api 1680 / apps/web 176.
- 1680 apps/api / 255 apps/ml / 176 apps/web tests green.

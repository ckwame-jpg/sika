# Smarter WNBA — Claude session spawn prompt

Copy-paste this into the new Claude session.

---

You are picking up sport expansion to WNBA for the sika sports trading copilot. Today only NBA + MLB are active ship targets. WNBA's 2026 season is live (started 2026-05-08, runs 140 days to 2026-09-24) — every week without coverage is missed picks you can't backfill. Code is at `/Users/chris/Workspace/locked-in/github/sika/`.

Read first, code second:

1. Read **`SMARTER_WNBA_HANDOFF.md`** at the repo root. It has the 8-PR execution sequence, design decisions, workflow requirements, file layout, and what NOT to do.
2. Read **`SMARTER_WNBA_PREP.md`** for the deep research backing the handoff — every URL, every file:line touchpoint, every gotcha. Don't re-derive.
3. Skim **`PUNCH_LIST_STATE_2026_05_16.md`** to confirm nothing else blocks WNBA.

Workflow requirements (non-negotiable, all spelled out in the handoff):

- **Worktree, not main.** Branch off `origin/main` per PR.
- **One PR per scope.** The handoff specifies 8 distinct PRs. Don't bundle.
- **TDD ordering.** Failing test first, then implementation.
- **Self-review with the 9-point checklist before push** (full list in handoff §"9-point self-review").
- **Codex review** via `codex exec --skip-git-repo-check --sandbox read-only "<focused prompt with 9-point list>"`. Default model is `gpt-5.5` — `gpt-5-codex` errors on ChatGPT accounts. If codex hangs (it did 4× in the previous session), fall back to the `python-reviewer` / `typescript-reviewer` subagent.
- **Frontend changes use the `/frontend-design` skill family.**
- **Address every P1 before merging.** Reply to P2s in the PR description if you choose not to fix them.
- **Admin-merge** with `gh pr merge <N> --squash --admin --body ""`.

Baseline tests must stay green: **1,556 apps/api · 247 apps/ml · 153 apps/web.**

Cross-package drift guards already in place (CI fails if you forget one side):
- `_ESPN_TEAM_ABBREVIATION_TO_DISPLAY_NAME` in BOTH `apps/api/app/clients/espn.py` AND `apps/ml/ml/interval_dataset.py`. Add the WNBA team map to BOTH sides in PR 1.

8-PR sequence (critical path PRs 1-6 are ~3 sessions, ~12-15 hours):

| PR | Scope | Effort |
|---|---|---|
| 1 | Sport scaffolding (allowlists, team maps, ESPN URLs, TS types, contracts regen) | ½ day |
| 2 | Market mapping (WNBA_PROP_ALIASES, title regex, KXWNBA ticker prefix) | ½ day |
| 3 | Gamelog parsing + stats query (`_build_game_logs` WNBA branch, season rollover) | ½ day |
| 4 | Scoring kernel WNBA branch + heuristic profiles | 1 day |
| 5 | Training pipeline registration | ½ day |
| 6 | Kalshi WNBA discovery + ingestion | ½ day |
| 7 (MVP+1) | WNBA injury endpoint | ½ day |
| 8 (MVP+1) | Operator UX polish | ½ day |

Two things to verify before assuming day-1 parity (both flagged in the prep doc):

1. **Kalshi WNBA per-game player props were not yet broadly live as of mid-May 2026** — only milestones (`kxwnba40pts`) + futures (`kxwnbamvp`, `kxwnbaseries`) confirmed. Hit Kalshi's events API at first WNBA slate refresh after PR 6 ships to confirm per-game prop coverage.
2. **WNBA doesn't mandate pre-tip lineup confirmation.** RotoWire often confirms starters post-tipoff. Smarter #16 "suppress, don't penalize" policy works correctly but fires more often.

Start by reading `SMARTER_WNBA_HANDOFF.md`.

---

## How to use this prompt

In a fresh Claude session, paste the section between the `---` markers above. The new session will read the handoff doc, which references the prep doc, and start with PR 1 (sport scaffolding).

If the new session asks clarifying questions, the answers are in the prep doc §5 (open design decisions) and the handoff doc's "What NOT to do" section.

If the new session reports something is unclear or missing, that's a signal to update one of the two docs and re-spawn, not to inline-clarify in chat.

## Estimated session count

- **3 sessions** to ship PRs 1-6 (critical path).
- **+1 session** for PRs 7-8 (MVP+1).
- **+0 sessions** for interval training of WNBA stat keys — once 3-4 weeks of WNBA games settle, `train-intervals` works with zero new code.

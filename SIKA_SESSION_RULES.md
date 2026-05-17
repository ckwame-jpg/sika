# Sika session rules

Patterns / behaviors that have bitten previous Claude sessions on this repo. Read these once at session start; re-read before any "I think the URL is…" or "based on what I know about X…" claim.

## 1. Research, don't fabricate

**The mistake (2026-05-16 session):** When asked what URL the operator needed to supply for Smarter #13 phase 2b-2 (BR referee tendency wiring), I wrote `https://www.basketball-reference.com/leagues/NBA_2026_officials.html` based on a pattern guess. **That URL doesn't exist.** The actual URL is `https://www.basketball-reference.com/referees/2026_register.html`.

The user caught it. Cost: their trust + a clarification round.

**The rule:** Before writing a URL, endpoint, table name, file path, config key, env var, or any other external/factual reference in a reply (or in a doc, commit message, or PR body):

1. **Did I read it from the codebase / config / official docs in this session?** → fine to write.
2. **Did I research it via WebSearch / WebFetch / a verified source in this session?** → fine to write, ideally cite.
3. **Am I pattern-matching from "I think the structure is usually like X"?** → STOP. Either research it now, or say "I don't know the exact URL — can you confirm?" Never write the guess as if it were verified.

The available research tools on this repo:
- **WebFetch** — works for most public sites. Returns 403 from basketball-reference.com from a fresh IP (BR throttles anonymous). Falls back to a `gh` CLI for GitHub URLs.
- **WebSearch** — surfaces page existence + titles even when WebFetch is blocked. Use this for URL pattern verification when the page itself isn't fetchable.
- **gh CLI via Bash** — for GitHub (PRs, issues, releases, search). Always prefer this over WebFetch for github.com URLs.

If both WebFetch + WebSearch can't get the answer (e.g. authenticated page, blocked domain), say so explicitly: *"I can't verify X from here — can you paste the URL / value / output?"* Don't fill the gap with a plausible-looking guess.

## 2. The basketball-reference 403 is real

WebFetch on `www.basketball-reference.com` returns HTTP 403 from this environment. The operator can view the same URL in their browser without throttling because they have a configured `basketball_reference_base_url` path and presumably an IP that hasn't been rate-limited.

This is documented in `apps/api/app/services/nba_referee_tendencies.py:28-31`:
> basketball-reference returns 403 to anonymous WebFetch from a fresh IP, so the URL+table layout decoding requires the operator's configured base_url path

When researching BR pages from a Claude session:
- **WebSearch** can confirm the URL exists + return the page title.
- **WebFetch** will return 403 on most attempts — don't treat the failure as evidence the page doesn't exist.
- **Operator paste** is the fallback for actual table contents.

## 3. The URL pattern for BR seasonal pages (verified)

Pattern (verified via WebSearch for seasons 2003-2026):
```
https://www.basketball-reference.com/referees/{season}_register.html
```

Where `{season}` is the calendar year the season ENDS (NBA's `2025-26` season → `2026_register.html`). Matches the existing `BasketballReferenceClient` convention of using `end_year` everywhere (see `apps/api/app/clients/basketball_reference.py` for `/leagues/NBA_{end_year}.html` patterns).

## 4. Punch-list status truth

The `[ ]` / `[x]` checkboxes in `SIKA_PUNCH_LIST.md` drift behind merged work. Trust `PUNCH_LIST_STATE_2026_05_16.md` (or the latest dated state snapshot) for status truth. The state snapshot is reconciled against `git log origin/main` at the snapshot's date.

## 5. Codex review reality (as of 2026-05-16)

- `gpt-5-codex` model errors on ChatGPT-account codex CLI usage. Default to `gpt-5.5` or just omit `--model`.
- Codex hung 4× during the 2026-05-16 session reviewing apps/ml + apps/api changes (and 5× more across the 2026-05-17 WNBA PRs). Self-review against the 9-point checklist below is the documented fallback. When codex DOES respond, it catches things self-review misses; the `python-reviewer` / `typescript-reviewer` subagents are also responsive and have caught real Mediums when codex was hung.

### 9-point self-review checklist (apply before every push)

1. Does the test fail without the change and pass with it?
2. Are types narrow (no `Any`, no `dict[str, Any]` at boundaries)?
3. Are inputs validated at the boundary and errors surfaced explicitly?
4. Any silent fallback that could mask a real bug?
5. Does the on-disk / API contract match what existing sports established?
6. Are imports / re-exports preserved for backward compat?
7. Did I touch only files this PR requires?
8. Is the PR description specific about scope, contract, and rollback?
9. Did codex (or the reviewer subagent) flag anything I haven't addressed?

## 6. Worktree vs repo-root contracts package

When you regenerate contracts in a worktree via `npm run contracts:generate`:
- The new `api.d.ts` + `openapi.json` land in the worktree.
- The worktree's `npm` workspaces resolve `@kalshi-sports-copilot/contracts` via a symlink to the **repo-root** `packages/contracts`, not the worktree's copy.
- So for local `tsc` to pick up the regenerated types, copy `packages/contracts/openapi.json` and `packages/contracts/generated/api.d.ts` from the worktree to the repo root before running type-check.
- CI / production are unaffected (they read the worktree's committed files).

## 7. Branch fresh from origin/main between PRs

When pulling main into the worktree after admin-merging, don't try to fast-forward an existing branch. Pattern:
```bash
git fetch origin main
git checkout -b claude/<next-pr-scope> origin/main
```
Repeat per PR. Branches lingering from prior PRs in the worktree complicate the rebase chain.

## 8. Codex sandbox file copies

When the agent makes file edits at the repo-root path (`/Users/chris/Workspace/locked-in/github/sika/X.md`) while the active worktree is `.claude/worktrees/.../X.md`, the edits land at the repo root but git operations from the worktree don't see them. Pattern: `cp <repo-root-path> <worktree-path>`, then stage + commit from the worktree. Or just use the worktree path in the Write/Edit call to begin with.

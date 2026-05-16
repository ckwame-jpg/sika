# Smarter #21 phase 2b/d — spawn prompt

Copy the block below into a fresh Claude Code session at the sika repo root. The handoff document `SMARTER_21_PHASE_2B_HANDOFF.md` (sibling file in the repo root) carries the full context; this prompt just bootstraps the session.

---

You are picking up **Smarter #21 phase 2b** (training-pipeline integration) and, once 2b is in production, **phase 2d** (scoring consumer + UI band) for the sika sports trading copilot.

## Read first, code second

1. Read `SMARTER_21_PHASE_2B_HANDOFF.md` (at the repo root). It has the full scope, on-disk contract, what's already shipped (PRs #133 and #140), open design decisions, file layout, and what NOT to do. Do not skip this — the contract between phases is precise and drifting from it silently breaks both ends.
2. Skim `SIKA_PUNCH_LIST.md` for the Smarter #21 section to confirm scope hasn't shifted.
3. Skim `SESSION_HANDOFF_2026_05_15.md` only if the handoff doc references a pattern you don't recognize.

## Workflow requirements (non-negotiable)

- **Worktree, not main.** Create a `claude/<topic>` branch off `origin/main` before any commits. Direct commits to main are guarded.
- **One PR per phase.** Phase 2b dataset extraction is one PR. Phase 2b CLI subcommand is a separate PR. Phase 2d scoring consumer is a separate PR. Phase 2d UI band is a separate PR. Do not bundle.
- **Parallel writing OK, sequential merging required.** You can write phase 2d's code (scoring consumer hook + UI band) on a branch in parallel with phase 2b shipping — there's no write-time dependency. But do NOT merge phase 2d until phase 2b has shipped AND the operator has run `python -m ml.cli train-intervals` for at least one stat key so the `interval_models/<stat_key>/` sidecar files actually exist. Without populated artifacts the consumer's gating (`apply_interval_models` returns empty map) means the new path never executes — you'd be merging dead code with no end-to-end verification.
- **TDD ordering.** Write the failing test first, then the implementation. The sika test suites are the source of truth — 1,529 apps/api / 186 apps/ml / 148 apps/web tests must stay green after each PR.
- **Use codex to double-check your work before you push.** Run `codex exec --model gpt-5-codex --skip-git-repo-check --sandbox read-only "<focused review prompt naming the files changed and the 9-point checklist below>"` against each PR's diff before opening it. If codex rate-limits, fall back to the `python-reviewer` (for apps/api or apps/ml) or `typescript-reviewer` (for apps/web) subagent — but always do an independent review pass, never skip it.
- **Frontend changes use frontend skills.** For phase 2d UI work, invoke the `/frontend` skill family before touching React/TypeScript.

## The 9-point self-review checklist (apply before every push)

1. Does the test fail without the change and pass with it?
2. Are types narrow (no `Any`, no `dict[str, Any]` at function boundaries unless serializing)?
3. Are inputs validated at the boundary and errors surfaced explicitly?
4. Is there any silent fallback that could mask a real bug?
5. Does the on-disk / API contract match what phase 2a and 2c established?
6. Are imports / re-exports preserved for backward compat?
7. Did I touch only files this phase requires?
8. Is the PR description specific about scope, contract, and rollback?
9. Did codex (or the reviewer subagent) flag anything I haven't addressed?

## What "done" looks like

- **Phase 2b ships** when: a new `train-intervals` CLI subcommand under `apps/ml/ml/cli.py` extracts settled-prediction + ESPN-gamelog joined rows, fits quantile regressors per family + stat key, writes the sidecar artifacts to the layout phase 2a defined, and emits coverage diagnostics. Tests cover the extraction join, the CLI happy path, the empty-data short-circuit, and the metadata round-trip.
- **Phase 2d ships** when: `_score_player_prop` in `apps/api/app/services/scoring/__init__.py` swaps the Poisson approximation for a CDF lookup against the loaded interval models (when present), the trade-ticket UI surfaces a "p10 / p50 / p90" band, and the consumer is gated on `apply_interval_models` returning a non-empty map so empty deployments keep the Poisson path.

## Open design decisions you'll have to make

The handoff lists five (CDF distribution choice, manifest versioning, training window, stat-key allowlist, manifest update sequence). For each, write your decision into the PR description with a one-line rationale. Don't ask the user unless the trade-off is genuinely 50/50 — pick the conservative option and surface the choice in the PR.

## Sanity-check commands

- `cd apps/api && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/ml && /Users/chris/Workspace/locked-in/github/sika/.venv/bin/python3 -m pytest --tb=short -q`
- `cd apps/web && npx tsc --noEmit && npx vitest run`

Run these before every commit. Run codex review before every push. Run the PR through GitHub before moving to the next phase.

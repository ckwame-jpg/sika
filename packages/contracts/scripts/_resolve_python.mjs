// Bug #39 — shared python resolver for ``dump`` + ``check`` scripts.
//
// Both the OpenAPI dump and the drift check need a Python interpreter
// with apps/api dependencies installed. Hardcoding ``../../.venv/bin/
// python`` worked for the canonical layout but broke in git worktrees
// (the venv lives in the parent checkout, not the worktree) and any
// CI / deploy that didn't put the venv at the literal default path.
//
// Resolution chain, first match wins:
//
//   1. ``SIKA_PYTHON_BIN`` env var — explicit operator override.
//   2. Walk up from ``startDir`` looking for ``.venv/bin/python``.
//      Catches both the in-tree case and the git-worktree case
//      (worktree → ``.claude/worktrees`` → ``.claude`` → main repo
//      where the venv typically lives).
//   3. ``python3`` from PATH — last-resort. Likely fails with a
//      ``ModuleNotFoundError`` for ``fastapi``, but that's a clearer
//      operator signal than a Node ``ENOENT`` on the binary path.

import { existsSync } from "node:fs";
import path from "node:path";

export function resolvePythonBin(startDir) {
  if (process.env.SIKA_PYTHON_BIN) {
    return process.env.SIKA_PYTHON_BIN;
  }
  let current = startDir;
  // Bound the climb so a misconfigured invocation can't walk to ``/``.
  for (let depth = 0; depth < 6; depth += 1) {
    const candidate = path.join(current, ".venv", "bin", "python");
    if (existsSync(candidate)) return candidate;
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return "python3";
}

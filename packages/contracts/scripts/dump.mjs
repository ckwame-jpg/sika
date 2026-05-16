#!/usr/bin/env node
// Bug #39 — wrapper around ``dump_openapi.py`` that picks a portable
// python (env override → ancestor venv walk → ``python3`` fallback)
// instead of the previous ``../../.venv/bin/python`` hardcode that
// failed in git worktrees and CI.
//
// First positional argument is the output path; defaults to
// ``packages/contracts/openapi.json`` (the dump script's own default).

import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { resolvePythonBin } from "./_resolve_python.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(here, "..");

const pythonBin = resolvePythonBin(packageRoot);
const scriptArgs = [path.join(here, "dump_openapi.py"), ...process.argv.slice(2)];

try {
  execFileSync(pythonBin, scriptArgs, { stdio: "inherit" });
} catch (err) {
  if (err && err.code === "ENOENT") {
    console.error(
      `Could not run python at ${pythonBin}. Set SIKA_PYTHON_BIN to a Python interpreter that has apps/api dependencies installed (typically the project's .venv) and re-run.`,
    );
  }
  process.exit(typeof err.status === "number" ? err.status : 1);
}

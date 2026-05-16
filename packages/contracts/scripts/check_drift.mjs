#!/usr/bin/env node
/**
 * Regenerates the OpenAPI spec + TS types into a temp location, then
 * diffs against the committed versions. Exits non-zero if they differ.
 *
 * Intended for CI: prevents the committed contract from drifting out of
 * sync with the FastAPI app definition.
 */

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { resolvePythonBin } from "./_resolve_python.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(here, "..");

const committedSpec = path.join(packageRoot, "openapi.json");
const committedTypes = path.join(packageRoot, "generated", "api.d.ts");

const tmpDir = mkdtempSync(path.join(tmpdir(), "sika-contracts-"));
const tmpSpec = path.join(tmpDir, "openapi.json");
const tmpTypes = path.join(tmpDir, "api.d.ts");

// Bug #39 — the previous version pinned ``pythonBin`` to
// ``<repoRoot>/.venv/bin/python`` and called ``execFileSync`` against
// that path unconditionally. In a git worktree (or any deploy without
// a per-checkout venv) the binary doesn't exist, so the script crashed
// with a Node ``ENOENT`` stack trace BEFORE comparing anything —
// exactly the failure mode the punch list called out. The resolver in
// ``_resolve_python.mjs`` walks up from ``packageRoot`` looking for
// the venv (and honors ``SIKA_PYTHON_BIN`` for CI overrides) so the
// worktree case finds the parent-repo venv automatically.
const pythonBin = resolvePythonBin(packageRoot);

try {
  try {
    execFileSync(
      pythonBin,
      [path.join(packageRoot, "scripts", "dump_openapi.py"), tmpSpec],
      { stdio: "inherit" },
    );
  } catch (err) {
    if (err && err.code === "ENOENT") {
      console.error(
        `Could not run python at ${pythonBin}. Set SIKA_PYTHON_BIN to a Python interpreter that has apps/api dependencies installed (typically the project's .venv) and re-run.`,
      );
    }
    throw err;
  }
  execFileSync(
    "npx",
    ["openapi-typescript", tmpSpec, "--output", tmpTypes],
    { stdio: "inherit", cwd: packageRoot },
  );

  const freshSpec = readFileSync(tmpSpec, "utf-8");
  const freshTypes = readFileSync(tmpTypes, "utf-8");
  const oldSpec = readFileSync(committedSpec, "utf-8");
  const oldTypes = readFileSync(committedTypes, "utf-8");

  const specDrift = freshSpec !== oldSpec;
  const typesDrift = freshTypes !== oldTypes;

  if (specDrift || typesDrift) {
    console.error("CONTRACT DRIFT DETECTED");
    if (specDrift) {
      console.error("  packages/contracts/openapi.json is stale");
    }
    if (typesDrift) {
      console.error("  packages/contracts/generated/api.d.ts is stale");
    }
    console.error("Run `npm run contracts:generate` and commit the result.");
    process.exit(1);
  }

  console.log("contracts in sync with apps/api");
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}

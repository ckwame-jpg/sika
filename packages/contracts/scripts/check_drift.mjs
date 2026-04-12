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

const here = path.dirname(fileURLToPath(import.meta.url));
const packageRoot = path.resolve(here, "..");
const repoRoot = path.resolve(packageRoot, "..", "..");

const committedSpec = path.join(packageRoot, "openapi.json");
const committedTypes = path.join(packageRoot, "generated", "api.d.ts");

const tmpDir = mkdtempSync(path.join(tmpdir(), "sika-contracts-"));
const tmpSpec = path.join(tmpDir, "openapi.json");
const tmpTypes = path.join(tmpDir, "api.d.ts");

const pythonBin = path.join(repoRoot, ".venv", "bin", "python");

try {
  execFileSync(
    pythonBin,
    [path.join(packageRoot, "scripts", "dump_openapi.py"), tmpSpec],
    { stdio: "inherit" },
  );
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

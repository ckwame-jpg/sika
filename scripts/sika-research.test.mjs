import test from "node:test";
import assert from "node:assert/strict";

import { formatPlainResponse, parseArgs, runResearchCommand } from "./sika-research.mjs";

function createStream() {
  let output = "";
  return {
    write(chunk) {
      output += String(chunk);
    },
    read() {
      return output;
    },
  };
}

test("parseArgs handles research flags", () => {
  const parsed = parseArgs([
    "--sport",
    "NBA",
    "--season",
    "2025",
    "--internal-only",
    "--json",
    "Summarize",
    "the",
    "board",
  ]);

  assert.deepEqual(parsed, {
    includeWeb: false,
    json: true,
    sportKey: "NBA",
    season: 2025,
    message: "Summarize the board",
    help: false,
  });
});

test("formatPlainResponse prints numbered citations", () => {
  const output = formatPlainResponse({
    message: "Topline",
    citations: [
      { title: "Source One", url: "https://example.com/one" },
      { title: "Source Two", url: "https://example.com/two" },
    ],
  });

  assert.match(output, /Topline/);
  assert.match(output, /1\. Source One/);
  assert.match(output, /https:\/\/example.com\/two/);
});

test("runResearchCommand posts admin-authenticated JSON and prints plain output", async () => {
  const stdout = createStream();
  const stderr = createStream();
  const calls = [];
  const exitCode = await runResearchCommand(
    ["--sport", "NBA", "--internal-only", "What", "changed", "today?"],
    {
      env: {
        SIKA_ADMIN_TOKEN: "secret",
        SIKA_API_BASE_URL: "https://sika.example.com/",
      },
      fetchImpl: async (url, init) => {
        calls.push({ url, init });
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              message: "Read-only answer",
              citations: [{ title: "Source One", url: "https://example.com/one" }],
            };
          },
        };
      },
      stdout,
      stderr,
    },
  );

  assert.equal(exitCode, 0);
  assert.equal(stderr.read(), "");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://sika.example.com/ops/research/query");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    message: "What changed today?",
    sport_key: "NBA",
    include_web: false,
  });
  assert.equal(calls[0].init.headers["X-Sika-Admin-Token"], "secret");
  assert.match(stdout.read(), /Read-only answer/);
  assert.match(stdout.read(), /Sources:/);
});

test("runResearchCommand fails clearly when admin token is missing", async () => {
  const stdout = createStream();
  const stderr = createStream();
  const exitCode = await runResearchCommand(["What", "changed?"], {
    env: {},
    stdout,
    stderr,
  });

  assert.equal(exitCode, 1);
  assert.equal(stdout.read(), "");
  assert.match(stderr.read(), /SIKA_ADMIN_TOKEN is required/);
});

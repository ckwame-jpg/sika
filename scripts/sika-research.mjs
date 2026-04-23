#!/usr/bin/env node

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

function printUsage(stream = process.stderr) {
  stream.write(
    [
      "Usage: sika research [options] <question>",
      "",
      "Options:",
      "  --sport <key>       Optional sport key such as NBA or MLB",
      "  --season <year>     Optional season override for stats-backed questions",
      "  --internal-only     Disable live web search and use Sika context only",
      "  --json              Print the raw JSON response",
      "  -h, --help          Show this help",
      "",
    ].join("\n"),
  );
}

export function parseArgs(argv) {
  const options = {
    includeWeb: true,
    json: false,
    sportKey: undefined,
    season: undefined,
    message: "",
    help: false,
  };
  const messageParts = [];
  let index = 0;

  while (index < argv.length) {
    const arg = argv[index];
    if (arg === "--") {
      messageParts.push(...argv.slice(index + 1));
      break;
    }
    if (arg === "--sport") {
      index += 1;
      const sportKey = argv[index];
      if (!sportKey) {
        throw new Error("Missing value for --sport");
      }
      options.sportKey = sportKey;
      index += 1;
      continue;
    }
    if (arg === "--season") {
      index += 1;
      const rawSeason = argv[index];
      if (!rawSeason) {
        throw new Error("Missing value for --season");
      }
      const season = Number.parseInt(rawSeason, 10);
      if (!Number.isInteger(season)) {
        throw new Error(`Invalid season: ${rawSeason}`);
      }
      options.season = season;
      index += 1;
      continue;
    }
    if (arg === "--internal-only") {
      options.includeWeb = false;
      index += 1;
      continue;
    }
    if (arg === "--json") {
      options.json = true;
      index += 1;
      continue;
    }
    if (arg === "-h" || arg === "--help") {
      options.help = true;
      index += 1;
      continue;
    }
    if (arg.startsWith("-")) {
      throw new Error(`Unknown option: ${arg}`);
    }
    messageParts.push(arg);
    index += 1;
  }

  options.message = messageParts.join(" ").trim();
  return options;
}

export function formatPlainResponse(payload) {
  const lines = [payload.message];
  if (payload.citations?.length) {
    lines.push("", "Sources:");
    for (const [index, citation] of payload.citations.entries()) {
      lines.push(`${index + 1}. ${citation.title}`);
      lines.push(`   ${citation.url}`);
    }
  }
  return `${lines.join("\n")}\n`;
}

export async function runResearchCommand(argv, io = {}) {
  const {
    env = process.env,
    fetchImpl = globalThis.fetch,
    stdout = process.stdout,
    stderr = process.stderr,
  } = io;

  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    stderr.write(`${error instanceof Error ? error.message : String(error)}\n\n`);
    printUsage(stderr);
    return 1;
  }

  if (options.help) {
    printUsage(stdout);
    return 0;
  }

  if (!options.message) {
    stderr.write("A research question is required.\n\n");
    printUsage(stderr);
    return 1;
  }

  const adminToken = env.SIKA_ADMIN_TOKEN;
  if (!adminToken) {
    stderr.write("SIKA_ADMIN_TOKEN is required for `sika research`.\n");
    return 1;
  }
  if (typeof fetchImpl !== "function") {
    stderr.write("Fetch is unavailable in this Node runtime.\n");
    return 1;
  }

  const apiBaseUrl = (env.SIKA_API_BASE_URL || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
  const payload = {
    message: options.message,
    include_web: options.includeWeb,
  };
  if (options.sportKey) {
    payload.sport_key = options.sportKey;
  }
  if (options.season != null) {
    payload.season = options.season;
  }

  let response;
  try {
    response = await fetchImpl(`${apiBaseUrl}/ops/research/query`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Sika-Admin-Token": adminToken,
      },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    stderr.write(`Research request failed: ${error instanceof Error ? error.message : String(error)}\n`);
    return 1;
  }

  let responseBody;
  try {
    responseBody = await response.json();
  } catch {
    responseBody = null;
  }

  if (!response.ok) {
    const detail =
      responseBody && typeof responseBody === "object" && "detail" in responseBody
        ? responseBody.detail
        : null;
    stderr.write(`Research request failed: ${response.status}${detail ? ` ${detail}` : ""}\n`);
    return 1;
  }

  if (!responseBody || typeof responseBody !== "object") {
    stderr.write("Research response failed: expected JSON object payload.\n");
    return 1;
  }

  stdout.write(
    options.json
      ? `${JSON.stringify(responseBody, null, 2)}\n`
      : formatPlainResponse(responseBody),
  );
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const exitCode = await runResearchCommand(process.argv.slice(2));
  if (exitCode !== 0) {
    process.exitCode = exitCode;
  }
}

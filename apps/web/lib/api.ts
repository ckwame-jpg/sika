import type { Schema } from "@kalshi-sports-copilot/contracts";

import type {
  DemoOrderCreate,
  DemoOrderRead,
  EventRead,
  HealthResponse,
  JobRefreshResponse,
  MarketDetailRead,
  MarketHistoryRead,
  ModelFamilyReadinessRead,
  ModelReadinessSettingsUpdate,
  ModelReadinessSummaryRead,
  PaperPositionCreate,
  PaperPositionExit,
  PaperPositionRead,
  ParlayPredictionRead,
  ParlayPredictionSummaryRead,
  PositionsRead,
  PredictionRead,
  PredictionSettlementResponse,
  PredictionSummaryRead,
  RefreshJobRead,
  RunDetailRead,
  RunRead,
  StatsQueryRead,
  TeamHistoryRead,
  TradeDeskResponse,
} from "./types";

// Slice 4: new product metadata endpoints source their types directly
// from the generated OpenAPI contract rather than the hand-written
// ``apps/web/lib/types.ts``. Surfaces are migrated to generated types
// one-by-one; ``lib/types.ts`` will be removed once every surface
// has moved.
export type ProductFreshnessResponse = Schema<"ProductFreshnessResponse">;
export type ProductScopeFreshnessRead = Schema<"ProductScopeFreshnessRead">;

const BASE = "/api";

async function request<T>(
  path: string,
  init?: RequestInit & { noRetry?: boolean },
): Promise<T> {
  // Bug #6, codex round-15 P2 on PR #40: callers can opt out of the
  // GET retry behavior. Used by ``fetchPositions({ force: true })``
  // — each retry would call ``/positions?force=true`` again, which
  // ``expire_kalshi_account_cache`` the freshly-populated cache and
  // trigger another Kalshi fetch. From one user click that could be
  // 3× upstream calls; opt out so a single click is a single fetch.
  const { noRetry, ...fetchInit } = init ?? {};
  const maxAttempts =
    noRetry || (fetchInit.method && fetchInit.method !== "GET") ? 1 : 3;
  const delays = [1000, 2000, 4000];
  let lastError: Error | undefined;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15_000);
      const res = await fetch(`${BASE}${path}`, {
        headers: { "Content-Type": "application/json", ...fetchInit.headers },
        ...fetchInit,
        signal: fetchInit.signal ?? controller.signal,
      });
      clearTimeout(timeout);

      if (!res.ok) {
        const raw = await res.text().catch(() => res.statusText);
        const text = raw.length > 240 ? `${raw.slice(0, 237)}...` : raw;
        const err = new Error(`${res.status} ${text}`) as Error & { status?: number };
        err.status = res.status;
        if (res.status >= 500 && attempt < maxAttempts - 1) {
          lastError = err;
          await new Promise((resolve) => setTimeout(resolve, delays[attempt]));
          continue;
        }
        throw err;
      }
      return res.json() as Promise<T>;
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
      const isNetworkError =
        lastError.name === "AbortError" ||
        lastError.name === "TypeError" ||
        lastError.message.includes("fetch");
      if (isNetworkError && attempt < maxAttempts - 1) {
        await new Promise((resolve) => setTimeout(resolve, delays[attempt]));
        continue;
      }
      throw lastError;
    }
  }

  throw lastError ?? new Error("Request failed");
}

export const fetchHealth = () => request<HealthResponse>("/health");
export const fetchProductFreshness = () =>
  request<ProductFreshnessResponse>("/product/freshness");
export const fetchTradeDesk = (sport?: string) => {
  const params = new URLSearchParams();
  if (sport) params.set("sport", sport);
  const qs = params.toString();
  return request<TradeDeskResponse>(`/trade-desk${qs ? `?${qs}` : ""}`);
};

export const fetchEvents = (sport?: string, day?: string) => {
  const params = new URLSearchParams();
  if (sport) params.set("sport", sport);
  if (day) params.set("day", day);
  const qs = params.toString();
  return request<EventRead[]>(`/events${qs ? `?${qs}` : ""}`);
};

export const fetchPositions = (options?: { force?: boolean }) =>
  // Codex round-15 P2 on PR #40: pass ``noRetry`` when forcing so a
  // 15 s client timeout doesn't lead to retries that each ``expire``
  // the cache and trigger another Kalshi fetch.
  options?.force
    ? request<PositionsRead>("/positions?force=true", { noRetry: true })
    : request<PositionsRead>("/positions");

export const openPaperPosition = (body: PaperPositionCreate) =>
  request<PaperPositionRead>("/paper-positions", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const exitPaperPosition = (id: number, body: PaperPositionExit) =>
  request<PaperPositionRead>(`/paper-positions/${id}/exit`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const submitDemoOrder = (body: DemoOrderCreate) =>
  request<DemoOrderRead>("/demo-orders", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const cancelDemoOrder = (id: number) =>
  request<DemoOrderRead>(`/demo-orders/${id}/cancel`, { method: "POST" });

export const fetchMarket = (ticker: string) =>
  request<MarketDetailRead>(`/markets/${encodeURIComponent(ticker)}`);

export const fetchMarketHistory = (ticker: string, range = "1D") =>
  request<MarketHistoryRead>(
    `/markets/${encodeURIComponent(ticker)}/history?range=${range}`,
  );

export const fetchRuns = (limit = 10) =>
  request<RunRead[]>(`/ops/runs?limit=${limit}`);

export const fetchRun = (id: number) =>
  request<RunDetailRead>(`/ops/runs/${id}`);

export const triggerRefresh = () =>
  request<JobRefreshResponse>(
    "/ops/jobs/refresh",
    { method: "POST" },
  );

export const fetchRefreshJob = (id: number) =>
  request<RefreshJobRead>(`/ops/jobs/${id}`);

export const fetchPredictions = (
  options: {
    sport?: string;
    market_family?: string;
    stat_key?: string;
    outcome?: string;
    captured_from?: string;
    captured_to?: string;
    limit?: number;
  } = {},
) => {
  const params = new URLSearchParams();
  if (options.sport) params.set("sport", options.sport);
  if (options.market_family) params.set("market_family", options.market_family);
  if (options.stat_key) params.set("stat_key", options.stat_key);
  if (options.outcome) params.set("outcome", options.outcome);
  if (options.captured_from) params.set("captured_from", options.captured_from);
  if (options.captured_to) params.set("captured_to", options.captured_to);
  if (options.limit) params.set("limit", String(options.limit));
  const qs = params.toString();
  return request<PredictionRead[]>(`/predictions${qs ? `?${qs}` : ""}`);
};

export const fetchPredictionSummary = (
  options: {
    sport?: string;
    market_family?: string;
    stat_key?: string;
    outcome?: string;
    captured_from?: string;
    captured_to?: string;
  } = {},
) => {
  const params = new URLSearchParams();
  if (options.sport) params.set("sport", options.sport);
  if (options.market_family) params.set("market_family", options.market_family);
  if (options.stat_key) params.set("stat_key", options.stat_key);
  if (options.outcome) params.set("outcome", options.outcome);
  if (options.captured_from) params.set("captured_from", options.captured_from);
  if (options.captured_to) params.set("captured_to", options.captured_to);
  const qs = params.toString();
  return request<PredictionSummaryRead>(`/predictions/summary${qs ? `?${qs}` : ""}`);
};

export const fetchModelReadinessSummary = () =>
  request<ModelReadinessSummaryRead>("/ops/models/readiness");

export const fetchModelReadinessDetail = (familyKey: string) =>
  request<ModelFamilyReadinessRead>(`/ops/models/readiness/${encodeURIComponent(familyKey)}`);

export const updateModelReadinessSettings = (body: ModelReadinessSettingsUpdate) =>
  request<ModelReadinessSummaryRead>("/ops/models/readiness/settings", {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const fetchParlayPredictions = (sportScope = "all", legCount?: number, limit = 100) => {
  const params = new URLSearchParams({ sport_scope: sportScope, limit: String(limit) });
  if (legCount != null) params.set("leg_count", String(legCount));
  return request<ParlayPredictionRead[]>(`/parlays/predictions?${params}`);
};

export const fetchParlayPredictionSummary = (sportScope = "all", legCount?: number) => {
  const params = new URLSearchParams({ sport_scope: sportScope });
  if (legCount != null) params.set("leg_count", String(legCount));
  return request<ParlayPredictionSummaryRead>(`/parlays/predictions/summary?${params}`);
};

export const triggerPredictionSettlement = () =>
  request<PredictionSettlementResponse>("/ops/jobs/settle-predictions", {
    method: "POST",
  });

export const queryStats = (body: {
  question: string;
  sport_key: string;
  season?: number;
  /** Codex round-2 P2 on PR #24: passthrough to the ESPN player
   *  search disambiguator (bug #13). Same-name players resolve to
   *  the right athlete instead of the first ESPN result. */
  team_hint?: string | null;
}) =>
  request<StatsQueryRead>("/research/stats/query", {
    method: "POST",
    body: JSON.stringify(body),
  });

/**
 * Convenience wrapper around the natural-language stats endpoint, used by
 * the trade-ticket pick-history strip. Phrasing matches the regex parser in
 * apps/api/app/services/stats_query.py:25 ("X's last N games" plus the
 * "home" / "away" / "vs opponent" tokens that the parser strips into filter
 * slots before pattern-matching).
 */
export interface PickHistoryOptions {
  opponent?: string | null;
  location?: "home" | "away" | null;
  /** Codex round-2 P2 on PR #24: forwarded to ``StatsQueryRequest.team_hint``
   *  so same-name player props (e.g. two "John Smith"s on different
   *  teams) resolve to the picked athlete instead of the first ESPN
   *  result. ``PickHistoryStrip`` sets this from
   *  ``selection.subjectTeam``. */
  teamHint?: string | null;
}

function buildPlayerHistoryQuestion(
  subjectName: string,
  n: number,
  opts: PickHistoryOptions = {},
): string {
  const locationToken =
    opts.location === "home" ? " home" : opts.location === "away" ? " away" : "";
  const opponentToken = opts.opponent ? ` vs ${opts.opponent}` : "";
  return `${subjectName}'s last ${n}${locationToken} games${opponentToken}`;
}

export const fetchPlayerHistory = (
  subjectName: string,
  sportKey: string,
  n = 5,
  opts: PickHistoryOptions = {},
) =>
  queryStats({
    question: buildPlayerHistoryQuestion(subjectName, n, opts),
    sport_key: sportKey.toUpperCase(),
    team_hint: opts.teamHint ?? null,
  });

export const fetchTeamHistory = (
  teamName: string,
  sportKey: string,
  n = 5,
  opts: PickHistoryOptions = {},
) =>
  request<TeamHistoryRead>("/research/teams/history", {
    method: "POST",
    body: JSON.stringify({
      team_name: teamName,
      sport_key: sportKey.toUpperCase(),
      n,
      opponent: opts.opponent ?? null,
      location: opts.location ?? null,
    }),
  });

export const keys = {
  health: "/health",
  sports: "/sports",
  sportAvailability: "/sports/availability",
  productSports: "/product/sports",
  productFreshness: "/product/freshness",
  tradeDesk: (sport?: string) =>
    `/trade-desk?sport=${sport ?? ""}`,
  events: (sport?: string, day?: string) =>
    `/events?sport=${sport ?? ""}&day=${day ?? ""}`,
  watchlistDiagnostics: "/ops/watchlist/diagnostics",
  positions: "/positions",
  market: (ticker: string) => `/markets/${ticker}`,
  marketHistory: (ticker: string, range: string) =>
    `/markets/${ticker}/history?range=${range}`,
  runs: "/ops/runs",
  run: (id: number) => `/ops/runs/${id}`,
  refreshJob: (id: number) => `/ops/jobs/${id}`,
  predictions: (args?: Record<string, string | number | undefined>) =>
    `/predictions?${new URLSearchParams(
      Object.entries(args ?? {}).flatMap(([key, value]) =>
        value == null || value === "" ? [] : [[key, String(value)]],
      ),
    ).toString()}`,
  predictionSummary: (args?: Record<string, string | number | undefined>) =>
    `/predictions/summary?${new URLSearchParams(
      Object.entries(args ?? {}).flatMap(([key, value]) =>
        value == null || value === "" ? [] : [[key, String(value)]],
      ),
    ).toString()}`,
  modelReadinessSummary: "/ops/models/readiness",
  modelReadinessDetail: (familyKey: string) => `/ops/models/readiness/${familyKey}`,
  parlayPredictions: (sportScope = "all", legCount?: number, limit = 100) =>
    `/parlays/predictions?sport_scope=${sportScope}&leg_count=${legCount ?? ""}&limit=${limit}`,
  parlayPredictionSummary: (sportScope = "all", legCount?: number) =>
    `/parlays/predictions/summary?sport_scope=${sportScope}&leg_count=${legCount ?? ""}`,
  playerHistory: (
    subjectName: string,
    sportKey: string,
    n = 5,
    opts: PickHistoryOptions = {},
  ) =>
    // Codex round-2 P2 on PR #24: include teamHint so two same-name
    // picks (different teams) get distinct SWR cache entries.
    `pick-history:player:${sportKey.toUpperCase()}:${subjectName}:${n}:${opts.location ?? ""}:${opts.opponent ?? ""}:${opts.teamHint ?? ""}`,
  teamHistory: (
    teamName: string,
    sportKey: string,
    n = 5,
    opts: PickHistoryOptions = {},
  ) =>
    `pick-history:team:${sportKey.toUpperCase()}:${teamName}:${n}:${opts.location ?? ""}:${opts.opponent ?? ""}`,
} as const;

import type {
  DemoOrderCreate,
  DemoOrderRead,
  EventRead,
  HealthResponse,
  JobRefreshResponse,
  MarketDetailRead,
  MarketHistoryRead,
  MarketListRead,
  ModelFamilyReadinessRead,
  ModelReadinessSummaryRead,
  PaperPositionCreate,
  PaperPositionExit,
  PaperPositionRead,
  ParlayPredictionRead,
  ParlayPredictionSummaryRead,
  ParlayRecommendationRead,
  PositionsRead,
  PredictionRead,
  PredictionSettlementResponse,
  PredictionSummaryRead,
  RecommendationRead,
  RunDetailRead,
  RunRead,
  SportRead,
  StatsQueryRead,
  WatchlistDiagnosticsRead,
  WatchlistCoverageRowRead,
} from "./types";

const BASE = "/api";

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${text}`);
  }
  return res.json() as Promise<T>;
}

export const fetchHealth = () => request<HealthResponse>("/health");
export const fetchSports = () => request<SportRead[]>("/sports");

export const fetchEvents = (sport?: string, day?: string) => {
  const params = new URLSearchParams();
  if (sport) params.set("sport", sport);
  if (day) params.set("day", day);
  const qs = params.toString();
  return request<EventRead[]>(`/events${qs ? `?${qs}` : ""}`);
};

export const fetchWatchlist = (sport?: string, limit = 50) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (sport) params.set("sport", sport);
  return request<RecommendationRead[]>(`/watchlist?${params}`);
};

export const fetchWatchlistCoverage = (sport?: string, limit = 250) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (sport) params.set("sport", sport);
  return request<WatchlistCoverageRowRead[]>(`/watchlist/coverage?${params}`);
};

export const fetchWatchlistDiagnostics = () =>
  request<WatchlistDiagnosticsRead>("/watchlist/diagnostics");

export const fetchParlayWatchlist = (sportScope = "all", legCount?: number, limit = 50) => {
  const params = new URLSearchParams({ sport_scope: sportScope, limit: String(limit) });
  if (legCount != null) params.set("leg_count", String(legCount));
  return request<ParlayRecommendationRead[]>(`/parlays/watchlist?${params}`);
};

export const fetchPositions = () => request<PositionsRead>("/positions");

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

export const fetchMarkets = (
  options: {
    sport?: string;
    family?: string;
    status?: string;
    search?: string;
    limit?: number;
  } = {},
) => {
  const params = new URLSearchParams();
  if (options.limit) params.set("limit", String(options.limit));
  if (options.sport) params.set("sport", options.sport);
  if (options.family) params.set("family", options.family);
  if (options.status) params.set("status", options.status);
  if (options.search) params.set("search", options.search);
  const qs = params.toString();
  return request<MarketListRead[]>(`/markets${qs ? `?${qs}` : ""}`);
};

export const fetchMarketHistory = (ticker: string, range = "1D") =>
  request<MarketHistoryRead>(
    `/markets/${encodeURIComponent(ticker)}/history?range=${range}`,
  );

export const fetchRuns = (limit = 10) =>
  request<RunRead[]>(`/runs?limit=${limit}`);

export const fetchRun = (id: number) =>
  request<RunDetailRead>(`/runs/${id}`);

export const triggerRefresh = () =>
  request<JobRefreshResponse>(
    "/jobs/refresh",
    { method: "POST" },
  );

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
  request<ModelReadinessSummaryRead>("/models/readiness");

export const fetchModelReadinessDetail = (familyKey: string) =>
  request<ModelFamilyReadinessRead>(`/models/readiness/${encodeURIComponent(familyKey)}`);

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
  request<PredictionSettlementResponse>("/jobs/settle-predictions", {
    method: "POST",
  });

export const queryStats = (body: {
  question: string;
  sport_key: string;
  season?: number;
}) =>
  request<StatsQueryRead>("/stats/query", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const keys = {
  health: "/health",
  sports: "/sports",
  events: (sport?: string, day?: string) =>
    `/events?sport=${sport ?? ""}&day=${day ?? ""}`,
  watchlist: (sport?: string, limit = 50) =>
    `/watchlist?sport=${sport ?? ""}&limit=${limit}`,
  watchlistCoverage: (sport?: string, limit = 250) =>
    `/watchlist/coverage?sport=${sport ?? ""}&limit=${limit}`,
  watchlistDiagnostics: "/watchlist/diagnostics",
  parlayWatchlist: (sportScope = "all", legCount?: number, limit = 50) =>
    `/parlays/watchlist?sport_scope=${sportScope}&leg_count=${legCount ?? ""}&limit=${limit}`,
  positions: "/positions",
  markets: (args?: Record<string, string | number | undefined>) =>
    `/markets?${new URLSearchParams(
      Object.entries(args ?? {}).flatMap(([key, value]) =>
        value == null || value === "" ? [] : [[key, String(value)]],
      ),
    ).toString()}`,
  market: (ticker: string) => `/markets/${ticker}`,
  marketHistory: (ticker: string, range: string) =>
    `/markets/${ticker}/history?range=${range}`,
  runs: "/runs",
  run: (id: number) => `/runs/${id}`,
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
  modelReadinessSummary: "/models/readiness",
  modelReadinessDetail: (familyKey: string) => `/models/readiness/${familyKey}`,
  parlayPredictions: (sportScope = "all", legCount?: number, limit = 100) =>
    `/parlays/predictions?sport_scope=${sportScope}&leg_count=${legCount ?? ""}&limit=${limit}`,
  parlayPredictionSummary: (sportScope = "all", legCount?: number) =>
    `/parlays/predictions/summary?sport_scope=${sportScope}&leg_count=${legCount ?? ""}`,
} as const;

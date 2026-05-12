import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { renderWithProviders } from "@/test/render";
import { coverOutcome, PickHistoryStrip, resolveStatValue } from "./pick-history-strip";
import type { TradeSelection } from "./trade-ticket";
import type {
  ModelReadinessSummaryRead,
  StatsQueryRead,
  TeamHistoryRead,
} from "@/lib/types";

const { mockFetchPlayerHistory, mockFetchTeamHistory, mockFetchSummary } = vi.hoisted(() => ({
  mockFetchPlayerHistory: vi.fn(),
  mockFetchTeamHistory: vi.fn(),
  mockFetchSummary: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchPlayerHistory: mockFetchPlayerHistory,
    fetchTeamHistory: mockFetchTeamHistory,
    fetchModelReadinessSummary: mockFetchSummary,
  };
});

function summaryFixture(n = 5): ModelReadinessSummaryRead {
  return {
    generated_at: "2026-05-11T00:00:00Z",
    ml_serving_mode: "heuristic",
    shadow_enabled: false,
    auto_promotion_enabled: false,
    min_settled_for_review: 40,
    min_shadow_coverage: 0.75,
    min_promotion_shadow_samples: 150,
    promotion_stability_days_required: 3,
    pick_history_default_n: n,
    families: [],
  };
}

function makePlayerSelection(overrides: Partial<TradeSelection> = {}): TradeSelection {
  return {
    kind: "player_prop",
    ticker: "TEST-MITCH-25",
    eventId: 1,
    marketTitle: "Donovan Mitchell: 25+ points",
    eventName: "Detroit Pistons at Cleveland Cavaliers",
    sportKey: "NBA",
    marketKind: "player_prop",
    displayLabel: "Donovan Mitchell 25+ points",
    projectedSideLabel: null,
    selectedSide: "yes",
    selectedSideProbability: 0.62,
    entryPrice: 0.55,
    edge: 0.27,
    confidence: 0.85,
    kalshiUrl: null,
    subjectName: "Donovan Mitchell",
    subjectTeam: "CLE",
    statKey: "points",
    threshold: 25,
    ...overrides,
  };
}

function makeGameLineSelection(overrides: Partial<TradeSelection> = {}): TradeSelection {
  return {
    kind: "game_line",
    ticker: "TEST-CLE-ML",
    eventId: 1,
    marketTitle: "Cleveland Cavaliers moneyline",
    eventName: "Detroit Pistons at Cleveland Cavaliers",
    sportKey: "NBA",
    marketKind: "moneyline",
    displayLabel: "Cavaliers ML",
    projectedSideLabel: null,
    selectedSide: "yes",
    selectedSideProbability: 0.7,
    entryPrice: 0.6,
    edge: 0.1,
    confidence: 0.8,
    kalshiUrl: null,
    numericLine: null,
    ...overrides,
  };
}

function playerHistoryFixture(metricsSeries: number[]): StatsQueryRead {
  return {
    question: "Donovan Mitchell's last 5 games",
    sport_key: "NBA",
    entity_name: "Donovan Mitchell",
    entity_id: "3908809",
    team_name: "Cleveland Cavaliers",
    query_type: "last_n_games",
    season: 2026,
    games_requested: 5,
    games_analyzed: metricsSeries.length,
    split: null,
    opponent: null,
    metric_labels: { points: "Points" },
    summary: {
      games: metricsSeries.length,
      wins: null,
      losses: null,
      draws: null,
      metrics: { points: metricsSeries[0] ?? null },
      stat_line: null,
      percentiles: {},
      metric_categories: {},
    } satisfies StatsQueryRead["summary"],
    game_logs: metricsSeries.map((value, index) => ({
      game_id: `g${index}`,
      game_date: `2026-05-0${9 - index}T19:00:00Z`,
      competition: null,
      team_name: "Cleveland Cavaliers",
      location: "home",
      opponent: "Detroit Pistons",
      opponent_abbreviation: "DET",
      result: "W",
      team_score: 110,
      opponent_score: 105,
      metrics: { points: value },
      stat_line: null,
    })) as StatsQueryRead["game_logs"],
    explanation: "",
    coverage_note: null,
    source: "espn_public",
  };
}

function teamHistoryFixture(
  results: { result: "W" | "L"; team: number; opp: number }[],
  teamName = "Cleveland Cavaliers",
): TeamHistoryRead {
  return {
    entity_id: "5",
    team_name: teamName,
    sport_key: "NBA",
    results: results.map((row, index) => ({
      game_date: `2026-05-0${9 - index}T19:00:00Z`,
      opponent: "Detroit Pistons",
      opponent_abbreviation: "DET",
      location: index % 2 === 0 ? "home" : "away",
      team_score: row.team,
      opp_score: row.opp,
      result: row.result,
    })),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFetchSummary.mockResolvedValue(summaryFixture(5));
});

describe("PickHistoryStrip — player prop", () => {
  it("renders bars + cleared-tally caption against the threshold", async () => {
    mockFetchPlayerHistory.mockResolvedValue(playerHistoryFixture([28, 19, 25, 31, 22]));
    renderWithProviders(<PickHistoryStrip selection={makePlayerSelection()} />);

    await waitFor(() => expect(screen.getByTestId("pick-history-strip")).toBeInTheDocument());
    expect(screen.getByText("3/5 cleared 25+")).toBeInTheDocument();
    expect(screen.getByTestId("mini-bars-bar-0")).toHaveAttribute("data-tone", "high");
    expect(screen.getByTestId("mini-bars-bar-1")).toHaveAttribute("data-tone", "low");
    expect(mockFetchPlayerHistory).toHaveBeenCalledWith("Donovan Mitchell", "NBA", 5, {
      location: null,
      opponent: null,
    });
  });

  it("hides itself when subjectName / statKey / threshold are missing", () => {
    const { container } = renderWithProviders(
      <PickHistoryStrip selection={makePlayerSelection({ subjectName: undefined })} />,
    );
    expect(container.firstChild).toBeNull();
    expect(mockFetchPlayerHistory).not.toHaveBeenCalled();
  });
});

describe("PickHistoryStrip — game line variants", () => {
  it("renders W/L pills for moneyline picks", async () => {
    mockFetchTeamHistory.mockResolvedValue(
      teamHistoryFixture([
        { result: "W", team: 116, opp: 109 },
        { result: "L", team: 97, opp: 107 },
        { result: "W", team: 121, opp: 111 },
        { result: "W", team: 105, opp: 99 },
        { result: "L", team: 88, opp: 102 },
      ]),
    );
    renderWithProviders(<PickHistoryStrip selection={makeGameLineSelection()} />);

    await waitFor(() => expect(screen.getByTestId("pick-history-strip-pills")).toBeInTheDocument());
    const pills = screen.getAllByText(/^[WL]$/);
    expect(pills).toHaveLength(5);
    expect(screen.getByText("3-2")).toBeInTheDocument();
  });

  it("renders a margin chart for spread picks with cover-aware coloring", async () => {
    mockFetchTeamHistory.mockResolvedValue(
      teamHistoryFixture([
        { result: "W", team: 116, opp: 109 },
        { result: "L", team: 97, opp: 107 },
        { result: "W", team: 121, opp: 111 },
        { result: "W", team: 105, opp: 99 },
        { result: "L", team: 88, opp: 102 },
      ]),
    );
    renderWithProviders(
      <PickHistoryStrip
        selection={makeGameLineSelection({
          marketKind: "spread",
          numericLine: -3.5, // Cavaliers -3.5
          selectedSide: "yes",
        })}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("pick-history-strip")).toBeInTheDocument());
    // margins: 7, -10, 10, 6, -14 → cover threshold = -(-3.5) = 3.5
    // covers: 7, 10, 6 → "high"; -10, -14 → "low"
    expect(screen.getByTestId("mini-bars-bar-0")).toHaveAttribute("data-tone", "high");
    expect(screen.getByTestId("mini-bars-bar-1")).toHaveAttribute("data-tone", "low");
    expect(screen.getByTestId("mini-bars-bar-2")).toHaveAttribute("data-tone", "high");
    expect(screen.getByTestId("mini-bars-bar-3")).toHaveAttribute("data-tone", "high");
    expect(screen.getByTestId("mini-bars-bar-4")).toHaveAttribute("data-tone", "low");
    expect(screen.getByText("3/5 covered")).toBeInTheDocument();
  });

  it("renders an event totals chart for total picks with over/under coloring", async () => {
    mockFetchTeamHistory.mockResolvedValue(
      teamHistoryFixture([
        { result: "W", team: 116, opp: 109 },
        { result: "L", team: 97, opp: 107 },
        { result: "W", team: 121, opp: 111 },
        { result: "W", team: 105, opp: 99 },
        { result: "L", team: 88, opp: 102 },
      ]),
    );
    renderWithProviders(
      <PickHistoryStrip
        selection={makeGameLineSelection({
          marketKind: "total",
          numericLine: 220.5,
          selectedSide: "yes", // over
        })}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("pick-history-strip")).toBeInTheDocument());
    // totals: 225, 204, 232, 204, 190 → over 220.5: indices 0, 2 → "high"
    expect(screen.getByTestId("mini-bars-bar-0")).toHaveAttribute("data-tone", "high");
    expect(screen.getByTestId("mini-bars-bar-1")).toHaveAttribute("data-tone", "low");
    expect(screen.getByTestId("mini-bars-bar-2")).toHaveAttribute("data-tone", "high");
    expect(screen.getByText("2/5 over")).toBeInTheDocument();
  });

  it("falls back to pills when a spread pick has no numeric line", async () => {
    mockFetchTeamHistory.mockResolvedValue(
      teamHistoryFixture([
        { result: "W", team: 116, opp: 109 },
        { result: "L", team: 97, opp: 107 },
      ]),
    );
    renderWithProviders(
      <PickHistoryStrip
        selection={makeGameLineSelection({ marketKind: "spread", numericLine: null })}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("pick-history-strip-pills")).toBeInTheDocument());
  });

  it("hides itself when the team name can't be inferred", () => {
    const { container } = renderWithProviders(
      <PickHistoryStrip
        selection={makeGameLineSelection({ marketTitle: "", displayLabel: "" })}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(mockFetchTeamHistory).not.toHaveBeenCalled();
  });
});

describe("PickHistoryStrip — N toggle + filters", () => {
  it("kicks off a second fetch when the operator picks a different N", async () => {
    mockFetchPlayerHistory.mockResolvedValue(playerHistoryFixture([28, 19, 25, 31, 22]));
    renderWithProviders(<PickHistoryStrip selection={makePlayerSelection()} />);

    await waitFor(() => expect(mockFetchPlayerHistory).toHaveBeenCalledTimes(1));

    const user = userEvent.setup();
    await user.click(screen.getByTestId("pick-history-strip-n-10"));

    await waitFor(() => expect(mockFetchPlayerHistory).toHaveBeenCalledTimes(2));
    expect(mockFetchPlayerHistory.mock.calls[1]).toEqual([
      "Donovan Mitchell",
      "NBA",
      10,
      { location: null, opponent: null },
    ]);
  });

  it("inherits the operator-wide default N from /ops/models/readiness", async () => {
    mockFetchSummary.mockResolvedValue(summaryFixture(20));
    mockFetchPlayerHistory.mockResolvedValue(playerHistoryFixture([28, 19, 25, 31, 22]));
    renderWithProviders(<PickHistoryStrip selection={makePlayerSelection()} />);

    // The summary loads asynchronously; the initial render falls back to N=5
    // until SWR resolves. We're verifying the steady-state default, so wait
    // for the operator-pinned fetch to fire.
    await waitFor(() => {
      const lastCall = mockFetchPlayerHistory.mock.calls.at(-1);
      expect(lastCall?.[2]).toBe(20);
    });
  });

  it("clicking the home filter re-fetches with location='home'", async () => {
    mockFetchTeamHistory.mockResolvedValue(
      teamHistoryFixture([{ result: "W", team: 116, opp: 109 }]),
    );
    renderWithProviders(<PickHistoryStrip selection={makeGameLineSelection()} />);

    await waitFor(() => expect(mockFetchTeamHistory).toHaveBeenCalledTimes(1));

    const user = userEvent.setup();
    await user.click(screen.getByTestId("pick-history-strip-filter-home"));

    await waitFor(() => expect(mockFetchTeamHistory).toHaveBeenCalledTimes(2));
    expect(mockFetchTeamHistory.mock.calls[1][3]).toEqual({ location: "home", opponent: null });
  });
});

describe("resolveStatValue — composite stat handling", () => {
  it("returns the direct value when the stat key exists in the metrics dict", () => {
    expect(resolveStatValue({ hits: 2, runs: 1, rbis: 3 }, "hits")).toBe(2);
    // total_bases contains underscore but is an atomic key — direct hit wins.
    expect(resolveStatValue({ total_bases: 4, hits: 2, runs: 1 }, "total_bases")).toBe(4);
  });

  it("sums atomic components when the composite key isn't emitted directly", () => {
    expect(resolveStatValue({ hits: 2, runs: 1, rbis: 3 }, "hits_runs_rbis")).toBe(6);
    expect(
      resolveStatValue({ points: 28, rebounds: 4, assists: 7 }, "points_rebounds_assists"),
    ).toBe(39);
  });

  it("returns null when any component of a composite is missing", () => {
    expect(resolveStatValue({ hits: 2, runs: 1 }, "hits_runs_rbis")).toBeNull();
    expect(resolveStatValue({ hits: 2 }, "home_runs_rbis")).toBeNull();
  });

  it("returns null on a missing atomic key and survives null metrics", () => {
    expect(resolveStatValue({ hits: 2 }, "unknown")).toBeNull();
    expect(resolveStatValue(null, "hits")).toBeNull();
    expect(resolveStatValue(undefined, "hits")).toBeNull();
  });

  it("ignores keys whose value is null or non-finite", () => {
    expect(resolveStatValue({ hits: null, runs: 1, rbis: 3 }, "hits_runs_rbis")).toBeNull();
    expect(resolveStatValue({ hits: NaN, runs: 1, rbis: 3 }, "hits_runs_rbis")).toBeNull();
  });
});

describe("coverOutcome — sign-correct cover/over coloring", () => {
  it("spread + yes returns high above threshold, low below, mid on push", () => {
    expect(coverOutcome(5, 3.5, "spread", "yes")).toBe("high");
    expect(coverOutcome(2, 3.5, "spread", "yes")).toBe("low");
    expect(coverOutcome(3.5, 3.5, "spread", "yes")).toBe("mid");
  });

  it("spread + no flips the comparison", () => {
    expect(coverOutcome(-5, -3.5, "spread", "no")).toBe("high");
    expect(coverOutcome(0, -3.5, "spread", "no")).toBe("low");
    expect(coverOutcome(-3.5, -3.5, "spread", "no")).toBe("mid");
  });

  it("total + yes treats high totals as covers; total + no inverts", () => {
    expect(coverOutcome(225, 220.5, "total", "yes")).toBe("high");
    expect(coverOutcome(210, 220.5, "total", "yes")).toBe("low");
    expect(coverOutcome(210, 220.5, "total", "no")).toBe("high");
    expect(coverOutcome(225, 220.5, "total", "no")).toBe("low");
    expect(coverOutcome(220.5, 220.5, "total", "yes")).toBe("mid");
  });
});

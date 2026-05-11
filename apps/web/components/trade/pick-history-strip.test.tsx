import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { renderWithProviders } from "@/test/render";
import { PickHistoryStrip } from "./pick-history-strip";
import type { TradeSelection } from "./trade-ticket";
import type { StatsQueryRead, TeamHistoryRead } from "@/lib/types";

const { mockFetchPlayerHistory, mockFetchTeamHistory } = vi.hoisted(() => ({
  mockFetchPlayerHistory: vi.fn(),
  mockFetchTeamHistory: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchPlayerHistory: mockFetchPlayerHistory,
    fetchTeamHistory: mockFetchTeamHistory,
  };
});

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

function teamHistoryFixture(results: { result: "W" | "L"; team: number; opp: number }[]): TeamHistoryRead {
  return {
    entity_id: "5",
    team_name: "Cleveland Cavaliers",
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

describe("PickHistoryStrip", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders 5 bars and a cleared-tally caption for a player prop", async () => {
    mockFetchPlayerHistory.mockResolvedValue(playerHistoryFixture([28, 19, 25, 31, 22]));

    renderWithProviders(<PickHistoryStrip selection={makePlayerSelection()} />);

    await waitFor(() => expect(screen.getByTestId("pick-history-strip")).toBeInTheDocument());

    expect(screen.getByText("3/5 cleared 25+")).toBeInTheDocument();
    // Bars 0 (28), 2 (25), 3 (31) hit; 1 (19), 4 (22) miss.
    expect(screen.getByTestId("mini-bars-bar-0")).toHaveAttribute("data-tone", "high");
    expect(screen.getByTestId("mini-bars-bar-1")).toHaveAttribute("data-tone", "low");
    expect(screen.getByTestId("mini-bars-bar-2")).toHaveAttribute("data-tone", "high");
    expect(screen.getByTestId("mini-bars-bar-4")).toHaveAttribute("data-tone", "low");
    expect(mockFetchPlayerHistory).toHaveBeenCalledWith("Donovan Mitchell", "NBA", 5);
  });

  it("renders 5 W/L pills for a game-line selection", async () => {
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
    expect(screen.getByText("116-109")).toBeInTheDocument();
    expect(mockFetchTeamHistory).toHaveBeenCalledWith("Cleveland Cavaliers", "NBA", 5);
  });

  it("renders nothing when a player prop lacks subjectName / statKey / threshold", () => {
    const { container } = renderWithProviders(
      <PickHistoryStrip selection={makePlayerSelection({ subjectName: undefined })} />,
    );
    expect(container.firstChild).toBeNull();
    expect(mockFetchPlayerHistory).not.toHaveBeenCalled();
  });

  it("renders nothing when a game-line selection has no parseable team", () => {
    const { container } = renderWithProviders(
      <PickHistoryStrip
        selection={makeGameLineSelection({ marketTitle: "", displayLabel: "" })}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(mockFetchTeamHistory).not.toHaveBeenCalled();
  });

  it("renders nothing when the player history response carries no values for the stat key", async () => {
    mockFetchPlayerHistory.mockResolvedValue(playerHistoryFixture([]));

    const { container } = renderWithProviders(<PickHistoryStrip selection={makePlayerSelection()} />);

    await waitFor(() => expect(mockFetchPlayerHistory).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });
});

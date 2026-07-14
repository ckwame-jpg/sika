import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// Mock the data-layer call BEFORE importing the component under test.
// keys/fetchHealth are consumed by the sources rail's useHealthStatus.
vi.mock("@/lib/api", () => ({
  queryStats: vi.fn(),
  fetchHealth: vi.fn().mockResolvedValue(null),
  keys: { health: "/health" },
}));

import { queryStats } from "@/lib/api";
import { StatsWorkspace } from "@/components/stats/stats-workspace";
import type { StatsQueryRead } from "@/lib/types";

const mockedQueryStats = queryStats as unknown as ReturnType<typeof vi.fn>;

function makeResult(overrides: Partial<StatsQueryRead> = {}): StatsQueryRead {
  return {
    entity_name: "Jalen Brunson",
    team_name: "NYK",
    sport_key: "NBA",
    source: "model v7.2",
    query_type: "recent_form",
    games_analyzed: 10,
    summary: {
      stat_line: "28.4 PTS · 7.1 AST · 3.3 3PM",
      metrics: { pts: 28.4, ast: 7.1, fg3m: 3.3 },
      games: 10,
      wins: 7,
      losses: 3,
      draws: 0,
      // Bug #40 phase 8 migration surfaced these — Wire<> requires
      // them on the wire (Pydantic emits {} default for both).
      percentiles: {},
      metric_categories: {},
    } as StatsQueryRead["summary"],
    metric_labels: { pts: "PTS", ast: "AST", fg3m: "3PM" },
    explanation: "Hot stretch; line 25.5 hit 7-of-10.",
    coverage_note: "Coverage: NBA logs through last night.",
    game_logs: [
      { game_id: "g1", game_date: "2025-03-10", opponent: "BOS", location: "away",
        team_score: 112, opponent_score: 108, result: "W",
        metrics: { pts: 31, ast: 8, fg3m: 4 } },
      { game_id: "g2", game_date: "2025-03-08", opponent: "PHI", location: "home",
        team_score: 118, opponent_score: 115, result: "W",
        metrics: { pts: 27, ast: 6, fg3m: 3 } },
      { game_id: "g3", game_date: "2025-03-06", opponent: "MIA", location: "away",
        team_score: 101, opponent_score: 107, result: "L",
        metrics: { pts: 24, ast: 9, fg3m: 2 } },
    ],
    ...overrides,
  } as StatsQueryRead;
}

describe("StatsWorkspace — Phase 2 sa-* rewrite", () => {
  beforeEach(() => {
    mockedQueryStats.mockReset();
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders the Stats Assistant header chrome", () => {
    render(<StatsWorkspace />);
    const card = screen.getByTestId("stats-assistant-card");
    expect(within(card).getByText("Stats Assistant")).toBeInTheDocument();
    expect(within(card).getByText("Research desk")).toBeInTheDocument();
    expect(within(card).getByText("/research/stats/query")).toBeInTheDocument();
    expect(within(card).getByText(/ready/i)).toBeInTheDocument();
  });

  it("shows exactly 5 suggestion chips", () => {
    render(<StatsWorkspace />);
    const chips = screen.getAllByTestId("sa-prompt");
    expect(chips).toHaveLength(5);
    expect(chips[0]).toHaveTextContent(/Jalen Brunson last 10 games/);
  });

  it("shows the empty orb before any query", () => {
    render(<StatsWorkspace />);
    expect(screen.getByTestId("sa-result-empty")).toBeInTheDocument();
    expect(screen.getByText(/Run a player stats query/i)).toBeInTheDocument();
  });

  it("offers only the shipped sports (NBA/NFL/MLB/WNBA/TENNIS)", () => {
    render(<StatsWorkspace />);
    const sport = screen.getByTestId("sa-sport") as HTMLSelectElement;
    const values = Array.from(sport.options).map((o) => o.value);
    // Smarter WNBA PR 3 enabled WNBA after wiring the /stats/query
    // backend branch. Soccer + UFC were removed from scope on
    // 2026-05-17.
    expect(values).toEqual(["NBA", "NFL", "MLB", "WNBA", "TENNIS"]);
  });

  it("clicking a suggestion chip fires the query and renders the answer", async () => {
    mockedQueryStats.mockResolvedValueOnce(makeResult());
    render(<StatsWorkspace />);
    fireEvent.click(screen.getAllByTestId("sa-prompt")[1]); // "Jayson Tatum this season"

    await waitFor(() => expect(mockedQueryStats).toHaveBeenCalledTimes(1));
    expect(mockedQueryStats).toHaveBeenCalledWith(
      expect.objectContaining({ question: "Jayson Tatum this season", sport_key: "NBA" }),
    );

    const answer = await screen.findByTestId("sa-answer");
    expect(within(answer).getByText(/ANSWER · NBA/)).toBeInTheDocument();
    expect(within(answer).getByText("Jalen Brunson")).toBeInTheDocument();
  });

  it("derives the chart from the first metric_labels key, not team_score", async () => {
    mockedQueryStats.mockResolvedValueOnce(makeResult());
    render(<StatsWorkspace />);
    fireEvent.click(screen.getByTestId("sa-run"));

    const chart = await screen.findByTestId("sa-answer-chart");
    // Primary key is "pts"; label reads "PTS · last 3"
    expect(within(chart).getByText(/PTS · last 3/)).toBeInTheDocument();
    // Bars render the per-game pts values (31, 27, 24), NOT team_scores.
    const svgText = chart.querySelector("svg")?.textContent ?? "";
    expect(svgText).toContain("31");
    expect(svgText).toContain("27");
    expect(svgText).toContain("24");
    expect(svgText).not.toContain("112"); // team_score must not leak through
  });

  it("skips the chart entirely when metric_labels is empty", async () => {
    mockedQueryStats.mockResolvedValueOnce(
      makeResult({ metric_labels: {}, summary: { stat_line: "—", metrics: {} } } as Partial<StatsQueryRead>),
    );
    render(<StatsWorkspace />);
    fireEvent.click(screen.getByTestId("sa-run"));

    await screen.findByTestId("sa-answer");
    expect(screen.queryByTestId("sa-answer-chart")).not.toBeInTheDocument();
  });

  it("skips the chart when there are no game_logs even if metric_labels has keys", async () => {
    mockedQueryStats.mockResolvedValueOnce(makeResult({ game_logs: [] }));
    render(<StatsWorkspace />);
    fireEvent.click(screen.getByTestId("sa-run"));

    await screen.findByTestId("sa-answer");
    expect(screen.queryByTestId("sa-answer-chart")).not.toBeInTheDocument();
  });

  it("surfaces the backend source in the answer footer", async () => {
    mockedQueryStats.mockResolvedValueOnce(makeResult({ source: "model v7.2" }));
    render(<StatsWorkspace />);
    fireEvent.click(screen.getByTestId("sa-run"));

    const answer = await screen.findByTestId("sa-answer");
    expect(within(answer).getAllByText(/model v7\.2/).length).toBeGreaterThan(0);
  });

  it("renders the loading state while queryStats is pending", async () => {
    let resolve!: (v: StatsQueryRead) => void;
    mockedQueryStats.mockImplementationOnce(
      () => new Promise<StatsQueryRead>((r) => { resolve = r; }),
    );
    render(<StatsWorkspace />);
    fireEvent.click(screen.getByTestId("sa-run"));

    expect(await screen.findByTestId("sa-result-loading")).toBeInTheDocument();
    resolve(makeResult());
    await screen.findByTestId("sa-answer");
  });

  it("swapping sport also swaps the question to that sport's first example (case-exact keys)", () => {
    render(<StatsWorkspace />);
    const sport = screen.getByTestId("sa-sport") as HTMLSelectElement;
    fireEvent.change(sport, { target: { value: "TENNIS" } });
    const input = screen.getByTestId("sa-input") as HTMLInputElement;
    expect(input.value).toBe("Novak Djokovic last 5 matches");
  });
});

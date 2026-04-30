import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModelReadinessPanel } from "@/components/predictions/model-readiness-panel";
import {
  activeStudyFamilyFixture,
  heuristicLaneFamilyFixture,
  modelReadinessSummaryFixture,
} from "@/test/fixtures/model-readiness-fixtures";
import { healthFixture } from "@/test/fixtures/trade-fixtures";
import { renderWithProviders } from "@/test/render";

const { mockFetchHealth, mockFetchModelReadinessSummary, mockFetchModelReadinessDetail, mockUpdateModelReadinessSettings } = vi.hoisted(() => ({
  mockFetchHealth: vi.fn(),
  mockFetchModelReadinessSummary: vi.fn(),
  mockFetchModelReadinessDetail: vi.fn(),
  mockUpdateModelReadinessSettings: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchHealth: mockFetchHealth,
    fetchModelReadinessSummary: mockFetchModelReadinessSummary,
    fetchModelReadinessDetail: mockFetchModelReadinessDetail,
    updateModelReadinessSettings: mockUpdateModelReadinessSettings,
  };
});

describe("ModelReadinessPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchHealth.mockResolvedValue(healthFixture);
  });

  it("distinguishes active study families from heuristic lanes while keeping runtime truth separate", async () => {
    mockFetchModelReadinessSummary.mockResolvedValue(modelReadinessSummaryFixture);
    mockFetchModelReadinessDetail.mockImplementation(async (familyKey: string) => (
      familyKey === heuristicLaneFamilyFixture.family_key ? heuristicLaneFamilyFixture : activeStudyFamilyFixture
    ));

    renderWithProviders(<ModelReadinessPanel />);

    expect((await screen.findAllByText("active study")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("heuristic lane").length).toBeGreaterThan(0);
    expect(screen.getAllByText("heuristic -> heuristic").length).toBeGreaterThan(0);
    expect(screen.getByText("Global Mode")).toBeInTheDocument();
    expect(screen.getByText("Shadow Capture")).toBeInTheDocument();
    expect(screen.getByText("Auto Promotion")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /shadow on/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /arm auto/i })).toBeInTheDocument();
    expect(screen.getByText("Promotion Samples")).toBeInTheDocument();
  });

  it("lets the operator enable shadow capture from the readiness panel", async () => {
    const user = userEvent.setup();
    mockFetchModelReadinessSummary.mockResolvedValue({
      ...modelReadinessSummaryFixture,
      ml_serving_mode: "heuristic",
      shadow_enabled: false,
      auto_promotion_enabled: false,
    });
    mockFetchModelReadinessDetail.mockResolvedValue(activeStudyFamilyFixture);
    mockUpdateModelReadinessSettings.mockResolvedValue({
      ...modelReadinessSummaryFixture,
      ml_serving_mode: "shadow",
      shadow_enabled: true,
      auto_promotion_enabled: false,
    });

    renderWithProviders(<ModelReadinessPanel />);

    await user.click(await screen.findByRole("button", { name: /enable shadow/i }));

    expect(mockUpdateModelReadinessSettings).toHaveBeenCalledWith({
      ml_serving_mode: "shadow",
      enqueue_shadow_backfill: true,
    });
  });

  it("shows settlement worker status when active study families have pending but no settled history", async () => {
    mockFetchModelReadinessSummary.mockResolvedValue({
      ...modelReadinessSummaryFixture,
      families: [
        {
          ...activeStudyFamilyFixture,
          settled_predictions: 0,
          pending_predictions: 42,
        },
        heuristicLaneFamilyFixture,
      ],
    });
    mockFetchModelReadinessDetail.mockResolvedValue({
      ...activeStudyFamilyFixture,
      settled_predictions: 0,
      pending_predictions: 42,
    });
    mockFetchHealth.mockResolvedValue({
      ...healthFixture,
      active_settlement_job: {
        id: 77,
        kind: "settlement",
        scope: "predictions",
        reason: "interval",
        status: "running",
        run_id: 91,
        error_message: null,
        details: {
          processed_so_far: 100,
          single_settlement_summary: { updated: 12 },
          parlay_settlement_summary: { updated: 2 },
        },
        queued_at: "2026-04-07T18:00:00Z",
        started_at: "2026-04-07T18:01:00Z",
        finished_at: null,
      },
    });

    renderWithProviders(<ModelReadinessPanel />);

    expect(await screen.findByTestId("model-settlement-status")).toHaveTextContent("42 pending predictions");
    expect(screen.getByTestId("model-settlement-status")).toHaveTextContent("running");
    expect(screen.getByTestId("model-settlement-status")).toHaveTextContent("updated 14");
  });

  it("defaults to the first active study family with captured prediction volume", async () => {
    const emptySingles = {
      ...activeStudyFamilyFixture,
      family_key: "nba_singles",
      label: "NBA singles",
      total_predictions: 0,
      settled_predictions: 0,
      pending_predictions: 0,
      coverage_predictions: 0,
      coverage_settled_predictions: 0,
      coverage_pending_predictions: 0,
      shadow_predictions: 0,
      shadow_backlog_predictions: 0,
      why_not_ready: "Only 0 settled predictions are available.",
      runtime: {
        ...activeStudyFamilyFixture.runtime,
        family_key: "nba_singles",
      },
    };
    mockFetchModelReadinessSummary.mockResolvedValue({
      ...modelReadinessSummaryFixture,
      families: [emptySingles, activeStudyFamilyFixture, heuristicLaneFamilyFixture],
    });
    mockFetchModelReadinessDetail.mockImplementation(async (familyKey: string) => (
      familyKey === "nba_singles" ? emptySingles : activeStudyFamilyFixture
    ));

    renderWithProviders(<ModelReadinessPanel />);

    await waitFor(() => expect(mockFetchModelReadinessDetail).toHaveBeenCalledWith("nba_props"));
    expect(screen.getByRole("heading", { name: "NBA props" })).toBeInTheDocument();
  });
});

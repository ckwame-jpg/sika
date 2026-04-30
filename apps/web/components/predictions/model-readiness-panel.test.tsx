import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModelReadinessPanel } from "@/components/predictions/model-readiness-panel";
import {
  activeStudyFamilyFixture,
  healthFixture,
  healthWithShadowBackfillQueued,
  heuristicLaneFamilyFixture,
  modelReadinessSummaryFixture,
} from "@/test/fixtures/model-readiness-fixtures";
import { renderWithProviders } from "@/test/render";

const { mockFetchModelReadinessSummary, mockFetchModelReadinessDetail, mockUpdateModelReadinessSettings, mockFetchHealth } = vi.hoisted(() => ({
  mockFetchModelReadinessSummary: vi.fn(),
  mockFetchModelReadinessDetail: vi.fn(),
  mockUpdateModelReadinessSettings: vi.fn(),
  mockFetchHealth: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchModelReadinessSummary: mockFetchModelReadinessSummary,
    fetchModelReadinessDetail: mockFetchModelReadinessDetail,
    updateModelReadinessSettings: mockUpdateModelReadinessSettings,
    fetchHealth: mockFetchHealth,
  };
});

describe("ModelReadinessPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetchHealth.mockResolvedValue(healthFixture);
  });

  it("uses readiness/calibration wording instead of active study and study ladder", async () => {
    mockFetchModelReadinessSummary.mockResolvedValue(modelReadinessSummaryFixture);
    mockFetchModelReadinessDetail.mockImplementation(async (familyKey: string) => (
      familyKey === heuristicLaneFamilyFixture.family_key ? heuristicLaneFamilyFixture : activeStudyFamilyFixture
    ));

    renderWithProviders(<ModelReadinessPanel />);

    expect((await screen.findAllByText("ML candidate")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("heuristic lane").length).toBeGreaterThan(0);
    expect(screen.getAllByText("heuristic -> heuristic").length).toBeGreaterThan(0);
    expect(screen.getByText(/Readiness path:/)).toBeInTheDocument();
    expect(screen.getByText(/calibrating/)).toBeInTheDocument();
    expect(screen.getByText(/ML live/)).toBeInTheDocument();
    expect(screen.queryByText("active study")).not.toBeInTheDocument();
    expect(screen.queryByText(/Study ladder/)).not.toBeInTheDocument();
    expect(screen.getByText("Global Mode")).toBeInTheDocument();
    expect(screen.getByText("Shadow Capture")).toBeInTheDocument();
    expect(screen.getByText("Auto Promotion")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /shadow on/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /arm auto/i })).toBeInTheDocument();
    expect(screen.getByText("Promotion Samples")).toBeInTheDocument();
  });

  it("renders shadow backfill status when backlog exists and coverage is low", async () => {
    mockFetchModelReadinessSummary.mockResolvedValue(modelReadinessSummaryFixture);
    mockFetchModelReadinessDetail.mockResolvedValue(activeStudyFamilyFixture);
    mockFetchHealth.mockResolvedValue(healthWithShadowBackfillQueued);

    renderWithProviders(<ModelReadinessPanel />);

    expect((await screen.findAllByText("shadow backfill queued")).length).toBeGreaterThan(0);
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
});

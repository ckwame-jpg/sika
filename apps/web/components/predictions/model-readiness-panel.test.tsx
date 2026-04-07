import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModelReadinessPanel } from "@/components/predictions/model-readiness-panel";
import {
  activeStudyFamilyFixture,
  heuristicLaneFamilyFixture,
  modelReadinessSummaryFixture,
} from "@/test/fixtures/model-readiness-fixtures";
import { renderWithProviders } from "@/test/render";

const { mockFetchModelReadinessSummary, mockFetchModelReadinessDetail } = vi.hoisted(() => ({
  mockFetchModelReadinessSummary: vi.fn(),
  mockFetchModelReadinessDetail: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchModelReadinessSummary: mockFetchModelReadinessSummary,
    fetchModelReadinessDetail: mockFetchModelReadinessDetail,
  };
});

describe("ModelReadinessPanel", () => {
  it("distinguishes active study families from heuristic lanes while keeping runtime truth separate", async () => {
    mockFetchModelReadinessSummary.mockResolvedValue(modelReadinessSummaryFixture);
    mockFetchModelReadinessDetail.mockImplementation(async (familyKey: string) => (
      familyKey === heuristicLaneFamilyFixture.family_key ? heuristicLaneFamilyFixture : activeStudyFamilyFixture
    ));

    renderWithProviders(<ModelReadinessPanel />);

    expect((await screen.findAllByText("active study")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("heuristic lane").length).toBeGreaterThan(0);
    expect(screen.getAllByText("heuristic -> heuristic").length).toBeGreaterThan(0);
  });
});

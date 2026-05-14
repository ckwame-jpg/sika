import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CalibrationBucketRead } from "@/lib/types";
import { ModelReadinessPanel } from "@/components/predictions/model-readiness-panel";
import {
  activeStudyFamilyFixture,
  modelReadinessSummaryFixture,
} from "@/test/fixtures/model-readiness-fixtures";
import { healthFixture } from "@/test/fixtures/trade-fixtures";
import { renderWithProviders } from "@/test/render";

const { mockFetchHealth, mockFetchModelReadinessSummary, mockFetchModelReadinessDetail } = vi.hoisted(() => ({
  mockFetchHealth: vi.fn(),
  mockFetchModelReadinessSummary: vi.fn(),
  mockFetchModelReadinessDetail: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchHealth: mockFetchHealth,
    fetchModelReadinessSummary: mockFetchModelReadinessSummary,
    fetchModelReadinessDetail: mockFetchModelReadinessDetail,
    updateModelReadinessSettings: vi.fn(),
  };
});

function buckets(populated: Array<Pick<CalibrationBucketRead, "label" | "settled_count" | "avg_predicted" | "actual_yes_rate">>): CalibrationBucketRead[] {
  return populated.map((row) => ({
    ...row,
    miscalibration:
      row.avg_predicted !== null && row.actual_yes_rate !== null
        ? Number((row.avg_predicted - row.actual_yes_rate).toFixed(4))
        : null,
  }));
}

describe("ModelReadinessPanel — reliability curve", () => {
  it("renders the empty-state message when no buckets have settled rows", async () => {
    const family = {
      ...activeStudyFamilyFixture,
      calibration_buckets: buckets([
        { label: "60-70%", settled_count: 0, avg_predicted: null, actual_yes_rate: null },
      ]),
    };
    mockFetchHealth.mockResolvedValue(healthFixture);
    mockFetchModelReadinessSummary.mockResolvedValue({
      ...modelReadinessSummaryFixture,
      families: [family],
    });
    mockFetchModelReadinessDetail.mockResolvedValue(family);

    renderWithProviders(<ModelReadinessPanel />);

    expect(await screen.findByText("Reliability curve")).toBeInTheDocument();
    expect(await screen.findByText(/No settled rows yet/i)).toBeInTheDocument();
  });

  it("renders one row per populated bucket with the miscalibration value", async () => {
    const family = {
      ...activeStudyFamilyFixture,
      calibration_buckets: buckets([
        { label: "60-70%", settled_count: 12, avg_predicted: 0.65, actual_yes_rate: 0.5 },
        { label: "70-80%", settled_count: 8, avg_predicted: 0.74, actual_yes_rate: 0.75 },
      ]),
    };
    mockFetchHealth.mockResolvedValue(healthFixture);
    mockFetchModelReadinessSummary.mockResolvedValue({
      ...modelReadinessSummaryFixture,
      families: [family],
    });
    mockFetchModelReadinessDetail.mockResolvedValue(family);

    renderWithProviders(<ModelReadinessPanel />);

    expect(await screen.findByText("60-70%")).toBeInTheDocument();
    expect(await screen.findByText("70-80%")).toBeInTheDocument();
    // 0.65 - 0.5 = 0.150 → over-confident, positive sign
    expect(await screen.findByText("+0.150")).toBeInTheDocument();
    // 0.74 - 0.75 = -0.010 → under-confident, negative sign
    expect(await screen.findByText("-0.010")).toBeInTheDocument();
  });
});

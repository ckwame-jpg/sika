import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ProductFreshnessBanner } from "@/components/layout/product-freshness-banner";
import { renderWithProviders } from "@/test/render";

const { mockFetchProductFreshness } = vi.hoisted(() => ({
  mockFetchProductFreshness: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchProductFreshness: mockFetchProductFreshness,
  };
});

describe("ProductFreshnessBanner", () => {
  it("renders degraded product status even when a snapshot exists", async () => {
    mockFetchProductFreshness.mockResolvedValue({
      overall_status: "degraded",
      scopes: [
        {
          scope: "NBA",
          generated_at: "2026-04-12T22:09:06Z",
          status: "degraded",
          event_count: 1,
          candidate_market_count: 0,
          scored_market_count: 0,
          recommendation_count: 0,
          coverage_prediction_count: 0,
          blocking_reason: "Current NBA/MLB events exist, but no current Kalshi markets are mapped to them.",
          generated_from_run_id: 1983,
        },
      ],
    });

    renderWithProviders(<ProductFreshnessBanner />);

    expect(await screen.findByTestId("product-freshness-banner")).toHaveTextContent(
      "Current NBA/MLB events exist",
    );
  });

  it("stays silent when product freshness is fresh", async () => {
    mockFetchProductFreshness.mockResolvedValue({
      overall_status: "fresh",
      scopes: [],
    });

    renderWithProviders(<ProductFreshnessBanner />);

    expect(screen.queryByTestId("product-freshness-banner")).not.toBeInTheDocument();
  });
});

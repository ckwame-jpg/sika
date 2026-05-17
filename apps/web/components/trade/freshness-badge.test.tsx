import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FreshnessBadge } from "@/components/trade/freshness-badge";
// Import the type from the canonical location (lib/types) rather
// than the component's re-export — matches how consuming code
// (trade-ticket.tsx) imports it.
import type { FreshnessStaleGroup } from "@/lib/types";

const penalizeGroup: FreshnessStaleGroup = {
  group_key: "mlb_weather",
  severity: "penalize",
  age_seconds: 7 * 3600, // 7 hours
  confidence_delta: -0.05,
  source: "load_weather",
};

const suppressGroup: FreshnessStaleGroup = {
  group_key: "nba_injury",
  severity: "suppress",
  age_seconds: 13 * 3600,
  confidence_delta: 0.0,
  source: "load_nba_injury_report",
};

describe("FreshnessBadge", () => {
  it("renders nothing when the stale list is empty", () => {
    const { container } = render(
      <FreshnessBadge staleGroups={[]} confidenceDelta={null} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when stale list is null/undefined", () => {
    const { container } = render(
      <FreshnessBadge staleGroups={null} confidenceDelta={null} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders an accessible group role with a descriptive label", () => {
    render(
      <FreshnessBadge
        staleGroups={[penalizeGroup]}
        confidenceDelta={-0.05}
      />,
    );
    // Screen-reader fallback. The label communicates the takeaway
    // (something is stale and what it cost the confidence) so an
    // operator using assistive tech gets the same signal as a sighted one.
    const region = screen.getByRole("group", { name: /stale feature/i });
    expect(region).toBeInTheDocument();
  });

  it("shows the per-group key + age + confidence delta for PENALIZE groups", () => {
    render(
      <FreshnessBadge
        staleGroups={[penalizeGroup]}
        confidenceDelta={-0.05}
      />,
    );
    // group_key is humanized: underscores stripped, lowercased so the
    // operator scans a list of groups quickly.
    expect(screen.getByText(/mlb weather/i)).toBeInTheDocument();
    // Age rendered in a humanized unit (hours/minutes), not raw seconds.
    expect(screen.getByText(/7h/i)).toBeInTheDocument();
    // Confidence delta surfaces as a signed percentage so the operator
    // knows how much the recommendation was penalized. Both the per-
    // row delta AND the summary delta print "-5%" here (the summary
    // is the sum of one -5% group), so use getAllByText — both are
    // valid + intentional.
    expect(screen.getAllByText(/-5%/).length).toBeGreaterThanOrEqual(1);
  });

  it("uses a SUPPRESS visual tone for severity=suppress", () => {
    render(
      <FreshnessBadge
        staleGroups={[suppressGroup]}
        confidenceDelta={0.0}
      />,
    );
    const region = screen.getByRole("group", { name: /stale feature/i });
    expect(region.getAttribute("data-max-severity")).toBe("suppress");
  });

  it("uses a PENALIZE visual tone when only PENALIZE groups are stale", () => {
    render(
      <FreshnessBadge
        staleGroups={[penalizeGroup]}
        confidenceDelta={-0.05}
      />,
    );
    const region = screen.getByRole("group", { name: /stale feature/i });
    expect(region.getAttribute("data-max-severity")).toBe("penalize");
  });

  it("escalates to SUPPRESS tone when a mixed list contains any suppress group", () => {
    render(
      <FreshnessBadge
        staleGroups={[penalizeGroup, suppressGroup]}
        confidenceDelta={-0.05}
      />,
    );
    const region = screen.getByRole("group", { name: /stale feature/i });
    expect(region.getAttribute("data-max-severity")).toBe("suppress");
  });

  it("surfaces aggregate confidence delta in the headline when nonzero", () => {
    render(
      <FreshnessBadge
        staleGroups={[
          penalizeGroup,
          { ...penalizeGroup, group_key: "mlb_bullpen", source: "bullpen" },
        ]}
        confidenceDelta={-0.10}
      />,
    );
    // -10% in the summary (sum of both -5%s).
    expect(screen.getByText(/-10%/)).toBeInTheDocument();
  });

  it("formats sub-hour ages in minutes", () => {
    render(
      <FreshnessBadge
        staleGroups={[
          { ...penalizeGroup, age_seconds: 45 * 60 }, // 45 minutes
        ]}
        confidenceDelta={-0.05}
      />,
    );
    expect(screen.getByText(/45m/i)).toBeInTheDocument();
  });

  it("formats multi-day ages in days when age_seconds >= 86400", () => {
    render(
      <FreshnessBadge
        staleGroups={[
          { ...penalizeGroup, age_seconds: 2 * 86400 }, // 2 days
        ]}
        confidenceDelta={-0.05}
      />,
    );
    expect(screen.getByText(/2d/i)).toBeInTheDocument();
  });

  it("renders the source label so operators know which cache fed the stale value", () => {
    render(
      <FreshnessBadge
        staleGroups={[penalizeGroup]}
        confidenceDelta={-0.05}
      />,
    );
    expect(screen.getByText(/load_weather/i)).toBeInTheDocument();
  });
});

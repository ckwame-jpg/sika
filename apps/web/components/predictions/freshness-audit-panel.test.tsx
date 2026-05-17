import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FreshnessAuditPanel } from "@/components/predictions/freshness-audit-panel";
import type { FreshnessAuditRowRead } from "@/lib/types";

const HURTFUL_ROW: FreshnessAuditRowRead = {
  group_key: "mlb_weather",
  stale_count: 24,
  fresh_count: 156,
  stale_avg_predicted: 0.62,
  fresh_avg_predicted: 0.61,
  stale_hit_rate: 0.45,
  fresh_hit_rate: 0.58,
  stale_calibration_miss: 0.17,
  fresh_calibration_miss: 0.03,
  calibration_delta: 0.14,
};

const NEUTRAL_ROW: FreshnessAuditRowRead = {
  group_key: "nba_workload",
  stale_count: 42,
  fresh_count: 218,
  stale_avg_predicted: 0.55,
  fresh_avg_predicted: 0.56,
  stale_hit_rate: 0.54,
  fresh_hit_rate: 0.55,
  stale_calibration_miss: 0.01,
  fresh_calibration_miss: 0.01,
  calibration_delta: 0.0,
};

describe("FreshnessAuditPanel", () => {
  it("renders an empty state when there are no rows", () => {
    render(<FreshnessAuditPanel rows={[]} />);
    // Empty state explains why nothing is showing rather than
    // rendering nothing — operators looking for the panel after
    // hearing about it shouldn't think it's broken.
    expect(screen.getByText(/no settled predictions/i)).toBeInTheDocument();
  });

  it("renders a row per audit entry with the group key", () => {
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW, NEUTRAL_ROW]} />);
    expect(screen.getByText(/mlb weather/i)).toBeInTheDocument();
    expect(screen.getByText(/nba workload/i)).toBeInTheDocument();
  });

  it("displays per-bucket counts", () => {
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} />);
    // Stale + fresh counts get their own cells so the operator can
    // discount low-N rows.
    expect(screen.getByText(/24/)).toBeInTheDocument();
    expect(screen.getByText(/156/)).toBeInTheDocument();
  });

  it("displays calibration miss percentages per bucket", () => {
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} />);
    // Calibration miss formatted as pct (×100) so operators reading
    // "17%" understand the magnitude vs "0.17".
    expect(screen.getByText(/17%/)).toBeInTheDocument();
    expect(screen.getByText(/3%/)).toBeInTheDocument();
  });

  it("displays the calibration delta with a signed-pct format and a directional tone", () => {
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} />);
    // +14% delta — staleness hurt calibration; operator should
    // consider promoting this group in the policy registry.
    expect(screen.getByText(/\+14%/)).toBeInTheDocument();
  });

  it("flags neutral rows as informational rather than actionable", () => {
    render(<FreshnessAuditPanel rows={[NEUTRAL_ROW]} />);
    // A zero or near-zero delta is the "no signal yet / no
    // staleness penalty observed" case. The row's data-tuning-signal
    // attribute lets ops dashboards filter for actionable rows
    // without re-deriving from the numbers.
    const row = screen.getByTestId(`freshness-audit-row-${NEUTRAL_ROW.group_key}`);
    expect(row.getAttribute("data-tuning-signal")).toBe("none");
  });

  it("flags rows with a positive delta as actionable", () => {
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} />);
    const row = screen.getByTestId(`freshness-audit-row-${HURTFUL_ROW.group_key}`);
    expect(row.getAttribute("data-tuning-signal")).toBe("promote");
  });

  it("displays a low-sample warning when stale_count or fresh_count is below the gate", () => {
    const lowN: FreshnessAuditRowRead = {
      ...HURTFUL_ROW,
      stale_count: 4,
    };
    render(<FreshnessAuditPanel rows={[lowN]} />);
    const row = screen.getByTestId(`freshness-audit-row-${lowN.group_key}`);
    // Below the recommended 20-row gate per the tuning playbook —
    // delta is reported but flagged as low-confidence so the operator
    // doesn't ship a policy off thin data.
    expect(row.getAttribute("data-tuning-signal")).toBe("low_sample");
  });

  it("has an accessible group role with a label", () => {
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} />);
    const region = screen.getByRole("region", {
      name: /freshness calibration audit/i,
    });
    expect(region).toBeInTheDocument();
  });
});

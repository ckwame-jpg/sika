import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FreshnessAuditPanel } from "@/components/predictions/freshness-audit-panel";
import type { FreshnessAuditMetaRead, FreshnessAuditRowRead } from "@/lib/types";

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

  it("suppresses the signed delta when the stale bucket is empty", () => {
    // Backend fills empty buckets with 0.0 sentinels, so the delta's
    // sign is an artifact of WHICH bucket is empty — never render it
    // as if it were a tuning signal.
    const emptyStale: FreshnessAuditRowRead = {
      ...HURTFUL_ROW,
      stale_count: 0,
      stale_avg_predicted: 0,
      stale_hit_rate: 0,
      stale_calibration_miss: 0,
      calibration_delta: -0.23,
    };
    render(<FreshnessAuditPanel rows={[emptyStale]} />);
    expect(
      screen.getByTestId(`freshness-audit-delta-unavailable-${emptyStale.group_key}`),
    ).toHaveTextContent("—");
    expect(screen.queryByText(/-23%/)).not.toBeInTheDocument();
    // Still styled/flagged as low_sample; caption reports the empty bucket.
    const row = screen.getByTestId(`freshness-audit-row-${emptyStale.group_key}`);
    expect(row.getAttribute("data-tuning-signal")).toBe("low_sample");
    expect(screen.getByText(/0 of 20 samples/i)).toBeInTheDocument();
  });

  it("suppresses the signed delta when the fresh bucket is empty", () => {
    const emptyFresh: FreshnessAuditRowRead = {
      ...HURTFUL_ROW,
      fresh_count: 0,
      fresh_avg_predicted: 0,
      fresh_hit_rate: 0,
      fresh_calibration_miss: 0,
      calibration_delta: 0.17,
    };
    render(<FreshnessAuditPanel rows={[emptyFresh]} />);
    expect(
      screen.getByTestId(`freshness-audit-delta-unavailable-${emptyFresh.group_key}`),
    ).toHaveTextContent("—");
    expect(screen.queryByText(/\+17%/)).not.toBeInTheDocument();
  });

  it("reports the genuinely smaller bucket in the low-sample caption", () => {
    // Regression: the caption used to report stale_count whenever it
    // was under the gate, even when fresh_count was smaller.
    const bothLow: FreshnessAuditRowRead = {
      ...HURTFUL_ROW,
      stale_count: 15,
      fresh_count: 3,
    };
    render(<FreshnessAuditPanel rows={[bothLow]} />);
    expect(screen.getByText(/3 of 20 samples/i)).toBeInTheDocument();
  });

  it("renders negative deltas with symmetric rounding", () => {
    // Math.round rounds -22.5 to -22 but +22.5 to +23; the panel
    // rounds the magnitude so equal-size deltas print equal sizes.
    const negative: FreshnessAuditRowRead = {
      ...HURTFUL_ROW,
      stale_count: 40,
      fresh_count: 40,
      stale_calibration_miss: 0.03,
      fresh_calibration_miss: 0.155,
      calibration_delta: -0.125,
    };
    render(<FreshnessAuditPanel rows={[negative]} />);
    expect(screen.getByText(/-13%/)).toBeInTheDocument();
  });

  it("labels the nominal window when the row cap did not clip", () => {
    const meta: FreshnessAuditMetaRead = {
      window_days: 30,
      row_limit: 250000,
      rows_scanned: 4200,
      row_limit_hit: false,
      effective_window_start: "2026-06-21T00:00:00Z",
    };
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} meta={meta} />);
    expect(screen.getByText(/window 30d/i)).toBeInTheDocument();
    expect(
      screen.queryByTestId("freshness-audit-window-clipped"),
    ).not.toBeInTheDocument();
  });

  it("labels a clipped window from the oldest scanned row when the cap hit", () => {
    const meta: FreshnessAuditMetaRead = {
      window_days: 30,
      row_limit: 250000,
      rows_scanned: 250000,
      row_limit_hit: true,
      effective_window_start: "2026-07-19T23:10:00Z",
    };
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} meta={meta} />);
    const label = screen.getByTestId("freshness-audit-window-clipped");
    expect(label).toHaveTextContent(/window clipped/i);
    expect(label).toHaveTextContent(/jul/i);
    expect(screen.queryByText(/window 30d/i)).not.toBeInTheDocument();
  });

  it("falls back to the nominal window label when meta is absent", () => {
    render(<FreshnessAuditPanel rows={[HURTFUL_ROW]} />);
    expect(screen.getByText(/window 30d/i)).toBeInTheDocument();
  });
});

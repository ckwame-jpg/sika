import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  composeHeadline,
  PredictionIntervalBand,
  type PredictionInterval,
} from "@/components/trade/prediction-interval-band";

const baseInterval: PredictionInterval = {
  p10: 24.5,
  p50: 28.2,
  p90: 31.8,
  threshold: 22.5,
  source: "interval_model_v1",
  coverage_status: "ok",
  yes_probability_from_interval: 0.86,
  yes_probability_from_poisson: 0.74,
  delta: 0.12,
};

describe("PredictionIntervalBand", () => {
  it("renders the [p10, p90] range with p50 + threshold labels", () => {
    render(<PredictionIntervalBand interval={baseInterval} />);
    // The component surfaces all three quantile labels + the
    // threshold somewhere in the DOM so operators can read the band
    // without hovering. Use partial-text matching because the
    // numbers may live inside larger label strings (e.g. "typical: 28.2").
    expect(screen.getAllByText(/24\.5/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/28\.2/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/31\.8/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/22\.5/).length).toBeGreaterThan(0);
  });

  it("renders the new floor / typical / ceiling labels", () => {
    // Redesign 2026-05-17b: the operator-facing labels switched from
    // p10 / p50 / p90 (statistical) to floor / typical / ceiling
    // (sports talk). The technical phrasing lives on the title tooltip.
    render(<PredictionIntervalBand interval={baseInterval} />);
    expect(screen.getByText(/^floor$/i)).toBeInTheDocument();
    expect(screen.getByText(/^typical$/i)).toBeInTheDocument();
    expect(screen.getByText(/^ceiling$/i)).toBeInTheDocument();
  });

  it("renders a plain-English headline that mentions median + verdict", () => {
    render(
      <PredictionIntervalBand interval={baseInterval} statKey="points" />,
    );
    // baseInterval: p10=24.5, p50=28.2, p90=31.8, threshold=22.5 →
    // over lean, threshold below p10 → easy-clear verdict.
    const headline = screen.getByTestId("prediction-interval-headline");
    expect(headline).toHaveTextContent(/model expects ~28\.2 pts/i);
    expect(headline).toHaveTextContent(/floor 24\.5, ceiling 31\.8/i);
    expect(headline).toHaveTextContent(/even on a bad night/i);
  });

  it("renders an accessible group role with a descriptive label", () => {
    render(<PredictionIntervalBand interval={baseInterval} />);
    // Screen-reader fallback for the SVG visualization. The label
    // mentions the threshold + the over/under direction so a
    // non-sighted operator gets the same takeaway as a sighted one.
    const region = screen.getByRole("group", { name: /prediction interval/i });
    expect(region).toBeInTheDocument();
  });

  it("applies an over-leaning visual tone when the threshold is below p50", () => {
    render(<PredictionIntervalBand interval={baseInterval} />);
    // baseInterval: threshold=22.5, p50=28.2 → over-leaning (threshold
    // is to the LEFT of p50, so most probability mass is above it).
    // The component exposes a stable data attribute so tests pin the
    // tone without depending on Tailwind class names that may change.
    const region = screen.getByRole("group", { name: /prediction interval/i });
    expect(region.getAttribute("data-lean")).toBe("over");
  });

  it("applies an under-leaning visual tone when the threshold is above p50", () => {
    render(
      <PredictionIntervalBand
        interval={{ ...baseInterval, threshold: 30.0 }}
      />,
    );
    // threshold=30.0 > p50=28.2 → under-leaning. Most probability mass
    // is BELOW the threshold; this is the "less likely to clear" tone.
    const region = screen.getByRole("group", { name: /prediction interval/i });
    expect(region.getAttribute("data-lean")).toBe("under");
  });

  it("surfaces the coverage status as a small badge", () => {
    render(<PredictionIntervalBand interval={baseInterval} />);
    // Operators need to know whether the band's swap is active.
    // The bad/warn variants of the badge are visually distinct so
    // the operator can tell when intervals are informational-only.
    expect(screen.getByText(/ok/i)).toBeInTheDocument();
  });

  it("renders a bad-coverage variant when coverage_status !== 'ok'", () => {
    render(
      <PredictionIntervalBand
        interval={{ ...baseInterval, coverage_status: "bad" }}
      />,
    );
    // The bad variant should signal to the operator that the
    // displayed band is informational (Poisson was used for scoring).
    const region = screen.getByRole("group", { name: /prediction interval/i });
    expect(region.getAttribute("data-coverage")).toBe("bad");
    // ``^bad$`` anchors to the coverage chip's standalone text — the
    // plain-English headline added in the 2026-05-17b redesign also
    // contains the word "bad" (e.g. "even on a bad night"), so a
    // loose ``/bad/i`` now matches multiple nodes.
    expect(screen.getByText(/^bad$/i)).toBeInTheDocument();
  });

  it("returns null when interval is null (point-estimate fallback path)", () => {
    const { container } = render(<PredictionIntervalBand interval={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("returns null when interval is undefined", () => {
    const { container } = render(
      <PredictionIntervalBand interval={undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe("composeHeadline — verdict logic", () => {
  it("emits 'even on a bad night' when threshold is strictly below p10 (over lean)", () => {
    expect(
      composeHeadline({ p10: 12, p50: 16, p90: 24, threshold: 10, lean: "over", statKey: "points" }),
    ).toMatch(/clears 10 even on a bad night/);
  });

  it("emits a 'could miss' hedge at the threshold === p10 boundary (codex pattern 6: overstatement)", () => {
    // ``threshold === p10`` is a ~90% clear, not 100% — calling it
    // "easy" would overstate the model's confidence at the boundary.
    // Codex pattern 6: avoid implicit overstatement at edge values.
    const headline = composeHeadline({
      p10: 12,
      p50: 16,
      p90: 24,
      threshold: 12,
      lean: "over",
      statKey: "points",
    });
    expect(headline).toMatch(/leans over 12/);
    expect(headline).toMatch(/floor night could miss/);
  });

  it("emits a 'could overshoot' hedge at the threshold === p90 boundary (under lean)", () => {
    // Mirror of the p10 boundary: at threshold === p90, the model
    // thinks ~10% of outcomes still overshoot the line.
    const headline = composeHeadline({
      p10: 18,
      p50: 22,
      p90: 28,
      threshold: 28,
      lean: "under",
      statKey: "points",
    });
    expect(headline).toMatch(/leans under 28/);
    expect(headline).toMatch(/ceiling night could overshoot/);
  });

  it("emits a cushion + floor-warning when threshold sits inside (p10, p50) for over lean", () => {
    // p50=16, threshold=14 → cushion = 2; p10=12 < threshold so the
    // bad-night could miss.
    const headline = composeHeadline({
      p10: 12,
      p50: 16,
      p90: 24,
      threshold: 14,
      lean: "over",
      statKey: "points",
    });
    expect(headline).toMatch(/leans over 14 by ~2/);
    expect(headline).toMatch(/floor night could miss/);
  });

  it("emits 'even on a great night' when threshold is at or above p90 (under lean)", () => {
    expect(
      composeHeadline({ p10: 18, p50: 22, p90: 28, threshold: 30, lean: "under", statKey: "points" }),
    ).toMatch(/stays under 30 even on a great night/);
  });

  it("emits a cushion + ceiling-warning when threshold sits inside (p50, p90) for under lean", () => {
    const headline = composeHeadline({
      p10: 18,
      p50: 22,
      p90: 28,
      threshold: 25,
      lean: "under",
      statKey: "points",
    });
    expect(headline).toMatch(/leans under 25 by ~3/);
    expect(headline).toMatch(/ceiling night could overshoot/);
  });

  it("falls back to unitless phrasing when statKey is missing", () => {
    expect(
      composeHeadline({ p10: 12, p50: 16, p90: 24, threshold: 10, lean: "over" }),
    ).toMatch(/model expects ~16\./);
  });

  it("formats common stat units (points → pts, rebounds → rebs, passing_yards → pass yds)", () => {
    expect(
      composeHeadline({ p10: 12, p50: 16, p90: 24, threshold: 10, lean: "over", statKey: "rebounds" }),
    ).toMatch(/~16 rebs/);
    expect(
      composeHeadline({ p10: 200, p50: 260, p90: 320, threshold: 250, lean: "over", statKey: "passing_yards" }),
    ).toMatch(/~260 pass yds/);
  });
});

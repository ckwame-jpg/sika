import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
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
    // numbers may live inside larger label strings (e.g. "p50: 28.2").
    expect(screen.getByText(/24\.5/)).toBeInTheDocument();
    expect(screen.getByText(/28\.2/)).toBeInTheDocument();
    expect(screen.getByText(/31\.8/)).toBeInTheDocument();
    expect(screen.getByText(/22\.5/)).toBeInTheDocument();
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
    expect(screen.getByText(/bad/i)).toBeInTheDocument();
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

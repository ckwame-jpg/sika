import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { IntervalModelsBadge } from "@/components/predictions/interval-models-badge";
import type { IntervalModelStatusRead } from "@/lib/types";

const okEntry: IntervalModelStatusRead = {
  family_key: "nba_props",
  stat_key: "points",
  sample_size: 127,
  empirical_coverage: 0.81,
  coverage_status: "ok",
  trained_at: "2026-05-16T10:00:00Z",
  window_start: "2026-04-16T00:00:00Z",
  window_end: "2026-05-16T00:00:00Z",
};

const warnEntry: IntervalModelStatusRead = {
  ...okEntry,
  stat_key: "rebounds",
  empirical_coverage: 0.62,
  coverage_status: "warn",
};

const badEntry: IntervalModelStatusRead = {
  ...okEntry,
  stat_key: "assists",
  empirical_coverage: 0.45,
  coverage_status: "bad",
};

const unknownEntry: IntervalModelStatusRead = {
  ...okEntry,
  stat_key: "made_threes",
  sample_size: null,
  empirical_coverage: null,
  coverage_status: "unknown",
};

describe("IntervalModelsBadge", () => {
  it("renders the empty-state when no intervals are trained", () => {
    render(<IntervalModelsBadge intervals={[]} />);

    expect(screen.getByText(/no interval models trained/i)).toBeInTheDocument();
    expect(
      screen.getByText(/python -m ml\.cli train-intervals/i),
    ).toBeInTheDocument();
  });

  it("renders one row per (family, stat) with coverage and status", () => {
    render(<IntervalModelsBadge intervals={[okEntry, warnEntry]} />);

    const pointsRow = screen.getByTestId("interval-row-nba_props-points");
    expect(within(pointsRow).getByText("points")).toBeInTheDocument();
    expect(within(pointsRow).getByText("0.81")).toBeInTheDocument();
    expect(within(pointsRow).getByText(/127/)).toBeInTheDocument();
    expect(within(pointsRow).getByTestId("interval-status-pill")).toHaveTextContent(/ok/i);

    const rebRow = screen.getByTestId("interval-row-nba_props-rebounds");
    expect(within(rebRow).getByText("rebounds")).toBeInTheDocument();
    expect(within(rebRow).getByText("0.62")).toBeInTheDocument();
    expect(within(rebRow).getByTestId("interval-status-pill")).toHaveTextContent(/warn/i);
  });

  it("shows '?' placeholders for unknown coverage entries", () => {
    render(<IntervalModelsBadge intervals={[unknownEntry]} />);

    const row = screen.getByTestId("interval-row-nba_props-made_threes");
    // Both samples + coverage render as "?" when metadata is missing.
    expect(within(row).getAllByText("?")).toHaveLength(2);
    expect(within(row).getByTestId("interval-status-pill")).toHaveTextContent(/unknown/i);
  });

  it("summarizes per-family counts in the header", () => {
    render(
      <IntervalModelsBadge
        intervals={[
          okEntry,
          warnEntry,
          {
            ...okEntry,
            family_key: "mlb_props",
            stat_key: "hits",
            coverage_status: "ok",
          },
        ]}
      />,
    );

    // The header summary lists "nba_props: 2 · mlb_props: 1" or similar.
    const header = screen.getByTestId("interval-models-header");
    expect(header).toHaveTextContent(/nba_props/);
    expect(header).toHaveTextContent(/mlb_props/);
    expect(header).toHaveTextContent(/2/);
    expect(header).toHaveTextContent(/1/);
  });

  it("uses the bad-status tone for out-of-band coverage", () => {
    render(<IntervalModelsBadge intervals={[badEntry]} />);

    const pill = screen.getByTestId("interval-status-pill");
    expect(pill).toHaveTextContent(/bad/i);
    // Tone class — same outcome-pill vocabulary as the rest of the panel.
    expect(pill.className).toMatch(/lost/);
  });
});

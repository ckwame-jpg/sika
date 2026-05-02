import { afterEach, describe, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { AdvancedMetricsGrid } from "./advanced-metrics-grid";

afterEach(() => {
  cleanup();
});

describe("AdvancedMetricsGrid", () => {
  test("renders only metrics tagged advanced", () => {
    render(
      <AdvancedMetricsGrid
        metrics={{ pts: 28.4, ts_pct: 0.612, usg_pct: 0.305 }}
        labels={{ pts: "PTS", ts_pct: "TS%", usg_pct: "USG%" }}
        percentiles={{ ts_pct: 78, usg_pct: 92 }}
        categories={{ pts: "basic", ts_pct: "advanced", usg_pct: "advanced" }}
      />,
    );
    expect(screen.queryByTestId("sa-advanced-pts")).not.toBeInTheDocument();
    expect(screen.getByTestId("sa-advanced-ts_pct")).toBeInTheDocument();
    expect(screen.getByTestId("sa-advanced-usg_pct")).toBeInTheDocument();
  });

  test("hides itself when no advanced metrics are present", () => {
    const { container } = render(
      <AdvancedMetricsGrid
        metrics={{ pts: 28.4 }}
        labels={{ pts: "PTS" }}
        percentiles={{}}
        categories={{ pts: "basic" }}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("color-codes the percentile bar based on value", () => {
    render(
      <AdvancedMetricsGrid
        metrics={{ ts_pct: 0.6, drtg: 110, ortg: 119 }}
        labels={{ ts_pct: "TS%", drtg: "DRtg", ortg: "ORtg" }}
        percentiles={{ ts_pct: 80, drtg: 50, ortg: 15 }}
        categories={{ ts_pct: "advanced", drtg: "advanced", ortg: "advanced" }}
      />,
    );
    const ts = screen.getByTestId("sa-advanced-ts_pct");
    const drtg = screen.getByTestId("sa-advanced-drtg");
    const ortg = screen.getByTestId("sa-advanced-ortg");
    expect(ts.querySelector(".sa-advanced-row-bar.is-high")).toBeInTheDocument();
    expect(drtg.querySelector(".sa-advanced-row-bar.is-mid")).toBeInTheDocument();
    expect(ortg.querySelector(".sa-advanced-row-bar.is-low")).toBeInTheDocument();
  });

  test("falls back to em-dash when value or percentile is missing", () => {
    render(
      <AdvancedMetricsGrid
        metrics={{ ts_pct: 0.612 }}
        labels={{ ts_pct: "TS%" }}
        percentiles={{}}
        categories={{ ts_pct: "advanced" }}
      />,
    );
    const row = screen.getByTestId("sa-advanced-ts_pct");
    expect(row.querySelector(".sa-advanced-row-bar.is-empty")).toBeInTheDocument();
  });
});

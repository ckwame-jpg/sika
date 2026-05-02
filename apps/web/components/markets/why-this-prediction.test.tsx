import { afterEach, describe, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { WhyThisPrediction } from "./why-this-prediction";

afterEach(() => {
  cleanup();
});

describe("WhyThisPrediction", () => {
  test("renders the top 3 advanced factors sorted by absolute delta", () => {
    const { container } = render(
      <WhyThisPrediction
        features={{
          advanced_factors: {
            efficiency_factor: 1.10,
            opp_def_factor: 0.92,
            usage_factor_advanced: 1.04,
            pace_factor_advanced: 1.13,
            // Near-zero noise that should be filtered
            opp_recent_form_factor: 1.001,
          },
        }}
      />,
    );
    // We display top 3 by |delta|: pace_factor_advanced (+13), efficiency_factor (+10), opp_def_factor (-8)
    expect(screen.getByTestId("why-driver-pace_factor_advanced")).toBeInTheDocument();
    expect(screen.getByTestId("why-driver-efficiency_factor")).toBeInTheDocument();
    expect(screen.getByTestId("why-driver-opp_def_factor")).toBeInTheDocument();
    // 5th factor (usage 1.04) and noise should not render
    expect(screen.queryByTestId("why-driver-usage_factor_advanced")).not.toBeInTheDocument();
    expect(screen.queryByTestId("why-driver-opp_recent_form_factor")).not.toBeInTheDocument();
    const driverRows = container.querySelectorAll('[data-testid^="why-driver-"]');
    expect(driverRows.length).toBe(3);
  });

  test("hides the panel when no advanced factors are present", () => {
    const { container } = render(<WhyThisPrediction features={{}} />);
    expect(container.firstChild).toBeNull();
  });

  test("hides the panel when features is null", () => {
    const { container } = render(<WhyThisPrediction features={null} />);
    expect(container.firstChild).toBeNull();
  });

  test("displays direction arrows reflecting boost vs suppress", () => {
    render(
      <WhyThisPrediction
        features={{
          advanced_factors: {
            quality_of_contact_factor: 1.12,
            pitcher_dominance_factor: 0.88,
          },
        }}
      />,
    );
    const boostRow = screen.getByTestId("why-driver-quality_of_contact_factor");
    const suppressRow = screen.getByTestId("why-driver-pitcher_dominance_factor");
    expect(boostRow).toHaveTextContent("↑");
    expect(suppressRow).toHaveTextContent("↓");
    expect(boostRow).toHaveTextContent("+12.0%");
    expect(suppressRow).toHaveTextContent("-12.0%");
  });

  test("prefers server-built _drivers payload with detail strings", () => {
    render(
      <WhyThisPrediction
        features={{
          _drivers: [
            {
              key: "quality_of_contact_factor",
              label: "Quality of contact",
              delta_pct: 12.0,
              direction: "up",
              detail: "Season barrel rate: 14.0%",
            },
            {
              key: "starter_factor_advanced",
              label: "Opposing starter quality",
              delta_pct: -8.0,
              direction: "down",
              detail: "Opposing starter xFIP: 3.20",
            },
          ],
          // ``advanced_factors`` is also present but should be ignored when
          // ``_drivers`` is provided.
          advanced_factors: {
            unrelated_factor: 1.99,
          },
        }}
      />,
    );
    const qoc = screen.getByTestId("why-driver-quality_of_contact_factor");
    expect(qoc).toHaveTextContent("Quality of contact");
    expect(qoc).toHaveTextContent("+12.0%");
    expect(screen.getByTestId("why-driver-quality_of_contact_factor-detail")).toHaveTextContent(
      "Season barrel rate: 14.0%",
    );
    const starter = screen.getByTestId("why-driver-starter_factor_advanced");
    expect(starter).toHaveTextContent("Opposing starter quality");
    expect(starter).toHaveTextContent("-8.0%");
    expect(screen.getByTestId("why-driver-starter_factor_advanced-detail")).toHaveTextContent(
      "Opposing starter xFIP: 3.20",
    );
    // The fallback advanced_factors row must NOT render when _drivers is present.
    expect(screen.queryByTestId("why-driver-unrelated_factor")).not.toBeInTheDocument();
  });

  test("falls back to advanced_factors when _drivers is empty", () => {
    render(
      <WhyThisPrediction
        features={{
          _drivers: [],
          advanced_factors: { efficiency_factor: 1.10 },
        }}
      />,
    );
    expect(screen.getByTestId("why-driver-efficiency_factor")).toBeInTheDocument();
  });

  test("omits detail row when detail is null in server payload", () => {
    render(
      <WhyThisPrediction
        features={{
          _drivers: [
            {
              key: "efficiency_factor",
              label: "Shooting efficiency",
              delta_pct: 10.0,
              direction: "up",
              detail: null,
            },
          ],
        }}
      />,
    );
    expect(screen.getByTestId("why-driver-efficiency_factor")).toBeInTheDocument();
    expect(screen.queryByTestId("why-driver-efficiency_factor-detail")).not.toBeInTheDocument();
  });
});

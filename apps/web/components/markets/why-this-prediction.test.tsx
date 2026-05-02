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
});

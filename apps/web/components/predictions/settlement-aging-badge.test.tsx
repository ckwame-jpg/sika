import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SettlementAgingBadge } from "@/components/predictions/settlement-aging-badge";
import type { SettlementAgingRead } from "@/lib/types";

const allClear: SettlementAgingRead = {
  bucket_0_to_1h: 0,
  bucket_1_to_6h: 0,
  bucket_6_to_24h: 0,
  bucket_beyond_24h: 0,
  total_pending_past_close: 0,
};

describe("SettlementAgingBadge", () => {
  it("renders the all-clear state when no buckets are populated", () => {
    render(<SettlementAgingBadge aging={allClear} />);

    expect(screen.getByText(/all clear/i)).toBeInTheDocument();
    expect(
      screen.getByText(/no predictions stuck past their market close/i),
    ).toBeInTheDocument();
    // No total-past-close pill in the all-clear state.
    expect(screen.queryByTestId("settlement-aging-total")).toBeNull();
  });

  it("renders bucket counts and the total roll-up when predictions are stuck", () => {
    render(
      <SettlementAgingBadge
        aging={{
          bucket_0_to_1h: 3,
          bucket_1_to_6h: 5,
          bucket_6_to_24h: 0,
          bucket_beyond_24h: 0,
          total_pending_past_close: 8,
        }}
      />,
    );

    const total = screen.getByTestId("settlement-aging-total");
    expect(total).toHaveTextContent("8 past close");
    // Roll-up pill stays at "pending" tone when the worst bucket is <6h.
    expect(total.className).toMatch(/pending/);
    expect(screen.getByTestId("settlement-aging-bucket-0-1h")).toHaveTextContent("3");
    expect(screen.getByTestId("settlement-aging-bucket-1-6h")).toHaveTextContent("5");
    expect(screen.getByTestId("settlement-aging-bucket-6-24h")).toHaveTextContent("0");
    expect(screen.getByTestId("settlement-aging-bucket-beyond-24h")).toHaveTextContent("0");
  });

  it("escalates the roll-up tone to lost when 6-24h bucket is populated", () => {
    render(
      <SettlementAgingBadge
        aging={{
          bucket_0_to_1h: 1,
          bucket_1_to_6h: 0,
          bucket_6_to_24h: 2,
          bucket_beyond_24h: 0,
          total_pending_past_close: 3,
        }}
      />,
    );

    const total = screen.getByTestId("settlement-aging-total");
    expect(total.className).toMatch(/lost/);
  });

  it("escalates the roll-up tone to lost when 24h+ bucket is populated", () => {
    render(
      <SettlementAgingBadge
        aging={{
          bucket_0_to_1h: 0,
          bucket_1_to_6h: 0,
          bucket_6_to_24h: 0,
          bucket_beyond_24h: 4,
          total_pending_past_close: 4,
        }}
      />,
    );

    const total = screen.getByTestId("settlement-aging-total");
    expect(total.className).toMatch(/lost/);
  });
});

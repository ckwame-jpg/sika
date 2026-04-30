import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TradePage from "@/app/(product)/trade/page";
import { renderWithProviders } from "@/test/render";

vi.mock("@/components/layout/header", () => ({
  Header: () => <header data-testid="trade-page-header" />,
}));

vi.mock("@/components/filters/sport-filter-select", () => ({
  SportFilterSelect: () => <div data-testid="sport-filter-select" />,
  useSportQueryParam: () => ({ sport: "NBA" }),
}));

vi.mock("@/components/trade/trade-desk", () => ({
  TradeDesk: ({ sport }: { sport?: string }) => (
    <div data-testid="trade-desk" data-sport={sport ?? ""} />
  ),
}));

describe("TradePage layout", () => {
  it("does not make main the sticky scroll ancestor for the trade ticket rail", () => {
    renderWithProviders(<TradePage />);

    const main = screen.getByRole("main");
    expect(main).toHaveClass("flex-1");
    expect(main).toHaveClass("overflow-visible");
    expect(main).not.toHaveClass("overflow-y-auto");
    expect(screen.getByTestId("trade-desk")).toHaveAttribute("data-sport", "NBA");
  });
});

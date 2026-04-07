import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TradeTicket, type TradeSelection } from "@/components/trade/trade-ticket";
import { renderWithProviders } from "@/test/render";

vi.mock("@/components/positions/trade-dialog", () => ({
  TradeDialog: () => null,
}));

const selection: TradeSelection = {
  kind: "player_prop",
  ticker: "KXNBAPTS-DAVION-10",
  eventId: 1,
  marketTitle: "Davion Mitchell: 10+ points",
  eventName: "Miami Heat at Toronto Raptors",
  sportKey: "NBA",
  marketKind: "player_prop",
  displayLabel: "Davion Mitchell 10+ points",
  projectedSideLabel: null,
  selectedSide: "yes",
  selectedSideProbability: 0.721,
  entryPrice: 0.4,
  edge: 0.321,
  confidence: 0.76,
  kalshiUrl: "https://kalshi.com/markets/davion-10",
  subjectName: "Davion Mitchell",
  subjectTeam: "TOR",
  statKey: "points",
  threshold: 10,
};

describe("TradeTicket", () => {
  it("renders the selected market without any portfolio exposure cards", () => {
    renderWithProviders(<TradeTicket selection={selection} />);

    expect(screen.getByTestId("trade-ticket-title")).toHaveTextContent("Davion Mitchell 10+ points");
    expect(screen.getByText("Paper trade")).toBeInTheDocument();
    expect(screen.queryByText("Your Exposure")).not.toBeInTheDocument();
    expect(screen.queryByText("Event Context")).not.toBeInTheDocument();
  });
});

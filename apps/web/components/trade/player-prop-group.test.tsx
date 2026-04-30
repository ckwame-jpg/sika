import { useState } from "react";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { PlayerPropGroup } from "@/components/trade/player-prop-group";
import { davionMitchellProp, tradeDeskFixtureWithNonMonotonicGroup } from "@/test/fixtures/trade-fixtures";
import { renderWithProviders } from "@/test/render";

function PlayerPropHarness() {
  const [selectedTicker, setSelectedTicker] = useState<string | undefined>(undefined);

  return (
    <PlayerPropGroup
      player={davionMitchellProp}
      selectedTicker={selectedTicker}
      onSelectThreshold={(_, __, ___, threshold) => {
        setSelectedTicker((current) => (current === threshold.ticker ? undefined : threshold.ticker));
      }}
    />
  );
}

describe("PlayerPropGroup", () => {
  it("falls back to the best threshold and keeps only one selected chip active at a time", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PlayerPropHarness />);

    const card = screen.getByTestId("trade-prop-card");
    expect(within(card).queryByText("DM")).not.toBeInTheDocument();
    expect(within(card).queryByText("OVER")).not.toBeInTheDocument();
    expect(within(card).getByText("davion mitchell")).toBeInTheDocument();
    expect(within(card).getByText("tor")).toBeInTheDocument();
    expect(within(card).getByText("points")).toBeInTheDocument();
    expect(within(card).getByText("72.1%")).toBeInTheDocument();
    expect(within(card).getByTestId("trade-prop-summary-label")).toHaveTextContent("10+ points");
    expect(within(card).getByTestId("trade-prop-summary-edge")).toHaveTextContent("+32.1%");

    await user.click(within(card).getByRole("button", { name: "4+" }));
    expect(within(card).getByTestId("trade-prop-summary-label")).toHaveTextContent("4+ assists");
    expect(within(card).getByTestId("trade-prop-summary-edge")).toHaveTextContent("+4.4%");
    expect(within(card).getAllByTestId("trade-threshold-chip").filter((chip) => chip.getAttribute("aria-pressed") === "true")).toHaveLength(1);

    await user.click(within(card).getByRole("button", { name: "10+" }));
    expect(within(card).getByTestId("trade-prop-summary-label")).toHaveTextContent("10+ points");
    expect(within(card).getAllByTestId("trade-threshold-chip").filter((chip) => chip.getAttribute("aria-pressed") === "true")).toHaveLength(1);
  });

  it("suppresses non-monotonic stat ladders", () => {
    renderWithProviders(
      <PlayerPropGroup
        player={tradeDeskFixtureWithNonMonotonicGroup.events[0].player_props[1]}
        onSelectThreshold={() => undefined}
      />,
    );

    expect(screen.queryByText("rebounds")).not.toBeInTheDocument();
  });
});

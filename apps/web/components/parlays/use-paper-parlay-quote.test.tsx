import { describe, expect, it } from "vitest";
import type { TradeSelection } from "@/components/trade/trade-ticket";
import {
  paperParlayLegFingerprint,
  paperParlayQuoteRequest,
} from "./use-paper-parlay-quote";

function makeLeg(ticker: string, side: string, price: number): TradeSelection {
  return {
    kind: "player_prop",
    ticker,
    eventId: 1,
    marketTitle: ticker,
    eventName: "Event",
    sportKey: "NBA",
    marketKind: "player_prop",
    displayLabel: ticker,
    projectedSideLabel: null,
    selectedSide: side,
    selectedSideProbability: 0.6,
    entryPrice: price,
    edge: 0.1,
    confidence: 0.8,
    kalshiUrl: null,
  };
}

describe("paper parlay server quote request", () => {
  it("fingerprints ticker, normalized side, and snapshot price", () => {
    const first = paperParlayQuoteRequest([
      makeLeg("A", "YES", 0.5),
      makeLeg("B", "no", 0.4),
    ]);
    const repriced = paperParlayQuoteRequest([
      makeLeg("A", "YES", 0.5),
      makeLeg("B", "no", 0.41),
    ]);
    expect(first).not.toBeNull();
    expect(repriced).not.toBeNull();
    expect(first!.legs[0].side).toBe("yes");
    expect(paperParlayLegFingerprint(first!)).not.toBe(
      paperParlayLegFingerprint(repriced!),
    );
  });

  it("does not call the quote endpoint shape for incomplete or invalid legs", () => {
    expect(paperParlayQuoteRequest([makeLeg("A", "yes", 0.5)])).toBeNull();
    expect(
      paperParlayQuoteRequest([
        makeLeg("A", "yes", 0.5),
        makeLeg("B", "maybe", 0.4),
      ]),
    ).toBeNull();
  });
});

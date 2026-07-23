import { describe, expect, it } from "vitest";
import {
  estimateTakerFeeDollars,
  orderTotalExceedsCap,
  quantizeToCentPrice,
  worstCaseTakerFeeDollars,
} from "@/lib/kalshi-fees";

describe("quantizeToCentPrice", () => {
  it("snaps sub-cent prices to the 1¢ tick", () => {
    expect(quantizeToCentPrice(0.2899)).toBe(0.29); // +245 american odds
    expect(quantizeToCentPrice(0.157)).toBe(0.16); // implied combo price
    expect(quantizeToCentPrice(0.4)).toBe(0.4); // already aligned
  });

  it("clamps to the tradable 1¢–99¢ band", () => {
    expect(quantizeToCentPrice(0.001)).toBe(0.01);
    expect(quantizeToCentPrice(0.999)).toBe(0.99);
  });
});

describe("estimateTakerFeeDollars", () => {
  it.each([
    // Keep these vectors identical to apps/api/tests/test_kalshi_fees.py.
    [25, 0.4, 0.42, 0.42],
    [50, 0.5, 0.88, 0.88],
    [50, 0.75, 0.66, 0.88],
    [1, 0.01, 0.01, 0.01],
    [3, 0.99, 0.01, 0.06],
  ])(
    "matches the Python parity vector for %d contracts at %f",
    (quantity, price, expectedFee, expectedWorstCaseFee) => {
      expect(estimateTakerFeeDollars(quantity, price)).toBe(expectedFee);
      expect(worstCaseTakerFeeDollars(quantity, price)).toBe(expectedWorstCaseFee);
    },
  );

  it("returns zero for an invalid quantity", () => {
    expect(estimateTakerFeeDollars(0, 0.4)).toBe(0);
  });
});

describe("orderTotalExceedsCap", () => {
  it("allows a cent-denominated total exactly equal to the cap", () => {
    // JavaScript represents 0.40 + 0.02 as 0.42000000000000004.
    expect(orderTotalExceedsCap(0.4, 0.02, 0.42)).toBe(false);
    expect(orderTotalExceedsCap(0.4, 0.02, 0.41)).toBe(true);
  });
});

import { describe, expect, it } from "vitest";
import { estimateTakerFeeDollars, quantizeToCentPrice } from "@/lib/kalshi-fees";

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
  it("matches Kalshi's ceil-to-cent formula without float noise", () => {
    expect(estimateTakerFeeDollars(25, 0.4)).toBe(0.42); // 0.07·25·.4·.6
    expect(estimateTakerFeeDollars(0, 0.4)).toBe(0);
  });
});

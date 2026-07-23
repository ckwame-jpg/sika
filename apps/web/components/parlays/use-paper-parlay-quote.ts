"use client";

import useSWR from "swr";
import { keys, quotePaperParlay } from "@/lib/api";
import type { PaperParlayQuoteRequest } from "@/lib/types";
import type { TradeSelection } from "@/components/trade/trade-ticket";

export function usePaperParlayServerQuote(legs: readonly TradeSelection[]) {
  const request = paperParlayQuoteRequest(legs);
  const fingerprint = request ? paperParlayLegFingerprint(request) : null;
  const quoteKey = fingerprint ? keys.paperParlayQuote(fingerprint) : null;
  const response = useSWR(
    quoteKey,
    () => quotePaperParlay(request!),
    {
      keepPreviousData: false,
      shouldRetryOnError: false,
    },
  );
  return { ...response, quoteRequest: request, quoteKey };
}

export function paperParlayQuoteRequest(
  legs: readonly TradeSelection[],
): PaperParlayQuoteRequest | null {
  if (legs.length < 2 || legs.length > 6) return null;
  const requestLegs: PaperParlayQuoteRequest["legs"] = [];
  for (const leg of legs) {
    const side = leg.selectedSide.trim().toLowerCase();
    const price = leg.entryPrice;
    if (
      (side !== "yes" && side !== "no") ||
      price == null ||
      !Number.isFinite(price) ||
      price <= 0 ||
      price >= 1
    ) {
      return null;
    }
    requestLegs.push({
      ticker: leg.ticker,
      side,
      suggested_price: price,
    });
  }
  return { legs: requestLegs };
}

export function paperParlayLegFingerprint(
  request: PaperParlayQuoteRequest,
): string {
  return JSON.stringify(
    request.legs.map((leg) => [leg.ticker, leg.side, leg.suggested_price]),
  );
}

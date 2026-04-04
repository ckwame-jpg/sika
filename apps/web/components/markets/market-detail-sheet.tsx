"use client";

import { useState } from "react";
import useSWR from "swr";
import { fetchMarket, keys } from "@/lib/api";
import type { MarketDetailRead, RecommendationRead } from "@/lib/types";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetBody,
} from "@/components/ui/sheet";
import { Badge, SportBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { PriceChart } from "./price-chart";
import {
  fmtPercent,
  fmtEdge,
  fmtVolume,
  fmtDatetime,
  fmtRelative,
  edgeClass,
  sideClass,
} from "@/lib/utils";
import { cn } from "@/lib/utils";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { usePriceDisplay } from "@/lib/price-display";
import { EDGE_EXPLANATION, ENTRY_LABEL, HEURISTIC_RELIABILITY_EXPLANATION, RELIABILITY_LABEL, WIN_PROB_LABEL } from "@/lib/market-copy";

interface StatRowProps {
  label: string;
  value: React.ReactNode;
}

function StatRow({ label, value }: StatRowProps) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="font-mono text-xs text-foreground">{value}</span>
    </div>
  );
}

interface MarketDetailSheetProps {
  ticker: string | null;
  onClose: () => void;
}

function MarketDetailContent({ ticker }: { ticker: string }) {
  const { formatPrice } = usePriceDisplay();
  const [tradeRec, setTradeRec] = useState<RecommendationRead | null>(null);
  const { data, isLoading, error } = useSWR<MarketDetailRead>(
    keys.market(ticker),
    () => fetchMarket(ticker),
    { refreshInterval: 15_000 },
  );

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-48 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <p className="text-xs text-negative">
        Failed to load market: {ticker}
      </p>
    );
  }

  const snap = data.latest_snapshot;
  const signal = data.latest_signal;
  const latestRecommendation = data.recommendations[0] ?? null;
  const isOpen = data.status === "open";
  const confidenceSemantics = String(signal?.scoring_diagnostics?.confidence_semantics ?? "heuristic_reliability");

  return (
    <div className="space-y-5 animate-fade-in">
      {/* Meta */}
      <div className="flex flex-wrap items-center gap-1.5">
        {data.sport_key && <SportBadge sport={data.sport_key} />}
        <Badge variant={isOpen ? "positive" : "default"}>
          {data.status}
        </Badge>
        {data.market_kind && (
          <Badge variant="outline">{data.market_kind}</Badge>
        )}
      </div>

      {/* Price history chart */}
      <div>
        <p className="mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">
          Price History
        </p>
        <PriceChart ticker={ticker} />
      </div>

      <Separator />

      {/* Snapshot */}
      {snap ? (
        <div>
          <p className="mb-1 text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Latest Snapshot
          </p>
          <div className="divide-y divide-border rounded border border-border">
            <div className="grid grid-cols-2">
              <div className="p-2 border-r border-border">
                <p className="text-xs text-muted-foreground">Yes Bid</p>
                <p className="font-mono text-sm text-foreground">{formatPrice(snap.yes_bid)}</p>
              </div>
              <div className="p-2">
                <p className="text-xs text-muted-foreground">Yes Ask</p>
                <p className="font-mono text-sm text-foreground">{formatPrice(snap.yes_ask)}</p>
              </div>
            </div>
            <div className="grid grid-cols-3">
              <div className="p-2 border-r border-border">
                <p className="text-xs text-muted-foreground">Last</p>
                <p className="font-mono text-sm text-foreground">{formatPrice(snap.last_price)}</p>
              </div>
              <div className="p-2 border-r border-border">
                <p className="text-xs text-muted-foreground">Volume</p>
                <p className="font-mono text-sm text-foreground">{fmtVolume(snap.volume)}</p>
              </div>
              <div className="p-2">
                <p className="text-xs text-muted-foreground">OI</p>
                <p className="font-mono text-sm text-foreground">{fmtVolume(snap.open_interest)}</p>
              </div>
            </div>
          </div>
          <p className="mt-1 text-right text-xs text-muted-foreground">
            Updated {fmtRelative(snap.captured_at)}
          </p>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">No snapshot available</p>
      )}

      {/* Signal */}
      {signal && (
        <>
          <Separator />
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">
              Signal — {signal.model_name}
            </p>
            {latestRecommendation?.source_badge_label && (
              <div className="mb-2">
                <Badge variant="outline">{latestRecommendation.source_badge_label}</Badge>
              </div>
            )}
            <div className="space-y-0.5">
              <StatRow label="Fair Yes" value={formatPrice(signal.fair_yes_price)} />
              <StatRow label="Fair No" value={formatPrice(signal.fair_no_price)} />
              {latestRecommendation && (
                <StatRow
                  label={WIN_PROB_LABEL}
                  value={<span>{fmtPercent(latestRecommendation.selected_side_probability)}</span>}
                />
              )}
              <StatRow
                label="Edge"
                value={
                  <span className={edgeClass(signal.edge)}>
                    {fmtEdge(signal.edge)}
                  </span>
                }
              />
              <StatRow
                label={confidenceSemantics === "calibrated_probability" ? "Confidence" : "Heuristic reliability"}
                value={
                  <span>{fmtPercent(signal.confidence)}</span>
                }
              />
            </div>
            <p className="mt-2 text-xs text-muted-foreground">{EDGE_EXPLANATION}</p>
            {confidenceSemantics !== "calibrated_probability" && (
              <p className="mt-2 text-xs text-muted-foreground">{HEURISTIC_RELIABILITY_EXPLANATION}</p>
            )}
            {signal.reasons.length > 0 && (
              <ul className="mt-2 space-y-0.5">
                {signal.reasons.map((r, i) => (
                  <li key={i} className="flex items-start gap-1.5 text-xs text-muted-foreground">
                    <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-accent/60" />
                    {r}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}

      {/* Recommendations */}
      {data.recommendations.length > 0 && (
        <>
          <Separator />
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">
              Recommendations
            </p>
            <div className="space-y-2">
              {data.recommendations.slice(0, 5).map((rec) => (
                <div
                  key={rec.id}
                  className="rounded border border-border bg-surface-hover p-2.5 text-xs space-y-1.5"
                >
                  <div className="flex items-center gap-2">
                    <span className={cn("font-mono font-medium", sideClass(rec.side))}>
                      {rec.side.toUpperCase()}
                    </span>
                    {rec.source_badge_label && <Badge variant="outline">{rec.source_badge_label}</Badge>}
                    <span className="text-muted-foreground">{ENTRY_LABEL}</span>
                    <span className="font-mono">{formatPrice(rec.suggested_price)}</span>
                    <span className={cn("ml-auto font-medium", edgeClass(rec.edge))}>
                      {fmtEdge(rec.edge)} edge
                    </span>
                  </div>
                  <p className="text-foreground">{rec.display_market_title ?? rec.market_title}</p>
                  <p className="font-mono text-[11px] text-muted-foreground">
                    {WIN_PROB_LABEL} {fmtPercent(rec.selected_side_probability)} · {RELIABILITY_LABEL} {fmtPercent(rec.confidence)}
                  </p>
                  <p className="text-muted-foreground leading-relaxed">{rec.rationale}</p>
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-xs text-muted-foreground/60">
                      {fmtDatetime(rec.captured_at)}
                    </p>
                    <Button
                      variant="secondary"
                      size="xs"
                      onClick={() => setTradeRec(rec)}
                    >
                      Trade
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {/* Close time */}
      {data.close_time && (
        <p className="text-xs text-muted-foreground">
          Closes {fmtDatetime(data.close_time)}
        </p>
      )}

      <TradeDialog
        open={tradeRec != null}
        onOpenChange={(open) => {
          if (!open) setTradeRec(null);
        }}
        defaults={tradeRec != null ? {
          destination: "paper",
          ticker: tradeRec.ticker,
          side: tradeRec.side.toLowerCase(),
          price: tradeRec.suggested_price,
        } : undefined}
        description={tradeRec != null
          ? `Route ${tradeRec.market_title} to paper or demo.`
          : "Choose whether to route this trade to paper or demo."}
      />
    </div>
  );
}

export function MarketDetailSheet({ ticker, onClose }: MarketDetailSheetProps) {
  return (
    <Sheet open={ticker !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right">
        <SheetHeader>
          <SheetTitle className="font-mono text-xs text-muted-foreground">
            {ticker ?? ""}
          </SheetTitle>
          <SheetDescription>
            Market detail — price history, signal & recommendations
          </SheetDescription>
        </SheetHeader>
        <SheetBody>
          {ticker && <MarketDetailContent ticker={ticker} />}
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}

"use client";

import useSWR from "swr";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { fetchMarketHistory, keys } from "@/lib/api";
import type { MarketHistoryRead } from "@/lib/types";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { format, parseISO } from "date-fns";
import { useMemo, useState } from "react";
import { usePriceDisplay } from "@/lib/price-display";
import { getChartPalette } from "@/lib/chart-colors";

const RANGES = ["1D", "7D", "30D"] as const;
type Range = (typeof RANGES)[number];

interface PriceChartProps {
  ticker: string;
}

function CustomTooltip({
  active,
  payload,
  label,
  formatPrice,
}: {
  active?: boolean;
  payload?: Array<{ value: number; dataKey: string }>;
  label?: string;
  formatPrice: (price: number | null | undefined) => string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-border bg-surface px-3 py-2 shadow-elevated text-xs">
      <p className="text-muted-foreground mb-1">
        {label ? format(parseISO(label), "MMM d, h:mm a") : ""}
      </p>
      {payload.map((p) => (
        <p key={p.dataKey} className="font-mono text-foreground">
          {p.dataKey === "yes_bid" && "Yes Bid: "}
          {p.dataKey === "yes_ask" && "Yes Ask: "}
          {p.dataKey === "last_price" && "Last: "}
          {p.dataKey === "mean_price" && "Mean: "}
          {formatPrice(p.value)}
        </p>
      ))}
    </div>
  );
}

export function PriceChart({ ticker }: PriceChartProps) {
  const { formatPrice } = usePriceDisplay();
  const palette = useMemo(getChartPalette, []);
  const [range, setRange] = useState<Range>("1D");
  const { data, isLoading } = useSWR<MarketHistoryRead>(
    keys.marketHistory(ticker, range),
    () => fetchMarketHistory(ticker, range),
    { refreshInterval: 30_000 },
  );

  if (isLoading) {
    return (
      <div className="space-y-2 p-1">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  const points = data?.points ?? [];
  const hasData = points.length > 0;

  const tickFormatter = (iso: string) => {
    try {
      if (range === "1D") return format(parseISO(iso), "h:mm a");
      return format(parseISO(iso), "MMM d");
    } catch {
      return iso;
    }
  };

  return (
    <div className="space-y-3">
      {/* Range selector */}
      <div className="flex items-center gap-1">
        {RANGES.map((r) => (
          <Button
            key={r}
            variant={range === r ? "primary" : "ghost"}
            size="xs"
            onClick={() => setRange(r)}
          >
            {r}
          </Button>
        ))}
      </div>

      {!hasData ? (
        <div className="flex h-40 items-center justify-center text-xs text-muted-foreground">
          No price history available
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={points} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <defs>
              <linearGradient id="yesGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={palette.line} stopOpacity={0.15} />
                <stop offset="95%" stopColor={palette.line} stopOpacity={0} />
              </linearGradient>
              <linearGradient id="meanGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={palette.positive} stopOpacity={0.12} />
                <stop offset="95%" stopColor={palette.positive} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke={palette.grid}
              vertical={false}
            />
            <XAxis
              dataKey="timestamp"
              tickFormatter={tickFormatter}
              tick={{ fontSize: 11, fill: palette.tick }}
              axisLine={false}
              tickLine={false}
              interval="preserveStartEnd"
            />
            <YAxis
              tickFormatter={(v) => formatPrice(v)}
              tick={{ fontSize: 11, fill: palette.tick }}
              axisLine={false}
              tickLine={false}
              domain={[0, 1]}
              ticks={[0.1, 0.25, 0.5, 0.75, 0.9]}
            />
            <Tooltip content={<CustomTooltip formatPrice={formatPrice} />} />
            {points.some((p) => p.mean_price != null) && (
              <Area
                type="monotone"
                dataKey="mean_price"
                stroke={palette.positive}
                strokeWidth={1.5}
                fill="url(#meanGradient)"
                dot={false}
                connectNulls
              />
            )}
            {points.some((p) => p.yes_bid != null) && (
              <Area
                type="monotone"
                dataKey="yes_bid"
                stroke={palette.line}
                strokeWidth={1.5}
                fill="url(#yesGradient)"
                dot={false}
                connectNulls
              />
            )}
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Header } from "@/components/layout/header";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { PriceDisplayMode, formatMarketPrice, usePriceDisplay } from "@/lib/price-display";

const DISPLAY_MODES: Array<{
  value: PriceDisplayMode;
  title: string;
  description: string;
}> = [
  {
    value: "american",
    title: "American odds",
    description: "Show prices as sportsbook-style odds like -110 and +400.",
  },
  {
    value: "prediction",
    title: "Prediction %",
    description: "Show prices as probabilities like 54.0%.",
  },
  {
    value: "kalshi",
    title: "Kalshi cents",
    description: "Show prices in Kalshi contract cents like 54¢.",
  },
];

export default function SettingsPage() {
  const { mode, setMode } = usePriceDisplay();

  return (
    <>
      <Header
        title="Settings"
        description="Display preferences for prices, odds, and trade inputs"
      />
      <main className="flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-3xl space-y-4">
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Price Display</h2>
                <p className="cosmos-panel-desc">
                  Choose how prices render across watchlist, markets, predictions, parlays, and trade dialogs.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body space-y-3">
              {DISPLAY_MODES.map((option) => {
                const active = option.value === mode;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setMode(option.value)}
                    className={cn(
                      "flex w-full items-start justify-between rounded-xl border px-4 py-4 text-left transition-colors",
                      active
                        ? "border-accent bg-accent/10"
                        : "border-border bg-surface hover:bg-surface-hover",
                    )}
                  >
                    <div>
                      <p className="text-sm font-medium text-foreground">{option.title}</p>
                      <p className="mt-1 text-xs text-muted-foreground">{option.description}</p>
                    </div>
                    <div className="text-right">
                      <p className="font-mono text-sm text-foreground">
                        {formatMarketPrice(0.52, option.value)}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Example for 52%
                      </p>
                    </div>
                  </button>
                );
              })}
            </div>
          </section>

          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Models</h2>
                <p className="cosmos-panel-desc">
                  Review ML family readiness, runtime health, shadow coverage, and fallback state.
                </p>
              </div>
              <Button variant="ghost" size="sm" asChild>
                <Link href="/settings/models" className="flex items-center gap-1">
                  Open
                  <ArrowRight size={12} />
                </Link>
              </Button>
            </div>
          </section>
        </div>
      </main>
    </>
  );
}

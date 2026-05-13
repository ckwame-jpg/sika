"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import useSWR from "swr";
import { Header } from "@/components/layout/header";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { PriceDisplayMode, formatMarketPrice, usePriceDisplay } from "@/lib/price-display";
import { fetchModelReadinessSummary, keys, updateModelReadinessSettings } from "@/lib/api";
import type { ModelReadinessSummaryRead } from "@/lib/types";

const PICK_HISTORY_DEPTH_OPTIONS = [5, 10, 20] as const;

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
  const { data: settings, mutate: refreshSettings } = useSWR<ModelReadinessSummaryRead>(
    keys.modelReadinessSummary,
    fetchModelReadinessSummary,
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );
  const currentDepth = settings?.pick_history_default_n ?? 5;

  async function selectDepth(next: number) {
    // Codex round-4 P2 on PR #24: previously this PATCH echoed back
    // the SWR-cached ``ml_serving_mode``. If another tab/operator
    // had flipped the mode in the meantime, a depth-only click here
    // would silently revert it. Send only the field we're actually
    // changing — the API now treats the mode as optional and leaves
    // it untouched when omitted.
    await updateModelReadinessSettings({
      pick_history_default_n: next,
    });
    await refreshSettings();
  }

  return (
    <>
      <Header title="Settings" />
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

          <section className="cosmos-panel" data-testid="pick-history-default-section">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Pick History Depth</h2>
                <p className="cosmos-panel-desc">
                  Default number of past games shown in the trade-ticket pick-history strip.
                  Per-pick toggles still override at runtime; this is the initial value when a
                  new pick is selected.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body">
              <div className="flex items-center gap-2">
                {PICK_HISTORY_DEPTH_OPTIONS.map((option) => {
                  const active = option === currentDepth;
                  return (
                    <button
                      key={option}
                      type="button"
                      onClick={() => void selectDepth(option)}
                      className={cn(
                        "rounded-lg border px-4 py-2 text-sm font-medium transition-colors",
                        active
                          ? "border-accent bg-accent/10 text-foreground"
                          : "border-border bg-surface text-muted-foreground hover:bg-surface-hover hover:text-foreground",
                      )}
                      data-testid={`pick-history-default-${option}`}
                      aria-pressed={active}
                    >
                      Last {option}
                    </button>
                  );
                })}
              </div>
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

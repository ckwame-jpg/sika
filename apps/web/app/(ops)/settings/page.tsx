"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import useSWR from "swr";
import { Header } from "@/components/layout/header";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  PriceDisplayMode,
  formatMarketPrice,
  usePriceDisplay,
} from "@/lib/price-display";
import {
  fetchModelReadinessSummary,
  keys,
  updateModelReadinessSettings,
} from "@/lib/api";
import type { ModelReadinessSummaryRead } from "@/lib/types";

const PICK_HISTORY_DEPTH_OPTIONS = [5, 10, 20] as const;
type PickHistoryDepth = (typeof PICK_HISTORY_DEPTH_OPTIONS)[number];

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

/** Shared chip class string for the small toggle chips (pick-history,
 *  narrator). Adopts the orphaned `.cosmos-chip` utility, sets text
 *  tone via the Tailwind `data-[active=true]:` variant, and pulls in
 *  the canonical `ring-focus` utility for keyboard-focus visibility.
 *  Defined once so the depth + narrator sections stay perfectly in
 *  sync — the only differences should be label + handler, not chrome. */
const CHIP_CLASS = cn(
  "cosmos-chip px-4 py-2 text-sm font-medium tracking-tight",
  "text-muted-foreground transition-colors hover:text-foreground",
  "data-[active=true]:text-foreground",
  "focus-visible:ring-focus",
);

export default function SettingsPage() {
  const { mode, setMode } = usePriceDisplay();
  const { data: settings, mutate: refreshSettings } =
    useSWR<ModelReadinessSummaryRead>(
      keys.modelReadinessSummary,
      fetchModelReadinessSummary,
      { revalidateOnFocus: false, revalidateOnReconnect: false },
    );
  const currentDepth = settings?.pick_history_default_n ?? 5;
  const narratorEnabled = settings?.narrator_enabled ?? false;

  async function selectDepth(next: PickHistoryDepth) {
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

  async function toggleNarrator(next: boolean): Promise<void> {
    // Smarter #31 — partial-PATCH idiom (same as depth above) so
    // flipping the narrator doesn't clobber other operator settings.
    await updateModelReadinessSettings({
      narrator_enabled: next,
    });
    await refreshSettings();
  }

  return (
    <>
      <Header title="Settings" />
      <main className="flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-3xl space-y-4">
          {/* Price Display — preview tiles using cosmos-chip chrome. */}
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Price Display</h2>
                <p className="cosmos-panel-desc">
                  Choose how prices render across watchlist, markets,
                  predictions, parlays, and trade dialogs.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body space-y-2">
              {DISPLAY_MODES.map((option) => {
                const active = option.value === mode;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setMode(option.value)}
                    aria-pressed={active}
                    data-active={active ? "true" : undefined}
                    className="cosmos-chip flex w-full items-center justify-between gap-4 px-4 py-3.5 text-left focus-visible:ring-focus"
                  >
                    <div className="min-w-0 space-y-0.5">
                      <p className="text-sm font-medium text-foreground">
                        {option.title}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {option.description}
                      </p>
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-0.5">
                      <span className="font-mono text-base font-medium tabular-nums tracking-tight text-foreground">
                        {formatMarketPrice(0.52, option.value)}
                      </span>
                      <span className="text-2xs uppercase tracking-[0.12em] text-muted-foreground/60">
                        Example for 52%
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          </section>

          {/* Pick History Depth — chip cluster. */}
          <section
            className="cosmos-panel"
            data-testid="pick-history-default-section"
          >
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Pick History Depth</h2>
                <p className="cosmos-panel-desc">
                  Default number of past games shown in the trade-ticket
                  pick-history strip. Per-pick toggles still override at
                  runtime; this is the initial value when a new pick is
                  selected.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body">
              <div
                className="flex flex-wrap items-center gap-2"
                role="group"
                aria-label="Pick history depth"
              >
                {PICK_HISTORY_DEPTH_OPTIONS.map((option) => {
                  const active = option === currentDepth;
                  return (
                    <button
                      key={option}
                      type="button"
                      onClick={() => void selectDepth(option)}
                      className={CHIP_CLASS}
                      data-active={active ? "true" : undefined}
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

          {/* AI Narrator — On/Off chip pair. */}
          <section
            className="cosmos-panel"
            data-testid="narrator-toggle-section"
          >
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">AI Narrator</h2>
                <p className="cosmos-panel-desc">
                  Adds a plain-English explanation under each
                  recommendation, grounded in the same features the
                  model uses. A verifier rejects any output that
                  references injuries, refs, weather, trades, or numbers
                  that aren't in the feature set. The mechanical
                  rationale is always shown alongside, so flipping this
                  off has no impact on what data you see.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body">
              <div
                className="flex items-center gap-2"
                role="group"
                aria-label="AI narrator toggle"
              >
                <button
                  type="button"
                  onClick={() => void toggleNarrator(true)}
                  className={CHIP_CLASS}
                  data-active={narratorEnabled ? "true" : undefined}
                  data-testid="narrator-toggle-on"
                  aria-pressed={narratorEnabled}
                >
                  On
                </button>
                <button
                  type="button"
                  onClick={() => void toggleNarrator(false)}
                  className={CHIP_CLASS}
                  data-active={!narratorEnabled ? "true" : undefined}
                  data-testid="narrator-toggle-off"
                  aria-pressed={!narratorEnabled}
                >
                  Off
                </button>
              </div>
            </div>
          </section>

          {/* Models — link panel, untouched but kept for visual
              rhythm with the chip sections above. */}
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Models</h2>
                <p className="cosmos-panel-desc">
                  Review ML family readiness, runtime health, shadow
                  coverage, and fallback state.
                </p>
              </div>
              <Button variant="ghost" size="sm" asChild>
                <Link
                  href="/settings/models"
                  className="flex items-center gap-1"
                >
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

"use client";

import { useState } from "react";
import { Header } from "@/components/layout/header";
import { KalshiAccountPanel } from "@/components/positions/kalshi-account-panel";
import { LegacyBucketPanel } from "@/components/positions/legacy-bucket-panel";
import { PaperBetsTable } from "@/components/positions/paper-bets-table";
import { PaperEarningsCard } from "@/components/positions/paper-earnings-card";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";

export default function PaperPositionsPage() {
  const [showNew, setShowNew] = useState(false);

  return (
    <>
      <Header
        title="Portfolio"
        actions={
          <Button
            variant="primary"
            size="sm"
            onClick={() => setShowNew(true)}
            className="gap-1.5"
          >
            <Plus size={13} />
            New Trade
          </Button>
        }
      />
      <main className="flex-1 overflow-y-auto p-3 sm:p-4">
        <div className="space-y-4">
          {/* Kalshi-side account (real money) — unchanged. */}
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Kalshi Account Picks</h2>
                <p className="cosmos-panel-desc">Live positions, account value, and fills</p>
              </div>
            </div>
            <div className="cosmos-panel-body flush">
              <KalshiAccountPanel />
            </div>
          </section>

          {/* Paper-side bookkeeping. The earnings tile mirrors the
              Kalshi panel's stat-tile rhythm (4 metrics, same card
              dimensions) so the page reads as one continuous
              portfolio view. The unified bets table follows
              underneath — singles and parlays merged into a single
              chronological feed. Demo orders are gone entirely
              (feature retired in an earlier phase). */}
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Paper Trade Earnings</h2>
                <p className="cosmos-panel-desc">
                  Simulated bets — no real money. Set a starting bankroll to see your earnings %.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body">
              <PaperEarningsCard />
            </div>
          </section>

          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Paper Bets</h2>
                <p className="cosmos-panel-desc">
                  All your simulated trades — single and parlay, open and settled. Parlay rows expand to show legs.
                </p>
              </div>
            </div>
            <div className="cosmos-panel-body flush">
              <PaperBetsTable />
            </div>
          </section>

          {/* Multi-user batch follow-up — pre-multi-user historical data
              (paper trades / demo orders / parlays with no owner).
              The component renders nothing when every legacy list is
              empty, so single-tenant deployments + fresh operators
              see no extra section. */}
          <LegacyBucketPanel />
        </div>
      </main>

      <TradeDialog
        open={showNew}
        onOpenChange={setShowNew}
        description="Open a paper trade for a single market."
      />
    </>
  );
}

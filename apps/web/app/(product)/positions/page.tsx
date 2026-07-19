"use client";

import { useState } from "react";
import { Header } from "@/components/layout/header";
import { KalshiAccountPanel } from "@/components/positions/kalshi-account-panel";
import { KalshiOrdersPanel } from "@/components/positions/kalshi-orders-panel";
import { LegacyBucketPanel } from "@/components/positions/legacy-bucket-panel";
import { PaperBetsTable } from "@/components/positions/paper-bets-table";
import { ExposureRail, PaperGaugeRow } from "@/components/positions/paper-earnings-card";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { Plus } from "lucide-react";

export default function PaperPositionsPage() {
  const [showNew, setShowNew] = useState(false);

  return (
    <>
      <Header
        title="Portfolio"
        actions={
          <button
            type="button"
            className="gi-btn"
            style={{ padding: "5px 12px", fontSize: 12, borderRadius: 10 }}
            onClick={() => setShowNew(true)}
          >
            <Plus size={13} />
            New Trade
          </button>
        }
      />
      <main className="flex-1 overflow-y-auto p-5">
        <div className="gi-screen">
          {/* Spec 5c gauge row: bankroll / at risk / 7d pnl / open bets. */}
          <PaperGaugeRow />

          <div className="gi-cols">
            <div className="gi-cols-main">
              <section className="gi-panel">
                <div className="gi-panel-head">
                  <span className="gi-glow-dot" style={{ "--gd": "var(--color-cosmos-violet-500)" } as React.CSSProperties} aria-hidden />
                  <h2 className="gi-panel-title">paper bets</h2>
                  <span className="gi-panel-sub">singles + parlays · parlay rows expand to show legs</span>
                </div>
                <PaperBetsTable />
              </section>

              <section className="gi-panel">
                <div className="gi-panel-head">
                  <span className="gi-glow-dot" aria-hidden />
                  <h2 className="gi-panel-title">kalshi account picks</h2>
                  <span className="gi-panel-sub">live positions, account value, and fills</span>
                </div>
                <div className="p-4">
                  <KalshiAccountPanel />
                </div>
              </section>

              {/* Real orders placed through sika — renders nothing until
                  Kalshi credentials are connected. */}
              <KalshiOrdersPanel />

              {/* Multi-user batch follow-up — pre-multi-user historical data
                  (paper trades / demo orders / parlays with no owner).
                  The component renders nothing when every legacy list is
                  empty, so single-tenant deployments + fresh operators
                  see no extra section. */}
              <LegacyBucketPanel />
            </div>

            <div className="gi-cols-rail hidden xl:block">
              <ExposureRail />
            </div>
          </div>
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

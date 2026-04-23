"use client";

import { useState } from "react";
import { Header } from "@/components/layout/header";
import { DemoOrdersTable } from "@/components/positions/demo-orders-table";
import { PaperPositionsTable } from "@/components/positions/paper-positions-table";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";

export default function PaperPositionsPage() {
  const [showNew, setShowNew] = useState(false);

  return (
    <>
      <Header
        title="Portfolio"
        description="Paper positions and demo orders"
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
        <div className="grid gap-4 xl:grid-cols-2">
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Paper Positions</h2>
                <p className="cosmos-panel-desc">Simulated trades without real money</p>
              </div>
            </div>
            <div className="cosmos-panel-body flush">
              <PaperPositionsTable />
            </div>
          </section>
          <section className="cosmos-panel">
            <div className="cosmos-panel-head">
              <div className="cosmos-panel-head-text">
                <h2 className="cosmos-panel-title">Demo Orders</h2>
                <p className="cosmos-panel-desc">Orders routed through the Kalshi demo environment</p>
              </div>
            </div>
            <div className="cosmos-panel-body flush">
              <DemoOrdersTable />
            </div>
          </section>
        </div>
      </main>

      <TradeDialog
        open={showNew}
        onOpenChange={setShowNew}
        defaults={{ destination: "paper" }}
        description="Create a new single-market trade and choose whether to send it to paper or demo."
      />
    </>
  );
}

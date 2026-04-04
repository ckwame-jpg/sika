"use client";

import { useState } from "react";
import { Header } from "@/components/layout/header";
import { DemoOrdersTable } from "@/components/positions/demo-orders-table";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";

export default function DemoOrdersPage() {
  const [showNew, setShowNew] = useState(false);

  return (
    <>
      <Header
        title="Demo Orders"
        description="Real Kalshi orders · manual approval required"
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
        <DemoOrdersTable />
      </main>

      <TradeDialog
        open={showNew}
        onOpenChange={setShowNew}
        defaults={{ destination: "demo" }}
        description="Create a new single-market trade and choose whether to route it to paper or demo."
      />
    </>
  );
}

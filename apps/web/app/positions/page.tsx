"use client";

import { useState } from "react";
import { Header } from "@/components/layout/header";
import { PaperPositionsTable } from "@/components/positions/paper-positions-table";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";

export default function PaperPositionsPage() {
  const [showNew, setShowNew] = useState(false);

  return (
    <>
      <Header
        title="Paper Positions"
        description="Simulated trades — no real money"
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
      <main className="flex-1 overflow-y-auto p-4">
        <PaperPositionsTable />
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

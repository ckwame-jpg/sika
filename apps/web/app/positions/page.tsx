"use client";

import { useState } from "react";
import { Header } from "@/components/layout/header";
import { DemoOrdersTable } from "@/components/positions/demo-orders-table";
import { PaperPositionsTable } from "@/components/positions/paper-positions-table";
import { TradeDialog } from "@/components/positions/trade-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Paper Positions</CardTitle>
                <CardDescription>Simulated trades without real money</CardDescription>
              </div>
            </CardHeader>
            <CardContent>
              <PaperPositionsTable />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Demo Orders</CardTitle>
                <CardDescription>Orders routed through the Kalshi demo environment</CardDescription>
              </div>
            </CardHeader>
            <CardContent>
              <DemoOrdersTable />
            </CardContent>
          </Card>
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

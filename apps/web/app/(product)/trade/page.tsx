"use client";

import { Suspense } from "react";
import { Header } from "@/components/layout/header";
import { useSportQueryParam } from "@/components/filters/sport-filter-select";
import { TradeDesk } from "@/components/trade/trade-desk";

function TradeContent() {
  const { sport } = useSportQueryParam();

  return (
    <div className="p-5">
      <TradeDesk sport={sport} />
    </div>
  );
}

export default function TradePage() {
  return (
    <>
      <Header title="Trade" />
      <main className="flex-1 overflow-visible">
        <Suspense>
          <TradeContent />
        </Suspense>
      </main>
    </>
  );
}

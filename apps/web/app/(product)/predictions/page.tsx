"use client";

import { Suspense } from "react";
import { Header } from "@/components/layout/header";
import { PredictionsDesk } from "@/components/predictions/predictions-desk";

export default function PredictionsPage() {
  return (
    <>
      <Header title="Predictions" />
      <main className="flex-1 overflow-y-auto p-4">
        <Suspense>
          <PredictionsDesk />
        </Suspense>
      </main>
    </>
  );
}

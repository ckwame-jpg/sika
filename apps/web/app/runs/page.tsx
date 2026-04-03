"use client";

import { Header } from "@/components/layout/header";
import { RunsDesk } from "@/components/runs/runs-desk";

export default function RunsPage() {
  return (
    <>
      <Header
        title="Runs"
        description="Refresh history, ingestion diagnostics, and emitted recommendations"
      />
      <main className="flex-1 overflow-hidden p-4">
        <RunsDesk />
      </main>
    </>
  );
}

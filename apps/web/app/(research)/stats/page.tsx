"use client";

import { Header } from "@/components/layout/header";
import { StatsWorkspace } from "@/components/stats/stats-workspace";

export default function StatsPage() {
  return (
    <>
      <Header title="Stats" description="How sharp has the sky been?" />
      <main className="flex-1 overflow-y-auto p-4">
        <StatsWorkspace />
      </main>
    </>
  );
}

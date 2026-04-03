"use client";

import { Header } from "@/components/layout/header";
import { StatsWorkspace } from "@/components/stats/stats-workspace";

export default function StatsPage() {
  return (
    <>
      <Header
        title="Stats"
        description="Global player query desk across NBA, MLB, NFL, Soccer, Tennis, and UFC"
      />
      <main className="flex-1 overflow-hidden p-4">
        <StatsWorkspace />
      </main>
    </>
  );
}

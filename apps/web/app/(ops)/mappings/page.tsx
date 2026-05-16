"use client";

import { Header } from "@/components/layout/header";
import { MappingsDesk } from "@/components/ops/mappings-desk";

export default function MappingsPage() {
  return (
    <>
      <Header title="Mappings" />
      <main className="flex-1 overflow-hidden p-4">
        <MappingsDesk />
      </main>
    </>
  );
}

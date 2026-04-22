"use client";

import { Suspense } from "react";
import { Sidebar } from "./sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { StarfieldCanvas } from "./starfield-canvas";
import { ShootingStars } from "./shooting-stars";
import { OrbitHint } from "./orbit-hint";

export function Shell({ children }: { children: React.ReactNode }) {
  return (
    <TooltipProvider delayDuration={300}>
      <StarfieldCanvas />
      <ShootingStars />
      <div className="app-shell">
        <Suspense>
          <Sidebar />
        </Suspense>
        <div className="main">{children}</div>
      </div>
      <OrbitHint />
    </TooltipProvider>
  );
}

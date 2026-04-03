"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type ContentView = "singles" | "parlays";

export function useViewQueryParam(defaultView: ContentView = "singles") {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const rawView = searchParams.get("view");
  const view: ContentView = rawView === "parlays" ? "parlays" : defaultView;

  function setView(nextView: ContentView) {
    const params = new URLSearchParams(searchParams.toString());
    if (nextView === defaultView) {
      params.delete("view");
    } else {
      params.set("view", nextView);
    }
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname);
  }

  return { view, setView };
}

export function ViewSwitch({
  view,
  onChange,
  className,
}: {
  view: ContentView;
  onChange: (view: ContentView) => void;
  className?: string;
}) {
  return (
    <div className={cn("inline-flex items-center gap-1 rounded-lg border border-border bg-surface p-1", className)}>
      <Button
        size="sm"
        variant={view === "singles" ? "secondary" : "ghost"}
        className="h-7 px-3"
        onClick={() => onChange("singles")}
      >
        Singles
      </Button>
      <Button
        size="sm"
        variant={view === "parlays" ? "secondary" : "ghost"}
        className="h-7 px-3"
        onClick={() => onChange("parlays")}
      >
        Parlays
      </Button>
    </div>
  );
}

"use client";

import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useHealthStatus } from "@/lib/health-status";

export function ApiBoundary({ children }: { children: React.ReactNode }) {
  const { error, isLoading, mutate } = useHealthStatus();

  if (isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-center">
          <RefreshCw size={20} className="animate-spin text-muted-foreground" />
          <p className="text-sm text-muted-foreground">Connecting to API...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <div className="flex max-w-md flex-col items-center gap-4 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-negative/10 text-lg text-negative">
            !
          </div>
          <div className="space-y-1.5">
            <h2 className="text-base font-semibold text-foreground">API unavailable</h2>
            <p className="text-sm text-muted-foreground">
              Unable to reach the backend. Make sure the API server is running on port 8000.
            </p>
          </div>
          <div className="flex gap-3">
            <Button variant="primary" size="sm" onClick={() => mutate()}>
              <RefreshCw size={13} className="mr-1.5" />
              Retry
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link href="/runs">View Runs</Link>
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}

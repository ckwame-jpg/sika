"use client";

import { useState } from "react";
import useSWR from "swr";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { disableAutoTrading, enableAutoTrading, fetchAutoTradingStatus, keys } from "@/lib/api";
import { useAdminToken } from "@/lib/admin-token";

function formatMoney(cents: number | null | undefined) {
  if (cents == null) return "—";
  return `$${(cents / 100).toFixed(2)}`;
}

export function LiveTradingBanner() {
  const admin = useAdminToken();
  const [busy, setBusy] = useState(false);
  const { data, mutate } = useSWR(
    admin.hasToken ? [keys.autoTradingStatus, admin.token, "global-banner"] : null,
    ([, token]) => fetchAutoTradingStatus(token),
    { refreshInterval: 30_000 },
  );

  if (!admin.hasToken || !data || (!data.enabled_by_env && !data.kill_switch_active)) {
    return null;
  }

  const handleDisable = async () => {
    setBusy(true);
    try {
      await disableAutoTrading(admin.token);
      await mutate();
    } finally {
      setBusy(false);
    }
  };

  const handleEnable = async () => {
    setBusy(true);
    try {
      await enableAutoTrading(admin.token);
      await mutate();
    } finally {
      setBusy(false);
    }
  };

  if (data.kill_switch_active) {
    return (
      <div className="flex items-center justify-between gap-3 border-b border-border bg-surface px-3 py-2 text-xs text-muted-foreground sm:px-5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Badge variant="negative">Live Trading Disabled</Badge>
          <span>Kill switch is active.</span>
        </div>
        <Button variant="positive" size="xs" onClick={handleEnable} disabled={busy}>
          Enable
        </Button>
      </div>
    );
  }

  if (!data.effective_enabled) {
    return (
      <div className="flex items-center justify-between gap-3 border-b border-warning/20 bg-warning/10 px-3 py-2 text-xs text-warning sm:px-5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Badge variant="warning">Live Trading Paused</Badge>
          <span>Environment is on, but trading is not currently effective.</span>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between gap-3 border-b border-negative/20 bg-negative/10 px-3 py-2 text-xs text-negative sm:px-5">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge variant="negative">Live Trading ON</Badge>
        <span>
          Real Kalshi orders may submit at {data.local_run_time} CT. Remaining today:{" "}
          <span className="font-mono">{formatMoney(data.remaining_budget_cents)}</span>.
        </span>
      </div>
      <Button variant="danger" size="xs" onClick={handleDisable} disabled={busy}>
        Disable
      </Button>
    </div>
  );
}

"use client";

import { useState } from "react";
import useSWR from "swr";
import { AdminTokenCard } from "@/components/admin/admin-token-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  disableAutoTrading,
  enableAutoTrading,
  fetchAutoTradeRuns,
  fetchAutoTradingStatus,
  keys,
  runAutoTradingNow,
} from "@/lib/api";
import { useAdminToken } from "@/lib/admin-token";
import type { AutoTradeDecisionRead, AutoTradeRunRead, AutoTradingStatusRead } from "@/lib/types";
import { fmtDatetime } from "@/lib/utils";

function formatMoney(cents: number | null | undefined) {
  if (cents == null) return "—";
  return `$${(cents / 100).toFixed(2)}`;
}

function statusVariant(status: string): "positive" | "negative" | "warning" | "default" {
  if (status === "completed") return "positive";
  if (status === "failed") return "negative";
  if (status === "running") return "warning";
  return "default";
}

function decisionSummary(decisions: AutoTradeDecisionRead[]) {
  const counts = decisions.reduce<Record<string, number>>((acc, item) => {
    const key = item.skip_reason || item.status;
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});
  return Object.entries(counts).slice(0, 6);
}

function RunSummary({ run }: { run: AutoTradeRunRead | null }) {
  if (!run) {
    return <p className="text-sm text-muted-foreground">No auto-trade run has been recorded yet.</p>;
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
        <span className="font-mono text-xs text-muted-foreground">
          Run #{run.id} · {run.local_trade_date} · {fmtDatetime(run.started_at)}
        </span>
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        <Metric label="Spent" value={formatMoney(run.spent_cents)} />
        <Metric label="Orders" value={`${run.submitted_order_count}`} />
        <Metric label="Candidates" value={`${run.candidate_count}`} />
      </div>
      {(run.skipped_reason || run.error_message) && (
        <p className="rounded border border-border bg-surface-hover px-3 py-2 text-xs text-muted-foreground">
          {run.error_message || run.skipped_reason}
        </p>
      )}
      {run.decisions.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {decisionSummary(run.decisions).map(([reason, count]) => (
            <Badge key={reason} variant="outline">
              {reason}: {count}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-border bg-surface-hover px-3 py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 font-mono text-sm text-foreground">{value}</p>
    </div>
  );
}

function StatusBody({
  status,
  runs,
  onRunNow,
  onDisable,
  onEnable,
  busy,
}: {
  status: AutoTradingStatusRead;
  runs: AutoTradeRunRead[];
  onRunNow: () => void;
  onDisable: () => void;
  onEnable: () => void;
  busy: boolean;
}) {
  const firstRunPending = status.effective_enabled && !status.latest_run;

  return (
    <div className="space-y-4">
      {status.effective_enabled && (
        <div className="rounded border border-negative/20 bg-negative/10 px-3 py-3 text-sm text-negative">
          Live trading can submit real Kalshi buy orders at {status.local_run_time} CT. Today&apos;s
          worst-case spend is capped at {formatMoney(status.daily_budget_cents)}.
          {firstRunPending ? " No live auto-trade run has executed yet." : ""}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1.5">
          <Badge variant={status.effective_enabled ? "positive" : "warning"}>
            {status.effective_enabled ? "Enabled" : "Not Trading"}
          </Badge>
          {status.kill_switch_active && <Badge variant="negative">Kill Switch</Badge>}
          <Badge variant={status.live_credentials_configured ? "positive" : "warning"}>
            {status.live_credentials_configured ? "Live Credentials" : "No Live Credentials"}
          </Badge>
          <Badge variant="outline">{status.market_scope}</Badge>
        </div>
        <div className="flex gap-2">
          {status.kill_switch_active ? (
            <Button size="sm" variant="positive" onClick={onEnable} disabled={busy}>
              Enable
            </Button>
          ) : (
            <Button size="sm" variant="secondary" onClick={onRunNow} disabled={busy}>
              Run Now
            </Button>
          )}
          {!status.kill_switch_active && (
            <Button size="sm" variant="danger" onClick={onDisable} disabled={busy}>
              Disable
            </Button>
          )}
          {status.kill_switch_active && (
            <Button size="sm" variant="secondary" onClick={onRunNow} disabled={busy}>
              Run Now
            </Button>
          )}
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-4">
        <Metric label="Daily Cap" value={formatMoney(status.daily_budget_cents)} />
        <Metric label="Used Today" value={formatMoney(status.spent_today_cents)} />
        <Metric label="Remaining" value={formatMoney(status.remaining_budget_cents)} />
        <Metric label="Run Time" value={`${status.local_run_time} CT`} />
      </div>

      <RunSummary run={status.latest_run} />

      {runs.length > 1 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground">Recent runs</p>
          <div className="space-y-1.5">
            {runs.slice(0, 5).map((run) => (
              <div key={run.id} className="flex items-center justify-between gap-3 rounded border border-border px-3 py-2 text-xs">
                <span className="font-mono text-muted-foreground">#{run.id} · {run.local_trade_date}</span>
                <span className="flex items-center gap-2">
                  <Badge variant={statusVariant(run.status)}>{run.status}</Badge>
                  <span className="font-mono">{formatMoney(run.spent_cents)}</span>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function AutoTradingPanel() {
  const admin = useAdminToken();
  const [busy, setBusy] = useState(false);
  const status = useSWR(
    admin.hasToken ? [keys.autoTradingStatus, admin.token] : null,
    ([, token]) => fetchAutoTradingStatus(token),
  );
  const runs = useSWR(
    admin.hasToken ? [keys.autoTradeRuns, admin.token] : null,
    ([, token]) => fetchAutoTradeRuns(token, 10),
  );

  const refresh = async () => {
    await Promise.all([status.mutate(), runs.mutate()]);
  };

  const handleRunNow = async () => {
    setBusy(true);
    try {
      await runAutoTradingNow(admin.token);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const handleDisable = async () => {
    setBusy(true);
    try {
      await disableAutoTrading(admin.token);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const handleEnable = async () => {
    setBusy(true);
    try {
      await enableAutoTrading(admin.token);
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  if (!admin.loaded) {
    return (
      <Card>
        <CardContent>
          <Skeleton className="h-24 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (!admin.hasToken) {
    return <AdminTokenCard onSubmit={admin.setToken} />;
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Live Auto-Trading</CardTitle>
          <CardDescription>
            Production trading is capped at $10 per local day.
          </CardDescription>
        </div>
        <Button variant="ghost" size="sm" onClick={admin.clearToken}>
          Lock
        </Button>
      </CardHeader>
      <CardContent>
        {status.error ? (
          <div className="space-y-3">
            <p className="rounded border border-negative/20 bg-negative/10 px-3 py-2 text-sm text-negative">
              {status.error.message}
            </p>
            <Button variant="secondary" size="sm" onClick={admin.clearToken}>
              Re-enter Token
            </Button>
          </div>
        ) : !status.data ? (
          <Skeleton className="h-32 w-full" />
        ) : (
          <StatusBody
            status={status.data}
            runs={runs.data ?? []}
            onRunNow={handleRunNow}
            onDisable={handleDisable}
            onEnable={handleEnable}
            busy={busy}
          />
        )}
      </CardContent>
    </Card>
  );
}

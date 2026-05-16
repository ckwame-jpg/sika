"use client";

// Smarter #25 — operator review queue for fuzzy market→event
// mappings. Two-pane layout matches the existing ops surfaces
// (``runs-desk``, ``readiness``): list on the left ordered by
// worst-confidence-first, detail drawer on the right with candidate
// breakdown + override form.

import { useEffect, useMemo, useState } from "react";
import useSWR, { mutate } from "swr";

import {
  fetchOpsMapping,
  fetchOpsMappings,
  keys,
  submitOpsMappingOverride,
} from "@/lib/api";
import type {
  MarketMappingListItemRead,
  MarketMappingStateRead,
} from "@/lib/types";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDatetime } from "@/lib/utils";
import { cn } from "@/lib/utils";

const CONFIDENCE_PRESETS: Array<{ label: string; value: number | undefined }> = [
  { label: "all", value: undefined },
  { label: "< 0.7", value: 0.7 },
  { label: "< 0.5", value: 0.5 },
  { label: "< 0.3", value: 0.3 },
];

const SPORT_PRESETS = ["all", "NBA", "MLB"];

function confidencePillClass(value: number | null): string {
  if (value == null) return "pending";
  if (value < 0.5) return "lost";
  if (value < 0.7) return "warning";
  return "settled";
}

function formatConfidence(value: number | null): string {
  if (value == null) return "—";
  return value.toFixed(2);
}

interface MappingsDeskState {
  maxConfidence: number | undefined;
  includeOverridden: boolean;
  sport: string;
  selectedTicker: string | null;
}

export function MappingsDesk() {
  const [state, setState] = useState<MappingsDeskState>({
    maxConfidence: 0.7,
    includeOverridden: false,
    sport: "all",
    selectedTicker: null,
  });

  const listOptions = useMemo(
    () => ({
      maxConfidence: state.maxConfidence,
      includeOverridden: state.includeOverridden,
      sport: state.sport === "all" ? undefined : state.sport,
    }),
    [state.maxConfidence, state.includeOverridden, state.sport],
  );

  const { data: rows, isLoading: rowsLoading } = useSWR<MarketMappingListItemRead[]>(
    keys.opsMappings(listOptions),
    () => fetchOpsMappings(listOptions),
    { refreshInterval: 30_000 },
  );

  // Auto-select the first row when the list loads or when the
  // current selection drops out of the filtered set.
  useEffect(() => {
    if (!rows || rows.length === 0) {
      if (state.selectedTicker !== null) {
        setState((prev) => ({ ...prev, selectedTicker: null }));
      }
      return;
    }
    const stillVisible = rows.some((row) => row.ticker === state.selectedTicker);
    if (!stillVisible) {
      setState((prev) => ({ ...prev, selectedTicker: rows[0].ticker }));
    }
  }, [rows, state.selectedTicker]);

  const { data: detail, isLoading: detailLoading } = useSWR<MarketMappingStateRead>(
    state.selectedTicker ? keys.opsMapping(state.selectedTicker) : null,
    () => fetchOpsMapping(state.selectedTicker as string),
  );

  return (
    <div className="grid h-full min-h-0 gap-4 overflow-auto xl:grid-cols-[420px_minmax(0,1fr)]">
      <section className="cosmos-panel relative z-10 min-h-0 overflow-hidden">
        <div className="cosmos-panel-head">
          <div className="cosmos-panel-head-text">
            <h2 className="cosmos-panel-title">Review queue</h2>
            <p className="cosmos-panel-desc">
              Auto-mapped markets ranked by ambiguity. Lowest confidence
              first.
            </p>
          </div>
        </div>
        <div className="cosmos-panel-body min-h-0">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              <span>conf</span>
              {CONFIDENCE_PRESETS.map((preset) => (
                <button
                  key={preset.label}
                  type="button"
                  onClick={() =>
                    setState((prev) => ({ ...prev, maxConfidence: preset.value }))
                  }
                  className={cn(
                    "rounded-md border px-2 py-0.5 text-xs",
                    state.maxConfidence === preset.value
                      ? "border-primary/60 bg-primary/10 text-foreground"
                      : "border-border/40 bg-transparent text-muted-foreground hover:text-foreground",
                  )}
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <Select
              value={state.sport}
              onValueChange={(value) =>
                setState((prev) => ({ ...prev, sport: value }))
              }
            >
              <SelectTrigger className="h-8 w-[120px]">
                <SelectValue placeholder="sport" />
              </SelectTrigger>
              <SelectContent>
                {SPORT_PRESETS.map((sport) => (
                  <SelectItem key={sport} value={sport}>
                    {sport === "all" ? "all sports" : sport}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={state.includeOverridden}
                onChange={(event) =>
                  setState((prev) => ({
                    ...prev,
                    includeOverridden: event.target.checked,
                  }))
                }
              />
              include overridden
            </label>
          </div>
          <ScrollArea className="min-h-0">
            {rowsLoading && (
              <div className="space-y-2">
                {Array.from({ length: 6 }).map((_, index) => (
                  <Skeleton key={index} className="h-12 w-full" />
                ))}
              </div>
            )}
            {!rowsLoading && rows && rows.length === 0 && (
              <div className="cosmos-table-empty">
                Nothing to review. Either every mapping cleared the
                threshold or the filters excluded everything.
              </div>
            )}
            {!rowsLoading && rows && rows.length > 0 && (
              <ul className="space-y-2">
                {rows.map((row) => {
                  const active = row.ticker === state.selectedTicker;
                  return (
                    <li key={row.ticker}>
                      <button
                        type="button"
                        onClick={() =>
                          setState((prev) => ({
                            ...prev,
                            selectedTicker: row.ticker,
                          }))
                        }
                        className={cn(
                          "w-full rounded-xl border px-3 py-2 text-left transition",
                          active
                            ? "border-primary/60 bg-primary/10"
                            : "border-border/40 hover:border-border/80 hover:bg-foreground/5",
                        )}
                      >
                        <div className="flex items-baseline justify-between gap-2">
                          <span className="font-mono text-xs">{row.ticker}</span>
                          <span
                            className={cn(
                              "outcome-pill",
                              confidencePillClass(row.mapping_confidence),
                            )}
                          >
                            {formatConfidence(row.mapping_confidence)}
                          </span>
                        </div>
                        <div className="mt-1 truncate text-sm text-foreground">
                          {row.title}
                        </div>
                        <div className="mt-0.5 text-xs text-muted-foreground">
                          {row.sport_key ?? "—"}
                          {" · "}
                          {row.event_name ?? "no event"}
                          {row.candidate_count > 0 && (
                            <>
                              {" · "}
                              {row.candidate_count} candidate
                              {row.candidate_count === 1 ? "" : "s"}
                            </>
                          )}
                          {row.mapping_overridden_at && (
                            <>
                              {" · "}
                              <span className="text-warning">overridden</span>
                            </>
                          )}
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </ScrollArea>
        </div>
      </section>

      <section className="cosmos-panel relative z-10 min-h-0 overflow-hidden">
        <div className="cosmos-panel-head">
          <div className="cosmos-panel-head-text">
            <h2 className="cosmos-panel-title">
              {state.selectedTicker ?? "Select a ticker"}
            </h2>
            <p className="cosmos-panel-desc">
              Candidate events the auto-mapper considered + manual override
              form.
            </p>
          </div>
        </div>
        <div className="cosmos-panel-body min-h-0">
          {state.selectedTicker == null && (
            <div className="cosmos-table-empty">
              Pick a market from the list to review its mapping candidates.
            </div>
          )}
          {state.selectedTicker != null && detailLoading && (
            <Skeleton className="h-48 w-full" />
          )}
          {state.selectedTicker != null && detail && (
            <MappingDetailCard
              detail={detail}
              listKey={keys.opsMappings(listOptions)}
            />
          )}
        </div>
      </section>
    </div>
  );
}

interface MappingDetailCardProps {
  detail: MarketMappingStateRead;
  listKey: string;
}

function MappingDetailCard({ detail, listKey }: MappingDetailCardProps) {
  const [submitting, setSubmitting] = useState<number | "clear" | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  async function applyOverride(eventId: number | null) {
    setSubmitting(eventId == null ? "clear" : eventId);
    setSubmitError(null);
    try {
      await submitOpsMappingOverride(detail.ticker, {
        event_id: eventId,
        reason: reason.trim() ? reason.trim() : null,
      });
      await Promise.all([
        mutate(keys.opsMapping(detail.ticker)),
        mutate(listKey),
      ]);
      setReason("");
    } catch (error) {
      setSubmitError(
        error instanceof Error ? error.message : "Override failed.",
      );
    } finally {
      setSubmitting(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="stats-tile">
          <p className="stats-tile-label">Confidence</p>
          <p className="stats-tile-value font-mono text-lg">
            {formatConfidence(detail.mapping_confidence)}
          </p>
        </div>
        <div className="stats-tile">
          <p className="stats-tile-label">Mapped event</p>
          <p className="stats-tile-value font-mono text-sm">
            {detail.event_id ?? "—"}
          </p>
        </div>
        <div className="stats-tile">
          <p className="stats-tile-label">Sport</p>
          <p className="stats-tile-value text-sm">{detail.sport_key ?? "—"}</p>
        </div>
        <div className="stats-tile">
          <p className="stats-tile-label">Overridden</p>
          <p className="stats-tile-value text-sm">
            {detail.mapping_overridden_at
              ? fmtDatetime(detail.mapping_overridden_at)
              : "no"}
          </p>
        </div>
      </div>

      {detail.mapping_overridden_reason && (
        <p className="text-xs text-muted-foreground">
          Override note: {detail.mapping_overridden_reason}
        </p>
      )}

      <div>
        <h3 className="cosmos-panel-title mb-2 text-sm">Candidates</h3>
        {detail.mapping_candidates.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No candidates were captured. The auto-mapper may have run before
            bug #17 added candidate persistence — clear the mapping and let
            the next refresh re-evaluate.
          </p>
        )}
        {detail.mapping_candidates.length > 0 && (
          <ul className="space-y-2">
            {detail.mapping_candidates.map((candidate) => {
              const isCurrent = candidate.event_id === detail.event_id;
              return (
                <li
                  key={candidate.event_id}
                  className={cn(
                    "flex items-start justify-between gap-3 rounded-xl border px-3 py-2",
                    isCurrent
                      ? "border-primary/60 bg-primary/10"
                      : "border-border/40",
                  )}
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm">
                      {candidate.event_name ?? `event #${candidate.event_id}`}
                    </div>
                    <div className="mt-0.5 text-xs text-muted-foreground">
                      event #{candidate.event_id}
                      {candidate.sport_key && ` · ${candidate.sport_key}`}
                      {candidate.time_delta_seconds != null && (
                        <>
                          {" "}· Δt {Math.round(candidate.time_delta_seconds)}s
                        </>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <span className="font-mono text-xs">
                      {candidate.score.toFixed(2)}
                    </span>
                    <Button
                      size="sm"
                      variant={isCurrent ? "ghost" : "primary"}
                      disabled={
                        submitting !== null || isCurrent
                      }
                      onClick={() => applyOverride(candidate.event_id)}
                    >
                      {submitting === candidate.event_id
                        ? "pinning…"
                        : isCurrent
                          ? "current"
                          : "pin"}
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="rounded-xl border border-border/40 px-3 py-2">
        <label className="text-xs text-muted-foreground" htmlFor="mapping-reason">
          Override note (optional)
        </label>
        <Input
          id="mapping-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={500}
          className="mt-1 h-8"
          placeholder="why this pin / why clearing"
        />
        <div className="mt-3 flex justify-between gap-2">
          <Button
            size="sm"
            variant="ghost"
            disabled={submitting !== null || detail.event_id == null}
            onClick={() => applyOverride(null)}
          >
            {submitting === "clear" ? "clearing…" : "clear mapping"}
          </Button>
        </div>
        {submitError && (
          <p className="mt-2 text-xs text-negative">{submitError}</p>
        )}
      </div>
    </div>
  );
}

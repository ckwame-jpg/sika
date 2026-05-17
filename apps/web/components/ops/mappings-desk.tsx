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

const SPORT_PRESETS = ["all", "NBA", "MLB", "WNBA"];

/** Shared cosmos-chip class for the confidence preset chips. Adopts
 *  the `.cosmos-chip` utility (globals.css:464-486); active state via
 *  the `data-active="true"` CSS attribute selector; canonical
 *  `focus-visible:ring-focus` for keyboard focus visibility.
 *  `min-h-[28px]` floors the touch target on phones — the original
 *  `py-0.5` produced ~22px which is below comfortable thumb size. */
const CONFIDENCE_CHIP_CLASS = cn(
  "cosmos-chip inline-flex min-h-[28px] items-center px-2 text-[11px] font-medium tracking-tight",
  "text-muted-foreground transition-colors hover:text-foreground",
  "data-[active=true]:text-foreground",
  "focus-visible:ring-focus",
);

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

  const { data: rows, isLoading: rowsLoading } = useSWR<
    MarketMappingListItemRead[]
  >(
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

  const { data: detail, isLoading: detailLoading } =
    useSWR<MarketMappingStateRead>(
      state.selectedTicker ? keys.opsMapping(state.selectedTicker) : null,
      () => fetchOpsMapping(state.selectedTicker as string),
    );

  return (
    <div className="grid h-full min-h-0 gap-4 overflow-auto xl:grid-cols-[420px_minmax(0,1fr)]">
      {/* Left pane — review queue. */}
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
          {/* Filter bar — wraps on narrow screens; ml-auto on the
              include-overridden label pushes it to the right edge of
              its line whether single-row or wrapped. */}
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] uppercase tracking-[0.14em] text-muted-foreground/70">
                conf
              </span>
              <div className="flex items-center gap-1">
                {CONFIDENCE_PRESETS.map((preset) => {
                  const active = state.maxConfidence === preset.value;
                  return (
                    <button
                      key={preset.label}
                      type="button"
                      onClick={() =>
                        setState((prev) => ({
                          ...prev,
                          maxConfidence: preset.value,
                        }))
                      }
                      className={CONFIDENCE_CHIP_CLASS}
                      data-active={active ? "true" : undefined}
                      aria-pressed={active}
                    >
                      {preset.label}
                    </button>
                  );
                })}
              </div>
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
            <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground">
              <input
                type="checkbox"
                checked={state.includeOverridden}
                onChange={(event) =>
                  setState((prev) => ({
                    ...prev,
                    includeOverridden: event.target.checked,
                  }))
                }
                className="h-3.5 w-3.5 cursor-pointer accent-accent focus-visible:ring-focus"
              />
              include overridden
            </label>
          </div>

          {/* Queue list */}
          <ScrollArea className="min-h-0">
            {rowsLoading && (
              <div className="space-y-2">
                {Array.from({ length: 6 }).map((_, index) => (
                  <Skeleton key={index} className="h-14 w-full" />
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
              <ul className="space-y-1.5">
                {rows.map((row) => {
                  const active = row.ticker === state.selectedTicker;
                  return (
                    <li key={row.ticker}>
                      <QueueRowButton
                        row={row}
                        active={active}
                        onSelect={() =>
                          setState((prev) => ({
                            ...prev,
                            selectedTicker: row.ticker,
                          }))
                        }
                      />
                    </li>
                  );
                })}
              </ul>
            )}
          </ScrollArea>
        </div>
      </section>

      {/* Right pane — detail. */}
      <section className="cosmos-panel relative z-10 min-h-0 overflow-hidden">
        <div className="cosmos-panel-head">
          <div className="cosmos-panel-head-text">
            <h2 className="cosmos-panel-title break-all">
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

interface QueueRowButtonProps {
  row: MarketMappingListItemRead;
  active: boolean;
  onSelect: () => void;
}

function QueueRowButton({ row, active, onSelect }: QueueRowButtonProps) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "relative w-full rounded-lg border px-3 py-2 text-left transition-colors",
        "focus-visible:ring-focus",
        active
          ? "border-accent/30 bg-accent/[0.04]"
          : "border-border/40 hover:border-border/80 hover:bg-white/[0.02]",
      )}
    >
      {/* Selection rail — audit-panel idiom. Only rendered when active
          so inactive rows don't carry visual noise. */}
      {active && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-accent/70"
        />
      )}
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate font-mono text-[11px] tracking-tight text-muted-foreground">
          {row.ticker}
        </span>
        <span
          className={cn(
            "outcome-pill",
            confidencePillClass(row.mapping_confidence),
          )}
        >
          {formatConfidence(row.mapping_confidence)}
        </span>
      </div>
      <div className="mt-1 line-clamp-2 break-words text-sm font-medium text-foreground">
        {row.title}
      </div>
      {/* Metadata line wraps on narrow screens — no `truncate` on
          event_name because `truncate` without `min-w-0` overflows
          flex-wrap containers, and clamping individual items here
          hides info the operator needs. */}
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10.5px] text-muted-foreground/80">
        <span className="font-mono uppercase tracking-[0.1em]">
          {row.sport_key ?? "—"}
        </span>
        <span className="opacity-40">·</span>
        <span className="break-words">{row.event_name ?? "no event"}</span>
        {row.candidate_count > 0 && (
          <>
            <span className="opacity-40">·</span>
            <span className="tabular-nums">
              {row.candidate_count} candidate
              {row.candidate_count === 1 ? "" : "s"}
            </span>
          </>
        )}
        {row.mapping_overridden_at && (
          <>
            <span className="opacity-40">·</span>
            <span className="rounded-sm border border-warning/30 bg-warning/10 px-1 py-px text-[9px] font-medium uppercase tracking-[0.1em] text-warning">
              overridden
            </span>
          </>
        )}
      </div>
    </button>
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
      {/* Stats grid — existing pattern (.stats-tile), untouched.
          2-col on mobile (sm-), 4-col on sm+. */}
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

      {/* Override note display — quiet informational chrome (the
          override was the operator's deliberate decision; the queue
          row's warning chip already signals "hand-edited"). */}
      {detail.mapping_overridden_reason && (
        <p className="rounded-md border border-border/50 bg-surface-hover/30 px-3 py-2 text-xs">
          <span className="text-[10px] uppercase tracking-[0.14em] text-muted-foreground/70">
            Override note
          </span>{" "}
          <span className="text-foreground/85">
            {detail.mapping_overridden_reason}
          </span>
        </p>
      )}

      {/* Candidates */}
      <div>
        <div className="mb-2 flex items-baseline justify-between gap-2">
          <h3 className="text-[10px] uppercase tracking-[0.14em] text-muted-foreground/80">
            Candidates
          </h3>
          {detail.mapping_candidates.length > 0 && (
            <span className="font-mono text-[10px] tabular-nums text-muted-foreground/60">
              {detail.mapping_candidates.length} evaluated
            </span>
          )}
        </div>
        {detail.mapping_candidates.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No candidates were captured. The auto-mapper may have run before
            bug #17 added candidate persistence — clear the mapping and let
            the next refresh re-evaluate.
          </p>
        )}
        {detail.mapping_candidates.length > 0 && (
          <ul className="space-y-1.5">
            {detail.mapping_candidates.map((candidate) => {
              const isCurrent = candidate.event_id === detail.event_id;
              const isPending = submitting === candidate.event_id;
              return (
                <li
                  key={candidate.event_id}
                  className={cn(
                    "relative flex items-start justify-between gap-3 rounded-lg border px-3 py-2",
                    isCurrent
                      ? "border-accent/30 bg-accent/[0.04]"
                      : "border-border/40",
                  )}
                >
                  {isCurrent && (
                    <span
                      aria-hidden
                      className="pointer-events-none absolute inset-y-1.5 left-0 w-[2px] rounded-full bg-accent/70"
                    />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="break-words text-sm font-medium text-foreground">
                      {candidate.event_name ?? `event #${candidate.event_id}`}
                    </div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[10.5px] text-muted-foreground/80">
                      <span className="font-mono tabular-nums">
                        event #{candidate.event_id}
                      </span>
                      {candidate.sport_key && (
                        <>
                          <span className="opacity-40">·</span>
                          <span className="font-mono uppercase tracking-[0.1em]">
                            {candidate.sport_key}
                          </span>
                        </>
                      )}
                      {candidate.time_delta_seconds != null && (
                        <>
                          <span className="opacity-40">·</span>
                          <span className="font-mono tabular-nums">
                            Δt {Math.round(candidate.time_delta_seconds)}s
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1.5">
                    <span className="font-mono text-xs font-medium tabular-nums tracking-tight text-foreground">
                      {candidate.score.toFixed(2)}
                    </span>
                    <Button
                      size="sm"
                      variant={isCurrent ? "ghost" : "primary"}
                      disabled={submitting !== null || isCurrent}
                      onClick={() => applyOverride(candidate.event_id)}
                    >
                      {isPending
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

      {/* Override action panel — note input + clear action. Full
          width on every breakpoint. */}
      <div className="rounded-lg border border-border/50 bg-surface-hover/30 p-3">
        <label
          className="text-[10px] uppercase tracking-[0.14em] text-muted-foreground/80"
          htmlFor="mapping-reason"
        >
          Override note (optional)
        </label>
        <Input
          id="mapping-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          maxLength={500}
          className="mt-1.5 h-8"
          placeholder="why this pin / why clearing"
        />
        <div className="mt-3 flex justify-end">
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
          <p
            role="alert"
            className="mt-2 rounded-md border border-negative/30 bg-negative/[0.05] px-2 py-1 text-xs text-negative"
          >
            {submitError}
          </p>
        )}
      </div>
    </div>
  );
}

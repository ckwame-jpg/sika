"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import {
  fetchModelReadinessSummary,
  fetchPlayerHistory,
  fetchTeamHistory,
  keys,
  type PickHistoryOptions,
} from "@/lib/api";
import { MiniBars, type MiniBarsTone } from "@/components/stats/mini-bars";
import type {
  ModelReadinessSummaryRead,
  StatsQueryRead,
  TeamGameResultRead,
  TeamHistoryRead,
} from "@/lib/types";
import type { TradeSelection } from "./trade-ticket";

/**
 * Inline last-N history for the selected pick.
 *
 *   - player_prop  → MiniBars colored pass/fail against the threshold.
 *   - game_line spread → margins chart, threshold = -numericLine, cover-aware.
 *   - game_line total  → event totals chart, threshold = |numericLine|,
 *                        over/under coloring by selectedSide.
 *   - game_line moneyline (or unknown game-line kind) → W/L pills row.
 *   - Otherwise (no usable identity) → renders nothing.
 *
 * Local state:
 *   - per-pick depth (5 | 10 | 20), initialised from the operator-wide
 *     default in /ops/models/readiness then overridable by the toggle.
 *   - opponent / location filter selection.
 *
 * SWR cache keys include the depth + filter values so each combination
 * caches independently and toggling back to a previously-viewed setting
 * is a cache hit.
 *
 * TODO(parlays): parlay picks do not flow through TradeSelection on /trade
 * today — they're surfaced on a separate parlays page entirely. When the
 * trade desk grows a parlay selection model, extend TradeSelection.kind
 * with "parlay" + a legs[] array and dispatch to a leg-stacking variant
 * here (each leg renders its own player-prop or game-line sub-strip).
 */

interface PickHistoryStripProps {
  selection: TradeSelection;
}

const HISTORY_OPTIONS = [5, 10, 20] as const;
type HistoryN = (typeof HISTORY_OPTIONS)[number];

function clampToHistoryOption(value: number | null | undefined): HistoryN {
  if (value === 10) return 10;
  if (value === 20) return 20;
  return 5;
}

export function PickHistoryStrip({ selection }: PickHistoryStripProps) {
  const { data: settings } = useSWR<ModelReadinessSummaryRead>(
    keys.modelReadinessSummary,
    fetchModelReadinessSummary,
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );
  const operatorDefault = clampToHistoryOption(settings?.pick_history_default_n ?? 5);
  const [override, setOverride] = useState<HistoryN | null>(null);
  const n: HistoryN = override ?? operatorDefault;

  const [location, setLocation] = useState<"home" | "away" | null>(null);
  const opponent = useMemo(() => inferOpponentName(selection), [selection]);
  const [opponentEnabled, setOpponentEnabled] = useState(false);
  const effectiveOpponent = opponentEnabled ? opponent : null;

  const controls: StripControlsProps = {
    n,
    onN: setOverride,
    location,
    onLocation: setLocation,
    opponent,
    opponentEnabled,
    onOpponentToggle: () => setOpponentEnabled((value) => !value),
  };

  if (selection.kind === "player_prop") {
    return (
      <PlayerPropStrip
        selection={selection}
        controls={controls}
        n={n}
        location={location}
        opponent={effectiveOpponent}
      />
    );
  }
  return (
    <GameLineStrip
      selection={selection}
      controls={controls}
      n={n}
      location={location}
      opponent={effectiveOpponent}
    />
  );
}

// -----------------------------------------------------------------------------
// Player prop strip

interface PlayerPropStripProps {
  selection: TradeSelection;
  controls: StripControlsProps;
  n: HistoryN;
  location: "home" | "away" | null;
  opponent: string | null;
}

function PlayerPropStrip({ selection, controls, n, location, opponent }: PlayerPropStripProps) {
  const subjectName = selection.subjectName;
  const statKey = selection.statKey;
  const threshold = selection.threshold;
  const ready = Boolean(subjectName && statKey && threshold != null);

  const opts: PickHistoryOptions = { location, opponent };

  const { data, error, isLoading } = useSWR<StatsQueryRead>(
    ready ? keys.playerHistory(subjectName!, selection.sportKey, n, opts) : null,
    () => fetchPlayerHistory(subjectName!, selection.sportKey, n, opts),
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );

  if (!ready) return null;
  if (isLoading) return <StripSkeleton n={n} controls={controls} />;
  if (error || !data) return null;

  const recentValues = data.game_logs
    .slice(0, n)
    .map((log) => log.metrics?.[statKey!])
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));

  if (recentValues.length === 0) return null;

  const thresholdNumber = Number(threshold);
  const clearedCount = recentValues.filter((value) => value >= thresholdNumber).length;
  const bandTone = (value: number): MiniBarsTone =>
    value >= thresholdNumber ? "high" : "low";

  return (
    <section className="pick-history-strip" data-testid="pick-history-strip">
      <StripHeader
        controls={controls}
        leadLine={
          <span>
            {statKey!.replace(/_/g, " ")} · last {recentValues.length} ·{" "}
            <span className="pick-history-strip-tally">
              {clearedCount}/{recentValues.length} cleared {thresholdNumber}+
            </span>
          </span>
        }
      />
      <MiniBars
        key={`player-${n}-${location ?? ""}-${opponent ?? ""}-${recentValues.length}`}
        points={recentValues}
        threshold={thresholdNumber}
        bandTone={bandTone}
        ariaLabel={`Last ${recentValues.length} ${statKey!.replace(/_/g, " ")} for ${subjectName}`}
      />
    </section>
  );
}

// -----------------------------------------------------------------------------
// Game-line strip — dispatches by marketKind

interface GameLineStripProps {
  selection: TradeSelection;
  controls: StripControlsProps;
  n: HistoryN;
  location: "home" | "away" | null;
  opponent: string | null;
}

function GameLineStrip({ selection, controls, n, location, opponent }: GameLineStripProps) {
  const teamName = inferTeamName(selection);
  if (teamName === null) return null;

  const opts: PickHistoryOptions = { location, opponent };

  const { data, error, isLoading } = useSWR<TeamHistoryRead>(
    keys.teamHistory(teamName, selection.sportKey, n, opts),
    () => fetchTeamHistory(teamName, selection.sportKey, n, opts),
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );

  if (isLoading) return <StripSkeleton n={n} controls={controls} />;
  if (error || !data || data.results.length === 0) return null;

  const recent = data.results.slice(0, n);
  const kind = (selection.marketKind || "").toLowerCase();
  const numericLine = selection.numericLine ?? null;
  const side = (selection.selectedSide || "").toLowerCase();

  if (kind === "spread" && numericLine !== null) {
    return (
      <SpreadChart
        team={data.team_name}
        recent={recent}
        numericLine={numericLine}
        side={side}
        controls={controls}
        n={n}
        location={location}
        opponent={opponent}
      />
    );
  }
  if (kind === "total" && numericLine !== null) {
    return (
      <TotalChart
        team={data.team_name}
        recent={recent}
        numericLine={numericLine}
        side={side}
        controls={controls}
        n={n}
        location={location}
        opponent={opponent}
      />
    );
  }

  // moneyline, first_five_winner, or any game-line pick missing numericLine.
  return (
    <MoneylinePills
      team={data.team_name}
      recent={recent}
      controls={controls}
      n={n}
      location={location}
      opponent={opponent}
    />
  );
}

// -----------------------------------------------------------------------------
// Game-line variants

interface ChartVariantProps {
  team: string;
  recent: TeamGameResultRead[];
  numericLine: number;
  side: string;
  controls: StripControlsProps;
  n: HistoryN;
  location: "home" | "away" | null;
  opponent: string | null;
}

function SpreadChart({
  team,
  recent,
  numericLine,
  side,
  controls,
  n,
  location,
  opponent,
}: ChartVariantProps) {
  const margins = recent.map((row) => row.team_score - row.opp_score);
  // numericLine is pre-signed from the picked side's perspective. Cover
  // condition: (margin) + numericLine > 0  →  margin > -numericLine.
  const coverThreshold = -numericLine;
  const outcomes = margins.map((margin) =>
    coverOutcome(margin, coverThreshold, "spread", side),
  );
  const coveredCount = outcomes.filter((tone) => tone === "high").length;

  return (
    <section className="pick-history-strip" data-testid="pick-history-strip">
      <StripHeader
        controls={controls}
        leadLine={
          <span>
            spread · last {margins.length} ·{" "}
            <span className="pick-history-strip-tally">
              {coveredCount}/{margins.length} covered
            </span>
          </span>
        }
      />
      <MiniBars
        key={`spread-${n}-${location ?? ""}-${opponent ?? ""}-${margins.length}`}
        points={margins}
        threshold={coverThreshold}
        bandTone={(_, index) => outcomes[index]}
        ariaLabel={`${team} last ${margins.length} margins vs cover line`}
      />
    </section>
  );
}

function TotalChart({
  team,
  recent,
  numericLine,
  side,
  controls,
  n,
  location,
  opponent,
}: ChartVariantProps) {
  const totals = recent.map((row) => row.team_score + row.opp_score);
  const absoluteLine = Math.abs(numericLine);
  const outcomes = totals.map((total) =>
    coverOutcome(total, absoluteLine, "total", side),
  );
  const overCount = outcomes.filter((tone) => tone === "high").length;
  const direction = side === "yes" ? "over" : "under";

  return (
    <section className="pick-history-strip" data-testid="pick-history-strip">
      <StripHeader
        controls={controls}
        leadLine={
          <span>
            total · last {totals.length} ·{" "}
            <span className="pick-history-strip-tally">
              {overCount}/{totals.length} {direction}
            </span>
          </span>
        }
      />
      <MiniBars
        key={`total-${n}-${location ?? ""}-${opponent ?? ""}-${totals.length}`}
        points={totals}
        threshold={absoluteLine}
        bandTone={(_, index) => outcomes[index]}
        ariaLabel={`${team} last ${totals.length} event totals vs line`}
      />
    </section>
  );
}

interface MoneylinePillsProps {
  team: string;
  recent: TeamGameResultRead[];
  controls: StripControlsProps;
  n: HistoryN;
  location: "home" | "away" | null;
  opponent: string | null;
}

function MoneylinePills({ team, recent, controls, n, location, opponent }: MoneylinePillsProps) {
  const wins = recent.filter((result) => result.result === "W").length;
  return (
    <section className="pick-history-strip" data-testid="pick-history-strip">
      <StripHeader
        controls={controls}
        leadLine={
          <span>
            {team} · last {recent.length} ·{" "}
            <span className="pick-history-strip-tally">
              {wins}-{recent.length - wins}
            </span>
          </span>
        }
      />
      <ol
        key={`pills-${n}-${location ?? ""}-${opponent ?? ""}-${recent.length}`}
        className="pick-history-strip-pills pick-history-strip-pills-animated"
        data-testid="pick-history-strip-pills"
      >
        {recent.map((row, index) => (
          <li
            key={`${row.game_date}-${index}`}
            className={`pick-history-strip-pill is-${row.result === "W" ? "win" : "loss"}`}
            data-result={row.result}
          >
            <span className="pick-history-strip-pill-letter">{row.result}</span>
            <span className="pick-history-strip-pill-score">
              {row.team_score}-{row.opp_score}
            </span>
            {row.opponent_abbreviation ? (
              <span className="pick-history-strip-pill-opp">
                {row.location === "away" ? "@" : "vs"}
                {row.opponent_abbreviation}
              </span>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}

// -----------------------------------------------------------------------------
// Header + controls + skeleton

interface StripControlsProps {
  n: HistoryN;
  onN: (n: HistoryN) => void;
  location: "home" | "away" | null;
  onLocation: (location: "home" | "away" | null) => void;
  opponent: string | null;
  opponentEnabled: boolean;
  onOpponentToggle: () => void;
}

function StripHeader({
  controls,
  leadLine,
}: {
  controls: StripControlsProps;
  leadLine: React.ReactNode;
}) {
  return (
    <header className="pick-history-strip-h">
      {leadLine}
      <div className="pick-history-strip-controls">
        <div className="pick-history-strip-n-toggle" role="group" aria-label="History depth">
          {HISTORY_OPTIONS.map((option) => (
            <button
              key={option}
              type="button"
              className={`pick-history-strip-n-pill${controls.n === option ? " is-active" : ""}`}
              onClick={() => controls.onN(option)}
              data-testid={`pick-history-strip-n-${option}`}
              aria-pressed={controls.n === option}
            >
              {option}
            </button>
          ))}
        </div>
        <div className="pick-history-strip-filter-chips" role="group" aria-label="History filters">
          <FilterChip
            active={controls.location === null && !controls.opponentEnabled}
            label="all"
            onClick={() => {
              controls.onLocation(null);
              if (controls.opponentEnabled) controls.onOpponentToggle();
            }}
            testId="pick-history-strip-filter-all"
          />
          <FilterChip
            active={controls.location === "home"}
            label="home"
            onClick={() => controls.onLocation(controls.location === "home" ? null : "home")}
            testId="pick-history-strip-filter-home"
          />
          <FilterChip
            active={controls.location === "away"}
            label="away"
            onClick={() => controls.onLocation(controls.location === "away" ? null : "away")}
            testId="pick-history-strip-filter-away"
          />
          {controls.opponent ? (
            <FilterChip
              active={controls.opponentEnabled}
              label={`vs ${controls.opponent}`}
              onClick={controls.onOpponentToggle}
              testId="pick-history-strip-filter-opponent"
            />
          ) : null}
        </div>
      </div>
    </header>
  );
}

function FilterChip({
  active,
  label,
  onClick,
  testId,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  testId: string;
}) {
  return (
    <button
      type="button"
      className={`pick-history-strip-chip${active ? " is-active" : ""}`}
      onClick={onClick}
      data-testid={testId}
      aria-pressed={active}
    >
      {label}
    </button>
  );
}

function StripSkeleton({ n, controls }: { n: HistoryN; controls: StripControlsProps }) {
  return (
    <section
      className="pick-history-strip is-loading"
      data-testid="pick-history-strip-skeleton"
      aria-busy="true"
    >
      <StripHeader controls={controls} leadLine={<span>last {n}</span>} />
      <div className="pick-history-strip-pills">
        {Array.from({ length: n }).map((_, index) => (
          <div key={index} className="pick-history-strip-pill is-placeholder" />
        ))}
      </div>
    </section>
  );
}

// -----------------------------------------------------------------------------
// Helpers

/**
 * Sign-correct cover/over outcome.
 *
 * Spread:
 *   side === "yes" → cover when `margin > coverThreshold`. Push at equality.
 *   side === "no"  → cover when `margin < coverThreshold`. Push at equality.
 *
 * Total:
 *   side === "yes" (over)  → "high" when value > threshold; push at equality.
 *   side === "no"  (under) → "high" when value < threshold; push at equality.
 *
 * Returns "mid" on exact pushes so the strip visually distinguishes them
 * from outright wins/losses.
 */
export function coverOutcome(
  value: number,
  threshold: number,
  market: "spread" | "total",
  side: string,
): MiniBarsTone {
  const normalizedSide = side.toLowerCase();
  if (value === threshold) return "mid";
  if (market === "spread") {
    if (normalizedSide === "yes") return value > threshold ? "high" : "low";
    return value < threshold ? "high" : "low";
  }
  // total
  if (normalizedSide === "yes") return value > threshold ? "high" : "low";
  return value < threshold ? "high" : "low";
}

/**
 * Parse the picked team's name from a game-line selection.
 * Strategy: prefer marketTitle (full team name + market kind suffix); fall
 * back to displayLabel (short form like "Cavaliers ML").
 */
function inferTeamName(selection: TradeSelection): string | null {
  const title = selection.marketTitle?.trim();
  if (title) {
    const kindKeywords = ["moneyline", "spread", "total", "over", "under", "puck line", "run line"];
    const lower = title.toLowerCase();
    let cutIndex = title.length;
    for (const keyword of kindKeywords) {
      const idx = lower.indexOf(keyword);
      if (idx > 0 && idx < cutIndex) cutIndex = idx;
    }
    const candidate = title.slice(0, cutIndex).trim().replace(/[·:-]+$/, "").trim();
    if (candidate.length >= 2) return candidate;
  }
  const label = selection.displayLabel?.trim();
  if (label) {
    const stripped = label.replace(/\s+(ML|moneyline|spread|total|over|under|[+-]?\d.*)$/i, "").trim();
    if (stripped.length >= 2) return stripped;
  }
  return null;
}

/**
 * Parse the opposing team's short name from the selection's eventName
 * ("Detroit Pistons at Cleveland Cavaliers"). Returns null if we can't
 * find a sensible opponent (used to decide whether to render the vs chip).
 */
function inferOpponentName(selection: TradeSelection): string | null {
  const eventName = (selection.eventName || "").trim();
  if (!eventName) return null;
  const teamName = inferTeamName(selection) ?? selection.subjectTeam ?? "";
  const separator = " at ";
  const idx = eventName.toLowerCase().indexOf(separator);
  if (idx === -1) return null;
  const away = eventName.slice(0, idx).trim();
  const home = eventName.slice(idx + separator.length).trim();
  const teamLower = teamName.toLowerCase();
  if (teamLower && teamLower === home.toLowerCase()) return away || null;
  if (teamLower && teamLower === away.toLowerCase()) return home || null;
  // Fuzzy match: use whichever side doesn't contain the picked team name.
  if (teamLower && home.toLowerCase().includes(teamLower)) return away || null;
  if (teamLower && away.toLowerCase().includes(teamLower)) return home || null;
  // For player props we don't know the team — fall back to the home side
  // as a useful default opponent chip.
  return home || away || null;
}

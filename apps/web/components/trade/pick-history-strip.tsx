"use client";

import { useState } from "react";
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
 *   - location filter (all/home/away).
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
  const [location, setLocation] = useState<"home" | "away" | null>(null);

  // Codex round-1 P3 on PR #24: per-pick overrides shouldn't leak
  // across selections. When the operator opens a new ticket, fall
  // back to the operator default (clear ``override``) and reset the
  // home/away filter so the strip behaves as if it's a fresh pick.
  // Use the "store info from previous renders" pattern instead of
  // ``useEffect`` so the reset is synchronous — otherwise the first
  // SWR call after a selection change still uses the stale override
  // and we waste a network request on the wrong depth.
  const [lastSeenTicker, setLastSeenTicker] = useState(selection.ticker);
  if (lastSeenTicker !== selection.ticker) {
    setLastSeenTicker(selection.ticker);
    setOverride(null);
    setLocation(null);
  }
  const effectiveOverride: HistoryN | null =
    lastSeenTicker === selection.ticker ? override : null;
  const effectiveLocation: "home" | "away" | null =
    lastSeenTicker === selection.ticker ? location : null;
  const n: HistoryN = effectiveOverride ?? operatorDefault;

  const controls: StripControlsProps = {
    n,
    onN: setOverride,
    location: effectiveLocation,
    onLocation: setLocation,
  };

  if (selection.kind === "player_prop") {
    return (
      <PlayerPropStrip
        selection={selection}
        controls={controls}
        n={n}
        location={effectiveLocation}
      />
    );
  }
  return (
    <GameLineStrip
      selection={selection}
      controls={controls}
      n={n}
      location={effectiveLocation}
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
}

function PlayerPropStrip({ selection, controls, n, location }: PlayerPropStripProps) {
  const subjectName = selection.subjectName;
  const statKey = selection.statKey;
  const threshold = selection.threshold;
  const ready = Boolean(subjectName && statKey && threshold != null);

  // Codex round-2 P2 on PR #24: forward ``selection.subjectTeam`` as
  // the ESPN team_hint so same-name players resolve to the picked
  // athlete (bug #13 enabled it on the backend, but the strip wasn't
  // passing it through).
  const opts: PickHistoryOptions = {
    location,
    opponent: null,
    teamHint: selection.subjectTeam ?? null,
  };

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
    .map((log) => resolveStatValue(log.metrics, statKey!))
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
        key={`player-${n}-${location ?? ""}-${recentValues.length}`}
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
}

function GameLineStrip({ selection, controls, n, location }: GameLineStripProps) {
  const teamName = inferTeamName(selection);
  const opts: PickHistoryOptions = { location, opponent: null };

  // Codex round-3 P1 on PR #24: keep useSWR unconditional to satisfy
  // rules-of-hooks — the previous early return changed hook order
  // when a prior selection had resolved a team and a subsequent one
  // didn't, which made ``next build`` fail. A ``null`` SWR key
  // short-circuits the fetch, so passing ``null`` when there's no
  // team to chart is equivalent to the old early return.
  const { data, error, isLoading } = useSWR<TeamHistoryRead>(
    teamName !== null ? keys.teamHistory(teamName, selection.sportKey, n, opts) : null,
    teamName !== null ? () => fetchTeamHistory(teamName, selection.sportKey, n, opts) : null,
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );

  if (teamName === null) return null;
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
        // Codex round-1 P2 on PR #24: pass the effective over/under
        // direction (folds copilot_direction × selected_side on the
        // backend) so Under-market YES picks color as ``under`` and
        // not ``over``.
        totalDirection={selection.totalDirection ?? (side === "yes" ? "over" : "under")}
        controls={controls}
        n={n}
        location={location}
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
}

function SpreadChart({
  team,
  recent,
  numericLine,
  side,
  controls,
  n,
  location,
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
        key={`spread-${n}-${location ?? ""}-${margins.length}`}
        points={margins}
        threshold={coverThreshold}
        bandTone={(_, index) => outcomes[index]}
        ariaLabel={`${team} last ${margins.length} margins vs cover line`}
      />
    </section>
  );
}

interface TotalChartProps extends ChartVariantProps {
  /** Effective direction the pick represents — folds the market's
   *  ``copilot_direction`` so Under-market YES picks color as
   *  ``under`` and not ``over``. */
  totalDirection: "over" | "under";
}

function TotalChart({
  team,
  recent,
  numericLine,
  side,
  totalDirection,
  controls,
  n,
  location,
}: TotalChartProps) {
  const totals = recent.map((row) => row.team_score + row.opp_score);
  const absoluteLine = Math.abs(numericLine);
  // ``coverOutcome``'s total branch keys on ``side`` semantics (yes
  // → cover-when-higher, no → cover-when-lower). For Under-direction
  // markets, the picked side's meaning is flipped — feed a synthetic
  // side that matches the effective direction so the coloring
  // matches what the operator sees in the ticket header.
  const effectiveSide = totalDirection === "over" ? "yes" : "no";
  const outcomes = totals.map((total) =>
    coverOutcome(total, absoluteLine, "total", effectiveSide),
  );
  const hitCount = outcomes.filter((tone) => tone === "high").length;

  return (
    <section className="pick-history-strip" data-testid="pick-history-strip">
      <StripHeader
        controls={controls}
        leadLine={
          <span>
            total · last {totals.length} ·{" "}
            <span className="pick-history-strip-tally">
              {hitCount}/{totals.length} {totalDirection}
            </span>
          </span>
        }
      />
      <MiniBars
        key={`total-${n}-${location ?? ""}-${totals.length}`}
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
}

function MoneylinePills({ team, recent, controls, n, location }: MoneylinePillsProps) {
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
        key={`pills-${n}-${location ?? ""}-${recent.length}`}
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
            active={controls.location === null}
            label="all"
            onClick={() => controls.onLocation(null)}
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
 * Read a player-prop stat value from a game log's metrics dict.
 *
 * Kalshi MLB / NBA markets sometimes use composite stat keys (e.g.
 * ``hits_runs_rbis``, ``points_rebounds_assists``) that the API's
 * game-log builder doesn't emit directly — the canonical scoring layer at
 * apps/api/app/services/scoring.py:1349-1373 instead derives them on
 * demand by summing the atomic components. We mirror that rule on the
 * client so the strip can chart any composite without a backend round
 * trip: try the direct lookup first, then if the stat key contains "_"
 * try splitting it into atomic components and summing.
 *
 * Returns null if no resolution is possible (e.g. a key like
 * ``home_runs_rbis`` whose first split component "home" isn't atomic).
 */
export function resolveStatValue(
  metrics: Record<string, number | null | undefined> | null | undefined,
  statKey: string,
): number | null {
  if (!metrics) return null;
  const direct = metrics[statKey];
  if (typeof direct === "number" && Number.isFinite(direct)) return direct;
  if (!statKey.includes("_")) return null;
  const parts = statKey.split("_");
  let total = 0;
  for (const part of parts) {
    const value = metrics[part];
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    total += value;
  }
  return total;
}

/**
 * Sign-correct cover/over outcome.
 *
 * Spread:
 *   ``threshold`` is the binary contract line — the same value the
 *   model integrates over in ``scoring.py`` (``P(margin > threshold)``).
 *   YES contract holders win when ``margin > threshold``; NO contract
 *   holders win when ``margin < threshold``. Exact equality is a push.
 *
 *   Codex round-3 P2 on PR #24: the prior implementation treated the
 *   NO contract as a sportsbook ``team +threshold`` spread bet
 *   (``margin > -threshold``) — that's a DIFFERENT event from the
 *   binary "team wins by ≥ threshold" the contract actually settles
 *   on. For "Cavs win by 3.5+" NO, the bet wins on margin -5 (Cavs
 *   lost by 5) and loses on margin +5 (Cavs blowout), but the old
 *   code marked those as miss/cover respectively.
 *
 * Total:
 *   ``threshold`` is the absolute total line. ``side === "yes"``
 *   means cover on a higher total, ``"no"`` means cover on a lower
 *   total — UNLESS the market itself is an Under line, in which
 *   case the YES/NO semantics flip (handled at the call site by
 *   flipping ``side`` before invoking this helper).
 *
 * Returns "mid" on exact pushes so the strip visually distinguishes
 * pushes from wins/losses.
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
    if (normalizedSide === "no") return value < threshold ? "high" : "low";
    return value > threshold ? "high" : "low";
  }
  // total
  if (normalizedSide === "yes") return value > threshold ? "high" : "low";
  return value < threshold ? "high" : "low";
}

/**
 * Parse the picked team's name from a game-line selection.
 *
 * Codex round-1 P2 on PR #24: real game-line ``marketTitle`` values
 * are matchup-style (``Miami Heat at Toronto Raptors Winner?``,
 * ``... Spread?``). The old strategy of slicing the title before the
 * market-kind keyword returned BOTH teams (everything before
 * "Winner") — the lookup then resolved to whichever team ESPN
 * happened to return first, not the side actually picked.
 *
 * Correct strategy:
 * - Winner / spread markets: pick the team from
 *   ``projectedSideLabel`` or ``displayLabel`` first — both encode
 *   only the chosen side.
 * - Totals: there is no single team to look up (the total is the
 *   matchup's sum); fall back to the event's home team via
 *   ``eventName`` parsing so the chart still has SOMETHING to
 *   render. Callers that want strict team correctness should hide
 *   the strip for totals.
 * - ``marketTitle`` is used only as a last-resort fallback for
 *   non-matchup titles.
 */
function inferTeamName(selection: TradeSelection): string | null {
  const stripKindSuffix = (value: string): string =>
    value
      .replace(/\s+(ML|moneyline|spread|total|over|under|[+-]?\d.*)$/i, "")
      .replace(/[·:-]+$/, "")
      .trim();

  // Codex round-2 P2 on PR #24: real trade-desk rows use ``game_winner``
  // and ``first_five_winner`` (and ``moneyline`` in older payloads), not
  // ``winner``. Match all three so the projected-side resolution doesn't
  // silently fall through to the matchup-title parser for normal picks.
  const winnerLikeKinds = new Set([
    "winner",
    "game_winner",
    "first_five_winner",
    "moneyline",
  ]);
  if (winnerLikeKinds.has(selection.marketKind) || selection.marketKind === "spread") {
    const projected = selection.projectedSideLabel?.trim();
    if (projected) {
      const cleaned = stripKindSuffix(projected);
      if (cleaned.length >= 2) return cleaned;
    }
    const label = selection.displayLabel?.trim();
    if (label) {
      const cleaned = stripKindSuffix(label);
      if (cleaned.length >= 2) return cleaned;
    }
  }

  // Totals (and any other game-line market that isn't keyed on one
  // team's perspective): use the home team parsed out of the event
  // name. ``eventName`` is ``"Away at Home"`` for NBA/MLB scoreboard
  // payloads; take the second half.
  if (selection.marketKind === "total") {
    const eventName = selection.eventName?.trim();
    if (eventName) {
      const atSplit = eventName.split(/\s+at\s+/i);
      const homeTeam = (atSplit.length === 2 ? atSplit[1] : eventName).trim();
      if (homeTeam.length >= 2) return stripKindSuffix(homeTeam);
    }
  }

  // Fallback: best-effort marketTitle slice for anything else.
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
  return null;
}

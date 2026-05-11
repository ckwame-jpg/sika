"use client";

import useSWR from "swr";
import { fetchPlayerHistory, fetchTeamHistory, keys } from "@/lib/api";
import { MiniBars, type MiniBarsTone } from "@/components/stats/mini-bars";
import type { StatsQueryRead, TeamHistoryRead } from "@/lib/types";
import type { TradeSelection } from "./trade-ticket";

const HISTORY_GAMES = 5;

interface PickHistoryStripProps {
  selection: TradeSelection;
}

/**
 * Shows the picked entity's last 5 games inside the trade ticket.
 *
 * Two treatments:
 *   - player_prop → MiniBars colored by pass/fail against the pick's threshold,
 *     plus an "N/5 cleared" caption.
 *   - game_line → W/L pills with final score under each.
 *
 * Hides itself entirely when the selection lacks usable identity (no
 * subjectName for props, no parseable team for game lines).
 */
export function PickHistoryStrip({ selection }: PickHistoryStripProps) {
  if (selection.kind === "player_prop") {
    return <PlayerPropStrip selection={selection} />;
  }
  return <GameLineStrip selection={selection} />;
}

function PlayerPropStrip({ selection }: { selection: TradeSelection }) {
  const subjectName = selection.subjectName;
  const statKey = selection.statKey;
  const threshold = selection.threshold;
  const ready = Boolean(subjectName && statKey && threshold != null);

  const { data, error, isLoading } = useSWR<StatsQueryRead>(
    ready ? keys.playerHistory(subjectName!, selection.sportKey, HISTORY_GAMES) : null,
    () => fetchPlayerHistory(subjectName!, selection.sportKey, HISTORY_GAMES),
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );

  if (!ready) return null;
  if (isLoading) return <StripSkeleton />;
  if (error || !data) return null;

  const recentValues = data.game_logs
    .slice(0, HISTORY_GAMES)
    .map((log) => log.metrics?.[statKey!])
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));

  if (recentValues.length === 0) return null;

  const thresholdNumber = Number(threshold);
  const clearedCount = recentValues.filter((value) => value >= thresholdNumber).length;
  const bandTone = (value: number): MiniBarsTone =>
    value >= thresholdNumber ? "high" : "low";

  return (
    <section className="pick-history-strip" data-testid="pick-history-strip">
      <header className="pick-history-strip-h">
        <span>{statKey!.replace(/_/g, " ")} · last {recentValues.length}</span>
        <span className="pick-history-strip-tally">
          {clearedCount}/{recentValues.length} cleared {thresholdNumber}+
        </span>
      </header>
      <MiniBars
        points={recentValues}
        threshold={thresholdNumber}
        bandTone={bandTone}
        ariaLabel={`Last ${recentValues.length} ${statKey!.replace(/_/g, " ")} for ${subjectName}`}
      />
    </section>
  );
}

function GameLineStrip({ selection }: { selection: TradeSelection }) {
  const teamName = inferTeamName(selection);
  const ready = teamName !== null;

  const { data, error, isLoading } = useSWR<TeamHistoryRead>(
    ready ? keys.teamHistory(teamName!, selection.sportKey, HISTORY_GAMES) : null,
    () => fetchTeamHistory(teamName!, selection.sportKey, HISTORY_GAMES),
    { revalidateOnFocus: false, revalidateOnReconnect: false },
  );

  if (!ready) return null;
  if (isLoading) return <StripSkeleton />;
  if (error || !data || data.results.length === 0) return null;

  const recent = data.results.slice(0, HISTORY_GAMES);
  const wins = recent.filter((result) => result.result === "W").length;

  return (
    <section className="pick-history-strip" data-testid="pick-history-strip">
      <header className="pick-history-strip-h">
        <span>{data.team_name} · last {recent.length}</span>
        <span className="pick-history-strip-tally">{wins}-{recent.length - wins}</span>
      </header>
      <ol className="pick-history-strip-pills" data-testid="pick-history-strip-pills">
        {recent.map((result, index) => (
          <li
            key={`${result.game_date}-${index}`}
            className={`pick-history-strip-pill is-${result.result === "W" ? "win" : "loss"}`}
            data-result={result.result}
          >
            <span className="pick-history-strip-pill-letter">{result.result}</span>
            <span className="pick-history-strip-pill-score">
              {result.team_score}-{result.opp_score}
            </span>
            {result.opponent_abbreviation ? (
              <span className="pick-history-strip-pill-opp">
                {result.location === "away" ? "@" : "vs"}{result.opponent_abbreviation}
              </span>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}

function StripSkeleton() {
  return (
    <section
      className="pick-history-strip is-loading"
      data-testid="pick-history-strip-skeleton"
      aria-hidden
    >
      <header className="pick-history-strip-h">
        <span>last 5</span>
      </header>
      <div className="pick-history-strip-pills">
        {Array.from({ length: HISTORY_GAMES }).map((_, index) => (
          <div key={index} className="pick-history-strip-pill is-placeholder" />
        ))}
      </div>
    </section>
  );
}

/**
 * Best-effort team-name extraction from a game-line selection.
 *
 * `displayLabel` is the operator-facing string ("Cavaliers ML",
 * "Cavaliers -3.5", "Cavaliers/Pistons O 220.5"). `marketTitle` carries the
 * full team name when it's available ("Cleveland Cavaliers moneyline").
 *
 * Strategy:
 *   1. If `marketTitle` starts with a multi-word capitalized phrase followed
 *      by a known market-kind keyword, take that phrase.
 *   2. Fall back to `displayLabel`'s first token (works for shortened forms
 *      like "Cavaliers ML"). If even that is missing, return null and the
 *      strip hides itself.
 */
function inferTeamName(selection: TradeSelection): string | null {
  const title = selection.marketTitle?.trim();
  if (title) {
    const kindKeywords = ["moneyline", "spread", "total", "over", "under", "puck line", "run line"];
    const lower = title.toLowerCase();
    let cutIndex = title.length;
    for (const keyword of kindKeywords) {
      const idx = lower.indexOf(keyword);
      if (idx > 0 && idx < cutIndex) {
        cutIndex = idx;
      }
    }
    const candidate = title.slice(0, cutIndex).trim().replace(/[·:-]+$/, "").trim();
    if (candidate.length >= 2) return candidate;
  }

  const label = selection.displayLabel?.trim();
  if (label) {
    // "Cavaliers ML", "Cavaliers -3.5" — take everything up to the first
    // numeric token or market suffix.
    const stripped = label.replace(/\s+(ML|moneyline|spread|total|over|under|[+-]?\d.*)$/i, "").trim();
    if (stripped.length >= 2) return stripped;
  }

  return null;
}

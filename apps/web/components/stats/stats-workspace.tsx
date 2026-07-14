"use client";

import { useEffect, useRef, useState } from "react";
import { queryStats } from "@/lib/api";
import { SPORT_LABELS, type SportKey, type StatsQueryRead } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useHealthStatus, getSyncState } from "@/lib/health-status";
import { AdvancedMetricsGrid } from "./advanced-metrics-grid";
import { MiniBars } from "./mini-bars";

const SUGGESTIONS = [
  "Jalen Brunson last 10 games",
  "Jayson Tatum this season",
  "LeBron vs BOS · lifetime",
  "Curry on 2nd night B2B",
  "Mahomes vs top-10 defenses",
];

// Keys MUST match SPORT_LABELS / SportKey exactly (uppercase).
const EXAMPLES: Record<SportKey, string[]> = {
  NBA: ["Jalen Brunson last 10 games", "Jayson Tatum this season"],
  NFL: ["Patrick Mahomes this season", "Josh Allen last 5 games"],
  MLB: ["Aaron Judge this season", "Mookie Betts last 10 games"],
  WNBA: ["Caitlin Clark last 10 games", "A'ja Wilson this season"],
  TENNIS: ["Novak Djokovic last 5 matches", "Carlos Alcaraz this season"],
};

// Smarter WNBA PR 3 wired the /stats/query backend WNBA branch
// (`_METRIC_LABELS`, `_build_game_logs`, `_build_summary_metrics`,
// `default_season_for_sport`), so WNBA is now safe to expose here.
// The dropdown reads from SPORT_LABELS directly — no longer gated.

const SEASON_OPTIONS = ["2026-27", "2025-26", "2024-25", "2023-24", "2022-23"];

// ESPN's `season` parameter is sport-specific:
//   NBA + Tennis (multi-year span): use the END year (e.g. "2025-26" -> 2026).
//   NFL + MLB: use the START / single year (e.g. "2025-26" -> 2025).
// The picker default is sport-aware so MLB/NFL pick the span whose
// start year is the active calendar season; NBA/Tennis pick the span
// whose end year matches the active season.
function defaultSeasonForSport(sport: SportKey, today: Date = new Date()): string {
  const year = today.getFullYear();
  const month = today.getMonth() + 1;
  if (sport === "NBA" || sport === "TENNIS") {
    const endYear = month >= 10 ? year + 1 : year;
    return `${endYear - 1}-${String(endYear).slice(2)}`;
  }
  // WNBA mirrors the backend's ``default_season_for_sport`` rollover at
  // month >= 5. May → Sept tag as the current calendar year; Jan → Apr
  // roll back to the previous season's year. Without this, an offseason
  // UI query would send next-year season to the API, which would 404
  // against the not-yet-started season.
  if (sport === "WNBA") {
    const startYear = month >= 5 ? year : year - 1;
    return `${startYear}-${String(startYear + 1).slice(2)}`;
  }
  // MLB/NFL: use the calendar-year start that's currently active. MLB
  // starts in March; before March, the previous calendar year is still
  // the most recent completed season.
  const startYear = sport === "MLB" && month < 3 ? year - 1 : year;
  return `${startYear}-${String(startYear + 1).slice(2)}`;
}

function resolveSeasonYear(season: string, sport: SportKey): number | undefined {
  if (!season) return undefined;
  const dashIdx = season.indexOf("-");
  if (dashIdx === -1) {
    const n = Number(season);
    return Number.isFinite(n) ? n : undefined;
  }
  const startYear = Number(season.slice(0, dashIdx));
  if (!Number.isFinite(startYear)) return undefined;
  return sport === "NBA" || sport === "TENNIS" ? startYear + 1 : startYear;
}

interface ChatTurn {
  id: number;
  question: string;
  result: StatsQueryRead;
}

interface StatsWorkspaceProps {
  initialSport?: SportKey;
}

export function StatsWorkspace({ initialSport = "NBA" }: StatsWorkspaceProps) {
  const [sportKey, setSportKey] = useState<SportKey>(initialSport);
  const [question, setQuestion] = useState(SUGGESTIONS[0]);
  const [season, setSeason] = useState(() => defaultSeasonForSport(initialSport));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const turnCounter = useRef(0);
  const chatEndRef = useRef<HTMLDivElement | null>(null);

  async function runSearch(nextQuestion?: string) {
    const finalQuestion = (nextQuestion ?? question).trim();
    if (!finalQuestion) {
      setError("Question is required");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const next = await queryStats({
        question: finalQuestion,
        sport_key: sportKey,
        season: resolveSeasonYear(season, sportKey),
      });
      setQuestion(finalQuestion);
      turnCounter.current += 1;
      setTurns((current) => [...current, { id: turnCounter.current, question: finalQuestion, result: next }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to query stats");
    } finally {
      setLoading(false);
    }
  }

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    if (turns.length > 0) {
      chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [turns.length]);

  const lastResult = turns.length > 0 ? turns[turns.length - 1].result : null;
  const recentQuestions = [...new Set(turns.map((turn) => turn.question))].slice(-3).reverse();

  return (
    <div className="gi-cols" data-testid="stats-workspace">
      <div className="gi-cols-main">
        {/* Compact desk header — keeps the research-desk framing. */}
        <section className="gi-panel" data-testid="stats-assistant-card">
          <div className="gi-panel-head">
            <span className="gi-chat-orb" aria-hidden />
            <div className="min-w-0">
              <div className="flex items-baseline gap-2">
                <h2 className="gi-panel-title">Stats Assistant</h2>
                <span className="gi-micro-label">Research desk</span>
              </div>
              <p className="gi-panel-sub">
                Ask about any player, team, or matchup across NBA, NFL, MLB, WNBA, and Tennis.
              </p>
            </div>
            <span className="ml-auto flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
              <span className="gi-glow-dot" style={{ "--gd": "var(--gi-green)" } as React.CSSProperties} aria-hidden />
              <span>ready</span>
              <span className="opacity-50">·</span>
              <span>/research/stats/query</span>
            </span>
          </div>

          <div className="gi-chat p-4" data-testid="sa-result">
            {turns.length === 0 && !loading && (
              <div className="sa-result-empty py-10 text-center" data-testid="sa-result-empty">
                <div className="gi-orb-stat mx-auto mb-3" aria-hidden>
                  <span className="core" />
                </div>
                <div className="text-sm font-medium text-foreground">Run a player stats query</div>
                <div className="mx-auto mt-1 max-w-[420px] text-xs text-muted-foreground">
                  Answers appear here as a conversation — mini gauges, a trend chart, and the exact
                  source the assistant used.
                </div>
              </div>
            )}

            {turns.length > 0 && (
              <div className="gi-chat-day">
                today · {new Date().toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}
              </div>
            )}

            {turns.map((turn) => (
              <div key={turn.id} className="gi-chat contents">
                <div className="gi-chat-user" data-testid="sa-user-turn">{turn.question}</div>
                <AssistantCard result={turn.result} onFollowUp={(next) => void runSearch(next)} />
              </div>
            ))}

            {loading && (
              <div className="gi-chat-card" data-testid="sa-result-loading">
                <div className="gi-chat-head">
                  <span className="gi-chat-orb" aria-hidden />
                  <span className="gi-chat-name">sika stats</span>
                  <span className="gi-chat-meta">scanning {sportKey} logs…</span>
                </div>
                <div className="gi-run-progress" aria-hidden />
              </div>
            )}

            {error && (
              <p className="text-xs text-negative" role="alert">
                {error}
              </p>
            )}
            <div ref={chatEndRef} />
          </div>
        </section>

        <div className="flex flex-wrap gap-2" role="list">
          {SUGGESTIONS.map((chip) => (
            <button
              key={chip}
              type="button"
              className="gi-chip focus-visible:ring-focus"
              onClick={() => runSearch(chip)}
              data-testid="sa-prompt"
            >
              ✦ {chip}
            </button>
          ))}
        </div>

        <div className="gi-composer">
          <span className="gi-chat-orb" aria-hidden />
          <input
            type="text"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="ask about any player, team, or market…"
            onKeyDown={(event) => {
              if (event.key === "Enter") runSearch();
            }}
            data-testid="sa-input"
            aria-label="Stats question"
          />
          <div className="sa-select">
            <select
              value={sportKey}
              onChange={(event) => {
                const nextSport = event.target.value as SportKey;
                setSportKey(nextSport);
                setSeason(defaultSeasonForSport(nextSport));
                const nextExample = EXAMPLES[nextSport]?.[0];
                if (nextExample) setQuestion(nextExample);
              }}
              aria-label="Sport"
              data-testid="sa-sport"
            >
              {(Object.entries(SPORT_LABELS) as Array<[SportKey, string]>).map(([key, label]) => (
                <option key={key} value={key}>{label}</option>
              ))}
            </select>
          </div>
          <div className="sa-select">
            <select
              value={season}
              onChange={(event) => setSeason(event.target.value)}
              aria-label="Season"
              data-testid="sa-season"
            >
              {SEASON_OPTIONS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
          <button
            className="gi-composer-send focus-visible:ring-focus"
            type="button"
            onClick={() => runSearch()}
            disabled={loading}
            aria-label="Run query"
            data-testid="sa-run"
          >
            ↑
          </button>
        </div>
      </div>

      <div className="gi-cols-rail hidden xl:block">
        <SourcesRail lastResult={lastResult} recentQuestions={recentQuestions} onAsk={(next) => void runSearch(next)} />
      </div>
    </div>
  );
}

/** Sources + recent questions rail (spec 5f right column). */
function SourcesRail({
  lastResult,
  recentQuestions,
  onAsk,
}: {
  lastResult: StatsQueryRead | null;
  recentQuestions: string[];
  onAsk: (question: string) => void;
}) {
  const { data: health } = useHealthStatus();
  const syncState = getSyncState(health);

  return (
    <div className="gi-rail" data-testid="stats-sources-rail">
      <span className="gi-micro-label rail">sources</span>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span
            className="gi-glow-dot"
            style={{ "--gd": lastResult ? "var(--gi-green)" : "var(--gi-faint)" } as React.CSSProperties}
            aria-hidden
          />
          stats feed
        </span>
        <span className="v">{lastResult ? lastResult.source : "idle"}</span>
      </div>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span
            className="gi-glow-dot"
            style={{ "--gd": syncState === "synced" ? "var(--gi-green)" : "var(--gi-amber)" } as React.CSSProperties}
            aria-hidden
          />
          kalshi markets
        </span>
        <span className="v">{syncState ?? "—"}</span>
      </div>
      <div className="gi-rail-stat">
        <span className="flex items-center gap-2">
          <span className="gi-glow-dot" style={{ "--gd": "var(--color-cosmos-violet-500)" } as React.CSSProperties} aria-hidden />
          scheduler
        </span>
        <span className="v">{health?.scheduler_enabled ? "on" : "off"}</span>
      </div>
      <div className="gi-rail-divider" />
      <span className="gi-micro-label rail">recent questions</span>
      {recentQuestions.length === 0 ? (
        <p className="text-[11.5px] text-muted-foreground">nothing asked yet this session.</p>
      ) : (
        recentQuestions.map((entry) => (
          <button
            key={entry}
            type="button"
            className="gi-btn-ghost justify-start text-left"
            onClick={() => onAsk(entry)}
          >
            {entry}
          </button>
        ))
      )}
    </div>
  );
}

function AssistantCard({
  result,
  onFollowUp,
}: {
  result: StatsQueryRead;
  onFollowUp: (question: string) => void;
}) {
  // Metric grid: when the backend tags metrics with categories, the basic
  // ones render here and the advanced ones drop into AdvancedMetricsGrid
  // below. When categories are absent (older API responses), every metric
  // renders here just like before.
  const categories = result.summary.metric_categories ?? {};
  const hasCategories = Object.keys(categories).length > 0;
  const metricEntries = Object.entries(result.summary.metrics)
    .filter(([key, value]) => {
      if (value == null) return false;
      if (!hasCategories) return true;
      return categories[key] !== "advanced";
    })
    .slice(0, 6);

  // A metric is chartable if at least one game log has a finite numeric value for it.
  const chartableKeys = new Set(
    metricEntries
      .map(([key]) => key)
      .filter((key) =>
        result.game_logs.some((g) => {
          const v = g.metrics?.[key];
          return typeof v === "number" && Number.isFinite(v);
        }),
      ),
  );

  const defaultMetric = metricEntries.find(([key]) => chartableKeys.has(key))?.[0];
  const [selectedMetric, setSelectedMetric] = useState<string | undefined>(defaultMetric);

  // Reset the selection whenever a new query result lands so the chart starts on the
  // primary metric for the new entity instead of stale state from the previous answer.
  useEffect(() => {
    setSelectedMetric(defaultMetric);
  }, [defaultMetric, result.entity_id]);

  const activeMetric = selectedMetric && chartableKeys.has(selectedMetric) ? selectedMetric : defaultMetric;

  const chartPoints: Array<{ value: number; label: string }> = [];
  if (activeMetric && result.game_logs.length > 0) {
    for (const g of result.game_logs.slice(0, 10)) {
      const v = g.metrics?.[activeMetric];
      if (typeof v === "number" && Number.isFinite(v)) {
        chartPoints.push({ value: v, label: g.opponent ?? "" });
      }
    }
  }
  const showChart = chartPoints.length > 0;

  // Spec mini-gauge grid: first three metrics as tiny conic gauges; ring
  // fill uses the backend percentile when present.
  const percentiles = result.summary.percentiles ?? {};
  const gaugeEntries = metricEntries.slice(0, 3);

  const followUps = EXAMPLES[result.sport_key as SportKey] ?? [];

  return (
    <div className="gi-chat-card" data-testid="sa-answer">
      <div className="gi-chat-head">
        <span className="gi-chat-orb" aria-hidden />
        <span className="gi-chat-name">sika stats</span>
        <span className="gi-chat-meta">
          {result.source} · {result.games_analyzed} game sample
        </span>
      </div>

      <div>
        <div className="sa-answer-eyebrow">ANSWER · {result.sport_key}</div>
        <div className="sa-answer-title">{result.entity_name}</div>
      </div>

      {result.summary.stat_line && (
        <p className="gi-chat-prose">{result.summary.stat_line}</p>
      )}

      {gaugeEntries.length > 0 && (
        <div className="gi-chat-gauges">
          {gaugeEntries.map(([key, value]) => {
            const pct = percentiles[key];
            return (
              <div key={key} className="gi-stat-chip" style={{ flexDirection: "row", gap: 10, textAlign: "left" }}>
                <div
                  className="gi-gauge sm"
                  style={
                    {
                      "--gg-p": Math.max(0, Math.min(100, pct ?? 60)),
                      "--gg-c": "var(--color-cosmos-cyan-500)",
                    } as React.CSSProperties
                  }
                  aria-hidden
                >
                  <span className="gi-gauge-value">
                    {value == null ? "—" : Number.isInteger(value) ? String(value) : value.toFixed(1)}
                  </span>
                </div>
                <div className="min-w-0">
                  <p className="k">{result.metric_labels[key] ?? key}</p>
                  <p className="v">
                    {value == null ? "—" : Number.isInteger(value) ? String(value) : value.toFixed(2)}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="sa-answer-grid">
        {metricEntries.map(([key, value]) => {
          const chartable = chartableKeys.has(key);
          const isActive = chartable && key === activeMetric;
          return (
            <button
              key={key}
              type="button"
              className={cn("sa-stat focus-visible:ring-focus", isActive && "is-active", !chartable && "is-static")}
              onClick={chartable ? () => setSelectedMetric(key) : undefined}
              disabled={!chartable}
              aria-pressed={chartable ? isActive : undefined}
              data-testid={`sa-metric-${key}`}
            >
              <div className="sa-stat-l">{result.metric_labels[key] ?? key}</div>
              <div className="sa-stat-v">
                {value == null ? "—" : Number.isInteger(value) ? String(value) : value.toFixed(2)}
              </div>
              {/* sa-stat-s reserved for backend-provided delta copy when available */}
            </button>
          );
        })}
      </div>

      {showChart && (
        <div className="gi-chart-card" data-testid="sa-answer-chart">
          <div className="gi-chart-card-head">
            <span>{result.metric_labels[activeMetric!] ?? activeMetric} · last {chartPoints.length}</span>
            <span>
              avg {(chartPoints.reduce((s, p) => s + p.value, 0) / chartPoints.length).toFixed(1)}
            </span>
          </div>
          <MiniBars points={chartPoints.map((p) => p.value)} />
          {result.game_logs.length > 0 && (
            <div className="gi-chart-card-foot">
              <span>{result.game_logs[Math.min(chartPoints.length, result.game_logs.length) - 1]?.game_date ?? ""}</span>
              <span>{result.game_logs[0]?.game_date ?? ""}</span>
            </div>
          )}
        </div>
      )}

      <AdvancedMetricsGrid
        metrics={result.summary.metrics}
        labels={result.metric_labels}
        percentiles={result.summary.percentiles ?? {}}
        categories={result.summary.metric_categories ?? {}}
      />

      {result.explanation && (
        <p className="sa-answer-explain">{result.explanation}</p>
      )}

      {result.coverage_note && (
        <p className="sa-answer-coverage">{result.coverage_note}</p>
      )}

      {result.game_logs.length > 0 && (
        <div className="sa-log-list">
          {result.game_logs.slice(0, 8).map((game) => (
            <div key={game.game_id} className="sa-log-row">
              <div className="sa-log-meta">
                <span className="sa-log-date">{game.game_date}</span>
                <span className="sa-log-opp">vs {game.opponent}</span>
                <span className="sa-log-loc">{game.location}</span>
              </div>
              <div className="sa-log-score">
                {game.team_score}–{game.opponent_score}
                <span className={cn("sa-log-result", game.result === "W" && "pos", game.result === "L" && "neg")}>
                  {game.result ?? "—"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {followUps.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {followUps.map((chip) => (
            <button
              key={chip}
              type="button"
              className="gi-chip"
              onClick={() => onFollowUp(chip)}
            >
              {chip}
            </button>
          ))}
        </div>
      )}

      <div className="sa-answer-foot">
        <span>Source: /research/stats/query</span>
        <span className="sa-dot" />
        <span>{result.source}</span>
      </div>
    </div>
  );
}

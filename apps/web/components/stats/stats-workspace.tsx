"use client";

import { useEffect, useState } from "react";
import { queryStats } from "@/lib/api";
import { SPORT_LABELS, type SportKey, type StatsQueryRead } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AdvancedMetricsGrid } from "./advanced-metrics-grid";

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
  SOCCER: ["Lionel Messi last 5 matches", "Kylian Mbappe this season"],
  TENNIS: ["Novak Djokovic last 5 matches", "Carlos Alcaraz this season"],
};

const CURRENT_SEASON = "2025-26";
const SEASON_OPTIONS = ["2025-26", "2024-25", "2023-24", "2022-23"];

// ESPN's `season` parameter is sport-specific:
//   NBA + Tennis (multi-year span): use the END year (e.g. "2025-26" -> 2026).
//   NFL + MLB + Soccer + UFC: use the START / single year (e.g. "2025-26" -> 2025).
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

interface StatsWorkspaceProps {
  initialSport?: SportKey;
}

export function StatsWorkspace({ initialSport = "NBA" }: StatsWorkspaceProps) {
  const [sportKey, setSportKey] = useState<SportKey>(initialSport);
  const [question, setQuestion] = useState(SUGGESTIONS[0]);
  const [season, setSeason] = useState(CURRENT_SEASON);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<StatsQueryRead | null>(null);

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
      setResult(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to query stats");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="stats-assistant-wrap" data-testid="stats-workspace">
      <section className="stats-assistant" data-testid="stats-assistant-card">
        <div className="sa-header">
          <div className="sa-icon" aria-hidden>
            <svg viewBox="0 0 24 24" width="18" height="18">
              <path d="M3 3v18h18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              <path d="M7 15l3-4 3 2 4-6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              <circle cx="17" cy="7" r="1.5" fill="currentColor" />
            </svg>
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="sa-title-row">
              <span className="sa-eyebrow">Research desk</span>
              <span className="sa-dot" />
              <span className="sa-endpoint">/research/stats/query</span>
            </div>
            <div className="sa-title">Stats Assistant</div>
            <div className="sa-sub">
              Cross-sport player query desk. Ask for recent form, season totals, or matchup context
              across NBA, NFL, MLB, Soccer, and Tennis.
            </div>
          </div>
          <span className="sa-status">
            <span className="sa-live-dot" />
            <span>ready</span>
          </span>
        </div>

        <div className="sa-prompts" role="list">
          {SUGGESTIONS.map((chip) => (
            <button
              key={chip}
              type="button"
              className="sa-prompt"
              onClick={() => runSearch(chip)}
              data-testid="sa-prompt"
            >
              <span className="sa-prompt-mark" aria-hidden>✦</span>
              <span>{chip}</span>
            </button>
          ))}
        </div>

        <div className="sa-input-row">
          <label className="sa-input">
            <span className="sa-input-icon" aria-hidden>
              <svg viewBox="0 0 24 24" width="14" height="14">
                <circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" strokeWidth="1.6" />
                <path d="M20 20l-4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
            </span>
            <input
              type="text"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Ask about a player, team, or matchup…"
              onKeyDown={(event) => {
                if (event.key === "Enter") runSearch();
              }}
              data-testid="sa-input"
              aria-label="Stats question"
            />
            <span className="sa-kbd" aria-hidden>↵</span>
          </label>
          <div className="sa-select">
            <select
              value={sportKey}
              onChange={(event) => {
                const nextSport = event.target.value as SportKey;
                setSportKey(nextSport);
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
            className="sa-run"
            type="button"
            onClick={() => runSearch()}
            disabled={loading}
            data-testid="sa-run"
          >
            {loading ? "Querying…" : "Run query"}
          </button>
        </div>
        {error && <p className="mt-2 text-xs text-negative" role="alert">{error}</p>}
      </section>

      <section className="sa-result" data-testid="sa-result">
        {loading ? (
          <LoadingState sportKey={sportKey} />
        ) : result ? (
          <StatsAnswer result={result} />
        ) : (
          <EmptyOrbState />
        )}
      </section>
    </div>
  );
}

function EmptyOrbState() {
  return (
    <div className="sa-result-empty" data-testid="sa-result-empty">
      <div className="sa-result-orb" aria-hidden />
      <div className="sa-result-title">Run a player stats query</div>
      <div className="sa-result-sub">
        Answers appear here with a compact splits table, a mini trend chart,
        and the exact source the assistant used.
      </div>
    </div>
  );
}

function LoadingState({ sportKey }: { sportKey: SportKey }) {
  return (
    <div className="sa-result-loading" data-testid="sa-result-loading">
      <span className="sa-result-bar" />
      <span className="sa-result-bar" />
      <span className="sa-result-bar" />
      <div className="sa-result-scan">scanning {sportKey} logs…</div>
    </div>
  );
}

function StatsAnswer({ result }: { result: StatsQueryRead }) {
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

  return (
    <div className="sa-answer" data-testid="sa-answer">
      <header className="sa-answer-header">
        <div>
          <div className="sa-answer-eyebrow">ANSWER · {result.sport_key}</div>
          <div className="sa-answer-title">{result.entity_name}</div>
        </div>
        <span className="sa-answer-source">
          <span className="sa-live-dot" aria-hidden />
          <span>
            {result.games_analyzed} games · {result.query_type.replaceAll("_", " ")}
          </span>
        </span>
      </header>

      {result.summary.stat_line && (
        <div className="sa-answer-line">{result.summary.stat_line}</div>
      )}

      <div className="sa-answer-grid">
        {metricEntries.map(([key, value]) => {
          const chartable = chartableKeys.has(key);
          const isActive = chartable && key === activeMetric;
          return (
            <button
              key={key}
              type="button"
              className={cn("sa-stat", isActive && "is-active", !chartable && "is-static")}
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
        <div className="sa-answer-chart" data-testid="sa-answer-chart">
          <div className="sa-answer-chart-label">
            <span>{result.metric_labels[activeMetric!] ?? activeMetric} · last {chartPoints.length}</span>
            <span className="sa-answer-chart-meta">
              avg {(chartPoints.reduce((s, p) => s + p.value, 0) / chartPoints.length).toFixed(1)}
            </span>
          </div>
          <MiniBars points={chartPoints.map((p) => p.value)} />
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

      <div className="sa-answer-foot">
        <span>Source: /research/stats/query</span>
        <span className="sa-dot" />
        <span>{result.source}</span>
      </div>
    </div>
  );
}

function MiniBars({ points }: { points: number[] }) {
  if (points.length === 0) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = Math.max(1, max - min);
  const mean = points.reduce((s, v) => s + v, 0) / points.length;
  const W = 400;
  const H = 90;
  const PAD_X = 20;
  const BAR_Y_TOP = 14;
  const BAR_AREA = 70;
  const FILL = 0.85;
  const FLOOR = 0.12;
  const yFor = (v: number) => {
    const fraction = ((v - min) / range) * FILL + FLOOR;
    return BAR_Y_TOP + BAR_AREA - fraction * BAR_AREA;
  };

  return (
    <div
      className="sa-chart-svg-wrap"
      style={{ width: "100%", maxWidth: 720, aspectRatio: `${W} / ${H}`, margin: "0 auto" }}
    >
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      width="100%"
      height="100%"
      role="img"
      aria-label="Trend chart"
      style={{ display: "block" }}
    >
      <line
        x1={0}
        x2={W}
        y1={yFor(mean)}
        y2={yFor(mean)}
        stroke="rgba(150,140,255,0.45)"
        strokeWidth={1}
        strokeDasharray="4 4"
      />
      {points.map((v, i) => {
        const x = PAD_X + (i / Math.max(1, points.length - 1)) * (W - 2 * PAD_X);
        const y = yFor(v);
        const h = BAR_Y_TOP + BAR_AREA - y;
        return (
          <g key={i}>
            <rect
              x={x - 10}
              y={y}
              width={20}
              height={h}
              rx={2}
              fill={v >= mean ? "rgba(120,210,200,0.78)" : "rgba(170,140,235,0.62)"}
            />
            <text
              x={x}
              y={BAR_Y_TOP - 4}
              textAnchor="middle"
              fill="rgba(210,220,240,0.85)"
              fontSize="10"
              fontFamily="var(--font-geist-sans), system-ui, sans-serif"
            >
              {Number.isInteger(v) ? v : v.toFixed(1)}
            </text>
          </g>
        );
      })}
    </svg>
    </div>
  );
}

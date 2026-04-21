"use client";

import { useState } from "react";
import { BarChart3, Plus, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { queryStats } from "@/lib/api";
import { SPORT_LABELS, type SportKey, type StatsQueryRead } from "@/lib/types";
import { fmtDate } from "@/lib/utils";
import { cn } from "@/lib/utils";

const SUGGESTIONS = [
  "Jalen Brunson last 10 games",
  "Jayson Tatum this season",
  "LeBron vs BOS · lifetime",
  "Curry on 2nd night B2B",
  "Mahomes vs top-10 defenses",
];

const EXAMPLES: Record<SportKey, string[]> = {
  NBA: ["Jalen Brunson last 10 games", "Jayson Tatum this season"],
  NFL: ["Patrick Mahomes this season", "Josh Allen last 5 games"],
  MLB: ["Aaron Judge this season", "Mookie Betts last 10 games"],
  SOCCER: ["Lionel Messi last 5 matches", "Kylian Mbappe this season"],
  TENNIS: ["Novak Djokovic last 5 matches", "Carlos Alcaraz this season"],
};

const CURRENT_SEASON = "2025-26";

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
      const seasonYear = season ? Number(season.slice(0, 4)) : undefined;
      const next = await queryStats({
        question: finalQuestion,
        sport_key: sportKey,
        season: Number.isFinite(seasonYear) ? seasonYear : undefined,
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
    <div className="flex flex-col gap-4">
      <section className="stats-card stats-header-card">
        <div className="stats-header-meta">
          <div className="stats-header-icon" aria-hidden>
            <BarChart3 size={18} />
          </div>
          <div className="stats-header-meta-text">
            <div className="stats-header-eyebrow">
              <span className="eyebrow">Research desk</span>
              <span className="path">/research/stats/query</span>
            </div>
            <h2 className="stats-header-title">Stats Assistant</h2>
            <p className="stats-header-desc">
              Cross-sport player query desk. Ask for recent form, season totals, or matchup context
              across NBA, NFL, MLB, Soccer, Tennis, and UFC.
            </p>
          </div>
          <span className="stats-ready-pill">
            <span className="dot" />
            READY
          </span>
        </div>

        <div className="stats-suggestion-row">
          {SUGGESTIONS.map((chip) => (
            <button
              key={chip}
              type="button"
              className="stats-suggest-chip"
              onClick={() => runSearch(chip)}
            >
              <Plus size={11} aria-hidden />
              <span>{chip}</span>
            </button>
          ))}
        </div>

        <div className="stats-query-row">
          <label className="stats-query-input">
            <Search size={14} aria-hidden />
            <Input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Ask for player form, season output, or last N games"
              onKeyDown={(event) => {
                if (event.key === "Enter") runSearch();
              }}
            />
          </label>
          <Select
            value={sportKey}
            onValueChange={(value) => {
              const nextSport = value as SportKey;
              setSportKey(nextSport);
              const nextExample = EXAMPLES[nextSport][0];
              if (nextExample) setQuestion(nextExample);
            }}
          >
            <SelectTrigger className="stats-select-trigger">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(SPORT_LABELS).map(([key, label]) => (
                <SelectItem key={key} value={key}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={season} onValueChange={setSeason}>
            <SelectTrigger className="stats-select-trigger">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {["2025-26", "2024-25", "2023-24", "2022-23"].map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant="primary"
            size="sm"
            className="stats-run-btn"
            onClick={() => runSearch()}
            disabled={loading}
          >
            {loading ? "Querying…" : "Run query"}
          </Button>
        </div>
        {error && <p className="mt-2 text-xs text-negative">{error}</p>}
      </section>

      <section className="stats-card stats-result-card">
        {result ? (
          <StatsResult result={result} />
        ) : (
          <EmptyOrbState />
        )}
      </section>
    </div>
  );
}

function EmptyOrbState() {
  return (
    <div className="stats-empty">
      <div className="stats-orb" aria-hidden>
        <span className="stats-orb-core" />
        <span className="stats-orb-halo" />
      </div>
      <h3 className="stats-empty-title">Run a player stats query</h3>
      <p className="stats-empty-desc">
        Answers appear here with a compact splits table, a mini trend
        chart, and the exact source the assistant used.
      </p>
    </div>
  );
}

function StatsResult({ result }: { result: StatsQueryRead }) {
  const metricEntries = Object.entries(result.summary.metrics)
    .filter(([, value]) => value != null)
    .slice(0, 6);

  return (
    <div className="stats-result">
      <header className="stats-result-head">
        <div>
          <h3 className="stats-result-title">{result.entity_name}</h3>
          <p className="stats-result-sub">
            {result.team_name ?? SPORT_LABELS[result.sport_key as SportKey] ?? result.sport_key}
            {" · "}
            {result.query_type.replaceAll("_", " ")}
            {" · "}
            {result.games_analyzed} analyzed
          </p>
        </div>
        <span className="stats-result-source">{result.source}</span>
      </header>

      {result.summary.stat_line && (
        <p className="stats-result-line">{result.summary.stat_line}</p>
      )}

      <div className="stats-metric-grid">
        {metricEntries.map(([key, value]) => (
          <div key={key} className="stats-metric">
            <p className="stats-metric-label">{result.metric_labels[key] ?? key}</p>
            <p className="stats-metric-value">
              {value == null ? "—" : Number.isInteger(value) ? String(value) : value.toFixed(2)}
            </p>
          </div>
        ))}
      </div>

      {result.explanation && (
        <p className="stats-result-explain">{result.explanation}</p>
      )}

      {result.coverage_note && (
        <p className="stats-coverage-note">{result.coverage_note}</p>
      )}

      {result.game_logs.length > 0 && (
        <div className="stats-log-list">
          <p className="stats-log-label">Latest {result.game_logs.length} games</p>
          {result.game_logs.slice(0, 8).map((game) => (
            <div key={game.game_id} className="stats-log-row">
              <div className="stats-log-meta">
                <span className="stats-log-date">{fmtDate(game.game_date)}</span>
                <span className="stats-log-opp">vs {game.opponent}</span>
                <span className="stats-log-loc">{game.location}</span>
              </div>
              <div className="stats-log-score">
                {game.team_score}–{game.opponent_score}
                <span className={cn("stats-log-result", game.result === "W" && "pos", game.result === "L" && "neg")}>
                  {game.result ?? "—"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

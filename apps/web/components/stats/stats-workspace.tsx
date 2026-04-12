"use client";

import { useState } from "react";
import { BarChart3, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
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

const EXAMPLES: Record<SportKey, string[]> = {
  NBA: ["Jalen Brunson last 10 games", "Jayson Tatum this season"],
  NFL: ["Patrick Mahomes this season", "Josh Allen last 5 games"],
  MLB: ["Aaron Judge this season", "Mookie Betts last 10 games"],
  SOCCER: ["Lionel Messi last 5 matches", "Kylian Mbappe this season"],
  TENNIS: ["Novak Djokovic last 5 matches", "Carlos Alcaraz this season"],
};

function SummaryMetric({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-surface-hover px-3 py-2">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </p>
      <p className="mt-1 text-sm font-medium text-foreground">{value}</p>
    </div>
  );
}

function metricValue(value: number | null): string {
  if (value == null) return "—";
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(2);
}

interface StatsWorkspaceProps {
  initialSport?: SportKey;
  compact?: boolean;
}

export function StatsWorkspace({
  initialSport = "NBA",
  compact = false,
}: StatsWorkspaceProps) {
  const [sportKey, setSportKey] = useState<SportKey>(initialSport);
  const [question, setQuestion] = useState(EXAMPLES[initialSport][0]);
  const [season, setSeason] = useState("");
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
        season: season ? Number(season) : undefined,
      });
      setQuestion(finalQuestion);
      setResult(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to query stats");
    } finally {
      setLoading(false);
    }
  }

  const examples = EXAMPLES[sportKey];
  const metricEntries = result
    ? Object.entries(result.summary.metrics)
        .filter(([, value]) => value != null)
        .slice(0, compact ? 4 : 8)
    : [];

  return (
    <div className="flex h-full flex-col gap-4">
      <Card className="border-border-bright bg-[linear-gradient(180deg,rgba(19,24,35,0.98),rgba(12,16,24,0.98))]">
        <CardHeader className="flex-col items-start gap-2 border-none">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-accent/20 bg-accent/10 text-accent">
              <BarChart3 size={15} />
            </div>
            <div>
              <CardTitle>Stats Assistant</CardTitle>
              <CardDescription>
                Cross-sport player query desk wired to `/research/stats/query`
              </CardDescription>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {examples.map((example) => (
              <Button
                key={example}
                variant="ghost"
                size="xs"
                onClick={() => runSearch(example)}
                className="h-auto rounded-full border border-border px-2.5 py-1 text-[11px] text-muted-foreground hover:text-foreground"
              >
                <Sparkles size={11} />
                {example}
              </Button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className={cn("grid gap-3", compact ? "grid-cols-1" : "grid-cols-[minmax(0,1fr)_112px_112px]")}>
            <Input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Ask for player form, season output, or last N games"
              className="h-10"
            />
            <Select
              value={sportKey}
              onValueChange={(value) => {
                const nextSport = value as SportKey;
                setSportKey(nextSport);
                setQuestion(EXAMPLES[nextSport][0]);
              }}
            >
              <SelectTrigger className="h-10">
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
            <Input
              value={season}
              onChange={(event) => setSeason(event.target.value)}
              placeholder="Season"
              className="h-10 font-mono"
            />
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={() => runSearch()}
              disabled={loading}
            >
              {loading ? "Querying…" : "Run Query"}
            </Button>
            {result && (
              <p className="text-xs text-muted-foreground">
                {result.entity_name} · {result.games_analyzed} analyzed · {result.source}
              </p>
            )}
          </div>
          {error && <p className="text-xs text-negative">{error}</p>}
        </CardContent>
      </Card>

      {result ? (
        <div className={cn("grid flex-1 min-h-0 gap-4", compact ? "grid-cols-1" : "grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]")}>
          <Card className="min-h-0">
            <CardHeader className="flex-col items-start gap-1 border-none">
              <CardTitle>{result.entity_name}</CardTitle>
              <CardDescription>
                {result.team_name ?? SPORT_LABELS[result.sport_key as SportKey] ?? result.sport_key} · {result.query_type.replaceAll("_", " ")}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className={cn("grid gap-2", compact ? "grid-cols-2" : "grid-cols-2 xl:grid-cols-4")}>
                <SummaryMetric label="Games" value={String(result.summary.games)} />
                <SummaryMetric label="Analyzed" value={String(result.games_analyzed)} />
                <SummaryMetric label="Season" value={String(result.season)} />
                <SummaryMetric label="Source" value={result.source} />
              </div>

              {result.summary.stat_line && (
                <div className="rounded-lg border border-border bg-surface-hover px-3 py-2.5">
                  <p className="text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                    Stat Line
                  </p>
                  <p className="mt-1 text-sm text-foreground">{result.summary.stat_line}</p>
                </div>
              )}

              <div className={cn("grid gap-2", compact ? "grid-cols-2" : "grid-cols-2 xl:grid-cols-4")}>
                {metricEntries.map(([key, value]) => (
                  <SummaryMetric
                    key={key}
                    label={result.metric_labels[key] ?? key}
                    value={metricValue(value)}
                  />
                ))}
              </div>

              <div className="space-y-2 text-sm text-muted-foreground">
                <p>{result.explanation}</p>
                {result.coverage_note && (
                  <p className="rounded-lg border border-warning/20 bg-warning/8 px-3 py-2 text-warning">
                    {result.coverage_note}
                  </p>
                )}
              </div>
            </CardContent>
          </Card>

          <Card className="min-h-0">
            <CardHeader className="flex-col items-start gap-1 border-none">
              <CardTitle>Game Log</CardTitle>
              <CardDescription>
                Latest {result.game_logs.length} tracked results
              </CardDescription>
            </CardHeader>
            <CardContent className="min-h-0 pb-0">
              <ScrollArea className={compact ? "h-[320px]" : "h-[460px]"}>
                <div className="space-y-2 pr-3">
                  {result.game_logs.map((game) => (
                    <div
                      key={game.game_id}
                      className="rounded-lg border border-border bg-surface-hover px-3 py-2.5"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-medium text-foreground">
                            {game.competition ?? `${game.team_name ?? result.entity_name} vs ${game.opponent}`}
                          </p>
                          <p className="text-xs text-muted-foreground">
                            {fmtDate(game.game_date)} · {game.location} · vs {game.opponent}
                          </p>
                        </div>
                        <div className="text-right">
                          <p className="font-mono text-xs text-foreground">
                            {game.team_score} - {game.opponent_score}
                          </p>
                          <p className="text-[11px] text-muted-foreground">
                            {game.result ?? "—"}
                          </p>
                        </div>
                      </div>
                      {game.stat_line && (
                        <p className="mt-2 text-xs text-foreground">{game.stat_line}</p>
                      )}
                    </div>
                  ))}
                </div>
              </ScrollArea>
            </CardContent>
          </Card>
        </div>
      ) : (
        <Card className="border-dashed">
          <CardContent className="flex h-full min-h-[220px] flex-col items-center justify-center gap-2 text-center">
            <div className="flex h-10 w-10 items-center justify-center rounded-full border border-border bg-surface-hover text-accent">
              <BarChart3 size={18} />
            </div>
            <div>
              <p className="text-sm font-medium text-foreground">Run a player stats query</p>
              <p className="text-xs text-muted-foreground">
                Ask for recent form, season totals, or matchup context across all supported sports.
              </p>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

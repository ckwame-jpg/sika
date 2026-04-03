"use client";

import useSWR from "swr";
import { fetchParlayWatchlist, keys } from "@/lib/api";
import type { ParlayRecommendationRead } from "@/lib/types";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge, SportBadge } from "@/components/ui/badge";
import { SkeletonRow } from "@/components/ui/skeleton";
import { cn, edgeClass, fmtDatetime, fmtEdge, fmtPercent } from "@/lib/utils";
import { parseParlayLegCount } from "@/components/parlays/parlay-filter-controls";

function sportScopeLabel(value: string) {
  if (value === "MIXED") return "NBA + MLB";
  return value;
}

function ParlayLegsCell({ parlay }: { parlay: ParlayRecommendationRead }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-foreground">{parlay.leg_count} legs</span>
        <Badge variant="outline">{sportScopeLabel(parlay.sport_scope)}</Badge>
      </div>
      <div className="space-y-1">
        {parlay.legs.map((leg) => (
          <p key={`${parlay.id}-${leg.leg_index}`} className="truncate text-xs text-muted-foreground">
            <span className="mr-1 font-mono text-foreground">{leg.leg_index}.</span>
            {leg.side.toUpperCase()} {leg.market_title}
          </p>
        ))}
      </div>
    </div>
  );
}

export function ParlayWatchlistSection({
  sportScope,
  legCount,
}: {
  sportScope: string;
  legCount: string;
}) {
  const numericLegCount = parseParlayLegCount(legCount);
  const { data, isLoading, error } = useSWR<ParlayRecommendationRead[]>(
    keys.parlayWatchlist(sportScope, numericLegCount, 50),
    () => fetchParlayWatchlist(sportScope, numericLegCount, 50),
    { refreshInterval: 30_000 },
  );

  const items = data ?? [];

  return (
    <Card>
      <CardHeader className="flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <CardTitle>Parlays</CardTitle>
          <CardDescription>
            Synthetic NBA and MLB combinations built from the strongest current single-pick edges.
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {error ? (
          <div className="flex h-24 items-center justify-center text-xs text-negative">
            Failed to load parlays.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-32">Captured</TableHead>
                  <TableHead>Legs</TableHead>
                  <TableHead className="w-32">Sports</TableHead>
                  <TableHead className="w-24">Odds</TableHead>
                  <TableHead className="w-24">Model</TableHead>
                  <TableHead className="w-20">Edge</TableHead>
                  <TableHead className="w-24">Confidence</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {isLoading
                  ? Array.from({ length: 4 }).map((_, index) => (
                      <SkeletonRow key={index} cols={7} />
                    ))
                  : items.length === 0
                    ? (
                      <TableRow>
                        <TableCell colSpan={7} className="py-10 text-center text-xs text-muted-foreground">
                          No parlays matched the current filters.
                        </TableCell>
                      </TableRow>
                    )
                    : items.map((parlay) => (
                        <TableRow key={parlay.id}>
                          <TableCell className="font-mono text-xs text-muted-foreground">
                            {fmtDatetime(parlay.captured_at)}
                          </TableCell>
                          <TableCell>
                            <ParlayLegsCell parlay={parlay} />
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-wrap gap-1">
                              {parlay.participating_sports.map((sport) => (
                                <SportBadge key={`${parlay.id}-${sport}`} sport={sport} />
                              ))}
                            </div>
                          </TableCell>
                          <TableCell className="font-mono text-xs text-foreground">
                            {parlay.american_odds}
                          </TableCell>
                          <TableCell className="font-mono text-xs text-muted-foreground">
                            {fmtPercent(parlay.combined_model_probability)}
                          </TableCell>
                          <TableCell>
                            <span className={cn("font-mono text-xs font-medium", edgeClass(parlay.edge))}>
                              {fmtEdge(parlay.edge)}
                            </span>
                          </TableCell>
                          <TableCell className="font-mono text-xs text-muted-foreground">
                            {fmtPercent(parlay.confidence)}
                          </TableCell>
                        </TableRow>
                      ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

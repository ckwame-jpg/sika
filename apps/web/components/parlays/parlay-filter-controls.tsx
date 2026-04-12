"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const PARLAY_SPORT_OPTIONS = [
  { value: "all", label: "All" },
  { value: "NBA", label: "NBA" },
  { value: "MLB", label: "MLB" },
] as const;

const PARLAY_SIZE_OPTIONS = [
  { value: "all", label: "All sizes" },
  { value: "2", label: "2 legs" },
  { value: "3", label: "3 legs" },
  { value: "4", label: "4 legs" },
  { value: "5", label: "5 legs" },
  { value: "6", label: "6 legs" },
] as const;

export function parseParlayLegCount(value: string): number | undefined {
  return value === "all" ? undefined : Number(value);
}

export function ParlayFilterControls({
  sportScope,
  onSportScopeChange,
  legCount,
  onLegCountChange,
}: {
  sportScope: string;
  onSportScopeChange: (value: string) => void;
  legCount: string;
  onLegCountChange: (value: string) => void;
}) {
  return (
    <>
      <div className="flex items-center justify-between gap-2 sm:justify-start">
        <span className="text-xs text-muted-foreground">Parlay sport</span>
        <Select value={sportScope} onValueChange={onSportScopeChange}>
          <SelectTrigger className="h-8 w-[min(200px,60vw)] text-xs sm:w-[120px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PARLAY_SPORT_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex items-center justify-between gap-2 sm:justify-start">
        <span className="text-xs text-muted-foreground">Parlay size</span>
        <Select value={legCount} onValueChange={onLegCountChange}>
          <SelectTrigger className="h-8 w-[min(200px,60vw)] text-xs sm:w-[120px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PARLAY_SIZE_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </>
  );
}

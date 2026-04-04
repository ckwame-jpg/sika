"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export type RecommendationViewMode = "balanced" | "quality" | "coverage";

interface QualityFilterSelectProps {
  value: RecommendationViewMode;
  onValueChange: (value: RecommendationViewMode) => void;
  triggerClassName?: string;
  includeCoverage?: boolean;
}

export function QualityFilterSelect({
  value,
  onValueChange,
  triggerClassName,
  includeCoverage = false,
}: QualityFilterSelectProps) {
  return (
    <Select value={value} onValueChange={(next) => onValueChange(next as RecommendationViewMode)}>
      <SelectTrigger className={triggerClassName}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="balanced">Balanced</SelectItem>
        <SelectItem value="quality">Quality</SelectItem>
        {includeCoverage && <SelectItem value="coverage">Coverage</SelectItem>}
      </SelectContent>
    </Select>
  );
}

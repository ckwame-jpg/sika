"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SPORT_OPTIONS, cn } from "@/lib/utils";

export function useSportQueryParam() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const sport = searchParams.get("sport") ?? "";

  function setSport(nextSport: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (!nextSport || nextSport === "all") {
      params.delete("sport");
    } else {
      params.set("sport", nextSport);
    }

    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname);
  }

  return {
    sport: sport || undefined,
    sportValue: sport || "all",
    setSport,
  };
}

interface SportFilterSelectProps {
  className?: string;
  triggerClassName?: string;
  allLabel?: string;
}

export function SportFilterSelect(props: SportFilterSelectProps) {
  const { className, triggerClassName, allLabel = "All sports" } = props;
  const { sportValue, setSport } = useSportQueryParam();

  return (
    <div className={className}>
      <Select value={sportValue} onValueChange={setSport}>
        <SelectTrigger className={cn("h-8 w-[140px]", triggerClassName)}>
          <SelectValue placeholder={allLabel} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">{allLabel}</SelectItem>
          {SPORT_OPTIONS.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

import { cn } from "@/lib/utils";

export function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "animate-pulse rounded bg-surface-hover",
        className,
      )}
      {...props}
    />
  );
}

export function SkeletonRow({ cols = 4 }: { cols?: number }) {
  return (
    <tr>
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-3 py-2.5">
          <Skeleton className="h-4 w-full" />
        </td>
      ))}
    </tr>
  );
}

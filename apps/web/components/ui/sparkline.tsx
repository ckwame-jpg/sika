"use client";

import { useId } from "react";

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  trend?: "up" | "down" | "auto";
  className?: string;
}

export function Sparkline({
  values,
  width = 56,
  height = 18,
  trend = "auto",
  className,
}: SparklineProps) {
  const gid = useId();
  const series = values && values.length >= 2 ? values : [0.5, 0.5];
  const min = Math.min(...series);
  const max = Math.max(...series);
  const span = Math.max(0.0001, max - min);
  const n = series.length;
  const pad = 1.2;
  const points = series.map((v, i) => {
    const x = pad + (i / (n - 1)) * (width - pad * 2);
    const y = pad + (1 - (v - min) / span) * (height - pad * 2);
    return [x, y] as const;
  });
  const linePath = points
    .map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`)
    .join(" ");
  const last = points[points.length - 1];
  const first = points[0];
  const fillPath =
    `${linePath} L${last[0].toFixed(2)} ${(height - pad).toFixed(2)} ` +
    `L${first[0].toFixed(2)} ${(height - pad).toFixed(2)} Z`;
  const trendDir = trend === "auto" ? (series[n - 1] >= series[0] ? "up" : "down") : trend;

  return (
    <svg
      className={`spark ${trendDir}${className ? ` ${className}` : ""}`}
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      preserveAspectRatio="none"
      aria-hidden
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.35" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={fillPath} fill={`url(#${gid})`} stroke="none" />
      <path
        d={linePath}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function randomWalk(n: number, up: boolean, seed: number): number[] {
  const out: number[] = [];
  let v = 0.5;
  let r = seed;
  for (let i = 0; i < n; i++) {
    r = (r * 9301 + 49297) % 233280;
    const step = (r / 233280 - 0.5) * 0.18;
    const bias = (up ? 1 : -1) * (0.02 + (i / n) * 0.04);
    v = Math.max(0.05, Math.min(0.95, v + step + bias));
    out.push(v);
  }
  return out;
}

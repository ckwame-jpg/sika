/**
 * Runtime-resolved theme colors for chart/SVG/canvas call sites.
 *
 * Recharts props, SVG presentation attributes, and canvas APIs can't consume
 * `var()`, so these helpers resolve tokens from the live cascade instead
 * (same pattern as probability-surface-hero's canvas). Static fallbacks keep
 * jsdom (vitest) and SSR rendering, where getComputedStyle returns empty
 * strings. The theme is static per session, so resolve-once is safe.
 */

const FALLBACKS: Record<string, string> = {
  "--accent": "hsl(262 68% 62%)",
  "--accent-hsl": "262 68% 62%",
  "--positive": "hsl(160 72% 46%)",
  "--positive-hsl": "160 72% 46%",
  "--border": "hsl(248 26% 17%)",
  "--muted-foreground": "hsl(215 18% 54%)",
  "--color-cosmos-violet-500-hsl": "261 100% 77%",
  "--color-cosmos-violet-50-hsl": "253 100% 96%",
  "--color-cosmos-success-hsl": "154 67% 59%",
  "--color-cosmos-border-soft": "hsl(0 0% 100% / 0.08)",
};

/** Resolve a CSS custom property from :root, falling back for jsdom/SSR. */
export function getToken(name: string): string {
  if (typeof document !== "undefined") {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    if (value) return value;
  }
  return FALLBACKS[name] ?? "";
}

export interface ChartPalette {
  /** Primary data line (accent violet). */
  line: string;
  /** Gradient/dot fill companion for `line` at low alpha. */
  lineSoft: string;
  /** Positive/secondary data line (money green). */
  positive: string;
  positiveSoft: string;
  /** Calibration/scatter dot fill + stroke. */
  dotFill: string;
  dotStroke: string;
  /** Axis grid strokes. */
  grid: string;
  /** Axis tick label fill. */
  tick: string;
  /** Chart frame / plot border. */
  frame: string;
  /** Diagonal / dashed reference strokes on dark panels. */
  reference: string;
  /** Above-threshold bar fill. */
  barHigh: string;
  /** Below-threshold / neutral bar fill. */
  barLow: string;
  /** Dashed threshold line in bar charts. */
  threshold: string;
  /** Numeric bar labels. */
  barLabel: string;
}

let cached: ChartPalette | null = null;

/** Named chart color roles resolved from the cosmos theme. Memoized. */
export function getChartPalette(): ChartPalette {
  if (cached) return cached;
  const accent = getToken("--accent");
  const positiveHsl = getToken("--positive-hsl");
  const violetHsl = getToken("--color-cosmos-violet-500-hsl");
  const successHsl = getToken("--color-cosmos-success-hsl");
  cached = {
    line: accent,
    lineSoft: `hsl(${getToken("--accent-hsl")} / 0.15)`,
    positive: getToken("--positive"),
    positiveSoft: `hsl(${positiveHsl} / 0.12)`,
    dotFill: `hsl(${positiveHsl} / 0.85)`,
    dotStroke: getToken("--positive"),
    grid: getToken("--border"),
    tick: getToken("--muted-foreground"),
    frame: getToken("--color-cosmos-border-soft"),
    reference: "hsl(0 0% 100% / 0.18)",
    barHigh: `hsl(${successHsl} / 0.78)`,
    barLow: `hsl(${violetHsl} / 0.62)`,
    threshold: `hsl(${violetHsl} / 0.45)`,
    barLabel: `hsl(${getToken("--color-cosmos-violet-50-hsl")} / 0.98)`,
  };
  return cached;
}

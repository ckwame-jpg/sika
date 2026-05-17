# Sika Design System

**Last reconciled:** 2026-05-17 · against `apps/web/app/globals.css` + every file in `apps/web/components/ui/` + a representative sample of composites.
**Drift cleanup:** late 2026-05-17 batch closed §5.1, §5.2, §5.3, §5.4 (partially), §6.1 (partially), §6.2 (partially), §6.5, and §9 rec 7 across [sika#196](https://github.com/ckwame-jpg/sika/pull/196) – [sika#203](https://github.com/ckwame-jpg/sika/pull/203). See §5 Drift report for status of each item.

This is an **audit + documentation** pass. It catalogs what exists, names the conventions, and flags drift. It does NOT propose new patterns — extension is a separate workflow.

## How to use this doc

| You are about to… | Read… |
|---|---|
| Build a new component | "Decision tree for new components" → "Pattern catalog" |
| Understand an existing class | Skim "Pattern catalog" by prefix |
| Add a new color / size / pattern | "Tokens" + "Naming conventions" + check "Drift report" for precedent |
| Fix inconsistency | "Drift report" |
| Review a teammate's PR | "Drift report" + "Naming conventions" |

**Not encyclopedic.** When a pattern is well-understood from its name + CSS, the doc says so and points at the source file rather than re-explaining it.

## Repo at a glance

- **Stack**: Next.js (App Router) · Tailwind v4 · CSS variables · cva for primitive variants
- **Theme**: Dark "cosmos" theme is the product default. Light mode exists in `:root` but operators run dark.
- **Token system**: All colors / fonts / radii / shadows / animations defined as CSS variables, mapped to Tailwind utilities via `@theme inline` in `globals.css:4-88`. New utilities (e.g. `bg-cosmos-violet`) work automatically once a token is defined.
- **Primitives**: Live in `apps/web/components/ui/`. Mostly Radix wrappers + cva variants.
- **Patterns**: Live as `.classname` blocks in `globals.css`. Each prefix corresponds to a feature surface (`event-card` → events feed, `sa-` → stats assistant, etc.).

---

## 1. Tokens

Every token is defined as a CSS variable in `globals.css`. The Tailwind `@theme inline` block at the top maps those variables to utility names. **Always prefer the utility (e.g. `bg-positive/10`) over a raw `var()` call** — the utility composes with opacity modifiers cleanly.

### 1.1 Color tokens

#### Semantic (used everywhere)

| Token | Tailwind utility | Dark value | Used for |
|---|---|---|---|
| `--background` | `bg-background` | `hsl(0 0% 0%)` (true black) | App background |
| `--surface` | `bg-surface` | `hsl(220 42% 8%)` | Card / panel surfaces |
| `--surface-hover` | `bg-surface-hover` | `hsl(220 40% 12%)` | Hover state for surfaces |
| `--border` | `border-border` | `hsl(220 28% 16%)` | Default borders |
| `--border-bright` | `border-border-bright` | `hsl(220 25% 22%)` | Hover / active borders |
| `--foreground` | `text-foreground` | `hsl(213 31% 91%)` | Default body text |
| `--muted` | `bg-muted` | `hsl(220 30% 12%)` | Muted surfaces |
| `--muted-foreground` | `text-muted-foreground` | `hsl(215 18% 54%)` | Secondary text |
| `--accent` | `bg-accent`, `text-accent` | `hsl(217 91% 60%)` | Primary action color (blue) |
| `--accent-dim` | `bg-accent-dim` | `hsl(217 91% 60% / 0.12)` | Tinted accent backgrounds |
| `--positive` | `bg-positive`, `text-positive`, `border-positive` | `hsl(160 72% 46%)` | Wins, gains, "ok" status |
| `--positive-dim` | `bg-positive-dim` | `hsl(160 72% 46% / 0.12)` | Tinted positive backgrounds |
| `--negative` | `bg-negative`, `text-negative`, `border-negative` | `hsl(0 85% 65%)` | Losses, errors, "bad" status |
| `--negative-dim` | `bg-negative-dim` | `hsl(0 85% 65% / 0.12)` | Tinted negative backgrounds |
| `--warning` | `bg-warning`, `text-warning`, `border-warning` | `hsl(40 95% 58%)` | "Pending", "warn", caution |

**Composition pattern:** every semantic color supports Tailwind opacity modifiers — `bg-positive/10`, `border-negative/40`, `text-warning/70`. **Use these everywhere** instead of inline `rgba()` or `hsl()`.

#### Cosmos accents (theme color)

| Token | Used for |
|---|---|
| `--cosmos-violet` | Primary cosmos accent — used in panels, hero gradients, orb decorations |
| `--cosmos-cyan` | Secondary cosmos accent — pairs with violet in gradients |

A larger cosmos ramp lives at `globals.css:221-267` as HSL triplets (e.g. `--color-cosmos-violet-500-hsl: 261 100% 77%`) so they compose with alpha at usage sites:

```css
background: hsl(var(--color-cosmos-violet-500-hsl) / 0.22);
```

**When you'd use the ramp directly:** custom decorative effects (orbs, hero gradients, the probability surface canvas). The ramp is NOT for typical component work — use the flat semantic tokens above instead.

#### Sport tints

| Token | Tailwind utility | Sport |
|---|---|---|
| `--sport-nba` | `text-sport-nba`, `bg-sport-nba/10` | NBA |
| `--sport-nfl` | … | NFL |
| `--sport-mlb` | … | MLB |
| `--sport-wnba` | … | WNBA |
| `--sport-soccer` | … | Soccer |
| `--sport-tennis` | … | Tennis |
| `--sport-ufc` | … | UFC (out of active scope but tokens exist) |

Used in `SportBadge` and via the `--sport-tint` CSS custom property pattern in `event-card` ([`events-feed.tsx:122`](components/events/events-feed.tsx#L122)) for per-sport accent coloring.

#### Cosmos-* extended tokens

Approximately 70 cosmos-prefixed tokens at `globals.css:120-218` for specialized surfaces:

- `--color-cosmos-surface-{prop,ticket-stat,stats-input,stats-metric}` — translucent surfaces for specific feature contexts
- `--color-cosmos-violet-{edge,hairline,row-tint,dashed,glow,orb-*,...}` — fine-grained violet variants
- `--color-cosmos-cyan-{edge,hairline,kpi-orb,tab-bg,tab-fg}` — fine-grained cyan variants
- `--color-cosmos-outcome-{won,lost,pending,cancel}-{border,bg,fg}` — the source of truth for `.outcome-pill` colors
- `--color-cosmos-{ready,coverage}-{border,bg,fg,dot}` — readiness panel surfaces
- `--color-cosmos-tile-{border,bg}` — `.stats-tile` colors
- `--color-cosmos-table-hover` — `.cosmos-table-wrap` row hover
- `--color-cosmos-text-{bright,muted,title-hl}` — text on cosmos surfaces

**Rule of thumb:** these are pattern-internal. You'll usually inherit them by using the matching pattern class (`.stats-tile`, `.outcome-pill`, `.cosmos-table-wrap`) rather than referencing them directly. If you need one directly, that's a signal you might be reinventing a pattern.

### 1.2 Typography

```css
--font-sans: var(--font-geist-sans), system-ui, sans-serif;
--font-mono: var(--font-geist-sans), system-ui, sans-serif;
```

**Both font-sans AND font-mono resolve to Geist Sans.** Geist's tabular-numerals feature (`cv02`, `cv03`, `cv04`, `cv11` enabled in `globals.css:310`) gives the monospace look for numbers without a separate font. **Use `font-mono tabular-nums` for all numeric data** — this is the convention.

Body styles (`globals.css:313-320`):
```css
body {
  font-family: var(--font-geist-sans);
  font-size: 14px;
  line-height: 1.5;
  text-transform: lowercase;   /* <-- application-wide */
}
```

**Application-wide lowercase.** Every label, button, badge inherits `text-transform: lowercase` unless explicitly overridden with a class. When you write `<span>POINTS</span>` it renders "points". Don't fight this — uppercase labels typically use `uppercase tracking-wider` Tailwind utilities for an intentional all-caps treatment (see `.cosmos-kpi-label`, `.stats-tile-label`).

### 1.3 Radii

```css
--radius-sm: 4px;
--radius:    6px;   /* default */
--radius-md: 8px;
--radius-lg: 10px;
--radius-xl: 12px;
--radius-2xl: 16px;
```

Use `rounded`, `rounded-md`, `rounded-lg`, `rounded-xl`, `rounded-2xl`. **Avoid `rounded-full` for non-pills** — pills should use the `.outcome-pill` pattern.

### 1.4 Shadows

```css
--shadow-surface:        0 1px 3px 0 rgb(0 0 0 / 0.4), …
--shadow-elevated:       0 4px 12px 0 rgb(0 0 0 / 0.5), …
--shadow-glow-accent:    0 0 16px 0 hsl(217 91% 60% / 0.2)
--shadow-glow-positive:  0 0 12px 0 hsl(160 72% 46% / 0.25)
--shadow-glow-cosmos:    0 0 24px 0 hsl(262 60% 58% / 0.35)
```

Usage: `shadow-surface`, `shadow-elevated`, `shadow-glow-accent`. **Use `shadow-elevated` for floating elements (modals, sheets), `shadow-surface` for inline cards, glows for celebratory or attention-grabbing moments only.**

### 1.5 Animation

```css
--animate-fade-in:        fade-in 150ms ease-out
--animate-slide-up:       slide-up 150ms ease-out
--animate-slide-in-right: slide-in-right 150ms ease-out
--animate-slide-in-left:  slide-in-left 150ms ease-out
```

Plus custom keyframes for decorative effects: `lo-spin`, `nav-orbit`, `live-pulse`, `stats-orb-pulse`. Standard transitions use `transition-colors duration-[120ms]` (140ms is the de-facto interaction speed).

### 1.6 Orphaned / under-used tokens (drift report seed)

These are defined but appear in 0 components by search:

- `--color-cosmos-violet-avatar-top`, `--color-cosmos-violet-avatar-bottom` (avatar pattern not present)
- `--color-cosmos-hero-shadow` (only used in one inline gradient)
- `--color-cosmos-stats-white` (could likely be replaced with `--color-cosmos-text-bright`)
- `--shadow-glow-positive` (defined but no consumers found via grep)

**Action:** when you add a new pattern, prefer renaming/repurposing one of these over creating a 71st cosmos token. If the orphan stays orphaned across two sessions, delete it.

---

## 2. UI primitives (`components/ui/`)

These are the lowest-layer components. Use the primitive over hand-rolling.

### Button — `button.tsx`

cva-driven primitive. Variants: `primary`, `secondary` (default), `ghost`, `danger`, `positive`, `link`. Sizes: `xs`, `sm`, `md` (default), `lg`, `icon`, `icon-sm`. `asChild` prop wraps a Radix Slot for composition.

**When to use which variant:**

| Variant | Use for |
|---|---|
| `primary` | Single primary action per surface (e.g. "Paper trade" on the ticket) |
| `secondary` | Most buttons. Default. Bordered surface. |
| `ghost` | Tertiary actions, inline buttons, close buttons |
| `danger` | Destructive irreversible actions (delete, force-close) |
| `positive` | Confirm a beneficial action (settle, approve) |
| `link` | Inline text-as-button |

```tsx
import { Button } from "@/components/ui/button";
<Button variant="primary" size="sm">Paper trade</Button>
<Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
```

### Badge — `badge.tsx`

cva-driven. Variants: `default`, `accent`, `positive`, `negative`, `warning`, `outline`, plus 6 sport-specific (`nba`, `nfl`, `mlb`, `soccer`, `tennis`, `ufc`).

```tsx
import { Badge, SportBadge } from "@/components/ui/badge";
<Badge variant="positive">settled</Badge>
<SportBadge sport={event.sport_key} />
```

**Note:** `Badge` (the primitive) and `.outcome-pill` (the CSS class pattern) overlap visually. **Use `.outcome-pill` when you need the won/lost/pending status semantics** — its color tokens are tuned for outcome states. **Use `<Badge variant="…">` for general-purpose label chips** that don't represent a settled outcome.

### Input — `input.tsx`

Plain `<input>` wrapper. `mono` prop applies `font-mono`. h-8 default.

### Skeleton — `skeleton.tsx`

`<Skeleton className="h-4 w-full" />` for loading placeholders. `<SkeletonRow cols={N} />` for table rows. Always wrap your skeleton in the same structural shape as the loaded content (don't render a generic spinner where you'll render a table; render `<SkeletonRow>` so the layout doesn't reflow).

### Table — `table.tsx`

`<Table> / <TableHeader> / <TableBody> / <TableRow> / <TableHead> / <TableCell>` — semantic table primitives. Rows get `hover:bg-surface-hover` and `data-[selected=true]:bg-accent/5` by default. Headers are `text-xs uppercase tracking-wide text-muted-foreground`.

### Sparkline — `sparkline.tsx`

`<Sparkline values={…} width={56} height={18} trend="auto" />`. Renders as `currentColor` so coloring is inherited. Includes a `linearGradient` fill. Falls back to a flat baseline when fewer than 2 values.

### Other primitives

- `Dialog`, `Sheet`, `Tooltip`, `Select`, `Separator`, `ScrollArea` — Radix wrappers. Standard usage; consult source.

### Custom hooks

- Spread under `lib/` — `usePriceDisplay`, `useHealthStatus`, `useSportQueryParam`, `useViewQueryParam`. Read source before adding similar hooks.

---

## 3. Pattern catalog

Patterns are defined as `.classname` blocks in `globals.css` and used across composites. **Usage frequency** measured by grep across `apps/web/components/`.

### `.cosmos-panel` (46 usages)

**What it is:** the canonical container for ops + research surfaces. Translucent gradient background, violet border, soft inner highlight + drop shadow.

**Definition:** `globals.css:435-486`. Has a child `.cosmos-panel-muted` for less-emphasized panels.

**Sub-parts:**
- `.cosmos-panel-head` — title + description block
- `.cosmos-panel-head-text` — text container inside head
- `.cosmos-panel-title` — the h2-like title
- `.cosmos-panel-desc` — supporting description
- `.cosmos-panel-body` — main content area (14px 18px padding); `.cosmos-panel-body.flush` to remove padding

**Use when:** building an /ops or /research surface that needs to feel like an instrument panel. Master-detail layouts. Settings sections.

**Don't use when:** building a tile or a chip (use `stats-tile` or `outcome-pill`). Building a trade-desk surface (those use bespoke `trade-*` classes).

**States:** Static — no built-in hover/active. Add Tailwind utilities if needed.

**Accessibility:** Use `<section>` as the root. If the panel has a single visible heading, that's enough; for multi-region pages use `aria-labelledby` pointing at the title id.

**Example** ([`runs-desk.tsx:87`](components/runs/runs-desk.tsx#L87)):
```tsx
<section className="cosmos-panel relative z-10 min-h-0 overflow-hidden">
  <div className="cosmos-panel-head">
    <div className="cosmos-panel-head-text">
      <h2 className="cosmos-panel-title">Recent Runs</h2>
    </div>
  </div>
  <div className="cosmos-panel-body min-h-0 pb-0">
    {/* … */}
  </div>
</section>
```

### `.stats-tile` (91 usages — most-used pattern)

**What it is:** small metric tile for ops dashboards. Rounded 10px border, subtle translucent background, label + value pair.

**Definition:** `globals.css:1885-1903`. Sub-parts: `.stats-tile-label` (uppercase tracking) + `.stats-tile-value` (bright foreground).

**Use when:** displaying a labeled numeric metric in an ops grid (readiness panel, runs detail, settings). The right shape for "X events", "12 settled", "Mode: shadow".

**Don't use when:** the value is the headline of the screen (use `.cosmos-kpi-value`). The value carries a directional tone (use `.trade-kpi-value.{pos,neg,warn}` for tone-aware metrics).

**Example** ([`runs-desk.tsx:42`](components/runs/runs-desk.tsx#L42)):
```tsx
<div className="stats-tile">
  <p className="stats-tile-label">{label}</p>
  <p className="stats-tile-value font-mono text-lg">{value}</p>
</div>
```

### `.outcome-pill` (35 usages)

**What it is:** colored chip for settled / pending / won / lost status. Pill-shaped (border-radius 999px). Uppercase 10.5px font, letterspaced.

**Definition:** `globals.css:1783-1821`. Variants by modifier class:
- `.won` / `.settled` — green (uses `--color-cosmos-outcome-won-*`)
- `.lost` — red
- `.push` / `.pending` / `.unresolved` — amber
- `.cancelled` — muted gray

**Use when:** representing a settled or in-flight outcome (prediction result, refresh job status, settlement state, mapping confidence band).

**Don't use when:** showing a label without semantic state — use `<Badge>` instead.

**States:** static. Add transitions in the consumer if needed.

**Accessibility:** the pill IS its own label. When you wrap it in a button or row, give that parent an accessible name; don't double-up.

**Example** ([`runs-desk.tsx:128`](components/runs/runs-desk.tsx#L128)):
```tsx
<span className={cn("outcome-pill", statusPillClass(run.status))}>
  {run.status}
</span>
```

Where `statusPillClass` maps domain status → variant class (`completed → settled`, `failed → lost`, etc).

### `.event-card` (24 usages)

**What it is:** sport-themed card for an event/game. Uses CSS custom property `--sport-tint` to color-coordinate per sport (NBA = purple, MLB = magenta, etc).

**Definition:** `globals.css:792-929`. Sub-parts: `-head`, `-toggle`, `-chev`, `-summary`, `-when`, `-markets`, `-body`, `-grid`, `-tile`, `-tile-label`, `-tile-value`, `-tile-sub`, `-empty`. Also has companion `.event-status-pill` for status (live / final / scheduled).

**Use when:** rendering a single game in a list. Has open/closed states for expandable variants.

**Don't use when:** the row is a market or a prediction (those have their own patterns: `.line-row`, `.pred-card`).

**Example** ([`events-feed.tsx:120`](components/events/events-feed.tsx#L120)):
```tsx
<article className="event-card" style={{ ["--sport-tint" as string]: tint }}>
  <header className="event-card-head">
    <SportPill sportKey={event.sport_key} />
    <StatusPill status={event.status} />
    <span className="event-card-when">{fmtTime(event.starts_at)}</span>
  </header>
  <div className="event-card-grid">
    <div className="event-card-tile">
      <div className="event-card-tile-label">Score</div>
      <div className="event-card-tile-value">…</div>
    </div>
  </div>
</article>
```

### `.trade-kpi` (39 usages)

**What it is:** big-format KPI card with a decorative violet orb, label, value, optional sub + sparkline. Used on the trade desk hero strip and the predictions hero strip.

**Definition:** `globals.css:723-790`. Sub-parts: `-orb`, `-label`, `-value` (variants: `.pos`, `.neg`, `.warn`), `-sub`, `-spark`. Plus container patterns `.trade-kpis`, `.pred-kpis`, `.parlay-kpis` for the strip layouts.

**Use when:** a single feature surface's hero strip — the visual landing where the operator forms the gist. NOT every metric tile (that's `.stats-tile`).

**Example** ([`trade-desk.tsx:197`](components/trade/trade-desk.tsx#L197)):
```tsx
<div className="trade-kpi" data-testid={testIdRoot}>
  <div className="trade-kpi-orb" aria-hidden />
  <div className="trade-kpi-label">{label}</div>
  <div className="trade-kpi-value" data-testid={testIdValue}>{value}</div>
  {sub && <div className="trade-kpi-sub">{sub}</div>}
</div>
```

### `.pred-card` (49 usages)

**What it is:** mobile prediction-ledger card. Compact stat grid + pills row.

**Definition:** `globals.css:1823-1883`. Sub-parts: `-head`, `-title`, `-sub`, `-time`, `-grid`, `-stat-label`, `-stat-value` (variants: `.pos`, `.neg`), `-pills`.

**Use when:** mobile rendering of prediction rows where the table primitive becomes unreadable.

### `.trade-ticket` + `ticket-*` (in trade-ticket.tsx)

**What it is:** the right-rail ticket on `/trade`. Bespoke vertical layout with eyebrow, title, lean line, ticket-pair stat grid, history strip, action buttons.

**Definition:** `globals.css:1314-1626`. Sub-parts include `ticket-eyebrow`, `ticket-title`, `ticket-lean`, `ticket-meta`, `ticket-pair`, `ticket-stat` (with `-label`, `-value`, variants `.pos`/`.neg`/`.accent`), `ticket-section-divider`, plus the empty-orb pattern `.trade-ticket-empty-orb` for the "pick a market" state.

**Use when:** never reuse outside the trade ticket. Bespoke to that one surface.

### `.sa-*` (stats assistant) — many usages in `stats/`

**What it is:** the bespoke "stats assistant" feature surface. Includes header, prompts (suggestion chips), input row with icon + kbd shortcut + select + run button, result zone (empty/loading/answer), and an advanced metrics grid.

**Definition:** `globals.css:2258-2871`. Many sub-parts — too many to enumerate; use the `sa-*` namespace consistently when extending and read source ([`stats-workspace.tsx`](components/stats/stats-workspace.tsx)).

**Use when:** never reuse outside the stats workspace. Bespoke prefix for that one feature surface.

**Notable empty/loading patterns to reuse-via-imitation:**
- `.sa-result-empty` + `.sa-result-orb` — the "no data yet" orb pattern. Used in `.trade-ticket.empty` as well. Worth recognizing as a sika idiom.
- `.sa-result-loading` + `.sa-result-bar` (×3, widths 42%/78%/60%) + `.sa-result-scan` — the "scanning {sport} logs…" loader. Distinctive sika personality.

### `.cosmos-toolbar` (3 usages)

**What it is:** translucent filter bar. Used at the top of /events to hold sport + date filters.

**Definition:** `globals.css:1722-1746`. Sub-parts: `-spacer`, `-meta`.

**Use when:** a horizontal filter row at the top of a feed. Currently only on /events.

### `.cosmos-chip` (defined `globals.css:464-486`, ~12 consumers)

**What it is:** chip-style toggle with hover + active states. Active state (via `data-active="true"` attribute selector) uses a violet/cyan gradient + glow.

**Adopted 2026-05-17** via [sika#198](https://github.com/ckwame-jpg/sika/pull/198) (Settings page price-display tiles + pick-history chips + narrator toggles) and [sika#199](https://github.com/ckwame-jpg/sika/pull/199) (Mappings Desk confidence preset chips). Canonical pattern for any toggle / selectable chip.

**Use when:** an exclusive-selection chip / preset / toggle. Pair with `data-active={active ? "true" : undefined}` for the active state and `focus-visible:ring-focus` for keyboard focus.

### `.cosmos-table-wrap` (used in mappings, runs)

**What it is:** dark themed table wrapper with hover row + uppercase headers. Has companion `.cosmos-table-empty` for the empty state.

**Definition:** `globals.css:1748-1782`.

**Use when:** rendering tabular data inside a `.cosmos-panel`. For trade-desk tables of mixed-type rows, use bespoke patterns (`.line-row`, `.prop-stat-row`).

### `.prop-group` + sub-parts (player-prop ladders)

**What it is:** collapsible group of player-prop thresholds. Definition: `globals.css:1147-1313`. Bespoke to the trade desk's prop ladder. Don't reuse.

### `.line-row` (game-line table rows)

**What it is:** flex row for game-line markets on the trade desk. Definition: `globals.css:1082-1145`. Bespoke; don't reuse.

### Sidebar / topbar / brand chrome (`.sidebar-*`, `.topbar-*`, `.brand-*`, `.nav-*`)

Layout shell at `globals.css:2885-3372`. Includes the orbit brand mark, sync pill, crumb breadcrumbs, live/UTC/refreshing chips. **Don't touch these for individual component PRs** — they're the application shell.

---

## 4. Naming conventions

### Class prefixes (kebab-case)

| Prefix | Scope | Examples |
|---|---|---|
| `cosmos-` | Cross-cutting cosmos theme patterns (panel, toolbar, table, chip, kpi-value, glow) | `cosmos-panel`, `cosmos-toolbar` |
| `trade-` | Trade desk surface | `trade-hero`, `trade-kpi`, `trade-kpis` |
| `ticket-` | Trade ticket (right rail) — bespoke | `ticket-eyebrow`, `ticket-pair`, `ticket-stat` |
| `event-` | Events feed | `event-card`, `event-status-pill` |
| `pred-` | Predictions ledger (mobile cards) | `pred-card`, `pred-card-grid` |
| `sa-` | Stats assistant (research) | `sa-input`, `sa-result-empty` |
| `stats-` | Stats research surfaces (pre-PR-4 stats workspace) | `stats-tile`, `stats-header-card` |
| `prop-` | Player prop ladder (trade desk) | `prop-group`, `prop-ladder` |
| `line-` | Game-line table rows (trade desk) | `line-row`, `line-row-spark` |
| `pick-history-` | Pick-history strip (trade ticket) | `pick-history-strip` |
| `archived-` | Archived/previous slate panel | `archived-slate` |
| `market-section-`, `market-filter-` | Trade desk market sections | … |
| `sidebar-`, `topbar-`, `brand-`, `nav-`, `crumb-`, `lo-` | App shell | … |
| `slate-status-pill` | Single-purpose status banner | … |
| `outcome-pill` | Won/lost/pending status chip | … |

### When to use which prefix

| Adding a new… | Use prefix |
|---|---|
| Cross-cutting pattern used in multiple surfaces | `cosmos-` |
| Pattern bespoke to ONE feature surface | The feature's prefix (`trade-`, `sa-`, etc) |
| Domain status chip with semantic meaning | Extend `outcome-pill` modifiers, or build a new `*-pill` if the semantics differ |
| Table row of a specific domain shape | `{domain}-row` (mirror `line-row`, `prop-stat-row`) |
| Composite that's clearly one-off | Bespoke prefix; don't shoehorn into an existing one |

### When NOT to create a new pattern

- The thing already exists. `stats-tile`, `cosmos-panel`, `outcome-pill` cover ~95% of "I need a card / chip / container" needs.
- The thing is a one-line Tailwind composition (e.g. `rounded-md border border-border bg-surface-hover px-2 py-1`). Just use the utilities inline.
- You're tempted to abstract three similar usages into a pattern. Wait for the fourth.

---

## 5. Drift report

Concrete instances of inconsistency to fix (not blocking, but track).

### 5.1 Arbitrary small-text literals (resolved)

**Resolved 2026-05-17** via [sika#200](https://github.com/ckwame-jpg/sika/pull/200). Tailwind's default text-size scale stops at `text-xs` (12px); everything smaller had to be written as `text-[10px]` / `text-[9px]` / `text-[10.5px]` arbitrary literals (35 occurrences across 14 files, with half-pixel values pixel-snapping inconsistently).

Two new tokens added to `@theme inline`:

```css
--text-2xs: 10px;
--text-3xs: 9px;
```

Tailwind v4 auto-generates `text-2xs` and `text-3xs` utilities. All 35 occurrences migrated (24× `text-[10px]` → `text-2xs`, 7× `text-[9px]` → `text-3xs`, 4× `text-[10.5px]` → `text-2xs` rounded down).

**Still as inline literals (out of scope, less common):** `text-[11px]` × 18, `text-[12.5px]` / `text-[13px]` / `text-[13.5px]` / `text-[11.5px]` × handful. Could be normalized in a follow-up if drift accumulates.

### 5.2 Inline opacity literals (resolved)

**Resolved 2026-05-17** via [sika#201](https://github.com/ckwame-jpg/sika/pull/201). The cosmos theme had `--color-cosmos-border-soft` / `--color-cosmos-border-softer` defined but didn't expose them as Tailwind utilities. Consumers fell back to `bg-white/[0.04]` / `border-white/[0.06]` inline literals — approximations of the cosmos tokens, but not actually them, so theme retones couldn't propagate.

Two new translucent overlay tokens added to `@theme inline`:

```css
--color-surface-soft:   hsl(0 0% 100% / 0.06);  /* bg/border-surface-soft */
--color-surface-softer: hsl(0 0% 100% / 0.03);  /* bg/border-surface-softer */
```

13 of 14 inline opacity literals migrated. Mode-independent (white at fixed alpha reads the same on light + dark backgrounds).

**One literal preserved:** `bg-white/[0.08]` × 1 in `model-readiness-panel.tsx` (progress-bar track) — one-off "stronger" tint that doesn't fit either token.

### 5.3 Components built without the design system (resolved)

**Resolved 2026-05-17** via [sika#196](https://github.com/ckwame-jpg/sika/pull/196). Both components were retro-redesigned through `/frontend-design` to adopt the cosmos eyebrow pattern (`ticket-stat-label` + small tonal signal chip), per-row left-rail accents lifted from `freshness-audit-panel.tsx`, and `font-mono tabular-nums tracking-tight` numerics. Behavior contracts (testid + role + data attributes) were preserved verbatim; visuals only.

Historical record:

- [`components/trade/freshness-badge.tsx`](components/trade/freshness-badge.tsx) — Smarter #22 PR A. Originally a custom severity-toned container with inline Tailwind utilities. Now uses the cosmos header rhythm + per-row severity rails (suppress red / penalize amber / ignore muted).
- [`components/trade/prediction-interval-band.tsx`](components/trade/prediction-interval-band.tsx) — Smarter #21 PR 4. Originally a custom SVG band with inline tokens. Now wraps the (still load-bearing) SVG with the cosmos eyebrow + a three-column p10/p50/p90 landmark grid; coverage status moved to an `ok` / `bad` signal chip in the eyebrow.

The audit-panel quality bar (`freshness-audit-panel.tsx`, built via `/frontend-design`) is what both now match — preserved as the canonical reference for the cosmos diagnostic-strip idiom.

### 5.4 Empty / loading / error state primitives (resolved)

**Resolved 2026-05-17** via [sika#202](https://github.com/ckwame-jpg/sika/pull/202). Two new canonical primitives in `apps/web/components/ui/`:

- **`<EmptyState>`** ([`empty-state.tsx`](components/ui/empty-state.tsx)) — rounded-xl border container with `tone` (default | error | warning | positive), title, description, optional icon + action slots. Carries `role="status"` + `aria-live="polite"` so screen readers announce on mount.
- **`<LoadingState>`** ([`loading-state.tsx`](components/ui/loading-state.tsx)) — full-section loader with `Loader2` spinner + required `label`. Carries `role="status"` + `aria-label` for AT announcement.

Migrated `events-feed.tsx` + `predictions-desk.tsx` error states to `<EmptyState tone="error">`. The previously-orphan loading primitive is available for future use.

**Preserved as bespoke sika personality:**
- `.trade-ticket-empty-orb` + `.trade-ticket-empty-orb-core` — orb on the trade ticket
- `.sa-result-empty` + `.sa-result-orb` — orb on the stats assistant
- `.cosmos-table-empty` — tabular shape
- `.sa-result-loading` + `.sa-result-bar` + `.sa-result-scan` — stats-assistant idiom

The `<RefreshCw className="animate-spin" />` inline-button spinners (4 occurrences) are NOT migrated — they're action-button context, not section-loader context. Adding `aria-label` to those parent buttons is a §6.2 follow-up.

### 5.5 Two parallel pill systems

`Badge` (the cva primitive in `ui/badge.tsx`) and `.outcome-pill` (CSS class) overlap visually but have different APIs and slightly different color tokens. The repo uses both. Today's rule of thumb: **outcome-pill for status with won/lost/pending semantics, Badge for everything else** — but this isn't documented in code anywhere and consumers occasionally cross the line.

**Recommendation:** add a JSDoc to each primitive declaring its intended use vs the other. Cheap.

### 5.6 `font-sans` and `font-mono` are aliases of the same font

`globals.css:34-35`:

```css
--font-sans: var(--font-geist-sans), system-ui, sans-serif;
--font-mono: var(--font-geist-sans), system-ui, sans-serif;
```

Both resolve to Geist. The `font-mono` utility is used 90+ times across the codebase for numeric data — semantically meaningful even when visually identical. **Don't conflate them.** Keep `font-mono tabular-nums` as the contract for "this is data" even though the font is Geist either way.

---

## 6. Accessibility cross-cuts

Patterns that recur across components, and where conventions agree / drift.

### 6.1 Empty states (resolved for the primitive; bespoke patterns still informational)

**Partially resolved 2026-05-17** via [sika#202](https://github.com/ckwame-jpg/sika/pull/202). The new `<EmptyState>` primitive carries `role="status"` + `aria-live="polite"` so consumers that use it get AT announcement for free. The two ad-hoc error states (`events-feed.tsx`, `predictions-desk.tsx`) now use the primitive.

**Bespoke patterns still without role/aria** (intentional — they're sika personality, not ad-hoc drift):

| Pattern | a11y status |
|---|---|
| `.trade-ticket.empty` (the orb pattern) | No role; relies on the surrounding ticket's context. Acceptable for trade ticket's design. |
| `.sa-result-empty` (the orb pattern) | No role; stats-assistant surface is intentionally personality-heavy. |
| `.cosmos-table-empty` | No role; the table's `<caption>` (if present) or context provides announcement. |

A future a11y pass could add `role="status"` to these too, but they're not blocking.

### 6.2 Loading states (resolved for the primitive; inline spinners still informational)

**Partially resolved 2026-05-17** via [sika#202](https://github.com/ckwame-jpg/sika/pull/202). The new `<LoadingState>` primitive carries `role="status"` + `aria-label`. For tabular loaders, `<Skeleton>` remains the right primitive (layout-preserving).

**Still without explicit role/aria** (acceptable, scoped follow-up):

| Pattern | a11y status |
|---|---|
| `<Skeleton>` / `<SkeletonRow>` | No role. Decorative placeholder; layout-preserving. Sighted users see motion; screen readers hear nothing — but the loaded content's announcement is what matters for AT. |
| `.sa-result-loading` (stats assistant) | No role; bespoke sika idiom. Could add `role="status" aria-label="Scanning {sport} logs"` in a follow-up. |
| `<RefreshCw className="animate-spin" />` inline button spinners (4 callers) | No role. These are action-button context — adding `aria-label` to the parent button (e.g. `aria-label="Refreshing"`) would close the gap. Scoped follow-up. |

### 6.3 Status pills

`.outcome-pill` and `Badge` are visual. The pill text IS the status, so when wrapped in a row the parent should describe the row + the status (e.g. "Run 123, status completed"). Most existing usages do this implicitly via the row's accessible name — but pills used in isolation (e.g. in a header) lack context.

### 6.4 Decorative orbs / cores

Patterns: `.trade-ticket-empty-orb`, `.sa-result-orb`, `.trade-kpi-orb`, `.crumb-orb`, `.sync-orb`, the brand mark's `.lo-ring`/`.lo-core`/`.lo-sat`. All are decorative — they should use `aria-hidden` on every wrapper. Spot-check: most do via `aria-hidden`, but not consistently.

### 6.5 Focus styles (resolved)

**Resolved 2026-05-17** via [sika#198](https://github.com/ckwame-jpg/sika/pull/198) (Settings page), [sika#199](https://github.com/ckwame-jpg/sika/pull/199) (Mappings Desk), and [sika#203](https://github.com/ckwame-jpg/sika/pull/203) (bare-button audit across the rest of the app).

`focus-visible:ring-focus` (defined `globals.css:341-343`) is now applied to:

- All `.cosmos-chip` consumers (Settings page chips, Mappings Desk confidence presets)
- All bespoke bare-button patterns: `.line-row`, `.event-card-toggle`, `.archived-slate-head`, `.sa-prompt`, `.sa-run`, `.sa-stat`, `.pick-history-strip-n-pill`, `.pick-history-strip-chip`, `.prop-head`, `.prop-threshold-chip`
- All inline ad-hoc buttons in `paper-positions-table.tsx`, `demo-orders-table.tsx`, `runs-desk.tsx`, `model-readiness-panel.tsx`, `kalshi-account-panel.tsx`, trade-desk mobile close button

WCAG 2.1 SC 2.4.7 (Focus Visible) — Level AA — satisfied across the non-shell interactive surface.

**Out of scope:** sidebar / topbar / nav shell. Per §3 ("Don't touch these for individual component PRs — they're the application shell"). Future PR if needed.

---

## 7. Decision tree for new components

```
I need a new …
│
├─ Container (wraps a feature surface)
│  ├─ Ops or research surface → .cosmos-panel
│  ├─ Trade-desk surface → bespoke trade-* class (consult trade-desk.tsx first)
│  ├─ Modal / sheet → ui/dialog or ui/sheet primitive
│  └─ Otherwise → start with .cosmos-panel; only diverge if you can defend it
│
├─ Tile / metric block (label + value)
│  ├─ Standalone metric → .stats-tile
│  ├─ Hero KPI strip → .trade-kpi (inside .trade-kpis/.pred-kpis/.parlay-kpis container)
│  ├─ Tone-aware metric (positive/negative/warn) → .trade-kpi-value with .pos/.neg/.warn modifier
│  └─ Otherwise → .stats-tile + Tailwind utilities for variant tones
│
├─ Chip / pill / badge
│  ├─ Settled outcome (won/lost/pending) → .outcome-pill + variant class
│  ├─ Sport label → <SportBadge sport={...} />
│  ├─ General-purpose label → <Badge variant="…">
│  └─ Toggle chip → .cosmos-chip + data-active="true" + focus-visible:ring-focus
│
├─ Table
│  ├─ Generic tabular data → Table primitives (ui/table.tsx)
│  ├─ Themed ops table inside .cosmos-panel → .cosmos-table-wrap
│  └─ Bespoke row layout for a specific domain → {domain}-row pattern (cf. .line-row, .prop-stat-row)
│
├─ Loading placeholder
│  ├─ Tabular → <SkeletonRow cols={N} />
│  ├─ Block → <Skeleton className="h-X w-X" />
│  ├─ Stats-assistant context → .sa-result-loading + .sa-result-bar + .sa-result-scan
│  └─ Section loader → <LoadingState label="…" /> (carries role="status" + aria-label)
│
├─ Empty state
│  ├─ Trade-desk empty → bespoke (orb pattern)
│  ├─ Stats-assistant empty → .sa-result-empty + .sa-result-orb
│  ├─ Table empty → .cosmos-table-empty
│  └─ Otherwise → <EmptyState tone="default|error|warning|positive" title=… description=… /> (carries role="status" + aria-live="polite")
│
└─ Anything else
   └─ Reach for /frontend-design skill. The component has personality and deserves design attention.
```

---

## 8. What's NOT in this doc

- **Storybook / external tooling.** This is one-operator software; the doc + the codebase IS the design system. No tooling overhead.
- **Visual design proposals.** Any recommendation to change how an existing pattern LOOKS belongs in a /frontend-design PR, not here.
- **Component-by-component a11y audit.** The cross-cuts section above flags categories; full per-component audit is a separate `/accessibility-review` pass.
- **Light-mode coverage.** Light-mode tokens exist (`:root`) but the product runs dark. Treat light mode as best-effort, not first-class.

---

## 9. Open recommendations (not commitments)

Captured here so they don't get lost. Each is a candidate for a follow-up PR; none block current work.

### Still open

1. **JSDoc `Badge` and `.outcome-pill` with usage guidance** → resolves §5.5 ambiguity.
2. **Delete orphaned tokens** (`--color-cosmos-violet-avatar-*`, `--color-cosmos-stats-white`, etc.) → housekeeping per §1.6.

### Resolved (historical record)

3. ~~Add `text-2xs` / `text-3xs` (10px / 9px) Tailwind utilities~~ → §5.1 resolved via [sika#200](https://github.com/ckwame-jpg/sika/pull/200) on 2026-05-17.
4. ~~Map cosmos surface tokens to Tailwind utilities~~ (`bg-surface-soft`, `border-surface-softer`) → §5.2 resolved via [sika#201](https://github.com/ckwame-jpg/sika/pull/201) on 2026-05-17.
5. ~~Build `<EmptyState>` + `<LoadingState>` primitives~~ → §5.4 + §6.1 + §6.2 partially resolved via [sika#202](https://github.com/ckwame-jpg/sika/pull/202) on 2026-05-17 (primitives shipped + ad-hoc consumers migrated; bespoke patterns intentionally preserved).
6. ~~Focus-visible audit of non-Button interactive elements~~ → §6.5 resolved via [sika#198](https://github.com/ckwame-jpg/sika/pull/198), [sika#199](https://github.com/ckwame-jpg/sika/pull/199), and [sika#203](https://github.com/ckwame-jpg/sika/pull/203) on 2026-05-17.
7. ~~Adopt `.cosmos-chip` on the settings page~~ → resolved via [sika#198](https://github.com/ckwame-jpg/sika/pull/198) on 2026-05-17. `.cosmos-chip` now has ~12 consumers across Settings + Mappings Desk; the orphan label in §3 below is stale.

---

## 10. Glossary

- **Cosmos theme**: the dark "deep-space" visual language sika uses — true-black backgrounds, violet/cyan accents, orb decorations, blurred translucent panels. Permeates every surface.
- **Operator**: the single user of sika. Internal tool, one-operator-at-a-time semantics.
- **Pattern**: a `.classname` block in `globals.css` that's reused across multiple components.
- **Primitive**: a component in `apps/web/components/ui/`. Lowest layer.
- **Composite**: a feature-specific component in `apps/web/components/{trade,predictions,events,…}/`. Built from primitives + patterns.
- **Token**: a CSS variable in `globals.css`. Mapped to Tailwind via `@theme inline`.
- **`cva`**: class-variance-authority. Used in primitives for variant + size matrices.

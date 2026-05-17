# Smarter #22 — Feature freshness SLA tuning playbook

**Status:** PR A shipped ([sika#186](https://github.com/ckwame-jpg/sika/pull/186)). PR B (policy expansion) gated on operator observation per this playbook.

Architecture #5 ([sika#169](https://github.com/ckwame-jpg/sika/pull/169) + [sika#173](https://github.com/ckwame-jpg/sika/pull/173) + [sika#175](https://github.com/ckwame-jpg/sika/pull/175)) shipped the freshness-layer plumbing. Smarter #22 PR A ([sika#186](https://github.com/ckwame-jpg/sika/pull/186)) shipped the operator-facing badge so you can SEE which picks are freshness-affected. This playbook describes how to tune the policy registry on top of those foundations — specifically, the discipline for promoting a group from the default `IGNORE` policy to `PENALIZE` or `SUPPRESS`.

**Read this before opening PR B.** The whole reason PR A shipped before PR B was so policy expansion could be informed by observed signals, not theoretical guessing.

## The decision tree

Before adding ANY policy entry to `FEATURE_GROUP_POLICIES`, answer these three questions in order:

1. **Does stale data flip the recommendation's correctness?** (e.g., the player was scratched but you're still recommending their prop because the lineup cache is 3h old.) → **`SUPPRESS`** via a `suppress_when` callback. The callback inspects the live feature values + metadata + family_key to make a value-driven decision; not a pure TTL gate.

2. **Does stale data degrade signal quality but not the underlying recommendation?** (e.g., a 7h-old weather forecast for an outdoor MLB game — the wind/temperature shift since the forecast doesn't void the pick, just adds uncertainty.) → **`PENALIZE`** with a confidence delta scaled to the typical magnitude of the staleness impact.

3. **Does stale data not really matter?** (Season-stable aggregates like park factors; derived features like NBA interaction terms; values that don't drift on the relevant timescale.) → **Leave as `IGNORE`** (don't add a registry entry; the `DEFAULT_POLICY` handles it).

If your answer is "I'm not sure," the answer is `IGNORE` until you've observed the badge for at least 2-3 sessions and have a concrete failure case.

## Observation discipline — what to look for in the badge

After PR A, every recommendation in the trade ticket carries a freshness badge when at least one group is stale. Each session, scan the badge signals and ask:

| Observation | What to write down |
|---|---|
| Group X showed up as stale on a pick that turned out to be a bad recommendation | "Stale X correlates with bad picks" — candidate for promotion |
| Group X showed up as stale on a pick that turned out to be a good recommendation | "Stale X is benign" — leave at IGNORE |
| Group X NEVER showed up stale even though the underlying data is obviously old | Either the freshness check is broken OR the group's TTL is too generous |
| Group X always shows up stale | Either the upstream refresh is broken OR the group's TTL is too aggressive |
| A SUPPRESS rule fired but you would have wanted the pick anyway | The SUPPRESS gate is over-aggressive; consider downgrading to PENALIZE |

Recommended cadence: **2-3 active scoring sessions** before opening PR B. That's enough to see each major group go stale at least once and form an opinion.

## Concrete groups and their tuning candidates

The scoring kernel currently emits these feature groups. Status indicates the existing policy in `apps/api/app/services/scoring/feature_groups.py:FEATURE_GROUP_POLICIES`.

### Already tuned (shipped in Architecture #5)

| Group | Policy | TTL | Why |
|---|---|---|---|
| `mlb_weather` | PENALIZE -5% | 6h | Forecasts drift but don't binary-flip an outdoor MLB game |
| `mlb_bullpen` | PENALIZE -5% | 4h | Bullpen rest is computed from schedule density; stale = missed a game in the window |
| `nba_workload` | PENALIZE -3% | 24h | Workload windows change daily; modest penalty for skipped daily refresh |
| `mlb_lineup` | SUPPRESS (callback) | n/a | Confirmed-and-scratched → drop the pick (Smarter #16) |
| `nba_injury` | SUPPRESS (callback) | n/a | OUT / DOUBTFUL with fresh report → drop the pick (Smarter #17) |

### Strong candidates for promotion (await observation)

| Group | Suggested policy | Suggested TTL | Reasoning |
|---|---|---|---|
| `mlb_starter` | PENALIZE -4% | 8h | Probable starter announcements drift in/out as injuries hit; ERA / xFIP-driven props mis-estimate when stale. **Bespoke `has_probable_starter_context` gate already covers the missing-context case in scoring — observation will tell whether stale-but-present-data needs a separate penalty.** |
| `nba_referee` (assignments) | PENALIZE -2% | 6h | Referee crews assigned 60-90 min pre-tip; stale data means you're using yesterday's crew assumption. Small delta because referee effect is itself small. |
| `mlb_batter` (recent splits) | PENALIZE -3% | 7d | Recent splits drift weekly; not a daily concern. **Note:** the group key probably needs splitting into `mlb_batter_recent` vs `mlb_batter_season` first — the season aggregates should stay IGNORE. |

### Likely stays IGNORE (season-stable or derived)

| Group | Why IGNORE |
|---|---|
| `mlb_park` | Park factors stable across season; rebuilds when the operator re-runs the offline park-factor pipeline (~1×/year) |
| `mlb_platoon` | Season splits; same logic as park |
| `nba_advanced` | Player advanced stats (season + recent); recent already covered via `nba_workload` |
| `nba_opponent_team` | Opponent team advanced; refreshed daily and gated by the daily NBA refresh job |
| `nba_clutch` / `nba_drives` / `nba_hustle` | Long-tail season aggregates; staleness on these is invisible to scoring outcomes |
| `nba_interaction` | Pure derived (usage × pace × 1/DRtg); no upstream cache, no `fresh_at` |
| `wnba_workload` | Same shape as `nba_workload`; activate the same PENALIZE -3% / 24h pattern when WNBA props go live |

## Choosing the penalty delta

Anchor against the existing PENALIZE entries (-3% to -5%). The rough heuristic:

- **-3%**: stale signal is weakly informative (workload, schedule density). Penalty is a hedge against noise.
- **-5%**: stale signal is genuinely degraded (weather forecast, bullpen state). Penalty is a meaningful downgrade.
- **-7% or worse**: probably the wrong call — if staleness hurts more than ~7%, the right answer is a `SUPPRESS` gate, not a heavier `PENALIZE`. PENALIZE is for "signal degraded but still informative;" if it's not informative anymore, drop the pick.

**Never use a positive delta.** PENALIZE is always a confidence hit; there's no scenario where stale data improves the recommendation.

## Choosing the TTL

Set the TTL to **just longer than the upstream cache's refresh cadence**. Examples:

- Cache refreshes every 4h → TTL = 4-5h. Stale only triggers when the refresh is genuinely late.
- Cache refreshes daily (NBA workload, MLB batter) → TTL = 24-28h.
- Cache refreshes weekly (MLB recent splits) → TTL = 7-8 days.

**The TTL is not "when the data goes bad;" it's "when the refresh job should have run by."** Picking a TTL shorter than the refresh cadence guarantees false positives.

For SUPPRESS-policy groups the TTL is unused — the gate is value-driven via the `suppress_when` callback, not time-driven. Set the TTL to the upstream cache's TTL for diagnostic display only.

## Writing a SUPPRESS callback

Pattern from the existing `mlb_lineup_suppress_when` / `nba_injury_suppress_when` in [feature_groups.py](apps/api/app/services/scoring/feature_groups.py):

```python
def my_group_suppress_when(ctx: SuppressionContext) -> str | None:
    # 1. Family gate FIRST. A stray feature on the wrong family must not trip this.
    if ctx.family_key != "nba_props":
        return None
    # 2. Metadata gate — only fire when the market actually requires this signal.
    if not ctx.metadata.get("copilot_requires_lineup"):
        return None
    # 3. Data-completeness gate — never suppress on missing data, only on
    #    affirmatively-bad data (caught by an emitter `*_data_complete` marker).
    if not (float(ctx.features.get("data_complete") or 0.0) >= 1.0):
        return None
    # 4. The actual suppression condition.
    if affirmatively_bad(ctx.features):
        return "my_reason_constant"  # added to suppression_reasons
    return None
```

Rules:
- **Family-gate first.** A `nba_*` group must check `family_key.startswith("nba_")`; otherwise an MLB row with the same key (from drift) trips the gate.
- **Never SUPPRESS on missing data.** That conflates "we don't know" with "we know it's bad." Use a `*_data_complete` marker to gate.
- **Return a stable string constant** for the suppression reason. The kernel translates it into `suppression_reasons` via an existing keyspace; new reasons need a matching entry there.
- **Keep callbacks pure** — no DB access, no I/O. The callback receives a `SuppressionContext` (features, metadata, family_key) and must return based on those alone.

## What NOT to do

- **Don't add a policy entry without observation.** This playbook exists because we don't trust hand-waved tuning. If you can't point at 2-3 sessions of badge data, you don't have enough signal.
- **Don't tune the existing 5 entries.** Architecture #5 set the current values by deliberate design. Re-tuning them without observation is exactly the trap PR A's UI-first approach was built to avoid.
- **Don't add a SUPPRESS for a group whose stale state is recoverable.** E.g., `mlb_weather` stale isn't a reason to drop the pick — operator-level judgment can still take a weather-uncertain pick. PENALIZE is the right shape.
- **Don't add a tight TTL "to be safe."** A 1h TTL on a 6h-refresh cache fires constantly with no signal; operators learn to tune the badge out, which defeats the whole purpose.
- **Don't add fresh_at to derived groups.** Pure-derived features (interaction terms, schedule-density signals) don't have an externally-refreshed cache; leaving `fresh_at=None` opts them out of the freshness check correctly.

## How to ship PR B

When you've completed the observation pass and have concrete tunings:

1. **Branch fresh from `origin/main`** per `SIKA_SESSION_RULES.md` rule 7.
2. **One PR per coherent batch of group additions.** Grouping by sport (all MLB additions in one PR, all NBA in another) is the cleanest split — the test surface naturally aligns.
3. **TDD ordering** (`SIKA_SESSION_RULES.md` workflow):
   - Per group, write a failing test that asserts the policy registry entry exists with the expected severity/TTL/delta.
   - For PENALIZE groups: write a test that constructs a `FeatureGroupSnapshot` with `fresh_at = now - (TTL + 1min)` and asserts `check_freshness` returns `is_stale=True` with the expected `confidence_delta`.
   - For SUPPRESS groups: write a test for the `suppress_when` callback covering happy path, family gate, metadata gate, missing data, and the affirmative suppression case.
4. **Self-review against the 9-point checklist** before push.
5. **Reviewer** — codex if responsive, python-reviewer subagent if codex hangs (per `SIKA_SESSION_RULES.md` rule 5).
6. **PR description** specifies: which groups got policies, the chosen TTL/delta + the observation evidence that drove the choice, the baseline test counts before/after, the rollback plan (revert is always safe — additive registry entries).
7. **Admin-merge** via `gh pr merge <N> --squash --admin --body ""`.

## Reference

- [`apps/api/app/services/scoring/feature_groups.py`](apps/api/app/services/scoring/feature_groups.py) — the registry, the policy types, the existing SUPPRESS callbacks.
- [`apps/api/app/services/scoring/__init__.py`](apps/api/app/services/scoring/__init__.py) — where `check_freshness` is called and how the penalty is applied to confidence.
- [`apps/web/components/trade/freshness-badge.tsx`](apps/web/components/trade/freshness-badge.tsx) — the operator-facing badge.
- [`apps/api/app/schemas.py`](apps/api/app/schemas.py) — `FreshnessStaleGroupRead` schema; surfaced on `TradeDeskThresholdRead` / `TradeDeskGameLineRead`.
- [`SIKA_SESSION_RULES.md`](SIKA_SESSION_RULES.md) — branching, codex fallback, worktree-vs-repo-root contracts package, research-first rule.
- [`SIKA_PUNCH_LIST.md`](SIKA_PUNCH_LIST.md) — Smarter #22 entry and current truly-open list.

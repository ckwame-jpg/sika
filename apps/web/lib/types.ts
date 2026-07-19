/* ─── Mapped directly from apps/api/app/schemas.py ─── */

import type { Schema } from "@kalshi-sports-copilot/contracts";

export type SportKey = "NBA" | "NFL" | "MLB" | "WNBA" | "TENNIS";

export const SPORT_LABELS: Record<SportKey, string> = {
  NBA: "NBA",
  NFL: "NFL",
  MLB: "MLB",
  WNBA: "WNBA",
  TENNIS: "Tennis",
};

/**
 * Bug #40 / Architecture #6 — web contracts migration.
 *
 * The hand-written interfaces in this file mirror the Pydantic
 * schemas in `apps/api/app/schemas.py`. The mirror has drifted —
 * Smarter #23 added `upstream_sources` to `HealthResponse` server-side
 * but the hand-written version never picked it up, so consumers had
 * no type-level signal that the field exists.
 *
 * The migration replaces each hand-written interface with a shim
 * re-export of the OpenAPI-generated `Schema<"…">` type. The
 * `Wire<T>` utility strips the optional modifier from every field
 * — recursively — because Pydantic always serializes
 * `field | None = None` as `{"field": null}` (the key is present on
 * the wire), even though openapi-typescript marks the field `?:`
 * per the OpenAPI nullable spec. The runtime contract is "always
 * present, maybe null"; this encodes that at the type level so
 * consumers don't have to handle a spurious `undefined`.
 *
 * The recursion is required because a top-level `Required<T>`
 * doesn't propagate into nested objects (e.g.
 * `HealthResponse.active_refresh_job` is `RefreshJobRead | null`,
 * and the nested `RefreshJobRead` keeps its optional fields unless
 * we recurse). Without the deep variant, a consumer passing
 * `health.active_settlement_job` into a function typed against
 * the shim's `RefreshJobRead` fails to type-check.
 *
 * Migration order: read-only endpoint families first. Each family's
 * types land in one PR so all consumers update together (no
 * half-migrated state).
 */
type Wire<T> = T extends (infer U)[]
  ? Wire<U>[]
  : T extends object
    ? { [K in keyof T]-?: Wire<T[K]> }
    : T;

// ── /health endpoint family (first migration) ──
export type HealthResponse = Wire<Schema<"HealthResponse">>;
export type RefreshJobRead = Wire<Schema<"RefreshJobRead">>;
export type UpstreamSourceHealthRead = Wire<Schema<"UpstreamSourceHealthRead">>;

// ── /predictions + /parlays/predictions endpoint family ──
export type PredictionRead = Wire<Schema<"PredictionRead">>;
export type PredictionSummaryRead = Wire<Schema<"PredictionSummaryRead">>;
export type ParlayPredictionLegRead = Wire<Schema<"ParlayPredictionLegRead">>;
export type ParlayPredictionRead = Wire<Schema<"ParlayPredictionRead">>;
export type ParlayPredictionSummaryRead = Wire<Schema<"ParlayPredictionSummaryRead">>;
export type PredictionSettlementResponse = Wire<Schema<"PredictionSettlementResponse">>;

// ── /ops/models/readiness endpoint family ──
//
// ``ReadinessStatus`` / ``RuntimeHealthStatus`` / ``StudyTrack`` are
// inline literal unions in the generated schema (not standalone
// component types). Pull them out via indexed-access so the source
// of truth stays the OpenAPI generation — adding a new readiness
// state on the API side flows through automatically.
export type ReadinessStatus = Wire<Schema<"ModelFamilyReadinessRead">>["readiness_status"];
export type RuntimeHealthStatus = Wire<Schema<"ModelFamilyRuntimeHealthRead">>["runtime_health"];
export type ReadinessBucketRead = Wire<Schema<"ReadinessBucketRead">>;
export type CalibrationBucketRead = Wire<Schema<"CalibrationBucketRead">>;
export type ModelFamilyRuntimeHealthRead = Wire<Schema<"ModelFamilyRuntimeHealthRead">>;
export type ModelFamilyReadinessRead = Wire<Schema<"ModelFamilyReadinessRead">>;
export type SettlementAgingRead = Wire<Schema<"SettlementAgingRead">>;
// Smarter #21 phase 2b — per-(family, stat_key) interval-model status
// surfaced in the readiness panel. Coverage status banding (ok / warn /
// bad / unknown) mirrors the ``python -m ml.cli inspect-intervals``
// classification so the browser and the CLI agree byte-for-byte.
export type IntervalModelStatusRead = Wire<Schema<"IntervalModelStatusRead">>;
// Smarter #22 PR B prep — per-stale-feature-group calibration audit
// row surfaced on the readiness panel so operators can see which
// IGNORE-default groups have a real staleness penalty in the data
// (positive ``calibration_delta``) before promoting them in the
// scoring policy registry. See SMARTER_22_TUNING_PLAYBOOK.md.
export type FreshnessAuditRowRead = Wire<Schema<"FreshnessAuditRowRead">>;
export type ModelReadinessSummaryRead = Wire<Schema<"ModelReadinessSummaryRead">>;
// Update DTOs use ``Partial<Schema<…>>`` instead of ``Wire<Schema<…>>``
// because the partial-PATCH idiom (caller sends only the fields they
// want to change) requires every field to be optional. The generated
// schema already marks fields with ``T | None = None`` Pydantic
// defaults as ``?:``; ``Partial<>`` extends that to the few fields
// that have non-None defaults (e.g. ``enqueue_shadow_backfill: bool
// = True``) so call sites like ``{ pick_history_default_n: 5 }``
// continue to type-check.
export type ModelReadinessSettingsUpdate = Partial<Schema<"ModelReadinessSettingsUpdate">>;
// Bug #235 — lightweight ack returned by PATCH
// ``/ops/models/readiness/settings``. The PATCH no longer echoes the
// full summary (which used to force ~22s of summary-build work inside
// the request handler); callers re-fetch via
// ``GET /ops/models/readiness`` (SWR ``mutate``) instead.
export type ModelReadinessSettingsApplied = Wire<Schema<"ModelReadinessSettingsApplied">>;

// ── /events + /markets endpoint family ──
//
// MarketDetailRead embeds RecommendationRead, EventRead,
// SignalSnapshotRead, and MarketSnapshotRead through the generated
// schema's component refs. Migrating MarketDetailRead alone would
// keep those inner types as the *generated* version while the
// hand-written exports stayed in place — TypeScript would treat
// ``RecommendationRead[]`` consumers as receiving an incompatible
// type. Migrate the whole transitive closure together.
export type EventParticipantRead = Wire<Schema<"EventParticipantRead">>;
export type EventRead = Wire<Schema<"EventRead">>;
export type RecommendationRead = Wire<Schema<"RecommendationRead">>;
export type MarketDetailRead = Wire<Schema<"MarketDetailRead">>;
export type MarketHistoryRead = Wire<Schema<"MarketHistoryRead">>;

// ── /trade-desk endpoint family ──
//
// The hand-written names dropped the ``Read`` suffix for the inner
// trade-desk types (TradeDeskGameLine vs. the generated
// TradeDeskGameLineRead, etc.). The shim aliases preserve the
// hand-written names so consumers don't need to rename.
export type TradeDeskGameLine = Wire<Schema<"TradeDeskGameLineRead">>;
export type TradeDeskThreshold = Wire<Schema<"TradeDeskThresholdRead">>;
// Smarter #21 phase 2d (PR 4) — operator-facing serialization of the
// interval consumer's diagnostic dict. Surfaced on
// ``TradeDeskThresholdRead.prediction_interval`` so the trade-ticket
// UI band can render the [p10, p90] range with a threshold tick
// without parsing a generic dict blob.
export type PredictionInterval = Wire<Schema<"PredictionIntervalRead">>;
// Smarter #22 PR A — Architecture #5 freshness diagnostics surfaced
// for the trade-ticket FreshnessBadge. One row per stale feature
// group; severity mirrors ``FeatureGroupSeverity`` on the API side.
export type FreshnessStaleGroup = Wire<Schema<"FreshnessStaleGroupRead">>;
export type TradeDeskPlayerProp = Wire<Schema<"TradeDeskPlayerPropRead">>;
export type TradeDeskEvent = Wire<Schema<"TradeDeskEventRead">>;
export type TradeDeskArchivedSlate = Wire<Schema<"TradeDeskArchivedSlateRead">>;
export type TradeDeskResponse = Wire<Schema<"TradeDeskResponse">>;

// ── /runs + /jobs endpoint family ──
export type RunSummaryCounts = Wire<Schema<"RunSummaryCounts">>;
export type RunRead = Wire<Schema<"RunRead">>;
export type RunDetailRead = Wire<Schema<"RunDetailRead">>;
export type JobRefreshResponse = Wire<Schema<"JobRefreshResponse">>;

// ── /positions + /paper-positions + /demo-orders endpoint family ──
//
// First migration to use the **bare Schema<>** convention for Create
// DTOs (alongside Wire<> for Read DTOs and Partial<> for Update DTOs).
// Bare Schema<> preserves the required-field constraints openapi-
// typescript emits — caller must supply every field the Pydantic
// schema doesn't default. The trade-off vs. Partial<>: stricter at
// compile time, but the few fields with Pydantic defaults
// (DemoOrderCreate.time_in_force, .action, .approved) now require
// explicit values at the call site.
export type PaperPositionRead = Wire<Schema<"PaperPositionRead">>;
export type DemoOrderRead = Wire<Schema<"DemoOrderRead">>;
export type KalshiAccountMarketPositionRead = Wire<Schema<"KalshiAccountMarketPositionRead">>;
export type KalshiAccountFillRead = Wire<Schema<"KalshiAccountFillRead">>;
export type DrawdownBrakeRead = Wire<Schema<"DrawdownBrakeRead">>;
export type PositionsRead = Wire<Schema<"PositionsRead">>;
export type PaperPositionCreate = Schema<"PaperPositionCreate">;
export type PaperPositionExit = Schema<"PaperPositionExit">;
export type DemoOrderCreate = Schema<"DemoOrderCreate">;
export type PaperParlayCreate = Schema<"PaperParlayCreate">;
export type PaperParlayLegCreate = Schema<"PaperParlayLegCreate">;
export type PaperParlayRead = Wire<Schema<"PaperParlayRead">>;
export type PaperParlayLegRead = Wire<Schema<"PaperParlayLegRead">>;
export type UserRead = Wire<Schema<"UserRead">>;
export type CurrentUserRead = Wire<Schema<"CurrentUserRead">>;
export type SwitchUserPayload = Schema<"SwitchUserPayload">;
export type CreateUserPayload = Schema<"CreateUserPayload">;
export type UserKalshiCredentialsCreate = Schema<"UserKalshiCredentialsCreate">;
export type UserKalshiCredentialsRead = Wire<Schema<"UserKalshiCredentialsRead">>;

// ── Real Kalshi orders (singles; combo Create/Preview types land with
// the phase-D routes — OpenAPI only emits route-referenced schemas) ──
export type KalshiOrderCreate = Schema<"KalshiOrderCreate">;
export type KalshiOrderRead = Wire<Schema<"KalshiOrderRead">>;
export type TradingSettingsRead = Wire<Schema<"TradingSettingsRead">>;
export type TradingSettingsUpdate = Schema<"TradingSettingsUpdate">;

// ── /stats-query + /team-history endpoint family ──
export type StatsSummaryRead = Wire<Schema<"StatsSummaryRead">>;
export type StatsGameLogRead = Wire<Schema<"StatsGameLogRead">>;
export type StatsQueryRead = Wire<Schema<"StatsQueryRead">>;
export type TeamGameResultRead = Wire<Schema<"TeamGameResultRead">>;
export type TeamHistoryRead = Wire<Schema<"TeamHistoryRead">>;

// ── /ops/market-mapping/* endpoint family (Smarter #25) ──
export type MarketMappingCandidateRead = Wire<Schema<"MarketMappingCandidateRead">>;
export type MarketMappingStateRead = Wire<Schema<"MarketMappingStateRead">>;
export type MarketMappingListItemRead = Wire<Schema<"MarketMappingListItemRead">>;
export type MarketMappingOverrideCreate = Schema<"MarketMappingOverrideCreate">;

// ── /product/* endpoint family ──
//
// ``ProductFreshnessResponse`` + ``ProductScopeFreshnessRead`` previously
// lived in lib/api.ts as ad-hoc shim re-exports (the slice-4 migration
// that predated this consolidated effort). Moved here so every generated-
// schema type has one canonical import surface.
export type ProductFreshnessResponse = Schema<"ProductFreshnessResponse">;
export type ProductScopeFreshnessRead = Schema<"ProductScopeFreshnessRead">;
export type ProductSportsResponse = Wire<Schema<"ProductSportsResponse">>;
export type SportRead = Wire<Schema<"SportRead">>;
export type SportAvailabilityRead = Wire<Schema<"SportAvailabilityRead">>;

// Bug #40 (Architecture #6) — web contracts migration COMPLETE.
// Every API DTO this file used to define by hand now flows from
// ``packages/contracts/generated/api.d.ts`` via the shim re-exports
// above. The only hand-written remnant is ``SportKey`` + ``SPORT_LABELS``
// at the top (display-only constants the API doesn't emit).

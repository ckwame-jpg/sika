// Re-exports the generated OpenAPI contract so downstream consumers have
// one import site. Regenerate with `npm run contracts:generate` from the
// repo root whenever the FastAPI app's schemas change.
//
// The generated file lives at ../generated/api.d.ts and is committed so CI
// can detect drift (see scripts/check_drift.mjs).

export type { paths, components, operations } from "../generated/api";

import type { components } from "../generated/api";

/** Helper: extract a response schema by name (e.g. Schema<"TradeDeskResponse">). */
export type Schema<K extends keyof components["schemas"]> =
  components["schemas"][K];

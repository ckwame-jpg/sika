# @kalshi-sports-copilot/contracts

Single source of truth for the TypeScript types of the FastAPI backend.

## How it works

1. `scripts/dump_openapi.py` imports the FastAPI app and writes
   `openapi.json` (stable-sorted, indented).
2. `openapi-typescript` converts `openapi.json` into
   `generated/api.d.ts`.
3. `src/index.ts` re-exports the generated `paths` / `components` /
   `operations` namespaces, plus a `Schema<K>` helper for pulling a
   named response schema.

Both `openapi.json` and `generated/api.d.ts` are **committed**. That way
the Vercel build (which does not have Python available) can consume the
generated types without having to regenerate them. The commit is the
contract.

## When to regenerate

Regenerate whenever you change anything the FastAPI app exposes —
schemas, response models, route shapes. From the repo root:

```bash
npm run contracts:generate
git add packages/contracts/openapi.json packages/contracts/generated/api.d.ts
```

## Drift detection

`npm run contracts:check` regenerates into a temp directory and diffs
against the committed artefacts. It exits non-zero if they differ,
printing instructions. Run it locally before pushing, and wire it into
CI when one exists.

## Consuming the types

In `apps/web`:

```ts
import type { Schema } from "@kalshi-sports-copilot/contracts";

type TradeDesk = Schema<"TradeDeskResponse">;
```

See `apps/web/lib/api.ts` for the first surface-level consumers
(`fetchProductSports`, `fetchProductFreshness`). The hand-written
`apps/web/lib/types.ts` will be migrated surface-by-surface and removed
in Slice 7a of the v2 plan.

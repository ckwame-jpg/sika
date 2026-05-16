# Repo git hooks

Hooks here live in-repo (so they can be reviewed in PRs and survive clones) but git only runs them after you point `core.hooksPath` at this directory once:

```
npm run install:hooks
```

(or directly: `git config core.hooksPath .githooks`)

Run it once per checkout. Worktrees inherit the setting from the main repo, so a single install covers all worktrees you spin up under `.claude/worktrees/`.

## What's here

### `pre-commit`

Bug #40 follow-up. After the Architecture #6 web contracts migration ([sika#148](https://github.com/ckwame-jpg/sika/pull/148)–[sika#159](https://github.com/ckwame-jpg/sika/pull/159)) shipped, `apps/web/lib/types.ts` is a thin shim over the generated `packages/contracts/generated/api.d.ts`. If the FastAPI schema changes and the generated file isn't regenerated and committed in the same change set, the web types silently disagree with the actual wire contract.

The hook fires when a commit stages either:

- `apps/api/app/schemas.py` (Pydantic models — direct schema definitions)
- `apps/api/app/api/routes.py` (response_model decorators — what gets emitted)

…and runs `npm run contracts:check`. That script regenerates the OpenAPI spec + TS types into a temp directory and diffs against the committed versions. Drift fails the commit with a one-line fix recipe.

99% of schema-touching changes hit one of those two files. For the rare edge case (e.g. a new router added to `app.main:app`), run `npm run contracts:check` manually before committing.

### Bypass

For genuine emergencies: `git commit --no-verify`. Follow up with the regen in a separate commit.

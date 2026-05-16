# Smarter #13 phase 2b-2 — operator-supplied table layout

## Status

- **URL verified:** `https://www.basketball-reference.com/referees/{end_year}_register.html` (e.g. `2026_register.html` for 2025-26 season). Pattern confirmed via WebSearch — every season 2003 → 2026 uses this shape.
- **Operator-supplied URL on 2026-05-16** from the live session: `https://www.basketball-reference.com/referees/2026_register.html` ✅
- **Still operator-blocked on:** the table layout (column header names, table id, HTML-comment wrapping).

## What's blocking the wiring

Basketball-reference returns HTTP 403 to anonymous WebFetch from fresh IPs. The operator can view the page in their browser without throttling (configured `basketball_reference_base_url` + presumably a non-rate-limited IP). The Claude session can't decode the table layout from a dev workstation.

The current parser at `apps/api/app/services/nba_referee_tendencies.py:160-228` (`parse_referee_tendency_rows`) assumes these BR column header names exactly:

| Column key | What it should be |
|---|---|
| `"Referee"` | Display name (e.g. "Scott Foster") |
| `"G"` | Games officiated this season |
| `"PF/G"` | Personal fouls called per game |
| `"FT/G"` | Free-throw attempts per game (both teams combined) |
| `"T"` | Total technicals this season |

If BR's actual columns differ, the parser is the file to update.

## What I need from the operator

**One of:**

1. **Best:** the operator opens `https://www.basketball-reference.com/referees/2026_register.html` in their browser, copies the table header row + 2-3 data rows (or screenshots them), and pastes back here. That gives full column-name + HTML-shape verification.
2. **Acceptable:** the operator runs the existing `BasketballReferenceClient._fetch_html_or_empty("/referees/2026_register.html")` from a python REPL in their environment (where the configured base URL + non-throttled IP works), then pastes the parsed HTML's `<table>` block headers back.
3. **Minimum:** just confirm the column headers exactly as they appear in the rendered table — case-sensitive, including punctuation. If they match the 5 above, the wiring ships unchanged.

## What I'll do once the operator confirms

~30 min, single PR (`claude/smarter-13-br-referee-fetcher`):

1. **`fetch_referee_season_stats(season: int) -> list[dict[str, Any]]` method on `BasketballReferenceClient`** in `apps/api/app/clients/basketball_reference.py`.
   - Path: `/referees/{end_year}_register.html`
   - Uses existing `_fetch_html_or_empty` helper (already handles request, retries, HTML-comment extraction).
   - Returns BR-shape raw rows ready for `parse_referee_tendency_rows`.
2. **Wire as the `fetcher` arg** in `apps/api/app/services/scoring/__init__.py` where `load_nba_referee_tendencies` is currently called with `_unavailable_referee_fetcher`.
3. **Refresh-job entry** so the cache warms daily (24h TTL is the existing default).
4. **Test** against a recorded BR response (the existing test infra already mocks `_fetch_html_or_empty`; just add a fixture row).
5. **Update parser** if the operator's column-header confirmation reveals differences.

Closes Smarter #13 entirely.

## Reference: where to wire in

```python
# apps/api/app/services/scoring/__init__.py — current state:
def _unavailable_referee_fetcher(season: int) -> list[dict[str, Any]]:
    raise NotImplementedError(
        "BR referee tendency fetcher not yet wired — see Smarter #13 phase 2b-2"
    )

# After PR:
from app.clients.basketball_reference import BasketballReferenceClient

def _br_referee_fetcher(season: int) -> list[dict[str, Any]]:
    client = BasketballReferenceClient()
    return client.fetch_referee_season_stats(season)
```

`load_nba_referee_tendencies` is called with `allow_network=False` from the synchronous scoring path; the actual network fetch belongs in the deferred refresh job (similar to how `load_nba_referee_assignments` was wired in PR #103). Reuse that pattern.

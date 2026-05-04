# Sika Follow-Up — NBA Advanced Stats + Scheduler Stall

Two independent investigations queued from the PR3 touch test (sika#16). Each has its own charter and verification criteria. You can run them in one session or split into two — they don't share context.

Open a new Claude Code session in `/Users/chris/Workspace/locked-in/github/sika`. Paste this entire file as the first message. Tell the assistant which one you want to tackle (or "both").

---

## 1. NBA Advanced Stats — Unblock the Data Path

### What's broken

`stats.nba.com` is unreachable from this network. Direct probe:

```bash
curl -sI --max-time 15 "https://stats.nba.com/stats/leaguedashteamstats?Season=2025-26&MeasureType=Advanced&PerMode=PerGame&LeagueID=00&...&" \
  -H "User-Agent: Mozilla/5.0 ..." -H "Referer: https://www.nba.com/"
# returns: HTTP: 000 (connection timeout after 15s)
```

The NBA Stats API blocks residential ISPs aggressively. The `NbaStatsClient` (`apps/api/app/clients/nba_stats.py`) uses correct headers, User-Agent rotation, and rate limits — those aren't the issue.

Symptom in the DB:
- `nba_advanced_gamelog_cache`, `nba_league_percentiles_cache`, `nba_team_gamelog_cache`, `nba_hustle_player_cache`, `nba_tracking_cache`, `nba_clutch_player_cache`, `nba_player_defense_cache`, `nba_team_advanced_cache` — all 0 rows
- `OperatorSetting nba_stats_disabled_until` was tripped (4 consecutive failures → 24-hour breaker). Reset during the touch test, but it'll re-trip the moment the daily `advanced_stats_warm` cron fires again.
- 0 NBA player IDs ever resolved into the `EspnPlayerSearchCache` sidecar, because `_resolve_nba_stats_id` (`apps/api/app/services/advanced_stats.py:909`) never got a successful response to extract IDs from.

### Three solution paths — pick one

**Path A — Network workaround (no code).** User runs the API behind a VPN, mobile hotspot, or different network where `stats.nba.com` isn't blocked. Verify with the curl probe above. Then:
```bash
# Reset breaker + retry
.venv/bin/python -c "
import os; os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot')
import sys; sys.path.insert(0,'apps/api')
from app.database import SessionLocal
from app.models import OperatorSetting
db = SessionLocal()
for k in ('nba_stats_disabled_until','nba_stats_consecutive_failures'):
    r = db.query(OperatorSetting).filter_by(key=k).one_or_none()
    if r: r.value = {}
db.commit(); db.close()"

# Trigger warm directly
.venv/bin/python -c "
import os; os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot')
import sys; sys.path.insert(0,'apps/api')
from app.database import SessionLocal
from app.services.advanced_stats import warm_nba_advanced_for_athletes
db = SessionLocal()
print(warm_nba_advanced_for_athletes(db, nba_stats_player_ids=[], season=2026).as_dict())
db.commit(); db.close()"
```
**Pros:** zero code change. **Cons:** user has to remember to run the API on a non-blocked network forever.

**Path B — Alternative data source.** Replace `NbaStatsClient` with a client that pulls from a non-blocked source. Best candidates:
- `basketball-reference.com` — comprehensive, scrapeable HTML, allows commercial use with attribution. Has team advanced, player advanced, hustle, tracking. Not 100% schema-equivalent — `OFF_RATING`/`DEF_RATING`/`PACE` are present, but field names differ; some PR1-2 features (e.g. `screen_assists`) only exist in NBA.com hustle reports.
- `hoopR` (R package, has a Python wrapper `hoopR-py`) — wraps stats.nba.com via Cloudflare-friendly endpoints; may also be blocked since it ultimately hits the same hosts.
- ESPN advanced endpoints — already used for ESPN gamelog; supports some advanced metrics but missing usage rate, pace, and the long-tail tracking data.
- nba_api (`swar/nba_api` on PyPI) — same upstream, won't help.

**Recommended sketch for Path B (basketball-reference):** add `apps/api/app/clients/basketball_reference.py` mirroring `NbaStatsClient`'s public method shape (`fetch_team_advanced`, `fetch_player_advanced_gamelog`, `fetch_league_percentiles`, etc.). Map BR field names to the schema `parse_result_set` already produces. Settings flag `nba_stats_source: Literal["nba_stats","basketball_reference"]` so it can be switched per-deploy. Need to handle BR's HTML-only payloads (use `lxml`/`beautifulsoup4` or a CSV endpoint where one exists).

**Pros:** durable; works on any network. **Cons:** ~1-2 days of work; some long-tail features (hustle deflections, screen assists, drives) may not have a 1:1 BR equivalent and would degrade the PR2c features.

**Path C — Proxy/mirror.** Find a community-maintained mirror of stats.nba.com or run our own egress proxy. Examples to investigate: `nba-api-cf` (Cloudflare workers), `nbastats-proxy` (varies). Configure via `nba_stats_base_url` setting (already exists at `apps/api/app/config.py`).

**Pros:** smallest code change (one URL flip). **Cons:** mirrors come and go; reliability is the third-party's problem; may break suddenly.

### Decision needed before coding

Ask the user:
1. Is a VPN/hotspot acceptable as a permanent solution? (→ Path A, done in 5 min)
2. If not, is the team OK with porting to basketball-reference as the canonical NBA source for this app? (→ Path B, ~2 days)
3. Do they trust running through a third-party stats.nba.com mirror? (→ Path C, ~1 hour)

Don't write code until that's settled. The path determines everything else.

### Verification (after fix)

```bash
# Cache populated
.venv/bin/python -c "
import os; os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot')
import sys; sys.path.insert(0,'apps/api')
from app.database import engine
from sqlalchemy import text
with engine.connect() as c:
    for t in ('nba_team_advanced_cache','nba_league_percentiles_cache','nba_advanced_gamelog_cache','nba_hustle_player_cache'):
        print(t, c.execute(text(f'SELECT count(*) FROM {t}')).scalar())"

# Stats Assistant query — should return advanced metrics
curl -s -X POST 'http://localhost:8000/research/stats/query' \
  -H 'Content-Type: application/json' \
  -d '{"question":"Jalen Brunson last 10 games","sport_key":"NBA","season":2026}' \
  | python -c "import sys,json; d=json.load(sys.stdin); print('advanced keys:', [k for k,v in d['summary']['metric_categories'].items() if v=='advanced']); print('percentiles:', d['summary']['percentiles'])"
```

Expected after fix: advanced keys include `ts_pct`, `usg_pct`, `off_rating`, `def_rating`, `pace`, `pie`. Percentiles populated for each.

UI verification: navigate to `/stats`, pick NBA, run "Jalen Brunson last 10 games". Advanced grid should render with color-coded percentile bars (already styled — sika#16 added the CSS).

---

## 2. Scheduler — Jobs Stalling and Reconciling

### What's broken

The single-worker job queue is wedged. Last 24h of `refresh_jobs`:
```
1602 market_discovery     failed     stalled - reconciled automatically
1556 advanced_stats_warm  failed     stalled - reconciled automatically
1542 cleanup              failed     stalled - reconciled automatically
1531 prop_refresh         failed     stalled - reconciled automatically
1528 settlement           failed     stalled - reconciled automatically
1490 prop_refresh         failed     stalled - reconciled automatically
1486 settlement           failed     stalled - reconciled automatically
1485 refresh              failed     stalled - reconciled automatically
1394 prop_refresh         failed     stalled - reconciled automatically
1391 prop_refresh         failed     stalled - reconciled automatically
```

Pattern: jobs queue, never get a `started_at`, then a reconciler force-fails them after a timeout. The currently-running `refresh` job has been "running" for several minutes per `/health` snapshot, while three others sit queued behind it.

### Where to start

```bash
# Where the worker loop and reconciler live
apps/api/app/services/scheduler.py
apps/api/app/services/refresh_jobs.py     # advance_*_job functions, _guarded_requeue_job, _fail_run
apps/api/app/services/maintenance.py      # if it exists — the cron that calls reconcile

# Look for these symbols
grep -n "stalled - reconciled\|reconcile_stalled\|WORKER_TIMEOUT_GRACE_SECONDS\|claim_budget" apps/api/app/services/

# The timeout helpers
grep -n "_worker_timeout_seconds\|maintenance_claim_budget_seconds" apps/api/app/services/refresh_jobs.py apps/api/app/config.py
```

### Hypotheses to test (in order)

1. **Single-worker queue genuinely overloaded.** The `current_slate` refresh has phases (`kalshi_ingest`, `mapping`, `warm`, `watchlist`) that take minutes each. If the per-phase budget × phases > worker timeout, the reconciler trips before the job finishes its own checkpoint. Fix: bump `maintenance_claim_budget_seconds` or split refresh into parallel sub-jobs.
2. **Lock contention on `refresh_jobs` table.** Multiple instances (uvicorn `--reload` spawns workers) racing on `SELECT ... FOR UPDATE SKIP LOCKED`. Check `ps aux | grep uvicorn` — currently shows two Python processes (43320 + 69121). The fork from `--reload` could be acquiring the lock without honoring it.
3. **A specific job is genuinely hanging on network I/O.** `advanced_stats_warm` calling `stats.nba.com` (15s read timeout × 30 retries = real wall-clock). `prop_refresh` doing Kalshi pagination over 8242 markets. Add per-job structured logs of phase + duration.
4. **`started_at` never set because `claim_job` errors out before the UPDATE commits.** Check the SQL flow in `claim_job` (`refresh_jobs.py`).

### Repro / observe

```sql
-- Look at the live queue right now
SELECT id, kind, scope, status,
       NOW() - COALESCE(started_at, queued_at) AS age,
       LEFT(error_message, 80) AS err
FROM refresh_jobs
WHERE status IN ('running','queued')
ORDER BY queued_at DESC LIMIT 20;

-- Reconciler firing pattern (last 48h)
SELECT date_trunc('hour', finished_at) AS hr,
       kind,
       count(*) FILTER (WHERE error_message ILIKE '%stalled%') AS stalled,
       count(*) FILTER (WHERE status='succeeded') AS ok
FROM refresh_jobs
WHERE finished_at > NOW() - INTERVAL '48 hours'
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
```

API endpoint to probe: `curl -s http://localhost:8000/health | jq '.active_refresh_job, .active_prop_refresh_job'` shows current phase + cursor.

### What "fixed" looks like

- A 24h window with zero `stalled - reconciled automatically` entries
- Each job kind has at least one `succeeded` row with reasonable duration
- `advanced_stats_warm` (when network is available — see section 1) successfully populates a non-zero NBA cache row count

### Tests to run after change

```bash
cd /Users/chris/Workspace/locked-in/github/sika
.venv/bin/pytest apps/api/tests/test_refresh_jobs.py -x -q
.venv/bin/pytest apps/api/tests/test_scheduler.py -x -q  # if it exists
```

### Don't touch

- The job kinds themselves — fix the worker, not the work definitions
- The `OperatorSetting` rows for breaker/cap counters — those are runtime state, not config
- The schema — there's no alembic; tables are defined in `models.py` and managed via `Base.metadata.create_all()`. Don't add migrations infrastructure as part of this change.

---

## State of `main` when you start

Top of `git log --oneline`:
```
b7b71de  fix(web): sport-aware season default + advanced grid CSS (PR 3c follow-up)
4203ef9  test(web): add PR 3c regression spec for AdvancedMetricsGrid
7eed105  PR 3d: ML v2 training — median imputation, weighting, promotion gate (#15)
7a116ff  PR 3c: stats query advanced metrics + league percentile ranks (#14)
caf6141  PR 3b: driver attribution (depends on #12) (#13)
1ddc456  PR 3a: heuristic factor audit — advanced primary, proxies fallback (#12)
486dd83  feat: advanced NBA/MLB stats — ingestion, scoring emit, UI, 6 rounds of polish (#11)
```

The `b7b71de` and `4203ef9` commits are on branch `claude/festive-poitras-8477c6` (open as sika#16, awaiting merge).

You're using **Postgres in Docker**, not the 2.2 GB SQLite file at `apps/api/kalshi_sports_copilot.db` (that's stale, unused — don't query it). Default URL: `postgresql+psycopg://postgres:postgres@localhost:5432/kalshi_sports_copilot`.

Web dev server needs Node v24 in PATH (`PATH=/Users/chris/.nvm/versions/node/v24.3.0/bin:$PATH`), or Tailwind 4 crashes on `structuredClone`. The default `node` is v16.

Good luck.

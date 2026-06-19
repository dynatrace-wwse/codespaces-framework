# Synchronizer — performance analysis

Requested in ISSUES-AND-FRS ("Do a performance analysis on loading Open PRs, recent
runs (empty why?), Audit"). Findings from live Redis + the endpoints in `dashboard/app.py`.

## Open PRs — cached, but CI enrichment is the cost
- `GET /api/sync/prs` serves a **5-min Redis cache** (`sync:prs`); on miss it runs the
  sync CLI to list PRs **and enriches each PR with its GitHub CI status** via `_gh_pr_ci`
  (one GitHub API call per PR). That per-PR fan-out is the expensive part on a cache miss
  (latency scales with PR count + GitHub rate limits).
- The cache is invalidated explicitly (`POST /api/sync/prs/invalidate`, writer-only) and
  the UI's refresh adds `?bust=` but still respects the backend cache.
- **Recommendation:** keep the 5-min cache; bound the CI enrichment — fetch CI status
  concurrently (bounded gather) and/or skip it for PRs older than N days, and surface
  "CI: n/a" rather than blocking. Consider a longer TTL (10–15 min) for PR lists.

## "Recent runs" empty — same rollover bug as Agentic History
- Sync command runs **are** logged (`type=sync-command` in `jobs:completed`), but live
  Redis shows **0 sync-command jobs in the 500-entry window** — they're crowded out by
  integration (388) + framework (82) runs and roll off within days. So "recent runs is
  empty" is **not** a logging gap; it's the single 500-cap `jobs:completed` list.
- **Recommendation:** apply the same fix shipped for Agentic History — archive
  `sync-command` jobs to a dedicated capped list (`sync:jobs:completed`, cap ~100) on
  completion and have `/api/sync/history` read it merged with `jobs:completed` (dedupe by
  job_id). See `_merge_agent_history` / `agent:jobs:completed` for the pattern.

## Audit — already the right pattern (cache + refresh)
- `GET /api/sync/audit` returns `sync:audit:latest` from Redis, **persisted (no TTL)** and
  rebuilt only when the user clicks **Refresh**. This is exactly the "cache the page,
  refresh on demand" approach the issue asked for — **no change needed.** It avoids
  re-running the (slow) audit on every page load.

## Live snapshot (2026-06-19)
`sync:status-summary`, `sync:prs`, `sync:issues`, `sync:audit:latest` were all absent
(TTL -2) at probe time — caches are lazy (built on first request, 5-min TTL) so an idle
period leaves them empty until the next load. Expected, not a bug.

## Summary
| Area | State | Action |
|---|---|---|
| Open PRs | 5-min cached; per-PR CI calls costly on miss | bound/parallelize CI enrichment; longer TTL |
| Recent runs | logged but rolled off the 500-cap | dedicated `sync:jobs:completed` list (like agent fix) |
| Audit | persisted cache + manual refresh | none — already optimal |

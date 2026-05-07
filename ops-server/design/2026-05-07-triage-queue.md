# Ops-Server Triage Queue + History Feed — Design Doc

**Date:** 2026-05-07
**Owner:** Sergio Hinojosa (@shinojosa)
**Source:** Office-hours session, intrapreneurship mode
**Status:** Plan agreed in principle; awaiting slicing decision

## Problem

The ops-server runs nightly + on-demand integration tests across 27 repos.
Code for the new k3d engine + config-driven Dynakube + MkDocs RUM refactoring
is "mostly done, finding edge cases as I test." Every morning during the
fan-out, triaging which repo broke and why is a hunt across logs.

The user wants a UI that surfaces last night's failures fast, with arch / time /
result. Stated UI asks: past-builds list, branch dropdown, repo+release links,
fix the concurrent-log-display bug, port the slick design from
dynatrace-wwse.github.io, move the audit page into the ops-server.

## Wedge

The smallest UI change that unblocks the k3d/Dynakube rollout:

- **Triage queue at top** — failed runs in last 24h, ranked by repeat severity.
- **History feed below** — reverse-chronological filterable feed.
- **Working logs** when you click in (concurrent-log bug fixed).

Visual polish (github.io design port, audit migration) is deferred — earns its
time after the data layer + bug fix ship.

## Why test infrastructure is the actual foundation

The framework has **two test layers**:

### Layer 1 — BATS unit tests (already structured)

`.devcontainer/test/unit/` contains 5 BATS files with **~101 `@test` cases**:
`test_dynakube.bats` (21), `test_env_management.bats` (44), `test_ingress.bats`
(19), `test_source_framework.bats` (12), `test_framework_apps_guard.bats` (5).
Run via `make test` → `bats test/unit/`. Fast, no cluster needed.

BATS already emits TAP output (and `--formatter junit` is available for
machine-readable XML). This layer is already structured — the gap is that
the ops-server doesn't run BATS as a pre-stage. `agents/CLAUDE.md` instructs
the Claude agent to run `make test` manually, and `workers/manager.py:660`
contains a string "Run tests: make test (if available)" — but no automated
invocation, no TAP parser, no per-test results recorded.

### Layer 2 — Integration tests (currently unstructured)

`.devcontainer/test/test_functions.sh` (225 lines) defines 10 assertion
helpers. Per-repo `integration.sh` files source the framework and call them.
Two foundational gaps:

1. `assertDynatraceOperator` and `assertDynatraceCloudNative` are **stubs** —
   they print "TBD" and don't assert. Nightly tests pass trivially for those
   steps. The triage queue can render whatever it wants; it can't render
   meaningful triage from tests that aren't actually testing.
2. No assertion writes structured pass/fail. Everything is
   `printInfo`/`printWarn`. The worker has no way to know which step failed
   except by log regex.

### Two-stage test pipeline

The worker should run a fast first gate before spinning up k3d:

```
Stage 1 (fast, ~30s):  make test           BATS → TAP/JUnit → structured
                                            On fail: skip Stage 2.
Stage 2 (slow, ~10m):  make integration    integration.sh → result.jsonl
                                            via _assert wrapper.
```

A BATS failure shouldn't waste 10 minutes spinning up k3d. A `result.jsonl`
failure shouldn't be parsed by log regex. Both layers feed the same triage
queue, with `failed_step` carrying enough context to distinguish them
(e.g., `bats:test_dynakube.bats:test_unset_default_dynakube` vs.
`integration:assertDynatraceOperator`).

## Concurrent-log-display bug — diagnosed

The livelog stream is correctly run-id-keyed (`job:livelog:{job_id}`). The bug
is in `job:running:{repo}:{arch}` — see `workers/manager.py:102` and
`worker-agent/agent.py:131`:

```python
running_key = f"job:running:{job['repo']}:{job.get('arch', 'arm64')}"
```

No branch, no run_id. When two jobs for the same repo+arch run concurrently,
the second overwrites the first; when the first finishes, it deletes the key
while the second is still running. The dashboard's `/api/builds/running`
(`scan_iter("job:running:*")`) loses one of them. The user clicks "live log"
on the surviving repo row and gets the wrong job's livelog (or none).

There is also no concurrency guard at enqueue time — nothing prevents two
jobs for the same `repo:arch:branch` from running simultaneously. The user's
stated rule is unenforced.

## Current Redis schema (what the code does today)

| Key | Type | Notes |
|---|---|---|
| `queue:test:{arm64\|amd64}` | LIST | per-arch test queue |
| `queue:test` | LIST | arch-agnostic / "both" |
| `queue:agent`, `queue:sync` | LIST | other queues |
| `job:running:{repo}:{arch}` | STRING (json) | meta about running job; collision bug |
| `job:livelog:{job_id}` | STRING | live tail, ex=3600. Correctly keyed. |
| `job:log:{job_id}` | STRING | full log, ex=86400*7, capped 256KB |
| `jobs:completed` | LIST | last 500 jobs (LTRIM -500). Read with LRANGE. |
| `worker:{worker_id}` | HASH | worker registration, TTL=REGISTRATION_TTL |
| `ci:{repo}:*:main` | HASH | GHA workflow_run events |
| `nightly:{run_id}:meta` | STRING (json) | per nightly run |

## Proposed schema delta

```
NEW

build:{run_id}                    HASH    full record
                                          repo, branch, arch, commit_sha, triggered_by,
                                          started_at, finished_at, duration_s, status,
                                          failed_step, failure_summary, worker_id, log_key
                                          TTL: 30 days

builds:by_time                    ZSET    score=finished_at, member=run_id
                                          ZADD on completion. Trim last 10000.

builds:by_repo:{repo}             ZSET    score=finished_at, member=run_id

builds:failed:active              ZSET    score=consecutive_failure_count
                                          member={repo}:{branch}:{arch}
                                          ZINCRBY on fail. ZREM on success.
                                          Drives the triage queue.

running:by_triple                 SET     members={repo}:{branch}:{arch}
                                          Concurrency lock at enqueue time.
                                          SADD returns 1 → claim. 0 → already running.

job:running:{run_id}              HASH    repo, branch, arch, started_at, worker_id, ref
                                          Replaces job:running:{repo}:{arch}.

DEPRECATE

job:running:{repo}:{arch}         remove. dashboard/app.py:179 must scan
                                  job:running:{run_id} keys instead.

KEEP

job:livelog:{job_id}              already correct.
job:log:{job_id}                  already correct.
jobs:completed                    keep for backward-compat. builds:by_time is authoritative.
worker:{worker_id}                unchanged.
```

## Lock semantics

```python
# At every enqueue site (webhook + dashboard + nightly):
triple = f"{repo}:{branch}:{arch}"
if not await pool.sadd("running:by_triple", triple):
    await pool.rpush(f"deferred:{triple}", json.dumps(job))
    return {"status": "deferred"}
await pool.rpush(f"queue:test:{arch}", json.dumps(job))

# At completion:
await pool.srem("running:by_triple", triple)
while deferred := await pool.lpop(f"deferred:{triple}"):
    await pool.rpush(f"queue:test:{arch}", deferred)
```

Crash recovery: on worker startup, reconcile `running:by_triple` against live
`job:running:{run_id}` HASHes; remove orphaned triples.

## Test layer redesign — the actual foundation

### `_assert` wrapper (cached, in framework)

```bash
# In .devcontainer/test/test_functions.sh

_assert() {
    local step="$1"; shift
    local description="$1"; shift
    local started_at=$(date +%s)
    local result_file="${RESULT_JSON:-/tmp/result.jsonl}"
    local error=""
    local status

    if "$@" 2>/tmp/last_assert.err; then
        status="pass"
    else
        status="fail"
        error="$(tail -10 /tmp/last_assert.err)"
    fi

    local finished_at=$(date +%s)
    printf '{"step":"%s","description":"%s","status":"%s","started_at":%d,"finished_at":%d,"duration_s":%d,"error":%s}\n' \
        "$step" "$description" "$status" "$started_at" "$finished_at" $((finished_at-started_at)) \
        "$(printf '%s' "$error" | jq -Rs .)" \
        >> "$result_file"

    [ "$status" = "pass" ]
}
```

### Refactored assertion (example)

```bash
assertDynatraceOperator() {
    _assert "dynatrace-operator" "Dynatrace operator deployment is healthy" \
        _check_dynatrace_operator
}

_check_dynatrace_operator() {
    kubectl wait --for=condition=Ready pod \
        -l app=dynatrace-operator -n dynatrace --timeout=120s
}
```

### Worker integration

After integration.sh exits, the worker:

```python
result_path = f"{job_workdir}/result.jsonl"
records = [json.loads(line) for line in open(result_path)]
failed = [r for r in records if r["status"] == "fail"]
build_record = {
    "run_id": job_id,
    "repo": job["repo"],
    "branch": job["ref"],
    "arch": job["arch"],
    "commit_sha": job["sha"],
    "triggered_by": job["triggered_by"],
    "started_at": records[0]["started_at"] if records else now,
    "finished_at": now,
    "duration_s": ...,
    "status": "fail" if failed else "success",
    "failed_step": failed[0]["step"] if failed else "",
    "failure_summary": failed[0]["error"][:500] if failed else "",
    "worker_id": "master",
    "log_key": f"job:log:{job_id}",
}
await pool.hset(f"build:{job_id}", mapping=build_record)
await pool.zadd("builds:by_time", {job_id: now})
await pool.zadd(f"builds:by_repo:{job['repo']}", {job_id: now})
triple = f"{job['repo']}:{job['ref']}:{job['arch']}"
if failed:
    await pool.zincrby("builds:failed:active", 1, triple)
else:
    await pool.zrem("builds:failed:active", triple)
```

## Plan, ordered

### Phase 0 — Small, independent, ship first (1-2 days)

**Bug fix PR.** Switch running-key from `job:running:{repo}:{arch}` to
`job:running:{run_id}`. Add `running:by_triple` SET with enqueue-time lock
check at all three call sites (webhook, dashboard, nightly). Add crash
recovery on worker startup. Update `dashboard/app.py:179` to scan the new
key. Smallest possible PR, unblocks correct concurrent-log display, enforces
the no-concurrent-runs rule. Independent of everything below.

### Phase 1 — Test layer foundation (3-5 days)

**1a. Wire BATS into the worker as Stage 1.**

Worker invokes `make test` (or `bats --formatter junit test/unit/`) before
`make integration`. Parse JUnit XML or TAP. Write per-`@test` results into
`result.jsonl` with step naming `bats:{file}:{test_name}`. On any failure,
short-circuit: skip Stage 2. Saves ~10 min per broken commit.

**1b. Implement stub integration assertions properly.**

- `assertDynatraceOperator`: `kubectl wait` on operator pod readiness +
  check CRDs are installed.
- `assertDynatraceCloudNative`: dynakube CR exists, status conditions report
  Ready, oneagent injected.

**1c. Add `_assert` wrapper to `test_functions.sh`.**

Refactor existing helpers. `result.jsonl` emission convention. Worker
exports `RESULT_JSON` env var pointing into the job workdir. Both Stage 1
(BATS-derived rows) and Stage 2 (integration assertions) append to the
same file.

### Phase 2 — Schema delta + UI (3-5 days)

4. Add `build:{run_id}` HASH + `builds:by_time` / `builds:by_repo:{repo}` /
   `builds:failed:active` ZSETs. Worker writes them on completion.
5. Build the triage queue page: `builds:failed:active` (top), reverse-chrono
   from `builds:by_time` (below). Drilldown: `build:{run_id}` HASH +
   `job:log:{job_id}` STRING.

### Phase 3 — Fleet sweep (2-3 days, opportunistic)

6. Audit each repo's `integration.sh`. Migrate any inline assertions to
   helpers. Most repos already use the helpers (the framework's own example
   is already thin).
7. Run nightly; verify triage queue surfaces real signal.

### Deferred (after merge ships)

- Port github.io slick design.
- Move audit page into ops-server.
- Branch dropdown UI polish.
- Per-repo build dashboard, per-arch matrix view.

## Observability — the "art of possible" layer

The framework's third principle is "observable by default." The ops-server is
the right place to demonstrate this. Today, `telemetry/reporter.py` already
ships 4 BizEvent types to `codespaces-tracker → DT BizEvents`, but the schema
is thin and key event types are missing. This section extends the plan with
what to instrument, what to query, and how the autonomous-diagnose loop
closes via MCP.

### Architectural rule — Redis is source of truth, DT is enrichment

The ops-server's operational UI (triage queue, history feed, live pipeline)
**must work with Redis-only data**. The framework is open-source and
self-hostable; making the dashboard depend on a Dynatrace tenant would lock
out OSS contributors. Dynatrace adds: cross-pipeline joins, anomaly
detection, geo enrichment, RUM correlation, dashboards visible to
stakeholders. Two layers, not coupled.

### Existing BizEvent surface

| Event type | Fields | Emitted from |
|---|---|---|
| `test.result` | `repository`, `ops.test.passed`, `ops.test.duration`, `ops.test.errors_detail` (500 char), `ops.nightly.run_id` | `report_test_result()` |
| `agent.{type}` | `ops.agent.success`, `ops.agent.details` (json blob) | `report_agent_action()` |
| `nightly.summary` | `ops.nightly.total/passed/failed/pass_rate/duration` | `report_nightly_summary()` |
| `sync.drift` | `ops.sync.current_version`, `target_version`, `drifted` | `report_sync_drift()` |

### Schema gaps in existing events

| Event | Missing field | Why |
|---|---|---|
| All | `framework.version` is hardcoded to `"ops-server"` | Should be the actual framework version under test (e.g. `v1.2.7`). Without it, no version-correlation queries. |
| All | `commit_sha` | Can't link a failure to the exact commit. |
| All | `arch`, `branch`, `triggered_by` | Can't segment dashboards. |
| All | `worker_id` | Can't separate ARM master vs. AMD remote contributions. |
| `test.result` | `failed_step`, `failure_summary` (vs. `errors_detail`) | Reuse the structured fields from `result.jsonl`. The 500-char `errors_detail` is too thin for triage. |
| `nightly.summary` | per-arch / per-stage breakdown | Can't compute "ARM nightly pass rate vs. AMD nightly pass rate." |

### New BizEvent types to emit

| Event type | Trigger | Key fields |
|---|---|---|
| `build.started` | worker picks up job | `run_id`, `queue_wait_ms`, `triggered_by`, `worker_id`, `arch`, `branch`, `commit_sha` |
| `build.stage.completed` | end of Stage 1 (BATS) or Stage 2 (integration) | `run_id`, `stage`, `duration_s`, `tests_total`, `tests_passed`, `tests_failed` |
| `build.assertion.failed` | each failed assertion in `result.jsonl` | `run_id`, `step`, `description`, `error` (truncated 1000 char), `stage` |
| `build.deferred` | enqueue blocked by `running:by_triple` lock | `repo`, `branch`, `arch`, `wait_for_run_id` (the run holding the lock) |
| `build.completed` | worker finishes a job | extends `test.result` with `failed_step`, `failure_summary`, `worker_id`, all the missing fields above |
| `worker.heartbeat` | every 60s per active worker | `worker_id`, `arch`, `active_jobs`, `cpu_pct`, `mem_pct` — for utilization dashboards |

### dtctl in the test layer — the actual demonstration

The integration tests verify k8s primitives are healthy. They do not verify
that **Dynatrace is doing its job**. That's the platform demo and it's the
gap that makes `assertDynatraceCloudNative` a stub today.

Add a new assertion that uses `dtctl` to query the COE tenant for entities
created by the test cluster:

```bash
# In test_functions.sh

assertDynatraceDataFlowing() {
    _assert "dt-data-flowing" "Test cluster data flowing into COE tenant" \
        _check_dt_entities_for_run
}

_check_dt_entities_for_run() {
    local run_id="${RUN_ID:-unknown}"
    local cluster_tag="codespaces-framework-test-${run_id}"
    local timeout=180
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        if dtctl query --json \
            "fetch dt.entity.kubernetes_cluster
             | filter contains(properties.cluster_name, '${cluster_tag}')
             | limit 1" \
            | jq -e '.records | length > 0' > /dev/null; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    return 1
}

assertDynatraceLogIngestion() {
    _assert "dt-log-ingestion" "Sample log line reaches DT log pipeline" \
        _check_dt_log_for_marker
}

_check_dt_log_for_marker() {
    local marker="ops-test-marker-${RUN_ID}-$(date +%s)"
    kubectl run -n default log-gen --image=busybox --rm --restart=Never \
        -- sh -c "echo ${marker}; sleep 1" > /dev/null 2>&1
    sleep 30
    dtctl query --json \
        "fetch logs | filter content == '${marker}' | limit 1" \
        | jq -e '.records | length > 0' > /dev/null
}
```

The BATS unit layer can stay tenant-free. The integration layer's
DT-validating assertions only fire when `DT_ENVIRONMENT` and `dtctl` are
configured (skip with a `pass` if not — don't fail open-source contributors'
local runs). This is a soft dependency, gated behind env presence.

### MCP autonomous-diagnose loop

`agents/CLAUDE.md` already wires the Claude agent to MCP + dtctl. Today the
agent handles bug-fix issues and Gen3 migration. Add a build-failure handler:

```
Trigger: build.completed event with status=fail (Redis pub/sub or webhook)
Agent flow:
  1. Read build:{run_id} HASH for failed_step + failure_summary
  2. Call DT MCP:
     - Get problems for time window [started_at, finished_at] filtered by
       cluster_tag
     - Query logs (DPL) for the failing pod
     - Pull metrics for the failing namespace (cpu, mem, pod restarts)
  3. Call DT MCP for last green build of same repo+branch+arch (build:{run_id}
     where status=success):
     - Diff metrics: was CPU 3x higher this run? Did pod restart count spike?
  4. Post comment on PR (or open issue if nightly):
     - Failure summary (from result.jsonl)
     - Probable cause (from metrics diff)
     - Last-green commit SHA + DT dashboard deep link
     - Suggested fix (model output, low confidence — labelled as such)
  5. Emit agent.{diagnose} BizEvent with success/failure of the loop
```

This is the closed loop: pipeline emits → DT detects → agent investigates →
human reviews suggested fix. Existing infra; just needs wiring.

### Dashboard panels — split between two surfaces

**Internal ops-server dashboard (Redis-backed, always works):**

1. **Live pipeline** — currently running jobs (per arch), queue depths,
   worker CPU/RAM (from `worker:{id}` HASH heartbeat).
2. **Triage queue** — top of `builds:failed:active` ZSET. Live; built earlier.
3. **History feed** — `builds:by_time` ZSET. Filterable. Built earlier.
4. **Concurrency view** — what's currently in `running:by_triple` SET +
   what's in `deferred:{triple}` LISTs (queue depth per blocked triple).

**Public DT dashboard (BizEvent-backed, the showcase):**

DQL queries that produce panels for `apps.dynatrace.com/ui/apps/dynatrace.dashboards`:

```sql
-- Pipeline health: builds/day, pass rate
fetch bizevents
| filter ops.event.type == "build.completed"
| summarize total = count(),
            passed = countIf(ops.test.passed == true),
            failed = countIf(ops.test.passed == false),
            p50 = percentile(ops.test.duration, 50),
            p95 = percentile(ops.test.duration, 95)
            by bin(timestamp, 1d), repository.name, arch
```

```sql
-- Top failing assertions across fleet (last 7d)
fetch bizevents
| filter ops.event.type == "build.assertion.failed"
| summarize fail_count = count() by step, repository.name
| sort fail_count desc
| limit 25
```

```sql
-- MTTR per repo: time from first fail to next pass
fetch bizevents
| filter ops.event.type == "build.completed"
| filter arch == "arm64"
| sort timestamp asc
| summarize streaks = collectArray({timestamp, ops.test.passed})
            by repository.name, branch
| ... (windowing logic)
```

```sql
-- Concurrency contention: how often does the lock defer?
fetch bizevents
| filter ops.event.type == "build.deferred"
| summarize defer_count = count() by bin(timestamp, 1h), repository.name
```

```sql
-- Framework version rollout — combine sync.drift events
fetch bizevents
| filter ops.event.type == "sync.drift"
| dedup repository.name by {timestamp desc}
| summarize on_target = countIf(ops.sync.drifted == false),
            drifted = countIf(ops.sync.drifted == true)
| ... (donut chart)
```

5. **Cross-pipeline join — student impact correlation.** This is the
   killer story. The codespaces-tracker emits `codespace.creation` BizEvents
   (already exists, geo-enriched). The ops-server emits `build.completed`.
   Join them on `framework.version` + `repository.name`:

```sql
fetch bizevents
| filter ops.event.type in ("codespace.creation", "build.completed")
| join kind=inner
       (fetch bizevents | filter ops.event.type == "build.completed")
       on framework.version, repository.name
| ... (panel: per-repo, per-version, count of student creations × pass rate
       of that version's last build)
```

Story: *"When a student opens a codespace for `obslab-livedebugger-petclinic`
on framework v1.2.7, was there a green build for that version+repo in the
last 24 hours? If not — risk window."*

### Davis AI / SLO

Define an SLO in DT:

```
SLO: ops-server-build-success-rate
Target: 95% over rolling 7 days
Metric:
  fetch bizevents
  | filter ops.event.type == "build.completed"
  | summarize passed = countIf(ops.test.passed == true),
              total = count()
  | fieldsAdd rate = passed / total * 100.0
```

When the SLO drifts, Davis emits a problem. The MCP autonomous-diagnose
agent can subscribe to that problem stream and run its investigation
automatically — without waiting for a human-tagged issue. This is the
"art of possible": SLO breach → autonomous root-cause → suggested fix PR.

### RUM correlation — already deployed, need to surface

Per VISION.md: *"Every deployed app gets automatic Real User Monitoring
through shared ingress."* The github.io pages for all 27 repos already emit
RUM. The ops-server dashboard should embed a RUM panel:

- Doc engagement per repo (sessions, time-on-page) — does broken
  documentation correlate with build failures?
- JS errors on the doc site — surface as a separate "docs health" panel
  alongside "build health."
- Geo distribution of doc readers vs. codespace creators — where does the
  funnel leak?

This is zero-engineering work in the ops-server itself — embed the existing
DT dashboard via iframe or query the same BizEvent stream.

### What this adds to Phase 1 / Phase 2

The observability layer extends the existing phases:

**Phase 1 additions (parallel to test-layer work):**
- Extend `telemetry/reporter.py` with the new event types
  (`build.started`, `build.stage.completed`, `build.assertion.failed`,
  `build.deferred`, `worker.heartbeat`).
- Pass `framework.version`, `commit_sha`, `arch`, `branch`,
  `triggered_by`, `worker_id` through every event.
- Add `assertDynatraceDataFlowing` and `assertDynatraceLogIngestion`
  helpers; gate behind `DT_ENVIRONMENT` env var.

**Phase 2 additions (parallel to UI work):**
- Build the public DT dashboard with the DQL queries above.
- Embed key DT panels into the ops-server internal dashboard via iframe
  (or Apps deep link) where it makes sense — never as a hard dependency.

**Phase 4 (new) — autonomous-diagnose loop:**
- Wire MCP-backed agent to subscribe to `build.completed` (status=fail)
  events. Walk through metric diff vs. last green. Post PR comment.
- Define the SLO in DT. Hook Davis problem stream → agent.

## Open questions

- Should `result.jsonl` also flow into Dynatrace BizEvents alongside Redis?
  Yes — emit one `build.assertion.failed` per failed line. Cheap and
  unlocks "top failing assertions across fleet" queries.
- AMD remote worker: same Redis pool? Confirm `MASTER_REDIS_URL` is reachable
  from AMD worker for the `running:by_triple` lock to work cross-host.
- Crash recovery: TTL on `running:by_triple` members vs. reconcile-on-startup.
  Trade-off — TTL means a hung worker eventually frees the lock; reconcile
  needs stable startup. Pick TTL with a long-enough window (e.g., 2× the
  longest expected job).
- Should the MCP autonomous-diagnose loop comment on PRs always, or only
  when failure repeats? Avoid noise; gate on `consecutive_failure_count >= 2`.
- Geo-correlation requires the codespaces-tracker geo-enrichment to
  carry through to `build.completed` joins. Worth confirming the schema
  on the tracker side.
- BATS step naming — TAP/JUnit emit per-`@test` results. Do we explode
  each as a `build.assertion.failed` (high cardinality) or summarize at the
  file level (loses granularity)? Recommend per-test for the 5 BATS files
  (~101 events on a clean run is fine), file-level rollup if it grows.
- How sensitive is COE tenant rate-limiting to the new event volume?
  At ~200 builds/day × ~10 assertions/build = ~2000 events/day from this
  source. Negligible, but document it.

## What I'm NOT recommending

- SQLite alongside Redis. Earlier office-hours suggestion; user pushed back
  correctly. Redis is sufficient at this scale and avoids new infra.
- Build all this in one big PR. Phase 0 is independent and ships first.
- Port the github.io design now. It's polish; ships after the data layer.
- Build the past-builds UI before fixing the test layer. The UI without
  structured assertions renders log-regex output, which is what the user is
  trying to escape from.

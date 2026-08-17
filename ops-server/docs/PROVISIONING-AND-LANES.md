# Provisioning, lanes and autoscaling — how it actually works

Status: written 2026-08-16 against `main` @ `061d4f0`, with the control loop **live**
(`CONTROL_LOOP_APPLY=1` in `/home/ops/.env`). Every claim below is either a file:line or a
number read off the running system on that date.

> **Updated the same day, on branch `epic/two-lanes-and-parallel-deploy`.** The steady state
> described in §7 and most of the gaps in §8 have changed. Read §12 first — it is the
> after-picture; the rest of the document is the analysis that produced it.

This document answers five questions in order:

1. What are the autoscalers, and which one owns which decision?
2. How does a session get from "learner clicks start" to "container on a worker"?
3. How are the two lanes (workshop / self-service) kept apart, and where can that break?
4. How is a worker scheduled in, and how is it torn down?
5. How do many app deploys avoid blocking each other?

---

## 0. The one-paragraph version

There are **two control problems**, deliberately solved differently. Workshop capacity is
**planned** — roster size, repo and start time are all known in advance, so machines are launched
on a schedule before the doors open, kept private to that workshop, and returned when the room
empties. Self-service capacity is **reactive** — nobody files a ticket before starting a lab, so a
feedback loop watches free seats and buys a machine when they run low. The two never share a
machine because they never share a *queue*: routing is queue topology, not a filter. On top of
both sits a **pacer** that decides *when* a queued session is allowed to start, because the failure
mode that killed every early load test was thirty simultaneous installs, not thirty sessions.

---

## 1. The three controllers

| Controller | Lives in | Runs | Decides | Acts on |
|---|---|---|---|---|
| **Control loop** | `dashboard/workshop_fleet.py:757` (`tick`) | every 30 s, in-process task started at `app.py:423` | workshop prewarm/teardown; daily scale-up, shrink, brake | EC2 + Redis |
| **Pacer** | `dashboard/pools.py:364` (`pacer_loop`) | every 5 s, task started at `app.py:414` | *when* a queued provision is admitted | Redis lists only |
| **Manual autoscaler** | `dashboard/fleet_policy.py` + `/api/fleet/autoscale/*` | on a human click (owner-only UI panel) | scale to a target seat count; cordon; reap | EC2 + Redis |

The manual autoscaler predates the control loop and still exists. **It is the one that is not
lane-aware** — see gap G2.

Everything that launches or terminates goes through `dashboard/fleet.py` (`scale_up:528`,
`scale_down:634`), which shells the AWS CLI v2 under the non-expiring `OrbitalFleetAutoscaler`
instance-profile role. `scale_down` refuses any instance not tagged `orbital-role=worker`, so no
control path can terminate the master or a pet worker.

---

## 2. Provisioning flow, end to end

```
  learner clicks "Start" in the DT app (any tenant)
        │
        │  POST /api/arena/provision   {userId, trainingId, workshopId?, tenant…}
        ▼
  ops-server  dashboard/app.py:api_arena_provision
        │  1. mint DT tokens (classic dt0c01 and/or gen3 dt0s16), build job payload
        │  2. write job:running:{job_id}  worker_id="queued"  (+ workshop_id if any)
        │  3. CHOOSE THE LANE  ─────────────────────────┐
        │       pools.target_queue(redis, ws_id, arch)  │  app.py:4682
        │         ws_id has a pool  → queue:pool:ws-…   │
        │         otherwise         → queue:test:amd64  │
        │  4. PACE                                      │
        │       pools.enqueue_paced(redis, target, job) │  app.py:4683
        │         bucket has a token AND nothing waiting → RPUSH target
        │         else                                  → RPUSH queue:pending:{target}
        ▼                                               │
  response {jobId, queuePosition, queue, wsUrl, …}      │
                                                        │
  pacer_loop (5 s)  ── LMOVE pending → target ──────────┘   rate = 1.5/min × workers serving
        │
        ▼
  worker agent  BLPOP [queue:direct:{id}, <its one shared queue>]      agent.py:1099
        │   daily worker    → queue:test:{arch}
        │   workshop worker → queue:pool:{POOL}          config.py:queue_keys
        ▼
  claim warm Sysbox slot  →  clone repo  →  postCreate/postStart  →  "Daemon ready"
        │
        ▼
  reaper watches the container id; on exit the slot is scrubbed and returned to the pool
```

Two properties worth naming:

* **One admission point.** Same-tenant rosters are provisioned by a server-side loop
  (`provision-all`); foreign-tenant learners self-provision when their own app notices
  `provisionRequestedAt`. With 70 tenants that second path is 70 independent clients firing inside
  one 10-second poll window and nothing trainer-side can pace it. Both reach
  `api_arena_provision`, so pacing it once covers both.
* **The queue position is returned to the learner** (`queuePosition`), so a dripped learner sees a
  number instead of an unexplained spinner.

---

## 3. Lane isolation — how, and where it leaks

### The mechanism

Isolation is **queue topology**, chosen precisely because a filter can have a bug and a missing
subscription cannot:

| Worker | `WORKER_POOL` | BLPOPs |
|---|---|---|
| daily (self-service) | `daily` | `queue:direct:{id}`, `queue:test:{arch}` |
| workshop | `ws-{workshopId}` | `queue:direct:{id}`, `queue:pool:ws-{workshopId}` |

`config.py:queue_keys` builds that list; `agent.py:1099 _consume_queue` is the only consumer. A
workshop worker **never subscribes** to the shared arch queue, so a self-service job cannot reach
it — there is no code path that could deliver it.

`WORKER_POOL` is set two ways at launch, on purpose:

* written into `/home/ops/.env` by cloud-init (`fleet.py:_build_user_data:298`) — what the agent reads;
* stamped as EC2 tag `orbital-pool={pool}` (`fleet.py:596`) — survives a stop/start (user-data only
  runs on first boot) and lets teardown find a workshop's machines **without consulting Redis**,
  which matters exactly when Redis is the thing that went wrong.

The mapping workshop → pool lives in the Redis hash `workshop:pools`
(`pools.py:WORKSHOP_POOL_KEY`), written **before** the instances exist so an early learner queues
on the pool queue rather than escaping to daily.

### Where it leaks (all four are known and two are deliberate)

| # | Leak | Deliberate? | File |
|---|---|---|---|
| L1 | `pool_for_workshop` **fails open** — any Redis error returns `""` and the job takes the shared queue | yes: "a workshop losing isolation is degraded; a workshop that cannot provision is failed" | `pools.py:85` |
| L2 | A provision request that **omits `workshopId`** is self-service by definition and lands on daily | yes, but it is the exact bug that put all 8 learners on the daily worker in rehearsal round 1 | `app.py:4652` |
| L3 | A workshop with **no dedicated pool** (small workshop, created before pools, or loop in dry run) runs on daily | yes — `{}` from `/api/workshops/{id}/fleet` means precisely this | `app.py:755` |
| L4 | Between `_register()` and the first heartbeat (**up to 30 s**) a workshop worker's hash has **no `pool` field**, and every reader treats absent as `daily` | **no — gap G3** | `agent.py:889` vs `agent.py:1038` |

---

## 4. Scheduling: when machines appear

### Workshop lane (planned)

```
 T-lead            T-0            T+duration      +30 min grace
 (default 45)                        └── or T+hold (default 240), whichever is LATER
    │               │                  │               │
    ├─ prewarm ─────┤                  │               │
    │  launch ceil(seats / seats_per_worker) × m6a.4xlarge, on-demand
    │  bind workshop:pools[ws] = ws-{id}   (BEFORE the machines exist)
    │  tag orbital-pool=ws-{id}, WORKER_CAPACITY=<planned seats>,
    │      WORKER_SLOT_MEMORY_MB=slot_memory_cap_mb(repo units)
    │
    ├── warming ────► ready   when every worker reports status=ready AND slots_degraded=0
    │                          (workshop_fleet.py:_pool_workers_ready:549)
    │
    │  learners provision → queue:pool:ws-{id} → paced at 1.5/min/worker
    │                                                       │
    └───────────────────────────────────────────────────────┴─► teardown
```

* **Both ends of that window are per workshop and trainer-editable** since 2026-08-17 — see §13.
  `PREWARM_LEAD_MINUTES` / `WORKSHOP_HOLD_MINUTES` are only the defaults now; ask
  `session_lead_minutes()` / `session_hold_minutes()`, never the env var.
* `due_for_prewarm` (`workshop_fleet.py:165`) is true from `scheduledAt − lead` and stays true
  past the start (a trainer who opens the room late must still get machines) **but is bounded by
  teardown** — the two predicates are mutually exclusive on purpose. They were not, and that made
  the loop launch and terminate the same machine forever for any workshop nobody ended.
* An explicit **end always wins over the clock** (`due_for_teardown:218`), so finishing early stops
  the bill.
* Purchasing is **on-demand, never spot** — a spot reclamation costs a learner their session with
  two minutes' notice and a Sysbox session cannot be migrated (no CRIU under Sysbox; verified).

### Daily lane (reactive)

`daily_scale_decision` (`workshop_fleet.py:265`) runs on **daily-pool workers only**, every tick:

| Signal | Threshold | Action | Why not the obvious thing |
|---|---|---|---|
| free seats | `< DAILY_MIN_FREE_SEATS` (4) | launch **1** × m6a.2xlarge spot | — |
| free seats low **but** warming seats would cover it | — | **wait** | a restarting pool looks identical to a full one; unguarded, every worker restart bought a spare instance |
| memory ≥ 70 %, sustained 4 ticks | `MEMORY_PRESSURE_THRESHOLD` | **shrink** advertised capacity by 1, **and** scale up to replace it | memory is the honest occupancy proxy; if it is hot below the advertised seat count, the profile is optimistic and selling the rest overfills anyway |
| CPU ≥ 70 %, sustained 4 ticks | `CPU_BRAKE_THRESHOLD` | set `admission_brake=1` (reversible) | **NOT a scale trigger.** A full 20-seat worker sits at ~24 % CPU; 70 % CPU only ever means an install burst, and a machine arriving 5–10 min later cannot help sessions already placed |
| — | `DAILY_MAX_WORKERS` (4) | refuse to scale | hard cost ceiling |

`admission_brake` is read by the agent every consume tick (`agent.py:945`) — it stops *new* intake
without cordoning, so the sessions already on the box keep their seats.

---

## 5. Teardown

### Workshop (`teardown_workshop_fleet:472`) — the order is the design

1. `state = draining` in `workshop:fleet`.
2. **Unbind** `workshop:pools[ws]` → no further learner can be routed to these machines.
3. Snapshot the pool's worker ids *before* cordoning (once the instances die their heartbeats stop
   and a scan by pool finds nothing).
4. `draining=1` on each worker → the agent stops claiming within one BLPOP timeout (5 s).
5. **Terminate the sessions** (`_terminate_workshop_sessions:626`): durable `terminating=1` flag
   **plus** a `ops:terminate` publish. The flag is what survives a worker that is restarting; the
   pub/sub is fire-and-forget.
6. Drop **queued-but-never-started** learners (`_drop_queued_jobs:655`) from
   `queue:pending:*`, `queue:pool:*`, `queue:test:*`, `queue:direct:*` by job id, and delete their
   `job:running` records — but **only** where `worker_id == "queued"` exactly. An absent
   `worker_id` is merely unknown, and deleting on unknown once deleted live learners' records
   mid-session (that bug leaked tokens, because `dt_token_ids` went with it).
7. Terminate instances = `record.instances ∪ EC2 tag:orbital-pool` — the tag query is
   load-bearing, not belt-and-braces: the record was empty on the first live run.
8. Delete the `worker:{id}` hashes. A terminated machine otherwise leaves a hash frozen at
   `warming`/`capacity:0` that every reader counts as a live daily worker that will never give a seat.

Sessions are terminated **before** instances so the per-slot teardown and the reaper get to run
instead of everything vanishing with the host.

### Daily

Daily workers are **not** torn down by the control loop. They shrink and brake; removal is the
manual autoscaler's `plan_scale_down` → cordon → `terminatable` → terminate, or a hand-driven
`POST /api/fleet/scale-down`. Cordon-then-terminate-when-empty is the only safe order because a
session cannot be moved.

### The two cost backstops

* `lifetime_minutes` arms `shutdown -h +N` **inside** the instance plus
  `--instance-initiated-shutdown-behavior terminate` — a kernel timer that needs nothing from
  Orbital, Redis, AWS creds or a working network. Passed for workshop launches since the two-lanes
  branch (gap G4, closed), and **derived** from the window rather than recomputed — see §13.
* Root volume is `DeleteOnTermination: true`, 300 GiB gp3 @ 500 MB/s / 6,000 IOPS, provisioned in
  `BlockDeviceMappings` at launch (`fleet.py:_root_block_device:321`) so no `ModifyVolume`
  cooldown ever applies.

---

## 6. The capacity arithmetic (why the numbers are what they are)

```
    seats = units(instance) // units(training)
```

`shared/capacity_units.py`. One unit = **one Kubernetes-101 session** (2,048 MiB reserved; 1,609
MiB measured committed). `shared/` and not `dashboard/` because **a worker is a sparse checkout
that does not clone `ops-server/dashboard`** — putting the table in `dashboard/` made amd002 derive
6 slots while amd001 derived 20 on identical hardware. A test pins the location.

| Instance | Units | k8s-101 seats | Astroshop seats |
|---|---|---|---|
| m6a.4xlarge | 20 (**measured**: 20/20 pass, 0/30 fail) | 20 | 5 |
| m6a.2xlarge | 10 | 10 | 2 |
| c5.2xlarge | 6 | 6 | 1 |

| Repo | Units | Source |
|---|---|---|
| `enablement-kubernetes-101` | 1 | measured 2026-08-12 |
| `enablement-dtwiz-101` | 1 | table |
| `demo-astroshop-problems` | **4** | measured 2026-08-16, 7,158 MiB/session |
| anything unprofiled | 3 | pessimistic default — **a guess, not a bound**; it under-estimated Astroshop |

Per-repo overrides live in the Redis hash `repo:units`, applied on the next tick with no deploy.

Two rules that only ever *lower* a number: an unprofiled training is priced as the heaviest known,
and a derived instance figure is clamped by physical RAM.

**Request vs limit.** Units are the *request* (planning). `WORKER_SLOT_MEMORY_MB` is the *limit* —
a runaway guard, currently 20480, sized as `slot_memory_cap_mb(4 units)`. It is not a reservation;
the unit model is what prevents overcommit.

**The old four-ceiling `min(memory, cpu, disk-bandwidth, disk-iops)` is now a diagnostic only.**
Bandwidth and IOPS bound how many installs may *start at once*, not how many a box *holds* — and
that rate belongs to the pacer.

### The pacer's rate, derived not chosen

On one m6a.4xlarge: 30 simultaneous installs exhausted the framework's 600 s readiness gate (0
passes), 20 used 53 of 60 retries, 12 used 29. So:

```
  1.5 installs/min/worker × ~8 min per install ≈ 12 in flight per worker
```

Expressed **per worker** and multiplied by the number of workers actually serving the queue
(`pools.workers_serving:242`), which is what makes the same setting correct for a one-machine
workshop and a five-machine bootcamp. Burst of 2/worker means a lone learner at a quiet moment is
admitted instantly — a fixed timer would tax the common case to fix the rare one.

---

## 7. What the fleet looks like right now (2026-08-16)

```
master-arm64                    role=master   cap 5    (not a lane)
worker-x86_64-amd001   m6a.4xlarge  pool=daily  cap 20  free 20   pet, on-demand
worker-x86_64-amd002   m6a.4xlarge  pool=daily  cap 20  free 20   pet, on-demand
worker-x86_64-spot-…937  m6a.2xlarge pool=daily  cap  6  free  6   spot, launched 13:52 by the loop
worker-x86_64-spot-…cd4  m6a.2xlarge pool=daily  cap  6  free  6   spot, launched 13:52 by the loop
```

Daily lane: **4 workers = `DAILY_MAX_WORKERS`. The daily lane is at its ceiling right now.**
Workshop lane: empty (no workshop within its prewarm window).

Live tunables (`/home/ops/.env` sets only `CONTROL_LOOP_APPLY=1` and `APP_DEPLOY_REF=v1.0.330`;
everything else is the code default): prewarm 45 min, teardown grace 30 min, tick 30 s,
`CONTROL_LOOP_WORKSHOPS=*`, daily min-free 4, daily max 4, daily type m6a.2xlarge, workshop type
m6a.4xlarge, pace 1.5/min/worker burst 2, `WORKER_CODE_BRANCH=main`.

---

## 8. Gaps and caveats

Ordered by what would hurt a 70-person bootcamp most.

### G1 — Autoscaled daily workers sell 6 seats instead of 10 (**live, costing money now**)

The golden AMI bakes `WORKER_CAPACITY=6` (from the c5.2xlarge era). `_derive_capacity()` gives an
explicit env var precedence over the unit model, and **daily** `scale_up()` is called with
`capacity=None` (`workshop_fleet.py:869`), so no `capacity_block` is written and the stale 6 wins.

Verified on the box:

```
$ sudo -u ops ssh 172.31.14.31 "sudo grep -E '^(WORKER_CAPACITY|WORKER_POOL)=' /home/ops/.env"
WORKER_CAPACITY=6
WORKER_POOL=daily
```
…on an instance whose IMDS reports `m6a.2xlarge` (10 units). **40 % of the daily capacity we are
paying for is invisible**, and because free seats read low, the loop keeps buying more machines
that are also 6. Workshop launches are unaffected — they pass `capacity` explicitly.

Fix (either, ideally both): pass `capacity=capacity_units.units_for_instance(DAILY_INSTANCE_TYPE)`
on the daily `scale_up`, and/or rebake the AMI without a `WORKER_CAPACITY` line so the unit model
is the only source.

### G2 — The manual autoscaler is lane-blind, and would cordon a prewarmed workshop

`_fleet_workers()` (`app.py:8977`) scans **every** `worker:*` with no pool filter, and
`fleet_policy.normalize_worker:399` **does not carry the `pool` field at all**. Consequences:

* `fleet_state` reports workshop seats as free self-service capacity → the Workers panel overstates
  available capacity and a human under-scales the daily lane.
* `plan_scale_down:576` sorts candidates **emptiest-first**. A workshop worker prewarmed 45 minutes
  early has `active_jobs=0` and is therefore the *first* thing a "scale down to target" click
  cordons; `terminatable:626` then terminates it. Nothing in that path knows the class starts in
  half an hour.

Fix: add `pool` to `normalize_worker`, filter `_fleet_workers` (or every policy call) to
`pool == "daily"`, and make `plan_scale_down`/`terminatable` skip non-daily pools outright.

### G3 — A warming workshop worker is counted as daily for up to 30 s

`_register()` (`agent.py:889`) writes no `pool` field; only the heartbeat does (`agent.py:1038`,
`HEARTBEAT_INTERVAL=30`). Every reader treats absent as daily. The window is short and the worker
has no warm slots yet, so the harm is a wrong number rather than a misrouted learner — but it also
means the pacer's `workers_serving` under-counts a new pool during its first tick.

Fix: one line — add `"pool": WORKER_POOL` to the `_register` fields.

### G4 — Workshop machines have no self-destruct timer

`provision_workshop_fleet:441` calls `scale_up` without `lifetime_minutes`, so the only thing that
ever terminates a workshop machine is the control loop's teardown. If the dashboard is down, or the
`workshop:fleet` record is lost, or the workshop is deleted rather than ended, the machines run
until someone notices. The EC2 tag query in teardown covers a *lost record*, but not a *dead loop*.

Fix: pass `lifetime_minutes = duration + grace + margin` (e.g. `durationMinutes + 90`). It costs
nothing and it is the only cost guarantee that survives total control-plane failure.

> **Closed** — `provision_workshop_fleet` now passes `_workshop_lifetime_minutes(session)`.
> Note the formula above is *no longer correct*: it must be **derived** from
> `teardown_at − prewarm_at`, not recomputed from duration, or a per-workshop hold pushes
> teardown past the timer and the machines die mid-session. §13.

### G5 — No spare machine in the workshop plan

`plan_workshop_capacity:232` does `ceil(seats / per)` with **no redundancy term**, even though
`capacity_units.instances_for_seats` defaults to `redundancy=1` and `repo_profiles.workers_for_seats`
exposes it. A 70-seat k8s-101 workshop plans exactly 4 × m6a.4xlarge (70/20 → 4, 80 seats). Lose one
host and 20 learners have nowhere to go, with no automatic replacement — the loop will not re-plan a
workshop whose record is already `warming`/`ready`.

Also note 70 seats needs exactly `MAX_SCALE_UP` (4) instances; the batching loop at `:439` handles
that, but a redundancy of +1 would push it to 5 and therefore two `run-instances` calls. That is
fine — it is why the batching exists.

### G6 — Daily lane is at `DAILY_MAX_WORKERS` today

4 daily workers, cap 4. With G1 that is 52 real seats, not the 60 the shapes allow. For a bootcamp
day with self-service traffic *alongside* the workshop, raise `DAILY_MAX_WORKERS` deliberately (and
fix G1 first, or you are buying 6-seat machines).

### G7 — `slots_degraded` gates ready, but nothing bounds the wait

`_pool_workers_ready:549` requires `slots_degraded == 0`. A worker that comes up short (observed:
`SysboxPool: 18/30 slots ready` while reporting fully warm) leaves the workshop record at `warming`
forever, and the daily guard "wait for warming seats" has no timeout either. Wants a bounded wait
that either proceeds degraded or launches a replacement.

### G8 — Reaper under real load at 30 seats, and IOPS

The reaper has been validated at 30 seats on the daily pool (0 stuck `docker wait`, `reaper_watching`
drained 6→0 and 3→0). Not yet validated is the *workshop* teardown-under-load path at 30, and there
is **zero IOPS telemetry anywhere** — every disk number in the model came from hand-run `iostat`.

### G9 — Fail-open routing is invisible

`pool_for_workshop` failing open (L1) produces one `log.warning` and nothing else. A workshop
silently delivered on the daily pool looks exactly like a workshop delivered correctly. Wants a
counter or a field on the fleet record so it shows up in the UI.

---

## 9. The UI gap, and what to build

**Today there is no lane marking anywhere in the UI.** Facts:

* `GET /api/workers` (`app.py:1141`) returns the raw worker hash, so `pool` **is already in the
  payload** — the Workers view simply never renders it (`static/app.js:1067`, which shows arch,
  active/capacity, heartbeat, CPU/mem/disk bars).
* `GET /api/fleet` (`fleet.py:list_fleet:507` → `_parse_instances:345`) **drops the tags entirely**,
  so `orbital-pool` never reaches the browser even though every instance carries it.
* The owner-only autoscale panel (`static/app.js:1139`) shows a single fleet-wide aggregate: free
  slots / in use / workers / warming / cordoned / booting / stale. No lane split.
* `GET /api/workshops/{id}/fleet` (`app.py:755`) is the only lane-aware surface and it is per
  workshop, not a fleet view.

### Proposed, smallest useful change

1. **`_parse_instances` keeps `orbital-pool`** (one line) → `/api/fleet` rows gain `pool`.
2. **Worker card badge** in `loadWorkers()`: `daily` (neutral) / `workshop · {title}` (accent) /
   `unassigned` (warning — that is G3 or a hand-built box), next to the existing role badge. The
   workshop title comes from `live:session:{id}` via a small map endpoint, or fall back to the id.
3. **Split the autoscale summary into two rows**, daily and workshop, driven by the same
   `fleet_state` called twice with a pool filter (which G2's fix makes possible anyway).
4. **A "Lanes" strip** on the Workers tab: `daily 52/60 seats · 4/4 workers (at cap)` and one line
   per active workshop pool: `ws-… · k8s-101 · 2 workers · 31/40 seats · ready`, sourced from
   `workshop:fleet` + `workshop:pools`.
5. **Colour the fleet table rows** by pool so a workshop machine is never mistaken for spare
   capacity by a human doing what G2 lets the planner do.

Order: (1)+(2) are ~20 lines and remove the "I cannot see which is which" problem immediately;
(3)–(5) follow the G2 backend fix.

---

## 10. Concurrent app deploys — the unicorn question

**Short answer: partly solved. Requests no longer block each other; the *build* is still one at a
time, on purpose.**

Three separate things were conflated, so take them apart:

### 10.1 The HTTP server is not the bottleneck

`uvicorn.run("dashboard.app:app", host=127.0.0.1, port=8080)` — **one process, one event loop, no
`workers=`** (`app.py:9257`). That is single-*process*, not single-*request*: FastAPI is async and
every deploy step is either `httpx` (async) or `asyncio.create_subprocess_exec` (async), so N
concurrent deploy requests do not block the loop or each other at the transport level. The dashboard
keeps serving the board, the PTY bridge and `/api/arena/*` throughout.

Two real consequences of one process, both already reasoned about in the code:

* The pacer's token buckets are **in process memory** (`pools.py` module docstring). Sound today;
  if uvicorn ever gets `workers>1`, each worker would independently admit a full burst and the
  buckets must move to Redis. **This is a hard constraint to remember before anyone "scales" the
  dashboard.**
* Restarting `ops-dashboard` mid-flight 502s the API for a few seconds and nginx answers HTML;
  a client calling `.json()` on that raises. It once cost 24 unrevoked tokens. Never restart during
  a live provisioning run.

### 10.2 The deploy *build* is deliberately serialised

```python
_DEPLOY_TREE_LOCK = asyncio.Lock()          # app_deploy.py:525
DEPLOY_UPLOAD_CONCURRENCY = int(os.environ.get("DEPLOY_UPLOAD_CONCURRENCY", "1"))
```

Every deploy runs `dt-app` in **one shared checkout** (`APP_REPO_DIR`): `_sync_repo()` does a
`git reset --hard` in it, `_stamp_ui_version()` rewrites a file in it, the build writes its `dist/`.
Two deploys at once do not queue — they **interleave inside the same working tree**, and the failure
mode is a tenant receiving a bundle built from another tenant's state. Before the lock there was no
guard at all, so a bootcamp morning where many tenants self-update at once was a *silent corruption*
risk, not a slow-but-correct one.

So the lock is the correctness fix. A waiting caller is reported as waiting (logged when > 1 s), and
each holder is bounded by `DEPLOY_TIMEOUT` (600 s). A killed child is reaped **inside** the lock, so
the next deploy never inherits a half-written `dist/`.

### 10.3 Long deploys no longer hold the caller

`POST /api/deploy/token-start` (`app_deploy.py:1736`) returns `{deployId}` immediately, runs the
flow in a background task, and writes the result to Redis under `DEPLOY_JOB_TTL`;
`GET /api/deploy/token-status/{deployId}` polls it. Cheap, certain validation (bad action,
non-Dynatrace tenant) still fails synchronously so an obviously wrong call never becomes a job to
poll. Failures — including unexpected ones — always write a terminal payload, so a caller can never
poll a job that will never finish.

**Net: N tenants updating at once is correct and non-blocking, but throughput is one build at a
time (≈ minutes each).** Seventy tenants self-updating on the same morning would drain slowly and
some would hit the 600 s timeout while queued behind others.

### 10.4 The path to real parallelism (not yet enabled, and why)

`dt-app deploy --skip-build` exists, so the correct answer for N tenants is **build once, upload N
times in parallel** — which is exactly what `DEPLOY_UPLOAD_CONCURRENCY` is a placeholder for. It is
**not** enabled because it is only safe if the built bundle is genuinely tenant-independent, and
`DT_APP_ENVIRONMENT_URL` **is present in the build env** (`app_deploy.py:540`). Shipping tenant A's
URL inside tenant B's bundle would be a worse bug than slowness.

Work item, in order:

1. Prove the bundle is tenant-independent — build twice for two different
   `DT_APP_ENVIRONMENT_URL`s and diff the artifacts. If they are identical, the variable is
   build-time-inert and parallel upload is safe.
2. Split `_run_deploy` into `build_once()` (holds `_DEPLOY_TREE_LOCK`, produces a versioned
   artifact path) and `upload(artifact, tenant, token)` (no lock).
3. Gate uploads with an `asyncio.Semaphore(DEPLOY_UPLOAD_CONCURRENCY)` and raise it to ~8.
4. Only then consider a build cache keyed on `APP_DEPLOY_REF`, so a second tenant on the same tag
   skips the build entirely.

Until step 1 is done, **leave it serialised** — the current behaviour is slow and correct.

---

## 11. Recommended order of work for the bootcamp

| # | Item | Gap | Size | Why now |
|---|---|---|---|---|
| 1 | Pass `capacity` on daily `scale_up` (and/or rebake the AMI) | G1 | 1 line + deploy | We are paying for 40 % capacity we cannot sell, today |
| 2 | `pool` in `normalize_worker`; filter `_fleet_workers`; skip non-daily in `plan_scale_down`/`terminatable` | G2 | small | A single scale-down click can cordon a prewarmed workshop |
| 3 | `pool` in `_register` | G3 | 1 line | Removes the 30 s lie |
| 4 | `orbital-pool` in `_parse_instances` + worker-card lane badge | UI | ~20 lines | "I cannot see which worker is for what" |
| 5 | `lifetime_minutes` on workshop launches | G4 | 1 line | Cost guarantee that survives a dead control plane |
| 6 | Decide `DAILY_MAX_WORKERS` for bootcamp day | G6 | env | Daily lane is at its cap right now |
| 7 | Redundancy +1 on the workshop plan | G5 | 1 line + cost decision | Losing a host today strands 20 learners |
| 8 | Lanes strip + split autoscale summary | UI | medium | Operator situational awareness on the day |
| 9 | Bounded wait on `warming` | G7 | small | A short pool currently hangs a workshop forever |
| 10 | Prove bundle tenant-independence → parallel upload | §10.4 | medium | Only matters if many tenants update the same morning |

---

## Related reading

* `ops-server/docs/HANDOFF-2026-08-14-workshop-pools.md` — the unit model's introduction
* `ops-server/docs/HANDOFF-2026-08-16-loop-live.md` — turning the loop on, and the two APPLY-only failure modes
* `ops-server/CLAUDE.md` §"Control loop" and §"Fleet autoscaler" — the operational rules
* `~/vault/enablement-framework/REPORT_2026-08-13_FLEET_CAPACITY_AND_DISK.md` — the disk/IOPS measurements


-----

There is a note to increase the IOPS nodes to 6,000. Please do so and update the workernodes we have. I want that we have by default, one worker node m6a.4xlarge per lane. So we will switch the AMD 1 for the self-service and AMD 2 for workshops. This way, a trainer can provision quickly without having to create new instances. Apply the changes in the UI so we can see the workernodes and their lanes. Add to the workernodes the buttons so we can cordonsurplus, reap empty and freeze. We do not need the other spot machines, specially if we have cpacitiy on the workernodes. 

Those two will be always running. Then for planned workshops, the scheduler will spin up more depending on how many seats are needed. But the half of the base worker node, if we have 20 seats, then 10 seats should be free. Regarding the recommended order of work, I like them, just move the last one to the first, because the boot camp will be like that.

A lot of tenants will update in the same morning. This is why it's important. I don't want that the application fails before the workshop. This is why we will move number 10 to 0, and then we continue with all the others.



---

## 12. What changed on 2026-08-16 (branch `epic/two-lanes-and-parallel-deploy`)

### The steady state now

| | |
|---|---|
| **amd001** `m6a.4xlarge` | `pool=daily` — self-service only, 20 seats |
| **amd002** `m6a.4xlarge` | `pool=workshop` — **lends 10 of its 20 seats to daily**, keeps 10 reserved so a workshop can start with no notice |
| spot workers | **none** — the two that existed were bought by a bug (G10), not by demand |

A workshop of **7 seats or fewer** binds the standing `workshop` lane and **launches nothing**,
so the room opens immediately instead of waiting ~8 minutes for an instance to boot and warm.
Above 7 it gets its **own** dedicated pool (`ws-{id}`) and its own machines, sized for all its
seats **plus one spare**, keeping full isolation between concurrent workshops.

Lending is enforced at **intake**, not by reservation: the borrow queue is dropped from the
worker's BLPOP key list once the cap is reached. That is the only moment it *can* be enforced —
a placed Sysbox session cannot be moved afterwards. Key order is priority order, so this box
always drains its own workshop queue before it takes borrowed work.

New knobs (all defaulted, none required in `.env`):
`WORKER_BORROW_POOL`, `WORKER_BORROW_FRACTION` (0.5), `WORKSHOP_STANDING_MAX_SEATS` (7),
`WORKSHOP_REDUNDANCY` (1), `WORKSHOP_LIFETIME_MARGIN_MINUTES` (60),
`WORKSHOP_WARMING_TIMEOUT_MINUTES` (20), `DEPLOY_UPLOAD_CONCURRENCY` (8).

### Gap status

| Gap | Was | Now |
|---|---|---|
| **G10** *(new)* — the warming guard summed `capacity` (= slots **already warm**, 0 during warm-up) so it never fired | bought a machine per worker restart | sums `slots_total`; regression test pins it |
| G1 — autoscaled daily workers advertised 6 seats | AMI's baked `WORKER_CAPACITY=6` won | daily `scale_up` pins capacity from the unit model |
| G2 — planners were lane-blind | a scale-down click could cordon a prewarmed workshop | `normalize_worker` carries `pool`; `fleet_state`, `plan_scale_down`, `terminatable` are daily-only |
| G3 — `_register` wrote no `pool` | workshop worker read as daily for ≤30 s | published from the first heartbeat |
| G4 — no self-destruct on workshop machines | only the loop's teardown ever killed them | `lifetime_minutes` **derived** from `teardown_at − prewarm_at` + margin (§13) |
| G5 — no spare in the workshop plan | losing a host stranded 20 learners | `WORKSHOP_REDUNDANCY=1` |
| G7 — unbounded `warming` wait | one short worker hung a workshop forever | proceeds DEGRADED after 20 min, loudly |
| UI — no lane anywhere | could not tell which machine was which | lane badge + coloured edge per worker card, lending line, lane strip; `/api/fleet` carries `orbital-pool` |
| §10 — deploys serialised | one build at a time for every tenant | build once, upload N in parallel |
| **G6** — daily lane at `DAILY_MAX_WORKERS` | 4/4 | **still open** — decide the bootcamp-day value |
| **G8** — reaper under workshop teardown at 30; IOPS telemetry | unmeasured | **still open** |
| **G9** — fail-open routing is silent | one log line | **still open** |

### Parallel deploy — measured, not projected

Precondition proved first: two builds of one commit with different `DT_APP_ENVIRONMENT_URL`
values produced **33/33 byte-identical files** under `dist/`. The only tenant hostname in the
bundle is COE, hardcoded in source as the analytics home, identical in both builds.

Credential isolation is structural, not incidental. `dt-app` derives its token cache as
`<root>/.dt-app/.tokens.json` **with no env override**, so every upload now runs in its own
hardlinked sandbox with its own empty `.dt-app`. `.dt-app` and `.env` are never copied in; the
build metadata under `.dt-app/build` is. (dt-app 1.9.0 also short-circuits on
`DT_APP_PLATFORM_TOKEN` and never consults that cache on our route — but that is a third-party
code path an upgrade could change, and this does not depend on it.)

Four tenants, reinstalling 1.0.330 concurrently:

| | |
|---|---|
| real builds | **1** (31.8 s), 3 reused |
| wall clock | **66.1 s** |
| serial equivalent | **227.0 s** — 3.4× |
| result | 4/4 OK, every tenant verified at 1.0.330 afterwards |
| sandboxes left behind | 0 |

A separate 6-tenant run through `/api/deploy/oauth` refused two tenants at **preflight**
(HTTP 412, `pvf2584h` and `bfs7010h`: their OAuth clients lack the document scopes, and SSO
returns 400 with an **empty** `error_description`). Both were left on their previous versions —
1.0.323 and 1.0.317 — which is the preflight doing exactly what it promises: refuse and install
nothing.

### Still to do

1. ~~**Volume IOPS 3,000 → 6,000 on amd001 + amd002.**~~ **Done 2026-08-17** with the `ubuntu`
   federated admin session (the `OrbitalFleetAutoscaler` role still deliberately cannot
   `ModifyVolume`). Both pets now match what `_root_block_device` gives every *launched*
   worker — 300 GiB gp3 / 500 MB/s / 6,000 IOPS — so the two long-lived boxes are no longer
   the only machines in the fleet with IOPS in their binding set.
   ```bash
   aws ec2 modify-volume --volume-id vol-06626d3323a72f97b --iops 6000   # amd001
   aws ec2 modify-volume --volume-id vol-08e9ee3a2f78f0ceb --iops 6000   # amd002
   ```
   The ~6 h cooldown now applies to both volumes; the previous modification was the
   throughput bump of 2026-08-13. Still unmeasured: nothing has re-run 30 sessions on a
   6,000-IOPS pet to confirm the projected 18 → 20 seats.
2. **Merge the branch.** Launched workers sync `origin/main`, so until then any machine the loop
   launches comes up without the lending code (harmless — only the standing box lends).
3. ~~Decide `DAILY_MAX_WORKERS` for bootcamp day (G6).~~ **Resolved: leave it at 4.** The lane
   split does not shrink the self-service ceiling, because amd002 vacating a daily slot returns
   exactly the headroom it took out of standing capacity:

   | | before the split | now |
   |---|---|---|
   | standing, zero spend | amd001 + amd002 = **40** | amd001 20 + amd002 lends 10 = **30** |
   | autoscaler headroom | 2 spot × 10 = 20 | 3 spot × 10 = **30** |
   | **ceiling** | **60** | **60** |

   The workshop lane is a separate pool and never consumes this cap, so a bootcamp's dedicated
   machines do not compete with self-service scaling at all.
4. ~~G9 — fail-open routing is silent.~~ **Done**: counted per workshop in-process (not in Redis —
   it records Redis being unavailable) and surfaced as `failedOpen` on
   `GET /api/workshops/{id}/fleet`.
5. ~~G8, first half — no IOPS telemetry anywhere.~~ **Done**: the worker heartbeat now publishes
   `disk_read_mbps`, `disk_write_mbps` and `disk_iops`, rendered on each worker card and flagged
   red past 80% of the provisioned 500 MB/s / 6,000 IOPS. Whole physical disks only — partitions
   double-count their parent and loop/dm devices are container overlay noise.
6. Still open: **G8, second half** — the reaper has not been exercised by a *workshop* teardown at
   30 seats (validated on the daily pool only).

---

## 13. The provisioning window became per-workshop and visible (2026-08-17, `e58f274`)

Before this, both ends of the workshop window were process-wide env vars with no override:
a trainer could not see when machines would appear, could not move it, and — worse — the
question "will there be machines for my class?" had no answer anywhere in the product.
`due_for_prewarm` / `due_for_teardown` already accepted `lead_minutes` / `grace_minutes`,
but the only production callers never passed them.

### The model

| Field on `live:session:{id}` | Default | Ceiling | Effect |
|---|---|---|---|
| `prewarmLeadMinutes` | `PREWARM_LEAD_MINUTES` = 45 | `LEAD_MINUTES_CAP` = 360 (6 h) | `prewarm_at = scheduledAt − lead` |
| `holdMinutes` | `WORKSHOP_HOLD_MINUTES` = 240 | `HOLD_MINUTES_CAP` = 1440 (24 h) | floor under `teardown_at` |

```python
teardown_at = start + max(durationMinutes + TEARDOWN_GRACE_MINUTES, holdMinutes)
```

**The hold is a floor, not a replacement.** No workshop ever loses machines earlier than it
did before the change: a 6 h booking still gets 6 h 30, not truncated to 4 h. The default
240 lands on "4 h after the start" for app-created workshops only because the app's create
form **never sends `durationMinutes`**, so they all fall back to the server's 120 — not
because the formula says four hours. Read that twice before quoting "4 h" as a rule.

Five functions in `workshop_fleet.py` are the whole API, and they are the *only* correct
way to ask:

| | |
|---|---|
| `session_lead_minutes(s)` / `session_hold_minutes(s)` | stored value **clamped on read**, else the default |
| `prewarm_at(s)` / `teardown_at(s)` | `None` when unscheduled; the two boundaries |
| `_workshop_lifetime_minutes(s)` | `teardown_at − prewarm_at + WORKSHOP_LIFETIME_MARGIN_MINUTES` |

### The three things that would have broken it

1. **Clamping belongs in the reader, not only the writer.** A value edited straight into
   Redis, or stored before a ceiling moved, must not be able to hold a fleet for a week.
   Validation at the API is a courtesy; the clamp is the guarantee.
2. **`_workshop_lifetime_minutes` must DERIVE, never recompute.** The old
   `lead + duration + grace` arms `shutdown -h +N` *before* the loop's own teardown as soon
   as the hold floor pushes teardown out — the class loses its machines mid-session and the
   kernel timer that did it leaves nothing in Orbital's logs. Worst case is now
   360 + 1440 + 60 ≈ 31 h, which is the point: the backstop must outlive the plan.
3. **`due_for_prewarm` still ends `return not due_for_teardown(session, now)`.** Widening the
   hold is exactly the change that can make both true at once, and that is the bug that made
   the loop launch and terminate the same machine every 30 s for three days. Pinned by a
   swept-timeline test at *both* ceilings — dry run cannot catch it, because dry run never
   transitions state.

### Before restarting, diff the decisions — not the code

Replay old-vs-new `due_for_prewarm`/`due_for_teardown` over every workshop in
`live:sessions:index` and require **zero** changed decisions. Done for this change: 47
workshops, 0 changes, so the first tick after the restart provably launched and terminated
nothing. It costs a minute and it is the only evidence that a scheduling restart is safe.

### The surface it feeds

`GET /api/workshops/{id}/fleet` now **always** answers a schedule block, including when
there is no fleet record — which is precisely the state the trainer needs the numbers in.
It used to return `{}` there.

```jsonc
{
  "scheduled_at": "…", "prewarm_at": "…", "teardown_at": "…",
  "lead_minutes": 45, "hold_minutes": 240,
  "max_lead_minutes": 360, "max_hold_minutes": 1440,
  "provisioned": false,           // no fleet record yet
  "standing": true,               // ≤ WORKSHOP_STANDING_MAX_SEATS — launches nothing, by design
  "standing_max_seats": 7
  // …the existing 12 keys, only when a fleet record exists
}
```

Additive only — the load test's `seats` / `workers` / `state` reads are untouched. Still
unauthenticated: it carries ids and counts, no credentials.

**`standing` exists because "0 workers" is ambiguous.** A workshop of 7 seats or fewer
(roster **plus the trainer**) rides the standing box's reserve and launches nothing on
purpose; rendered from `workers === 0` alone that is indistinguishable from a failed
launch, and the app shipped exactly that bug for one version. Branch on `standing`.

The app (`FleetWindowBanner`, ≥1.0.332) renders this in two places — Workshops →
*Show details*, and the trainer's in-workshop view — with an **Adjust** control that PATCHes
both fields while the workshop is `scheduled` or `open` (Orbital 409s a running edit).
---

## 14. A workshop is sized for the room it BOOKED, not the people who showed up (2026-08-17)

`_roster_size()` was the only seat source the planner ever had:

```python
return int(await redis.scard(f"live:session:{ws_id}:roster")) + 1
```

Learners join a workshop **with a code**. They never appear on a roster, and a trainer is under
no obligation to build one — so for a workshop advertised at 10, 40 or 70 seats this returned
**1**, every plan landed under `WORKSHOP_STANDING_MAX_SEATS`, and every workshop was quietly put
on the standing box's reserve with nothing launched. The banner then said so, in a sentence that
read like a policy rather than a bug: *"1 seat counted, including you."*

The provisioning window exists precisely so machines are up **before** anyone arrives. That is
the one moment when there is nobody to count, which is why counting was the wrong question.

### The seat source

`planned_seats(session, roster_count)` — pure, in `workshop_fleet.py`:

| Input | Seats planned |
|---|---|
| `maxSeats` > 0 | `maxSeats` + trainer team |
| `maxSeats` == 0 (means *unlimited*, unplannable) | roster + trainer team |
| `maxSeats` unparseable / absent | trainer team |

`maxSeats` caps the **roster** only, so the trainer team (up to `MAX_TRAINERS` = 5) is added on
top either way — every trainer takes an environment. Clamped to `live_sessions.MAX_SEATS` **on
read**, for the same reason the window minutes are: this value feeds `RunInstances`, and a number
edited straight into Redis must not be able to buy an unbounded fleet.

### The lane decision is no longer permanent

Booked capacity moves after prewarm — a trainer raises the cap, a roster fills — and the lane was
chosen once and never revisited, so a workshop that grew rode the standing reserve for its whole
delivery. `needs_bigger_fleet(state, rec, seats)` is a **third scheduling predicate**, and the
warning in §Control loop (and the same note in §13) about checking a new predicate against the other two applies to it:

* it fires only on a record with `standing: true`, i.e. one that launched **nothing**;
* it is checked **after** teardown, so a workshop past its window is given back rather than
  upgraded on its way out;
* acting clears `standing`, so it can never fire twice on the same workshop — no oscillation.

It **only upgrades**. Downgrading would terminate machines a room may already be sitting on, to
save a few hours of spend.

`GET /api/workshops/{id}/fleet` gains `planned_seats`, answered **before** provisioning too —
"will there be machines for my class?" is a question about the booking, and the moment a trainer
asks it is the moment nobody has registered yet.

### Measured live

Two workshops booked at `maxSeats: 10` with 2 trainers, both sitting on the standing lane at
`seats: 1`, upgraded on the first tick after the restart and were ready 18 minutes before the
first one started:

```
workshop ws_msx6508v-df67cb: outgrew the standing lane (12 seats > 7) — planning dedicated machines
workshop ws_msx6508v-df67cb: launching 2 × m6a.4xlarge for 12 seats (12 seats ÷ 20/worker +1 spare)
workshop ws_msx6508v-df67cb: 1 worker(s) ready
```

Three other standing records in the same tick stayed standing (booked ≤ 7) — the threshold still
does its job; it was never the threshold that was wrong.

Those runs each launched **two** machines for twelve seats: one real plus a `WORKSHOP_REDUNDANCY`
spare. That spare has since been removed — see §15. Note that `WORKSHOP_SEAT_SAFETY` does **not**
enter the arithmetic at all: `seats_per_worker` accepts the argument and ignores it, because
safety is already baked into the unit table.

---

## 15. The workshop spare stopped being bought (2026-08-17)

`WORKSHOP_REDUNDANCY` now defaults to **0**.

The spare was justified by "a host dies mid-delivery, and the loop does not re-plan a workshop
whose pool is already bound, so there is no automatic remedy." True as far as it goes — but it
never bought what that sentence implies. **The sessions on a dead host die with it**, containers
and all; a warm spare offers somewhere to *re-provision*, never continuity. And standing up a
replacement machine takes minutes, which is the same order as re-provisioning onto a spare that
has been paid for since prewarm.

What actually has to hold is the **lifetime of the containers a connected class is sitting on**,
and the spare has no bearing on that.

The cost it was carrying was not marginal. `seats_per_worker` is 20 for k8s-101 on an
m6a.4xlarge, so:

| Booked seats | Machines needed | With the old spare | Overhead |
|---|---|---|---|
| 8–20 | 1 | 2 | **100%** |
| 21–40 | 2 | 3 | 50% |
| 41–60 | 3 | 4 | 33% |
| 61–70 | 4 | 5 | 25% |

— held for the whole window (prewarm + duration + hold), not just the class. Combined with the
standing threshold, one booked seat took a workshop from **0 machines at 7 seats to 2 at 8**.

Kept as an env var rather than deleted: a delivery that genuinely cannot tolerate a re-provision
sets `WORKSHOP_REDUNDANCY=1` for that fleet with no code change, and `plan_workshop_capacity`
still takes `redundancy=` explicitly.

One knock-on worth knowing: 70 seats now plans **exactly 4** machines, which sits precisely *on*
`fleet.MAX_SCALE_UP` rather than over it. The batching loop in `provision_workshop_fleet` is
therefore no longer exercised by the bootcamp size — `test_a_bootcamp_still_EXCEEDS_the_per_call_cap`
pins a larger workshop so the loop keeps a test that fails if someone removes it.

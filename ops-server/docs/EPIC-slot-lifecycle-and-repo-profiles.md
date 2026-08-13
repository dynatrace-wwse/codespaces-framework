# EPIC — Slot lifecycle hardening + per-repo capacity profiles

**Opened** 2026-08-13 · **Branch** `epic/slot-lifecycle-and-repo-profiles` · **Status** designed, not started
**Origin** the 2026-08-13 capacity measurement — `~/vault/enablement-framework/REPORT_2026-08-13_FLEET_CAPACITY_AND_DISK.md`

---

## Why this epic exists

Two things came out of measuring worker capacity:

1. **A live bug**: a mass teardown can strand a worker — it advertises 0 free slots
   while holding 30 healthy ones, and only an agent restart clears it. The recovery
   restart can itself return at partial capacity while logging `Worker fully warm`.
2. **A modelling error**: capacity was measured as one global number for one repo.
   It has to be *per repo*, because a self-service learner on a heavy repo
   (Astroshop) can otherwise wreck a workshop sharing the same box.

Plus a reframing that changes the target number itself (§1).

---

## 1. The reframing — capacity and concurrency are two different numbers

`WORKER_CAPACITY=30` currently means **both** "how many sessions fit" and "how many
things may happen at once". Those are different questions with different answers,
and conflating them is why the measurement could only produce one number.

| | Governs | Derived from |
|---|---|---|
| **Capacity** | how many seats a box sells | **steady-state** footprint per repo |
| **Concurrency** | how many heavy operations run at once | a **drip rate** / admission limiter |

**Consequence for the measured 18.** That figure came from firing all 30 installs at
the *same instant* — deliberately the worst case, to find the ceiling by breaking it.
A workshop does not do that. If provisioning drips over 10–20 minutes, that burst
disappears and the constraint becomes steady state, which is **30 on memory**.

One nuance to preserve: the heavy step (`dynatraceDeployOperator`) is triggered by the
**learner mid-workshop**, not at provisioning. Staggering provisioning removes one
burst, not both. Trainer pacing (`unlockPath` / `gateAhead`) and natural learner drift
soften the second. Realistic range is **between 18 and 30**, and step 6 re-measures it
rather than assuming.

> **Decision (Sergio, 2026-08-13):** provisioning does not need to be fast. Drip it over
> 10–20 min — UX and reliability beat provisioning latency. Be **pessimistic** on
> weights, not optimistic.

---

## 2. The sockets already exist

This is wiring measured data into existing machinery, not new architecture.

| Need | Existing socket | Gap |
|---|---|---|
| Per-repo weights | `worker-agent/scheduler.py` → `cost_of(job)` | Keys on `suite` / `type`, **not repo**. Every learner session is `TYPE_COST["daemon"] = COST_MEDIUM = 2` whether it is k8s-101 or Astroshop. Costs are hand-assigned 1/2/4, never measured. |
| Per-repo container sizing | `config.WORKER_SLOT_LIMITS`, `WORKER_SLOT_MEM_LIMIT_MB`, `WORKER_SLOT_CPU_SHARES`, `WORKER_SLOT_PIDS_LIMIT` | Flat 4096 MiB for everything; **off by default** (`WORKER_SLOT_LIMITS=0`). |
| Dedicated workshop machines | `queue:direct:{WORKER_ID}` (`agent.py:929`) | Works — it is the lever used to pin load tests to one worker. Not exposed as a workshop concept. |
| Staggered provisioning | `scheduler.CostScheduler` | Admits as fast as budget allows; no drip rate. |
| Capacity model | `dashboard/fleet_policy.py` | Four ceilings, all global and all k8s-101-derived. |

---

## 3. Step 1 — slot lifecycle hardening (do this first, it is a live bug)

Independent of the profiling work and should not wait on it.

### 3.1 What actually happens today

```
docker rm -fv sb-slot-amd001-18 rc=1: Error response from daemon:
cannot remove container "sb-slot-amd001-18": could not kill container:
tried to kill container, but did not receive an exit event
```

1. Mass teardown wedges Docker/Sysbox reaping.
2. `_kill_job_container()` reads `rc=1` as "not removed", logs
   `no live container … (already gone?)`, gives up.
3. Its entire contract is its own docstring — *"killing the outer Sysbox makes the
   executor's `docker wait` return, triggering the finally block."* The kill failed,
   so **nothing returned**.
4. The job coroutine blocks forever → `active_jobs.pop()` never runs → the
   `job:running:` key is never deleted.
5. Heartbeat publishes `slots_free = max(0, ready − active)` = **0, indefinitely**.
   Nothing alerts; `status` still reads `ready`.

`_terminate_reconciler` cannot rescue it: the job is still in `active_jobs` with a live
slot, so it re-kills in a loop.

**Measured 2026-08-13:** 17 `docker wait` alive 20 min after terminate · 30 orphaned
`job:running:*` keys · 0 `Finished:` log lines · `active_jobs`=30 / `slots_free`=0 ·
30 genuinely healthy warm slots.

**Second half — the recovery restart came back short and said it was fine:**

```
SysboxPool: 18/30 slots ready
Worker fully warm after 342s
```

18 running, **8 stuck in `created` (never started)**, 4 never created. `docker start` on
a wedged one returns `rc=0` instantly — dockerd was healthy; the 30-at-once init burst
simply failed for 12 while dockerd was still under teardown pressure. The pool never
retried and never reported the shortfall, so the worker silently ran at 60%. The
`acquire()` liveness probe cannot help — those slots never enter the ready queue to be
claimed. A second restart with dockerd calm gave 30/30 in 305 s.

**One root cause, both halves: fan-out across all 30 slots is not resilient to partial
failure.** Teardown assumes every kill lands; warm-up assumes every start lands.
Neither retries; both report success.

### 3.2 The fix — four parts, all required

**(a) Key on container ID, not name.** Slot names (`sb-slot-amd001-19`) are stable and
**recycled**, so a stale `docker wait` on a name can end up watching a *later* session's
container. Capture `.Id` at creation and watch that.

**(b) Replace 30 blocked `docker wait` processes with one reaper.** A single task polls
`docker ps -q --no-trunc` every ~3 s and resolves any watched ID no longer in the running
set. That is **one subprocess per tick regardless of session count** — cheaper than today
— and it demotes Docker's exit event from load-bearing to advisory. This single change
kills the whole bug class.

**(c) The waiter must never depend on the killer succeeding.** This is the actual lesson.
Terminate records *intent*; the reaper sees "terminated but still running", retries the
kill with escalation, and after N attempts **resolves the future anyway** and marks the
slot unhealthy for rebuild. The job always exits; the slot is always reclaimed. Worst
case costs one slot a rebuild instead of the whole worker.

**(d) Warm-up must retry and report honestly.** Retry failed slot inits with backoff.
Never log `Worker fully warm` when `ready < total` — publish `slots_degraded` so a short
pool is visible instead of silent.

### 3.3 Remove the trigger too

Stagger **teardown** at the same drip rate as provisioning. The root trigger was 30
simultaneous `docker rm -fv` on nested Sysbox containers. Both directions need the drip:
staggering alone leaves the bug latent for the next Docker hiccup, and the reaper alone
means wedging the daemon on every teardown.

### 3.4 The regression test that was missing

A **teardown-under-load** case in the nightly: provision N, terminate all N at once,
assert the worker returns to `slots_ready == slots_total` with **zero** orphaned
`job:running:*` keys and **zero** stray `docker wait` processes. Both halves of this bug
would have been caught by exactly that assertion.

---

## 4. Step 2 — measure repo profiles at steady state

The existing numbers are **install-time**; capacity needs **steady-state during the
workshop**. Different measurement, and the one that was never taken.

Four numbers per repo:

| Field | Feeds | Note |
|---|---|---|
| `steady_memory_mb` | capacity | during the workshop, **not** the install peak |
| `steady_cpu` | capacity | measured 0.127 vCPU for k8s-101 |
| `install_burst_cost` | rate limiter | the operator / DynaKube step |
| `disk_mb` | rate limiter | pull + extract (~3,750 MB for k8s-101) |

Measure **k8s-101 first** (light, and install-time numbers already exist), then
**Astroshop** as the heavy counterexample — the whole point is the ratio between them.

⚠️ **Sizing on post-create measurements understates by ~half.** A k8s-101 session goes
857 MiB → 1,609 MiB committed once the lab runs. Measure with the lab deployed.

---

## 5. Step 3 — the profile lives in the repo

`.devcontainer/resource-profile.json`, so it travels with the repo, the synchronizer
distributes it across all 27, and a repo that changes its weight updates its own profile.

```json
{
  "steady_memory_mb": 1609,
  "steady_cpu": 0.13,
  "install_burst_cost": 4,
  "disk_mb": 3750,
  "measured_on": "2026-08-13",
  "measured_with": "m6a.4xlarge, 30 sessions"
}
```

**Unprofiled repo → pessimistic default: treat as heavy.** One line of policy that
implements the "be pessimistic" rule and fails safe when someone adds a repo nobody
measured.

Consumers: `scheduler.cost_of()` (admission), `config.WORKER_SLOT_*` (cgroup limits),
`fleet_policy` (seat maths).

---

## 6. Step 4 — split capacity from concurrency

- **Capacity** from profiles: `slots = min(memory, cpu, disk-bw, disk-iops)` per repo
  rather than one global k8s-101 number.
- **Concurrency** as a drip rate in `CostScheduler`: admit at most *k* heavy operations
  per interval. This is the knob that makes §1's reframing real.

---

## 7. Step 5 — dedicated workshop pools

Pin a workshop's sessions to specific workers via `queue:direct:{WORKER_ID}` so a
self-service learner on a heavy repo cannot degrade a running workshop. Smallest change
in the epic, but only meaningful once profiles exist (step 3).

---

## 8. Step 6 — re-derive the seat numbers

With staggering live, re-measure. Expect close to 30 for k8s-101 — **measure it, do not
infer it.** Every capacity number in this system has been wrong at least once, and each
time it was inferred rather than loaded:

| Model | Claimed | Measured |
|---|---|---|
| memory only | 30 (r-family) | CPU bound at 16 |
| memory + CPU | 30 (m6a.4xlarge) | **0/30** — disk bound at 18 |
| + disk bandwidth | 30 @ 500 MB/s | **8/30** — IOPS bound at 18 |
| + disk IOPS | 18 | ✓ reproduces both runs |

---

## 9. Deliberately deferred

**IOPS 3,000 → 5,000** (~$10/mo per worker). If provisioning is staggered, the disk burst
that motivated it largely goes away. Hold until step 6 says whether it is still needed.
Note AWS enforces a **~6 hour cooldown** between modifications of the same volume.

**`WORKER_CAPACITY` cap.** Both workers still advertise 30. A 25-learner workshop can
still fill one box. Left as an explicit owner decision, not changed unilaterally —
and step 4 supersedes it by making the number per-repo.

---

## 10. Recommended order

1. **Slot lifecycle hardening** (§3) — live bug, do not gate on anything else
2. **Measure repo profiles at steady state** (§4) — k8s-101, then Astroshop
3. **Wire profiles into `cost_of()` + slot limits** (§5), pessimistic default
4. **Split capacity from concurrency** (§6)
5. **Dedicated workshop pools** (§7)
6. **Re-derive seat numbers** (§8)

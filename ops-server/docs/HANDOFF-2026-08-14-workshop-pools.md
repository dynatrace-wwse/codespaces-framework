# HANDOFF — 2026-08-14 — Capacity in units, and the four bugs a load test found

**Read first.** Supersedes the earlier version of this file (rehearsal-day notes).

| | |
|---|---|
| **Branch** | `epic/workshop-pools-and-autoscale` (pushed, **NOT merged**) |
| **Tests** | 845 dashboard/shared/provisioning/workers + worker-agent suites green |
| **Deployed to** | master ops checkout + amd001 + amd002, by branch checkout |
| **Load test** | `tools/capacity/workshop_loadtest.py` — 5/5 assertions, twice consecutively |

---

## The change in one line

Capacity is now **one number per machine and one number per training**:

```
seats = units(instance) // units(training)
```

`shared/capacity_units.py`. One unit is one Kubernetes-101 session. An m6a.4xlarge is
20 units — measured, not derived: 20/20 sessions passed on that shape and 0/30 did. A
4xlarge is worth exactly two 2xlarges.

## What it replaced, and why

A four-term model — memory, CPU, disk bandwidth, disk IOPS, take the minimum. It was
correct and unusable. Two of its four terms bound how many installs may **start at
once**, not how many sessions a box can **hold**, so a machine's planned capacity
changed whenever a volume was reconfigured: the same m6a.4xlarge was described as 18,
30 and 73 seats within two days.

Paced admission already handles the start rate. Splitting the two lets the planning
number be a table of what has actually been observed to work.

The ceiling functions are kept as **diagnostics** — `limiting_factor` and
`rank_by_cost` still answer "would more IOPS buy seats", which is real advice. They
just no longer decide the plan. `fleet_policy.slots_for_instance` keeps its old meaning
and its tests; planning moved to `fleet_policy.planned_slots`.

Two rules make it fail safe, and both only ever *lower* a number:

* an unprofiled training is priced as the heaviest one we know (3 units);
* a derived instance figure is clamped by physical memory.

---

## Four bugs, none of which a unit test could have found

### 1. The reconciler deleted every paced learner's record

`api_arena_provision` writes `worker_id="queued"` before enqueueing. No worker
registers under that name, so the terminate reconciler concluded the owning worker had
vanished and **deleted the whole `job:running` record**.

While a job sat in the queue for a second or two this almost never fired. The pacer
holds a learner for minutes, so it fired on every parked learner: on 12 seats, the two
admitted in the opening burst kept their records and **all ten who waited lost theirs**.

Nothing looked broken. The worker recreated a bare record, the session came up, the
learner could work. What vanished was everything Orbital knew about the session:

| Lost field | Consequence |
|---|---|
| `workshop_id` | ending the workshop terminates nothing |
| `dt_token_ids` | terminate cannot revoke — two leaked tokens per paced learner |
| `expires_at` | environment inherits the worker's 24 h default |
| `arena_user` | session no longer attributable to the learner |

Fixed in `workers/manager.py:_dead_worker_candidate` — same shape as the Codespace
guard already in that function.

### 2. `end` terminated nothing

Setting state, writing the completion record, applying TTLs, returning 200 — and
twelve environments still running ten minutes later. The teardown code existed; its
only caller was the control loop, which is off by default and fires on the clock
rather than on the trainer's action. `cancel` had the same gap.

Load-bearing for the capacity work, not tidiness: a seat held by an ended workshop is a
seat the next workshop's plan already spent.

### 3. Ending a workshop left its parked learners in the queue

Flagging `terminating` only reaches a job a worker has claimed. A workshop can end with
learners still behind the pacer, and those were admitted afterwards — building
environments for a workshop nobody was attending. Measured: three.

### 4. The fleet-scaled drip was a no-op

`worker:{id}:app_ports_free` is a **list**. `HGETALL` on it raises WRONGTYPE, which
aborted the whole worker scan, so every queue paced at the one-worker rate no matter
how many machines served it. Benign in direction — it drips slower, never faster —
which is exactly why nothing caught it.

---

## Fleet consequences, all conservative

| Change | Was | Now |
|---|---|---|
| Daily worker capacity | 30, typed into `/home/ops/.env` | **20**, derived from the instance type |
| Slot memory limit | 4,096 MiB | **8,192 MiB** |
| Provision drip | fixed 4/min per queue | **1.5/min per worker** (~12 installs in flight per volume) |

The workers now derive their own capacity, so the number a machine advertises and the
number the planner assumed cannot drift apart. An explicit `WORKER_CAPACITY` still
wins — the workshop planner sets it per launch.

**`shared/` exists because a worker is a sparse checkout that does not clone
`ops-server/dashboard`.** Putting the unit table in `dashboard/` made amd002 derive 6
slots while amd001 derived 20, on identical hardware. `setup-worker.sh` now includes
`ops-server/shared/**`. A test pins the location.

---

## Load test result

`tools/capacity/workshop_loadtest.py --seats 12`, run twice back to back:

| Assertion | Result |
|---|---|
| 1. workers advertise what the planner assumed | PASS — 20, model says 20 |
| 2. admitted in phases, not in one burst | PASS — 4 at rest, then 1 per 20 s |
| 3. every seat came up | PASS — 12/12 in **171 s**, then **172 s** |
| 4. all seats landed in one pool | PASS |
| 5. fleet healthy after teardown | PASS — 20/20 free both workers, 0 parked |

Two runs within one second of each other is the point: the ask was consistency, not
density.

It mints real learner tokens, which earlier runs could not — bots died at
`postCreateCommand` in four seconds and never reached the code the teardown assertions
are about. That was itself three defects in `provisioning/`
(`docs/known-issues/arena-oauth-mint-sso-url.md`).

---

## Still owed

1. **Astroshop never bootstraps** — `docs/known-issues/astroshop-never-bootstraps.md`.
   Every Astroshop session ever delivered has been an empty dev container, because
   nothing mints the `DT_API_TOKEN`/`DT_PLATFORM_TOKEN` its `post-create.sh` gates on.
   `DT_API_TOKEN` is ready to add; `DT_PLATFORM_TOKEN` needs
   `platform-token:tokens:write` on the SRO account client. Until then Astroshop's cost
   cannot be measured and stays at the heavy default of 3 units.
2. **Turn the control loop on** — it is sizing correctly in dry-run
   (`13 seats ÷ 20/worker → 1 × m6a.4xlarge`). Read a few ticks, then
   `CONTROL_LOOP_APPLY=1`.
3. **A run with a dedicated workshop pool.** Both runs above placed on the daily pool,
   because the loop is in dry-run so no workshop machines were launched. Isolation is
   proven by last night's rehearsal, not by these.
4. **The reaper under load** is still unvalidated — teardown of 12 was clean, but 12 is
   not 30.
5. **Merge to main**, so autoscaled workers get this code from their boot sync.
6. **Consider raising volume IOPS 3,000 → 6,000** (~$15/month per worker). At 3,000 the
   IOPS ceiling is 18 installs, and the drip is what keeps us under it. Doubling it
   removes the tightest constraint for the price of a coffee.

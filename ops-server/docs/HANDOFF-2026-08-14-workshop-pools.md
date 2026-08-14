# HANDOFF — 2026-08-14 — Workshop pools, paced provisioning, fleet control loop

**Read first.** Everything else is linked from here.

| | |
|---|---|
| **Branch** | `epic/workshop-pools-and-autoscale` (pushed, **NOT merged**) |
| **Tests** | 770 dashboard + 108 worker-agent green |
| **Deployed to** | master ops checkout + amd001 + amd002, **by branch checkout, not merge** |
| **Report** | https://claude.ai/code/artifact/4df36acd-7176-455a-87be-02c4ac112ed5 |
| **Rehearsal tooling** | `ops-server/tools/capacity/workshop_rehearsal{,_watch}.sh` |

---

## Rolling back, if anything looks wrong

Nothing here is merged, so rollback is a checkout:

```bash
# master
sudo -u ops git -C /home/ops/enablement-framework/codespaces-framework checkout main
sudo systemctl restart ops-dashboard
# workers
for w in autonomous-enablements-worker autonomous-enablements-worker-2; do
  ssh $w "sudo -u ops git -C /home/ops/enablement-framework/codespaces-framework checkout main \
          && sudo systemctl restart ops-worker-agent"
done
```

`/home/ops/.env` was appended to under a `# ── rehearsal 2026-08-14` marker, and backed
up first to `/home/ops/.env.bak-prerehearsal-20260814-002237`. **Remove that block before
treating the fleet as production** — it sets `CONTROL_LOOP_APPLY=1` scoped to a single
rehearsal workshop, a 3-minute prewarm lead and a 1-minute teardown grace.

---

## What was built

| Commit | What |
|---|---|
| `dee9fe9` | workers self-update at boot (`WORKER_CODE_REF`) and belong to a pool (`WORKER_POOL`) |
| `b58ba9f` | workshop jobs routed to `queue:pool:{pool}`; paced admission (token bucket) |
| `0ed9560` | teardown hardening — reaper, ID-keyed waits, staggered kills, honest warm-up |
| `531871b` | the control loop — workshop prewarm/teardown + daily autoscale |
| `242eb77` | the admission brake actually bites (it was a flag nothing read) |
| `749b8e6` | dry-run default, workshop allowlist, launchable branch |
| `58bc604` | resolve a workshop's repo so its profile is found |
| `7c4222c` | slot memory cap sized from the repo profile |
| `1e00ed8` | rehearsal harness |
| + | instance-id capture via `tag:orbital-pool`; `fleet:pressure` key rename |

---

## The three design decisions worth not re-litigating

### 1. CPU is not the scale signal — memory is

Measured: 0.127 vCPU per session, so a **completely full** 30-seat worker sits at about
**24% CPU**. A 70%-CPU scale trigger therefore never fires for occupancy. It fires only on
install bursts, which are transient, and the machine it adds arrives 5–10 minutes later
and cannot help sessions already placed — a Sysbox session cannot be migrated.

Memory reaches 70% at roughly **25 of 30 seats**, which is an early warning with time left
to act. Three signals, three jobs:

| signal | meaning | action |
|---|---|---|
| free seats low | demand | launch a worker |
| **memory ≥70% sustained** | **the repo profile is optimistic** | launch **and shrink that worker's advertised seats** |
| CPU / IO pressure | a transient burst | admission brake. **Do not scale** |

The shrink is the part that matters: adding a machine while still advertising seats the
box cannot back overfills it again.

### 2. Capacity is a weight budget, not a seat count

A count only means anything if every session weighs the same. A workshop is one repo, so
budget ÷ weight is a clean seat count. The daily pool is heterogeneous, where a count was
never meaningful and a "don't mix repos" rule would be unenforceable. **An unprofiled repo
is planned as the heaviest thing we know** — the line that makes the system fail safe when
someone adds a repo and forgets to measure it.

### 3. Pool isolation is queue topology, not a filter

```
daily worker    → BLPOP queue:direct:{id}, queue:test:{arch}
workshop worker → BLPOP queue:direct:{id}, queue:pool:{POOL}
```

Self-service work **cannot reach** a workshop box. A filter can regress; a queue a process
never reads from cannot deliver. Blank `WORKER_POOL` resolves to `daily`, which is the
upgrade path for the existing fleet — resolving blank to a pool queue would have made
every worker go silent on restart.

---

## Bugs the live run caught that the unit suite could not

- **`scale_up` returns `instance_id`, not `InstanceId`.** The workshop record captured an
  empty instance list, so teardown called `scale_down([])` and **terminated nothing**.
  Fixed twice over: accept either spelling, and make teardown ask EC2 for
  `tag:orbital-pool` instead of trusting the record. Unit tests never call the real
  `scale_up`, which is the whole argument for the rehearsal harness.
- **`PRESSURE_KEY = "worker:pressure"` collided with the `worker:*` scan** — the counter
  appeared in `/api/workers` as a worker named `pressure`. Now `fleet:pressure`.
- **`trainingId` is a catalog id, not a repo name.** A live workshop stores
  `kubernetes-101` for `enablement-kubernetes-101`, so the profile missed and every
  workshop was sized at 6 seats/worker from the heavy default instead of 20 — a silent 3×
  over-provision, and one nobody would question because falling back is meant to be the
  unusual path.

---

## Traps worth not rediscovering

- **Bots must JOIN before `provision-all`.** Unjoined roster emails return
  "not-joined — will provision on entry" and queue nothing. A rehearsal that skips the
  join measures nothing while looking like a scheduling failure.
- **The join endpoint answers `{"state":...,"joinedCount":N}`** — no `"joined"` key.
  Pattern-matching for one reports 0/8 while all eight joined.
- **Live-session writes need the Orbital service bearer** (`ORBITAL_TOKEN` in
  `/home/ops/.env`) or a signed-in org member. A bare curl gets a 401 that says so.
- **Deploy ordering bites during validation.** The first rehearsal worker launched from a
  dashboard running a commit older than `WORKER_CODE_BRANCH` and `slot_memory_mb`, so it
  synced `main` and came up with slot limits off. The mechanism was fine; the running code
  was two commits behind. Redeploy the master *before* trusting a launched worker.

---

## Measured

| Quantity | Value |
|---|---|
| Warm-up, 30 slots, staged (`WARM_CONCURRENCY=6`) | 333 s / 340 s, `slots_degraded=0` |
| Warm-up, same, previously all-at-once | 305 s — the burst is gone at almost no cost |
| Workshop worker boot → ready (20 slots) | **~15 min** — the number the prewarm lead must cover |
| Seats/worker, k8s-101, ×0.55 planning safety | **20** (30 by memory alone) |
| Workers for 70 seats | **4** → 80 seats, survives losing one machine |
| Astroshop declared pod memory (helm values) | **6,320 MiB** across 10 components |
| Slot cap live on both workers | 4,096 MiB — **below** the above |
| OOM kills observed | none — a vanished margin, not an outage |

---

## Still owed

1. **Measure Astroshop at steady state**, with the lab deployed (post-create understates
   by ~half). It sets the daily pool's generic slot cap and replaces the manifest-derived
   estimate.
2. **Turn the control loop on for real** — read a few dry-run ticks, then
   `CONTROL_LOOP_APPLY=1` and widen `CONTROL_LOOP_WORKSHOPS`.
3. **Merge to main**, so autoscaled workers get this code from their own boot sync rather
   than needing `WORKER_CODE_BRANCH`.
4. **Deploy build-once/upload-many** — proven safe (two builds with different
   `DT_APP_ENVIRONMENT_URL` are byte-identical), turns ~87 min of live registration into ~3.
5. **nginx**: gzip for JSON (only `text/html` is compressed today), upstream keepalive,
   and back the 4 s lab log poll off to 8 s during a workshop.

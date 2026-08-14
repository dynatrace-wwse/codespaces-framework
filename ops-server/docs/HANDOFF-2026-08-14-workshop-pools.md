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
| Workshop worker boot → ready (20 slots) | **~9 min** (launch 00:27:21 → registered ~00:30 → 20/20 at 00:38:34, of which 6 min was pool warm after an agent restart) — the number the prewarm lead must cover |
| Seats/worker, k8s-101, ×0.55 planning safety | **20** (30 by memory alone) |
| Workers for 70 seats | **4** → 80 seats, survives losing one machine |
| Astroshop declared pod memory (helm values) | **6,320 MiB** across 10 components |
| Slot cap live on both workers | 4,096 MiB — **below** the above |
| OOM kills observed | none — a vanished margin, not an outage |

---

## Rehearsal result (2026-08-14, workshop `ws_mss7gmca-71d809`)

| # | Assertion | Result |
|---|---|---|
| 1 | Machines launched automatically at prewarm time | **PASS** — fired 00:27:18, exactly 3 min before the 00:30:01 start, sized 9 seats ÷ 20/worker → 1 × m6a.4xlarge |
| 2 | Launched worker runs current agent code | **PASS (mechanism)** — synced and stamped `WORKER_CODE_REF` at boot. It synced `main`, because the dashboard was two commits behind `WORKER_CODE_BRANCH` at launch time |
| 3 | provision-all admits in phases | **PASS** — 2 admitted immediately, then 1 per ~15 s: `pacer: admitted 1 to queue:pool:ws-…, 5 still waiting` → `4` → `3` → `2` → `1` → `0` |
| 4 | Self-service never lands on workshop machines | **PASS, with the bug it caught** (see below) |
| 5 | Teardown returns the machines | pending at time of writing — fires 01:06 |

**Assertion 4 is the one worth reading.** In round one all eight workshop learners landed
on the DAILY worker while the workshop's own machine sat idle at 20/20 — because
`provision-all` never passed `workshopId`, so pool routing saw an untagged session. After
the fix, round two placed all eight on the workshop worker, and a self-service session
started mid-workshop went to the daily worker and never touched the workshop's machine:

```
round 1 (pre-fix)   WORKSHOP-BOT  -> worker-x86_64-amd001         x8   ✗
round 2 (post-fix)  WORKSHOP-BOT  -> worker-x86_64-spot-3a5794e5  x8   ✓
                    SELF-SERVICE  -> worker-x86_64-amd001         x1   ✓
```

Bot sessions fail at `postCreateCommand` in ~4 s because no DT tokens are minted for them.
That is expected and does not affect assertions 1–4, which are about scheduling, routing
and placement. It does mean the run exercised instance teardown rather than
teardown-under-load; the 30-at-once session teardown still wants its own run.

## Still owed

1. **Fix the arena OAuth mint URL, then measure Astroshop.** The measurement is BLOCKED,
   not skipped: `provisioning/dt_token_provisioner.py:33` posts the client-credentials
   grant to the tenant host instead of `sso.dynatrace.com`, so no harness can provision a
   real environment. Written up in `docs/known-issues/arena-oauth-mint-sso-url.md`.
   The same blocker means **the reaper has not been validated under load** — sessions
   without tokens die at `postCreateCommand` and never reach the wait the reaper replaces.
   Both want a real environment; fix the mint first and they come together.
2. **Raise the daily pool's slot cap** from a flat 4,096 MiB. Astroshop declares 6,320 MiB
   of pod limits, so 8,192 is defensible on the declared figures alone, without waiting for
   the steady-state measurement. The workshop path is already safe — an unprofiled repo
   gets a 12 GiB cap.
3. **Turn the control loop on for real** — read a few dry-run ticks, then
   `CONTROL_LOOP_APPLY=1` and widen `CONTROL_LOOP_WORKSHOPS`.
4. **Merge to main**, so autoscaled workers get this code from their own boot sync rather
   than needing `WORKER_CODE_BRANCH`.
5. **Deploy build-once/upload-many** — proven safe (two builds with different
   `DT_APP_ENVIRONMENT_URL` are byte-identical), turns ~87 min of live registration into ~3.
6. **nginx**: gzip for JSON (only `text/html` is compressed today), upstream keepalive,
   and back the 4 s lab log poll off to 8 s during a workshop.

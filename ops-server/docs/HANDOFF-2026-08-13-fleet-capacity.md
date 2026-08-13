# HANDOFF — 2026-08-13 — Fleet capacity, disk ceilings, slot lifecycle

**Read this first. It is the entry point; everything else is linked from here.**

| | |
|---|---|
| **Continuation branch** | `epic/slot-lifecycle-and-repo-profiles` (pushed, 1 commit `004167c`, design-only) |
| **The plan** | `codespaces-framework/ops-server/docs/EPIC-slot-lifecycle-and-repo-profiles.md` |
| **Full report** | `~/vault/enablement-framework/REPORT_2026-08-13_FLEET_CAPACITY_AND_DISK.md` |
| **Shareable report** | https://claude.ai/code/artifact/57c414ff-f650-429e-a2d9-d43821a68d18 |
| **Measurement tooling** | `codespaces-framework/ops-server/tools/capacity/` (+ its README) |
| **Fleet state at handoff** | amd001 30/30 ready · amd002 30/30 ready · 0 orphan keys · 0 stuck waits · Orbital 200 |
| **`main` at handoff** | `8535c87`, clean, deployed to master + both workers |

---

## Where to start next session

```bash
cd /home/ubuntu/enablement-framework/codespaces-framework
git checkout epic/slot-lifecycle-and-repo-profiles
cat ops-server/docs/EPIC-slot-lifecycle-and-repo-profiles.md   # §3 is step 1
```

**Step 1 is §3 of the epic — slot lifecycle hardening.** It is a live production bug,
it does not depend on any other step, and it was deliberately left unimplemented
because it changes the job lifecycle on a live fleet and warrants a review first.

---

## What shipped to `main` (done, deployed, verified)

| Commit | What |
|---|---|
| `133cce3` | loadtest reconciles tracked sessions against Orbital — a 10-day-old shared state file made provisioning a silent no-op |
| `0be8bd0` | recycled slots keep no workspace; verified before clone (was a 20% dead-session rate) |
| `d944312` | disk as a third capacity ceiling — m6a.4xlarge plans 18, not 30 |
| `ea6a0d7` | `acquire()` never hands out a dead slot; stderr no longer merged into stdout |
| `8497ca1` | workers launch with a 500 MiB/s root volume — the AMI bakes 125 |
| `8535c87` | **IOPS as a fourth ceiling** — 500 MB/s alone did not buy 30 seats |

All three hosts (master + amd001 + amd002) verified on `8535c87`, services restarted.

---

## The three findings that matter

### 1. Capacity is 18 per m6a.4xlarge — but that number is about to change

Measured by scoring how much of the framework's own 60-retry gate the worst session
consumed (retries ≈ `3N − 7`):

| N | Retries used | Result |
|---:|---|---|
| 12 | 29/60 | 12/12 pass |
| 20 | 53/60 | 20/20 pass |
| 30 | exhausted | **0/30 fail** |

**Caveat that reframes it (Sergio, 2026-08-13):** this fired all 30 installs at the
*same instant* — the worst case, chosen to break the system. A workshop does not do
that. With provisioning dripped over 10–20 min the burst disappears and the constraint
becomes steady-state memory, which is 30. The learner-triggered install burst remains,
so the honest range is **between 18 and 30**. Epic §8 re-measures it.

### 2. Raising disk throughput bought nothing — IOPS is a separate ceiling

I predicted 500 MB/s would restore 30 seats. **Measured 8/30.** The prediction was
wrong in the same way as the one it replaced: arithmetic that had never been loaded.

An install has two disk phases and the model described one:

| Phase | Work | Shape | Bound by |
|---|---|---|---|
| 1 | image pull + extract | large sequential | bandwidth ✅ fixed |
| 2 | ActiveGate JVM start, k8s API | small random | **IOPS** ❌ still 3,000 |

Phase 2 sat at **3,381 IOPS against 3,000 provisioned**, `r_await` 7.6–8.3 ms, only
~50 MB/s. **It kills rather than slows**: the ActiveGate boots too slowly to answer its
own readiness probe, kubelet liveness-kills it, and the restart discards all boot
progress.

| ActiveGate restarts | Sessions | Passed |
|---:|---:|---|
| 0 | 5 | **5 (100%)** |
| ≥1 | 25 | 3 |

### 3. ⚠️ OPEN — mass teardown can strand a worker

Worse than the seat count because it is silent and persists. Full detail in epic §3.

- 17 `docker wait` blocked 20 min after terminate · 30 orphaned `job:running:*` keys ·
  `active_jobs`=30 / `slots_free`=0 against **30 healthy warm slots**
- Trigger: `docker rm -fv` fails `could not kill container: tried to kill container,
  but did not receive an exit event`
- The recovery restart came back **18/30** with 8 containers wedged in `created`, and
  logged `Worker fully warm`
- Only an agent restart clears it; a second restart with dockerd calm gave 30/30

**One root cause both halves: fan-out across all 30 slots is not resilient to partial
failure.** Teardown assumes every kill lands; warm-up assumes every start lands.
Neither retries; both report success.

---

## Decisions taken this session (Sergio)

1. **Provisioning does not need to be fast.** Drip 30 sessions over 10–20 min. UX and
   reliability beat provisioning latency.
2. **Capacity must be per-repo, measured.** k8s-101 is light; Astroshop is far heavier.
   Weigh each repo so capacity planning becomes arithmetic.
3. **Workshops get dedicated machines.** A self-service learner on a heavy repo must not
   be able to degrade a running workshop.
4. **Measure during the workshop, not at install time.** The steady-state footprint is
   what determines how many seats fit.
5. **Be pessimistic, not optimistic.** Allow headroom above the measured weight; an
   unprofiled repo is treated as heavy.

---

## Open items, in priority order

| # | Item | Owner call? | Notes |
|---|---|---|---|
| 1 | **Slot lifecycle hardening** (epic §3) | no — just needs review | Live bug. Reaper + bounded wait + staggered teardown + honest warm-up reporting. |
| 2 | Measure repo profiles at steady state (§4) | no | k8s-101 then Astroshop. **Post-create numbers understate by ~half** — a session goes 857 → 1,609 MiB once the lab runs. |
| 3 | Wire profiles into `cost_of()` + slot limits (§5) | no | `.devcontainer/resource-profile.json`, synced by the synchronizer. |
| 4 | Split capacity from concurrency (§6) | no | Supersedes item 6 below. |
| 5 | Dedicated workshop pools (§7) | no | `queue:direct:{WORKER_ID}` already exists. |
| 6 | **Cap `WORKER_CAPACITY`** | **YES** | Both workers still advertise 30. A 25-learner workshop can fill one box and fail for most of them. Left deliberately unchanged. |
| 7 | IOPS 3,000 → 5,000 (~$10/mo/worker) | **YES** | **Deferred on purpose** — staggering may remove the need. ~6 h AWS cooldown per volume. |
| 8 | Bake the ActiveGate image into the slot image | no | It is the component every failing session was stuck on. |
| 9 | Pre-existing red test `dashboard/test_workshops.py::test_delete_only_before_started` | no | Fails on `main` independently of this work. |

---

## Traps worth not rediscovering

- **`rc=0` is a weak claim.** `waitForAllReadyPods` only waits on pods that *already
  exist*. Verify from the DynaKube's `.status.phase` + `.status.conditions`.
- **`logMonitoring` is scheduled late by design.** Its absence at one instant proves
  nothing — an earlier pass of this analysis wrongly called 14 sessions broken on that.
- **`iostat -x` field order**: total MB/s is `($3+$9)/1024`. Wrong indices report a
  plausible-looking fraction of the truth.
- **`bootcamp_loadtest.py` legacy state file** made a whole run a silent no-op that
  looked exactly like a capacity failure. Fixed in `133cce3`; suspect it if a run does
  nothing.
- **Slot names are stable and recycled**, so a stale `docker wait` on a *name* can end
  up watching a later session's container. Any fix must key on container **ID**.
- **Check after every load test**: `ps -ef | grep -c "[d]ocker wait"` = 0, orphan
  `job:running:*` = 0, `slots_ready == slots_total`. **But check for a real live session
  first** — one job / one wait / one key with `terminating` unset is a learner, not a leak.
- **Running the worker-agent tests** needs `--import-mode=importlib`; the modules use
  relative imports and the directory has a hyphen. Without it you get 8 collection errors
  that look like a broken suite:
  ```bash
  cd ops-server
  /home/ops/ops-venv/bin/python -m pytest worker-agent -q --import-mode=importlib   # 83 pass
  /home/ops/ops-venv/bin/python -m pytest dashboard/test_fleet.py dashboard/test_fleet_policy.py -q   # 82 pass
  ```

---

## Credentials — nothing further needed

Orbital runs on the **`OrbitalFleetAutoscaler`** instance profile; scale up/down needs no
human credential, verified working after the federated token expired. Blast radius is the
tag `ManagedBy=orbital-autoscaler`.

The role deliberately lacks `ec2:ModifyVolume` and **does not need it** — `8497ca1` sets
root-volume throughput at `RunInstances`, which the role's existing `volume/*` permission
already covers. The two long-lived workers were modified once by hand.

⚠️ **Flag for EDE (Dynatrace IT):** the federated role `dtRoleRegionsAdvancedUser` **can
create IAM roles and instance profiles** — verified with a probe role, then deleted. Not
implied by the role name. It also cannot read Service Quotas.

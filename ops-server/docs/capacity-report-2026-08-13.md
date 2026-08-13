# Fleet capacity, the disk ceiling, and credential independence

**Date:** 2026-08-13 · **Scope:** Orbital worker fleet, autoscaler, Sysbox slot lifecycle
**Repos:** `codespaces-framework` (`133cce3` → `8497ca1`)
**Status:** shipped to master + amd001 + amd002 · disk change applied live · confirming run below

---

## 1. The one-line answer

**An m6a.4xlarge carries 18 simultaneous k8s-101 installs, not 30.**
The limit was never memory or CPU — it is **the volume**, and it binds in two independent
ways. Raising throughput 125 → 500 MiB/s (~$35/month for the pair) lifted the bandwidth
ceiling and **did not raise the seat count**, because IOPS then bound at the same 18.
That was measured, not assumed — see §5, which falsifies the projection §4 was built on.

Before this work the fleet advertised 30 seats per worker and would have accepted a
25-learner workshop onto one box. Roughly the last third of that cohort would have
watched "Run solution" fail, with nothing in Orbital indicating why.

---

## 2. How capacity was measured — and why the old number was wrong

The previous 30 was **inferred from memory arithmetic**, never loaded. That is the
error this report exists to correct, so the method matters as much as the number.

### 2.1 Score the gate, not the outcome

The framework's `dynatraceDeployOperator` ends in `waitForAllPods`, which retries
**60 times at 10-second intervals — a 600-second gate**. A learner experiences
exhausting that gate as a failed step.

Pass/fail on that gate is a single bit and tells you nothing until you have already
broken the system. Reading **how many of the 60 retries the worst session consumed**
turns it into a continuous measure — so two runs give you a line and you can predict
the ceiling instead of bisecting toward it.

| Sessions | Retries used (worst) | Result | Mean install |
|---:|---:|---|---:|
| 12 | **29** / 60 | 12/12 PASS | 335 s (max 357) |
| 20 | **53** / 60 | 20/20 PASS | 507 s (max 649) |
| 30 | **60 exhausted** | **0/30 FAIL** | 857 s, all stuck on ActiveGate |

Retries scale as roughly **3N − 7** — about 30 seconds of extra gate per added session.
Solving for 60 gives a hard ceiling near **22**; planning at 80% of the gate gives **18**.

### 2.2 `rc=0` is a weak claim — verify from the cluster's own state

`deployApplicationMonitoring` finishes with `waitForAllReadyPods dynatrace`, which
**only waits on pods that already exist**. A component that has not been created yet is
invisible to it, so a green exit code can coexist with a half-built stack.

Verification therefore reads the **DynaKube's own `.status.phase` and its ~25
`.status.conditions`** (`ActiveGateStatefulSet`, `OtelStatefulSet`,
`LogMonitoringDaemonSet`, `LogMonitoringSettings`, `Tokens`, …), which is where the
operator reports its own errors. Script: `scratchpad/verify_dk2.sh` (jsonpath only — a
nested Python heredoc mangles `phase` through a doubled `docker exec`).

> **Pod counting is the wrong instrument twice over.** `logMonitoring` is scheduled
> late by design, so its absence at any single instant proves nothing — an earlier
> draft of this analysis called 14 sessions broken on exactly that mistake.

### 2.3 The components are really deployed

Framework defaults carry `log_monitoring: true` and `telemetry_ingest: true`, so every
k8s-101 run in this report includes the full stack — **7 pods**: operator, webhook ×2,
CSI driver, ActiveGate, log monitoring, OTel collector. Container-only tests were
rejected as too weak a bar.

---

## 3. Three ceilings, and which one binds

A session is constrained by three independent resources. Planning capacity is the
**minimum** of all three — shipped as `fleet_policy.slots_for_instance` (`d944312`).

| Ceiling | Per-session cost | Formula |
|---|---|---|
| **Memory** | 1,609 MiB committed (anon+slab) | `(host_mem − 1260 base − 1500 cache) / 1609`, ×0.8 |
| **CPU** | ~265 CPU-seconds per lab install | `265·N / vCPU ≤ 600` → `N ≤ 2.26 × vCPU` |
| **Disk** | ~3.75 GB of pull+extract | `(600 × 0.8 + 70) / (3750 / throughput)` |

Each was proven by breaking it:

- **12 full-lab on c5.2xlarge → memory.** PSI 35%, pods restarting 3–6×, ActiveGate
  `Init:ImagePullBackOff`, 3 API servers dead. (The same 12 at *post-create only* were
  fine — **the lab is what breaks it**, which is why sizing on post-create is a trap:
  a session roughly doubles its footprint mid-lab, 857 MiB → 1,609 MiB.)
- **30 full-lab on r6a.2xlarge → CPU.** Memory was comfortable (41.7/62.9 GiB, PSI ~0)
  but CPU PSI held 98% for 18 minutes and **all 30 missed the gate**.
- **30 full-lab on m6a.4xlarge → disk.** Memory peaked 48.6/62.9 GiB and CPU spiked only
  late, while the **volume sat pinned at 125.2 MiB/s against a gp3 baseline of exactly
  125**. That flat line is the whole finding.

> **Steady-state capacity ≠ simultaneous-install capacity.** Thirty sessions *fit* on the
> box. They cannot *install at once* — and in a workshop, where the trainer says "everyone
> run step 4 now", only the second number matters.

---

## 4. The disk fix

### 4.1 What was done

Both long-lived workers were raised from the gp3 free baseline to 500 MiB/s, online,
with no downtime and no instance restart:

```
vol-06626d3323a72f97b (amd001)  125 → 500 MiB/s
vol-08e9ee3a2f78f0ceb (amd002)  125 → 500 MiB/s
```

gp3 caps throughput at **0.25 MiB/s per provisioned IOPS**, so 500 MiB/s requires
2,000 IOPS — the volumes already carry 3,000, so **no IOPS purchase was needed**.
Confirmed live afterwards at 310 MB/s on a single-stream `dd` (queue depth 1;
parallel installs push closer to the provisioned figure).

### 4.2 The durable half — new workers were born crippled

The golden AMI (`ami-01c331ae9b0054602`) bakes its root volume at
`VolumeType: gp3, Throughput: 125`, and `scale_up()` launched from it with **no
`BlockDeviceMappings` override at all**. Every autoscaled worker would therefore have
been born at ~18 seats while registering itself as 30 — the fix to the two live boxes
would have been silently undone by the next scale-up.

`8497ca1` sets the root volume explicitly at `RunInstances`. This needs **no IAM
change** (the role's `RunInstances` statement already covers `volume/*`), and it beats
`ModifyVolume`-after-launch, which would race the worker registering itself ready.
`DeleteOnTermination` stays true so a terminated spot worker leaves no 300 GiB volume
behind. Three tests assert the throughput exceeds the baseline, that IOPS can sustain
it, and that the volume is not orphaned.

### 4.3 Cost

Only the 375 MiB/s above the free baseline is billable.

| | Per worker | Both workers |
|---|---:|---:|
| us-east-1 list ($0.04/MB/s-month) | $15.00/mo | **$30.00/mo** |
| London, ~16% higher (estimate) | ~$17.40/mo | ~$34.80/mo |

The exact London rate could not be confirmed — the federated role deliberately lacks
`pricing:GetProducts`. Treat $30/month as a floor.

**Against the alternative:** a third m6a.4xlarge is ~$583/month and adds 18 seats.

| | Seats | $/month | $/seat-month |
|---|---:|---:|---:|
| Two workers, baseline disk | 36 | $1,167 | $32.40 |
| Two workers, 500 MiB/s only | **36** *(measured — no gain)* | ~$1,202 | $33.40 |
| Two workers, 500 MiB/s + 5,000 IOPS | 60 *(projected, §5.4)* | ~$1,222 | ~$20.40 |
| Three workers, baseline disk | 54 | $1,750 | $32.40 |

⚠️ **The second row is the correction.** Throughput alone bought no seats — see §5.
The $35/month is not wasted (it is a precondition for the third row) but it did not,
by itself, change what the fleet can deliver.

Sources: [Amazon EBS pricing](https://aws.amazon.com/ebs/pricing/) ·
[EBS General Purpose SSD volumes](https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html)

---

## 5. The confirming run — and what it falsified

**The 500 MB/s → 30 seats projection was wrong. Measured: 8/30.**

| | 30 sessions @ 125 MB/s | 30 sessions @ 500 MB/s |
|---|---|---|
| Passed the gate | **0 / 30** | **8 / 30** |
| Exhausted 60 retries | 30 | 22 |
| Mean install | 857 s | 772 s |
| Max install | — | 886 s |
| Peak volume throughput | 125.2 MB/s (hard pin) | **523.2 MB/s** |

Raising throughput was **necessary and not sufficient**. The bandwidth ceiling
genuinely lifted — 88% of samples now exceed the old hard limit of 125 — and the pass
rate moved from zero to eight. It did not reach 30, and 18 stands as the shipped number.

### 5.1 Why — a lab install has two disk phases, and the model described one

| Phase | Work | Shape | Bound by |
|---|---|---|---|
| 1 | image pull + extract | large sequential | **bandwidth** |
| 2 | ActiveGate JVM start, container + k8s API | small random | **IOPS** |

During phase 2 the volume sat at **~3,381 IOPS against 3,000 provisioned**, `r_await`
7.6–8.3 ms, while delivering only ~50 MB/s. High IOPS, high latency, low bandwidth is
the signature of IOPS starvation — a completely different resource from the one that
was raised.

### 5.2 It kills the install rather than slowing it

IOPS starvation here is not a tax, it is a death spiral:

1. The ActiveGate JVM boots too slowly under IO latency.
2. Its readiness probe times out — `dial tcp 10.42.0.14:9999: connect: connection refused`.
3. kubelet kills the container on the liveness probe.
4. The restart **discards all boot progress** and the clock starts over.
5. The 600 s gate exhausts.

The correlation across the 30 sessions is near-total:

| ActiveGate restarts | Sessions | Passed |
|---:|---:|---|
| 0 | 5 | **5 (100%)** |
| 1 | 3 | 0 |
| 2 | 6 | 2 |
| 3 | 13 | 1 |

**Every session whose ActiveGate never restarted passed. Almost none that restarted did.**

### 5.3 What this changes in the model

`fleet_policy` now carries a **fourth ceiling** (`8535c87`):
`slots_for_instance = min(memory, cpu, disk-bandwidth, disk-iops)`, with the two disk
terms reported separately because raising one and not the other buys nothing.

| Configuration | Model | Measured |
|---|---:|---|
| m6a.4xlarge @ 125 MB/s / 3,000 IOPS | 18 | 18 pass, 30 fails ✓ |
| m6a.4xlarge @ 500 MB/s / 3,000 IOPS | 18 | 8/30 ✓ |
| m6a.4xlarge @ 500 MB/s / 5,000 IOPS | 30 | **untested projection** |

### 5.4 Next lever — and it is blocked

Reaching 30 needs **~5,000 IOPS** (165 per session × 30). That is 2,000 above the free
baseline at $0.005/IOPS-month ≈ **$10/month per worker** — cheaper than the throughput
change. Two constraints:

- **AWS enforces a ~6 hour cooldown between modifications of the same volume.** Both were
  modified at 11:46 UTC and still read `optimizing`, so the IOPS change cannot be applied
  or tested until roughly 18:00 UTC.
- **It is a projection, and the last projection in this document was wrong by 22 sessions.**
  It must be loaded before it is planned on.

There is also a **$0 lever worth testing first**: the failure is a probe killing a JVM
that is merely slow. Lengthening the ActiveGate readiness/liveness thresholds, or
pre-baking the ActiveGate image into the slot image (open item 2), may recover most of
the gap without buying anything.

---

## 6. Fleet-wide results (before the disk change)

| Load | Made the 600 s gate | Fully Ready later |
|---|---|---|
| 60 fleet-wide (27 + 29) | **17/56 (30%)** | 28/56 (50%) after ~10 min |
| **36 fleet-wide (18 + 17)** | **35/35 (100%)** | 35/35 `phase=Running`, **zero failing conditions** |

The 36-session run is the validated pre-change number: 18 per worker with the full
Dynatrace stack healthy, log-monitoring pod present in 35/35. Peaks: memory 35.5/62.9 GB,
CPU PSI 92, IO PSI 89, disk 130.7 MB/s.

Note the 60-session shape: components **converge after** the gate. They are not broken,
they are slower than a learner is willing to wait — which is the same thing from the
learner's seat.

---

## 6b. ⚠️ OPEN: mass teardown bricks a worker (found cleaning up this run)

**This is more urgent than the seat count**, because it is silent and it persists.

Tearing down the 30 test sessions left amd001 advertising **0 free slots while holding 30
healthy warm ones**:

| Signal | Value |
|---|---|
| `docker wait sb-slot-*` alive | **17**, started 11:55–12:00, still blocked 20 min after terminate |
| `job:running:*` keys | **30** |
| `Finished:` log lines in 20 min | **0** |
| `active_jobs` / `slots_free` | 30 / **0** |
| Actual healthy warm slots | **30, all rebuilt** |

The chain:

1. Mass teardown → Docker/Sysbox cannot reap the container:
   `docker rm -fv sb-slot-amd001-18 rc=1: could not kill container: tried to kill
   container, but did not receive an exit event`.
2. `_kill_job_container()` reads rc=1 as "not removed", logs
   `Terminate …: no live container among [...] (already gone?)`, and gives up.
3. But its entire contract is the docstring's claim — *"killing the outer Sysbox makes the
   executor's `docker wait` return, triggering the finally block."* **The kill failed, so
   nothing returned.**
4. The job coroutine blocks forever, so `active_jobs.pop()` never runs and the running key
   is never deleted.
5. The heartbeat publishes `slots_free = max(0, ready − active)` = **0, indefinitely**.
   The scheduler routes nothing to the box. Nothing alerts. `status` even reads `ready`.

`_terminate_reconciler` cannot rescue it — the job is still in `active_jobs` with a live
slot, so it re-kills in a loop forever.

**Only `systemctl restart ops-worker-agent` clears it** (verified: 17 waits → 0, 30 keys → 0,
followed by a full ~10-minute re-warm of all 30 slots).

**Aggravating detail:** slot names (`sb-slot-amd001-19`) are stable and recycled, so a stale
`docker wait` on a *name* can end up watching a later session's container. Any fix must key
on the container **ID**, and must be **bounded** — a terminated job's coroutine has to exit
even when Docker never delivers an exit event.

### Second half: the recovery restart also came back short — and said "fully warm"

Restarting the agent cleared the stuck waits, but the pool re-initialized to **18 of 30** and
logged, one second apart:

```
SysboxPool: 18/30 slots ready
Worker fully warm after 342s
```

On the box: **18 running, 8 stuck in `created` (never started), 4 never created at all.**
`docker start` on a wedged one returns rc=0 instantly — dockerd was healthy. The 30-at-once
init burst simply failed for 12 while dockerd was still under teardown pressure, and the pool
**never retried them and never reported the shortfall**. The worker silently ran at 60%
capacity. `acquire()`'s liveness probe cannot help, because those slots never enter the ready
queue to be claimed. A second restart, with dockerd calm, gave **30/30 in 305 s**.

**One root cause, both halves: fan-out across all 30 slots is not resilient to partial
failure.** Teardown assumes every kill lands; warm-up assumes every start lands. Neither
retries, and both report success. A fix should cover both — and stop calling a partial warm
"fully warm".

Impact if unfixed: every cohort teardown risks stranding a worker, and every recovery risks
silently returning it at partial capacity.

**Fleet verified healthy after the second restart:** amd001 and amd002 both 30/30 ready,
0 orphan keys, 0 stuck waits, Orbital 200.

---

## 7. Defects found and fixed along the way

Three of these were only visible under load. All are shipped to master and both workers.

### 7.1 Recycled slots kept the previous session's workspace (`0be8bd0`)

**3 of 15 provisions failed** — a 20% dead-session rate that would hit a cohort directly.
The error blamed git, not the slot:

```
git clone ... failed (rc=128): fatal: destination path
'/home/ops/workdir/slots/21/workspace/enablement-kubernetes-101'
already exists and is not an empty directory.
```

- `SysboxPool.release(healthy=True)` wiped inner Docker state but **never the workspace**.
  The next job's pre-clone `rm -rf` was the only cleanup a reused slot ever got.
- That `rm` **discarded its exit code**; the host `rmtree` passed `ignore_errors=True`;
  `mkdir(exist_ok=True)` masked the leftovers. A failed clean was invisible until git
  blew up two steps later.
- Root cause is a **race, not permissions**: the previous inner `dt` container
  bind-mounts that same directory, and while the mount is going away `rm -rf` takes
  **EBUSY on the mountpoint**. Only under a burst, only on some slots.

`release()` now empties the workspace itself, *after* `docker rm -fv dt` frees the mount
and while nothing waits on the slot — so the retry costs no learner time. Ordering is
load-bearing and asserted by parsing the source. The executor's clean is verified with
bounded retries and raises `SlotWorkspaceDirty`, releasing the slot **unhealthy** for a
full re-init rather than cloning into an assumed-empty directory.

### 7.2 `acquire()` handed out dead slots (`ea6a0d7`)

**4 of 60, then 1 of 36** provisions landed on a slot whose container was already gone.
Being in the warm queue only proves the container ran when it was *enqueued* — it can die,
or still be mid-rebuild after a mass teardown, before a job claims it. `acquire()` now
probes with `docker inspect` and rebuilds a dead slot instead of handing it over. The
probe **fails open**, so a flaky Docker CLI cannot empty the pool.

*Self-inflicted, same commit:* my first cut merged **stderr into stdout**, so Docker's own
`No such container` landed where a directory listing was expected — a dead slot reported
itself as an un-emptyable workspace and retried three times pointlessly. Streams are now
separate and `SlotContainerGone` raises immediately.

### 7.3 The load test silently did nothing (`133cce3`)

A **10-day-old shared state file** at `/tmp/bootcamp_loadtest_state.json`, owned by another
user so never rewritten, was read as a fallback. Every bot printed "already tracked",
nothing provisioned, and the run failed minutes later with 18/18 EXPIRED — which looks
exactly like a capacity failure. Provisioning now reconciles against Orbital the way
teardown already did.

### 7.4 Two autoscaler defects that would have broken every new worker

- `_register()` ran **before** `sysbox_pool.init()`, so a booting worker advertised full
  nominal capacity with zero usable slots — 4m35s of lying, for 12 slots.
- **`draining` appeared zero times in `worker-agent/`.** The master set it, the agent
  ignored it, and scale-down drain was a **no-op**.

Plus two found only by launching a real worker from the golden AMI: `WORKER_SSH_HOST`
inherited from the box it was baked from (master SSHed to the wrong worker), and the
master's `ops` SSH config held only three hardcoded hostnames, so any autoscaled worker
gave `Permission denied (publickey)` — breaking the PTY shell **and** the app window,
since the app registry is read over the same SSH chain.

---

## 8. Credential independence — Orbital no longer uses pasted AWS keys

The master carries instance profile **`OrbitalFleetAutoscaler`**, created 2026-08-13.
`/home/ops/.aws/credentials` was moved aside — a credentials *file* outranks IMDS in the
AWS chain, so it had to go or the expiry problem would persist.

Verified **after** the federated token expired: `ubuntu` got `ExpiredToken` while `ops`
worked — `RunInstances` / `Terminate` / `Stop` allowed, untagged production instances
denied, quota readable (3,264 on-demand vCPUs).

- **Blast radius is the tag `ManagedBy=orbital-autoscaler`.** `fleet.py`'s
  `FLEET_TAG_KEY/VALUE` must match the policy or every launch fails
  `UnauthorizedOperation`. `CreateTags` is restricted to `ec2:CreateAction=RunInstances`,
  so the role cannot tag an existing instance and then kill it. An explicit `Deny`
  protects the master. No `iam:PassRole`.
- **The role does not need `ec2:ModifyVolume`** and was not granted it. New workers are
  born at the right throughput via `BlockDeviceMappings` (§4.2); the two live volumes
  were a one-time human-run change.
- `scale_up()` takes `purchasing` (spot | on-demand) and `lifetime_minutes`, which arms
  `shutdown -h +N` with terminate-on-shutdown — a self-destruct that depends on no
  scheduler, no Orbital, and no live credential. EventBridge Scheduler is denied to this
  account, so that in-instance timer is the only workable "stop at time T".

Policy JSON and revocation steps: `ops-server/docs/iam-fleet-autoscaler.md`, `docs/iam/`.

> **Flag for EDE (Dynatrace IT):** the federated role `dtRoleRegionsAdvancedUser` **can
> create IAM roles and instance profiles** — verified with a probe role, then deleted.
> That is not implied by the role's name. It also cannot read Service Quotas.

---

## 9. Open items

| # | Item | Why it matters |
|---|---|---|
| 0 | **Fix the stuck `docker wait` (§6b)** | The only item here that can strand a worker silently and indefinitely. Bounded wait, keyed on container ID. |
| 1 | **Cap `WORKER_CAPACITY` per worker** to the measured number | Workers still advertise 30. Nothing stops a 25-learner workshop filling one box and failing for all 25. **This is a policy call, deliberately not made unilaterally.** |
| 2 | Bake the ActiveGate image into the slot image | It is the component every failing session was stuck on. Removes it from the disk burst entirely. |
| 3 | Stagger installs 30–60 s | Simultaneity is the enemy; the gate is per-session, not per-cohort. |
| 4 | **Raise IOPS 3,000 → 5,000 and re-run 30** | The measured next ceiling (§5.4). ~$10/mo per worker. **Blocked until ~18:00 UTC** by the gp3 modification cooldown. |
| 5 | **Test the $0 lever first: ActiveGate probe thresholds** | The failure is a probe killing a JVM that is merely slow (§5.2). May recover most of the gap for free. |
| 6 | Verify the built app bundle is tenant-independent | Would let `DEPLOY_UPLOAD_CONCURRENCY` rise. |
| 7 | Phase 5 — Singapore (ap-southeast-1) AMI copy | Regional cells; note that APAC workers *worsen* keystroke latency (~680 ms vs 340) because the PTY SSHes from the eu-west-2 master. |
| 8 | Pre-existing red test `test_delete_only_before_started` | Fails on `main` independently of this work. |

---

## 10. Things worth not relearning

- **Warm-up ≈ 20 s/slot** (6 → 2m09s, 12 → 4m35s, 30 → 10m19s). The bottleneck is piping
  the test image into each slot's inner dockerd, not launch speed. **Pre-warm beats
  faster instances.**
- **Live migration between spot instances is not possible.** CRIU needs `--userns=host`
  plus seccomp/apparmor unconfined, which contradicts Sysbox's isolation model; nested
  dockerd+k3d mount trees are CRIU's documented failure case. Use rebuild-on-interrupt
  via `resume_step` + `LAB_SOLUTION` replay (~3 min) instead.
- **Workers are overlayfs on ext4**, so `--storage-opt size=` (disk quota) is unavailable;
  it needs XFS with prjquota. Memory/CPU/PID limits work via cgroup v2 — shipped behind
  `WORKER_SLOT_LIMITS=1`, using `--cpu-shares` **not** `--cpus` (a hard quota throttles
  the k3d startup burst).
- `queue:direct:{worker_id}` pins jobs to one worker — the lever that makes single-box
  load tests possible.
- Owner gate for fleet actions: `OPS_FLEET_OWNERS` in `/home/ops/.env`.

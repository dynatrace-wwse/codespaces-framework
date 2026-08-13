# Capacity measurement tools

The scripts that produced the 2026-08-13 capacity numbers. They live here because
the numbers in `dashboard/fleet_policy.py` are only defensible if the measurement
can be repeated — every constant in that file was wrong at least once, and each
time it was a load test that caught it.

Full write-up: `~/vault/enablement-framework/REPORT_2026-08-13_FLEET_CAPACITY_AND_DISK.md`

## The method, in one paragraph

Do **not** score a capacity run pass/fail. The framework's `dynatraceDeployOperator`
ends in `waitForAllPods`, which retries **60 times at 10 s** — a 600 s gate. Read
*how many of the 60 retries the worst session consumed* and pass/fail becomes a
continuous measure: two runs give you a line, and you can predict the ceiling
instead of bisecting toward it. Measured on m6a.4xlarge, retries ≈ `3N − 7`.

And do **not** trust `rc=0`. `deployApplicationMonitoring` finishes with
`waitForAllReadyPods`, which only waits on pods that **already exist** — a
component not yet created is invisible to it. Verify from the DynaKube's own
`.status.phase` and `.status.conditions`.

## Scripts

| Script | What it does |
|---|---|
| `fire_simultaneous_installs.sh [outdir]` | Fires the operator + app-monitoring install in **every occupied slot at the same instant**. Skips warm-but-empty slots, which would return instantly and inflate the pass rate. Prints pass count, mean and max. |
| `verify_dynakube_health.sh [outdir]` | Per-session verdict from the DynaKube's own status — phase, all ~25 conditions, pod readiness, log-monitoring presence. jsonpath only (a nested Python heredoc mangles `phase` through a doubled `docker exec`). |
| `sample_pressure.sh [logfile]` | Samples `/proc/pressure/{cpu,io,memory}` and memory during a run. PSI is what separates a disk stall from a CPU stall. |

Run them **on the worker**, as they shell into the slots directly:

```bash
scp ops-server/tools/capacity/*.sh autonomous-enablements-worker:/tmp/
ssh autonomous-enablements-worker '/tmp/sample_pressure.sh /tmp/psi.log &
                                    /tmp/fire_simultaneous_installs.sh /tmp/lab30'
ssh autonomous-enablements-worker '/tmp/verify_dynakube_health.sh /tmp/vdk'
```

Pin a run to one worker by draining the other — that is also a live test of the
drain cordon, which was a **no-op** until 2026-08-12:

```bash
redis-cli -a "$REDIS_PASSWORD" hset worker:worker-x86_64-amd002 draining 1
# ... run ...
redis-cli -a "$REDIS_PASSWORD" hset worker:worker-x86_64-amd002 draining 0
```

## Two traps that cost hours

- **Disk throughput sampling:** `iostat -x` field order is
  `Device r/s rkB/s rrqm/s %rrqm r_await rareq-sz w/s wkB/s …` — total MB/s is
  `($3+$9)/1024`. Getting the indices wrong silently reports a plausible-looking
  fraction of the truth.
- **`bootcamp_loadtest.py` state file:** it reads a legacy shared
  `/tmp/bootcamp_loadtest_state.json` as a fallback. A stale one owned by another
  user makes every bot print "already tracked", provision nothing, and fail
  minutes later looking exactly like a capacity failure. Fixed in `133cce3`
  (reconciles against Orbital), but check it if a run does nothing.

## ⚠️ Mass teardown can strand the worker

Terminating many sessions at once has twice wedged Docker's container reaping,
leaving jobs blocked in `docker wait` forever — the worker then advertises 0 free
slots while holding healthy ones, and only `systemctl restart ops-worker-agent`
clears it. The recovery restart can *itself* come back short (18/30) and still log
`Worker fully warm`. Check after every run:

```bash
ssh <worker> 'ps -ef | grep -c "[d]ocker wait"'          # expect 0
redis-cli -a "$REDIS_PASSWORD" --scan --pattern "job:running:*" | grep -c .   # expect 0
redis-cli -a "$REDIS_PASSWORD" hmget worker:<id> slots_ready slots_total      # expect equal
```

See `docs/EPIC-slot-lifecycle-and-repo-profiles.md` — fixing this is step 1 of that epic.

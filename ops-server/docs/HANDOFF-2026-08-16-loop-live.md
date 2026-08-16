# HANDOFF — 2026-08-16 — The control loop is live, and Astroshop finally runs

Follows `HANDOFF-2026-08-14-workshop-pools.md`, which introduced the unit model. This
one closes its "still owed" list.

| | |
|---|---|
| **Branch** | `epic/workshop-pools-and-autoscale` |
| **Tests** | 868 Python + 998 app, green |
| **Deployed** | master + amd001 + amd002, all on the same commit |
| **App** | **1.0.329**, deployed SRO + COE + sprint, `credential: oauth` on all three |
| **`CONTROL_LOOP_APPLY`** | **1** — the loop may now launch and terminate machines |

---

## 1. Astroshop had never bootstrapped. Now it does.

`post-create.sh` gates the whole workshop on `DT_API_TOKEN` **and**
`DT_PLATFORM_TOKEN`, and nothing minted either. Every session ever delivered was an
empty dev container: k3d up, no demo, no error, board healthy.

Fixing it needed a `kind` on `TokenSpec`, because this workshop needs **two token
families in the same session** — classic `dt0c01` for the SDLC event helpers and the
credential vault, gen3 `dt0s16` for monaco and dtctl, whose platform APIs refuse a
classic Api-Token whatever its scopes.

| Repo | Change | State |
|---|---|---|
| `demo-astroshop-problems` | `.devcontainer/yaml/dt-tokens.yaml`, all 4 tokens | **#39 merged** |
| `codespaces-framework` | `kind`/`aliases`, gen3 minting, prefix-routed revoke, `GET /api/arena/trainings/{id}/token-specs` | this branch |
| `dynatrace-app-enablements` | provision asks Orbital for the repo's specs; mints mixed sets | **#79 merged, 1.0.329** |

**The app had no way to ask what a repo declared** — that was the real gap. Orbital
honoured `dt-tokens.yaml` only on the scripted path, which no learner uses.

Proof, from inside a live session:

```
DT_OPERATOR_TOKEN=<classic>    DT_API_TOKEN=<classic>
DT_INGEST_TOKEN=<classic>      DT_BIZEVENTS_TOKEN=<same value as DT_API_TOKEN>   ← aliased
DT_PLATFORM_TOKEN=<platform>   ← a different token FAMILY, in the same session
```

(Values redacted. The point is the shape: three classic `dt0c01` tokens and one
gen3 `dt0s16` alongside them, which is what the gate needs and what no single
minting path could produce before.)

`platform-token:tokens:write` **is now granted** on the SRO account client (it 400'd
on 08-14; this was recorded as blocked on an account admin). `environment-api:api-tokens:write`
still 400s and does not need to.

### Two traps worth keeping

* **A classic API drops unrecognised scopes rather than rejecting them.** Sending gen3
  scope names to `createApiToken` yields a token that authenticates and can do
  nothing — worse than the bug being fixed. `kind: platform` therefore skips classic
  routing entirely instead of being translated.
* **A 201 is not evidence.** Effective permissions are `scopes ∩ the OWNER's IAM
  policy`. Both halves were probed against endpoints matching their *granted* scopes.

---

## 2. Astroshop costs 4 units, not 3 — and the slot cap was nearly too small

First measurement that means anything (every earlier one described an empty container
at 495 MiB / 1.4% CPU):

| | |
|---|---|
| per session, plateaued | **7,158 MiB** |
| units | **4** → **5 seats** on an m6a.4xlarge (k8s-101 gets 20) |
| published | `repo:units demo-astroshop-problems` — live, no deploy |

The unprofiled default of 3 was **under**-estimating, so the fail-safe was not as safe
as assumed. Worth remembering: an unmeasured training's default is a guess, not a bound.

**`WORKER_SLOT_MEMORY_MB` 8192 → 20480.** 8192 was sized from Astroshop's *declared*
6,320 MiB of pod limits; declared limits omit k3d itself, the GitLab install and the
load generator. A correct session ran at **87%** of its cap. The cap is a limit, not a
reservation — what prevents overcommit is the unit model.

`bootstrapWorkshop` takes 20–25 minutes, and **two concurrent bootstraps peg a 16-vCPU
m6a.4xlarge at 100% CPU**. That is a start-rate constraint, which the pacer owns — not
a holding constraint. It is the clearest evidence yet for keeping the two separate.

---

## 3. Two APPLY-only failure modes, found before turning the loop on

Both were invisible in dry run, because dry run has no state transition to oscillate
between.

### The loop would have launched and terminated the same machine forever

`due_for_prewarm` was unbounded on the late side, so a trainer opening the room late
still gets machines. The consequence: for any workshop nobody ended, prewarm AND
teardown were **both** true, and the loop evaluates prewarm first. Launch → tear down
for being past the window → launch again, forever, for a workshop nobody attends.

Found with a real one — **`ws_mss02l80-8c310e` "K8s 101 - BC", opened 2026-08-13,
still `running`**, which the loop had been asking to prewarm every 30 seconds for
three days. Left in place: it is another trainer's record and the fix makes it inert.

Fixed by making the two predicates mutually exclusive. Costs the late trainer nothing —
teardown is not due until scheduled end *plus* grace, so a room can still open 2h19m late.

### Every worker restart would have bought a spare instance

A warming worker reports 0 free seats, so a restarting pool looks exactly like a full
one. Seen live while deploying: `would scale_up=1 — 0 free seats < 4`, with both
workers mid-restart and **nothing queued**. A fleet-wide deploy would have bought one
machine per worker, each arriving about when it stopped being needed.

The guard is narrow on purpose: wait only when the returning seats would **cover** the
shortfall. A warming worker does not excuse a pool that will still be short.

> **Generalise both:** any two predicates that drive opposite actions on the same
> object must be checked for mutual exclusivity, not just individually.

---

## 4. Deploy trap that cost real time

**Both workers were six commits behind** — including `0ed9560`, the reaper fix — while
every command in the documented deploy loop returned success.

A worker's clone tracks only `main`, so `git fetch origin` leaves `origin/<branch>` at
whatever it last saw, and the follow-up `reset`/`merge` reports *"Already up to date"*
against a stale ref. Deploying a branch needs an explicit refspec:

```bash
fetch origin refs/heads/$B:refs/remotes/origin/$B
```

Documented in `ops-server/CLAUDE.md`. **Always print the three HEADs — "up to date" is
not evidence.** (In zsh, write `${B}:refs/...`; `$B:r` is a modifier and silently eats
the `r`.)

---

## Still owed

1. **The reaper under real load.** Still the biggest unknown. `0ed9560` keys the wait
   on the container **id** and bounds it, but the failure it fixes
   (`docker wait` hung forever after a mass teardown, worker advertising 0 free seats
   while holding 20 idle ones) has not been reproduced against the fix at scale.
2. **Merge to main.** Autoscaled workers sync from `main` at boot, so until then a
   machine the loop launches itself comes up **without** any of this.
3. **Volume IOPS 3,000 → 6,000** (~$15/month/worker). At 3,000 the ceiling is ~18
   concurrent installs and the drip is what keeps us under it.
4. **A stuck-warming worker now blocks scale-up** by design. If the "came back short"
   bug (`SysboxPool: 18/30 slots ready` reported as *fully warm*) recurs, the pool will
   wait on seats that never arrive. Worth a bounded timeout on that guard.

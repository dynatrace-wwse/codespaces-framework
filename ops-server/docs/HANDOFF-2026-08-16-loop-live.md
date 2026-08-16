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

## 4b. A 31-seat workshop, delivered without anyone clicking anything

The run the whole epic was for, on `CONTROL_LOOP_APPLY=1`:

| Step | What happened |
|---|---|
| sizing | `31 seats ÷ 20/worker` → **2 × m6a.4xlarge**, from the unit table |
| launch | both started, tagged `orbital-pool=ws-…`, bound to a private queue |
| readiness | loop waited for **20/20 slots on each**, then flipped the record to `ready` |
| admission | 26 of 30 parked behind the pacer on `queue:pool:ws-…` |
| placement | **15 / 15** across the two machines |
| came up | **30/30 in 524 s** |
| isolation | `pools ['ws-ws_msvssoab-ba0af8']` — the daily pool never moved off 20/20 free |
| teardown | both instances terminated, both worker records dropped, 60 tokens revoked |

**5/5 assertions.** Three earlier 12-seat runs on the daily pool came up in 171 s, 172 s
and 161 s. Consistency was the ask, not density.

The launched machines ran this branch, not `main`, via `WORKER_CODE_BRANCH` — which is
what that override is for and the only reason this could be proven before merging.

---

## 4c. The reaper, at 30 seats, on workers that survive the teardown

**A dedicated pool cannot test this.** Its machines are terminated when the workshop
ends, so a hung `docker wait` there is harmless by construction. The failure that
bricked amd001 on 2026-08-13 — 17 `docker wait` processes blocked for ever, the worker
advertising 0 free seats while holding 30 warm ones — only strands a **persistent**
worker. So this needed its own run on the daily pool.

30 seats, placed 17 / 13, torn down together:

| | |
|---|---|
| came up | 30/30 in 544 s |
| teardown | ~3 min, both workers back to **20/20 free** |
| stuck `docker wait` | **0** on both, during and after |
| `reaper_watching` | drained 6 → 0 and 3 → 0 |
| orphan `job:running` keys | 0 |
| tokens | 60 revoked |

The zero is structural, not lucky: `0ed9560` replaced the per-job blocking
`docker wait` with **one shared poller keyed on the container id**, so there are no
such processes left to hang. Slot names are recycled, which is why the id matters —
a wait keyed on a *name* can outlive its session and report a later learner's exit.

(The run was done with `CONTROL_LOOP_WORKSHOPS=none` so the loop would not launch idle
workshop machines for a workshop whose seats were deliberately on daily. **That
narrowing has been removed**; the loop is back to `workshops=*`.)

---

## 5. Two more, found by things going wrong rather than by tests

### A dashboard restart cost 24 live tokens

Restarting `ops-dashboard` mid-run 502s the API for a few seconds and answers with
an nginx HTML page, which `.json()` raises on. That raise landed **inside** the load
test's teardown block, so the revoke below it never ran.

The tokens were classic, so no per-owner cap was at risk and they were recovered by
hand — but the same shape on a gen3 tenant is the 2026-08-08 outage. Two fixes: the
poll helpers retry (a poll loop that cannot tolerate one bad response is not a poll
loop, and Orbital restarts are a normal event), and the teardown **diagnostics** now
sit in their own `try` so a failure to *check* the fleet can never skip the *cleanup*
underneath it.

### The reconciler fix had a tail

Stopping `worker_id="queued"` from being read as an orphan was right — it was
deleting live learners' records mid-session. But `"queued"` was also the only thing
that ever cleaned up a learner who **never started at all**. Such a learner has no
environment to reap, so after the fix nothing would ever touch their record: five
survived an ended workshop, reading as running sessions with nothing behind them.

Ending the workshop now drops them, alongside their queued payloads — and **only** on
the explicit `"queued"` marker. An absent `worker_id` is merely *unknown*, and
deleting on unknown would take a live learner's record with it. That is the opposite
asymmetry to `_dead_worker_candidate`, which treats both as not-dead. In each case
the safe answer is the one that does less.

---

## 6. Security: what CodeQL and GitGuardian found

**CodeQL, 10 alerts**, all on code this branch added after #152 took the repo to zero.
The load-bearing one: two sites logged a **token endpoint's raw response body**. That
body is the only useful diagnostic on a 4xx — SSO hard-400s an unheld scope and leaves
`error_description` empty — and the same shape of body can carry an `access_token`.
`shared.log_safety.safe_error_detail` now parses, keeps a whitelist of error fields,
and reports anything else by its key names or its length, never its values.

Also a real **SSRF**: discovery builds a URL from a caller-supplied tenant and fetches
it from inside the ops server. A tenant of `http://169.254.169.254/` would have made
Orbital fetch its own instance credentials. `probeable_host()` requires https and a
Dynatrace host, parses **once**, and returns only a hostname — the scheme and path are
literals at the call site. It also drops userinfo: `https://user:pw@evil.example.com\
@sro97894.apps.dynatrace.com/` resolves to the legitimate tenant, and the rebuilt probe
carries neither the credentials nor the decoy host onto the wire.

What remains flagged is inherent to the features: discovery must fetch a caller-named
tenant, and a failed mint must be able to say something about the response. Both are
now mitigated rather than eliminated.

**GitGuardian** flags the literal `dt0s16.SOMETHING` in a test 38 commits back — the
prefix plus enough uppercase matches its Dynatrace pattern. Renamed at head, but GG
scans every commit in the PR, so clearing it needs a dismissal or a history rewrite.
(A separate finding — real, revoked token prefixes quoted in this very document — was
removed by squashing the commit that introduced them, so it is not recoverable from
history either.)

---

## Still owed

1. **Merge to main.** `fleet._build_user_data` syncs a launched worker to
   `origin/main`, and main's agent has **zero `WORKER_POOL` references** — so until
   this merges, a machine the loop launches itself comes up poolless and without the
   unit model. `WORKER_CODE_BRANCH` exists for exactly this and is currently set to
   the epic branch in `/home/ops/.env`: **remove that line once merged**, or launched
   workers will keep syncing a branch that is no longer where the work lands.
2. **Volume IOPS 3,000 → 6,000** (~$15/month/worker). At 3,000 the ceiling is ~18
   concurrent installs and the drip is what keeps us under it.
3. **A stuck-warming worker now blocks scale-up** by design. If the "came back short"
   bug (`SysboxPool: 18/30 slots ready` reported as *fully warm*) recurs, the pool will
   wait on seats that never arrive. Worth a bounded timeout on that guard.
4. **The two advisory security checks.** Neither is required (only
   `codespaces-integration-test-with-dynatrace-deployment` is), and both are described
   in §6 — the GitGuardian one needs a dashboard dismissal, the CodeQL ones are
   mitigated-not-eliminable.

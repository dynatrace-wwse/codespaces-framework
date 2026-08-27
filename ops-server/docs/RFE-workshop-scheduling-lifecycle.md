# RFE — Workshop scheduling, environment lifetime, and end-of-life

**Status:** proposed · **Raised:** 2026-08-19 · **Author:** Sergio Hinojosa
**Components:** `ops-server/dashboard/workshop_fleet.py`, `ops-server/dashboard/app.py`,
`dynatrace-app-enablements` (workshop form, workshop views)
**Trigger:** APAC Bootcamp `ws_msz3k831-10139c`, 2026-08-19

---

## 0. Why this exists

The APAC bootcamp ran on 2026-08-19. Three things happened that nobody could
explain from the UI, and all three come from the same root: **the workshop's
schedule is described by five independent clocks, only two of which the trainer
can see, and none of which are anchored to the moment the workshop actually
started.**

What the trainer saw:

| Time (UTC) | What happened |
|---|---|
| 05:00 | control loop launched 4 × `m6a.4xlarge` (prewarm, 60 min lead) |
| 06:24 | 6 more launched — 10 machines, 72 seats planned |
| 06:00 | `scheduledAt` — the *booked* start |
| **07:31:56** | **`startedAt` — the trainer actually pressed Start, 1 h 32 late** |
| 07:31–08:20 | ~44 learners provisioned environments |
| 08:25:17 | a trainer pressed **Terminate all environments** → 41 killed in 47 s |
| 08:29–09:38 | 14 learners self-reprovisioned; each got a **fresh 2 h** TTL |
| 09:30 | the expiry reaper killed exactly **one** environment |
| **10:00** | **fleet teardown due** — `scheduledAt + 240`, *not* `startedAt + 240` |
| ≤10:15 | hard teardown after the busy-worker deferral expires |

The class was given a 4-hour hold. It received **2 h 28 of machine time after
the real start**, because the hold is measured from the time the workshop was
*booked* for. Separately, every learner environment carried a flat 2 h TTL that
has nothing to do with the 4 h hold the trainer configured — so even a fleet
that lived to 10:00 would have started killing individual environments at 09:31.

Nothing warned anyone. No message, no banner, no auto-close. Machines simply go
away.

This RFE specifies (a) the complete current behaviour, (b) the user stories that
should replace it, and (c) exactly how each one is verified.

---

## 1. Current state — complete analysis

### 1.1 The five clocks

A workshop is governed by five independent timers. They are computed in
different modules, anchored to different instants, and **none of them is
reconciled against the others**.

| # | Clock | Computed in | Anchor | Governs | Visible to trainer |
|---|---|---|---|---|---|
| 1 | `prewarm_at` | `workshop_fleet.py:252` | `scheduledAt` | when EC2 machines are launched | yes — fleet banner |
| 2 | `teardown_at` | `workshop_fleet.py:260` | `scheduledAt` | when EC2 machines are destroyed | yes — fleet banner |
| 3 | `expires_at` | `app.py:4752` | **each learner's own provision** | when ONE environment is killed | no |
| 4 | in-instance `shutdown -h +N` | `workshop_fleet.py:328` | boot | backstop if the loop dies | no |
| 5 | `workshop_end` | `workshop_fleet.py:354` | `scheduledAt` | "X min left" banner, and clock 3's intended input | partially |

Clocks 1, 2, 4 and 5 are anchored on **`scheduledAt`**. Clock 3 is anchored on
each learner's own `provision` call. `startedAt` — the only instant a human in
the room would call "the start" — **anchors nothing**.

### 1.2 Every variable, and where it comes from

#### Set by the trainer, in the app's create/edit form

`ui/app/training/utils/workshopForm.ts:57-72` defines the entire form model.

| Field | Form label | Default | Cap | Anchor | Reaches |
|---|---|---|---|---|---|
| `scheduledAt` | Date + Time + Timezone → `toUtcIso()` | — | must be future | — | every clock |
| `timezone` | Timezone select | browser zone | valid IANA | — | display + wall-clock conversion only |
| `maxSeats` | Seats | 10 | 200 (`live_sessions.MAX_SEATS`) | — | `planned_seats()` → machine count |
| `prewarmLeadMinutes` | **"Provision before start (min)"** | 45 | 360 | `scheduledAt` | `prewarm_at` |
| `holdMinutes` | **"Hold after start (min)"** | 240 | 1440 | **`scheduledAt`** | `teardown_at` floor |

#### Not set anywhere — but read everywhere

| Field | Where it is read | Value in practice |
|---|---|---|
| `durationMinutes` | `workshop_end()`, `workshop_session_hours()`, `WorkshopTimeBanner` | **always absent** |

`durationMinutes` has full plumbing — `api/orbital.function.ts:454` and `:571`
forward it *if present*, `live_sessions._INT_FIELDS` validates it, the PATCH
route accepts it — but **no UI writes it**. `WorkshopFormInput` has no duration
field, and neither create (`LiveSessionsAdmin.tsx:658`) nor edit (`:646`) sends
one.

The team knew. `workshop_fleet.py:87-90` says so out loud:

> *"It exists because the app's create form does not send `durationMinutes` at
> all, so every workshop it creates falls back to the server's 120 and would
> have lost its machines 2h30 after the start."*

`WORKSHOP_HOLD_MINUTES = 240` was introduced as a **workaround for the missing
field**, rather than removing the field. Both now coexist, and the phantom
still has teeth (see D3).

#### Server tunables

| Constant | File:line | Default | Meaning |
|---|---|---|---|
| `PREWARM_LEAD_MINUTES` | `workshop_fleet.py:77` | 45 | fallback lead |
| `LEAD_MINUTES_CAP` | `:97` | 360 | clamped **on read** |
| `WORKSHOP_HOLD_MINUTES` | `:91` | 240 | fallback hold floor |
| `HOLD_MINUTES_CAP` | `:98` | 1440 | clamped **on read** |
| `TEARDOWN_GRACE_MINUTES` | `:81` | 30 | added to `workshop_end` |
| `TEARDOWN_DEFER_MAX_MINUTES` | `:171` | 15 | how long a busy worker delays teardown |
| `WORKSHOP_WARMING_TIMEOUT_MINUTES` | `:166` | 20 | `warming` → `ready (degraded)` |
| `WORKSHOP_LIFETIME_MARGIN_MINUTES` | `:160` | 60 | margin on the in-instance backstop |
| `CONTROL_TICK_S` | `:99` | 30 | control loop period |
| `WORKSHOP_STANDING_MAX_SEATS` | — | 7 | at or below, launch nothing |
| `WORKSHOP_REDUNDANCY` | — | 0 | spare machines (deliberately none) |
| `ORBITAL_SESSION_HOURS` | `app.py:4734` (env) | **2** | flat environment TTL |
| `MAX_SESSION_HOURS` | `app.py:4570` | 12 | ceiling on any requested TTL |
| `WORKSHOP_ENV_GRACE_HOURS` | `app.py:4576` | 2 | added to `durationMinutes`→hours |
| `CONTROL_LOOP_APPLY` | `:113` | **1 in prod** | loop actually launches/terminates |

### 1.3 The complete flow, phase by phase

#### Phase 1 — Create

1. Trainer fills the form. `validateWorkshopForm()` requires a **future**
   `scheduledAt`, 1–200 seats, and (optionally) the two window values, each
   1..cap.
2. `liveSessionsService.create()` → app function `orbital.function.ts` →
   `POST /api/live/sessions`. The owning tenant is stamped **server-side**
   (`ownTenant()`), never client-supplied.
3. Orbital validates via `live_sessions.validate_create` + `validate_schedule`,
   writes hash `live:session:{id}`, roster set, and adds to
   `live:sessions:index` (zset by `createdAt`).
4. State = `scheduled` (a workshop created with `scheduledAt`) or `open`.

**No machines exist yet. `durationMinutes` was never written.**

#### Phase 2 — Prewarm

Every `CONTROL_TICK_S` (30 s), `workshop_fleet.tick()`:

1. Reads `live:sessions:index` (ZREVRANGE). *A failed read yields `[]` and
   `index_ok = False` — deliberately fail-safe: the orphan reaper then reaps
   nothing.*
2. For each workshop, loads the hash and the fleet record `workshop:fleet[{id}]`.
3. `should_prewarm(state, session, now)` → `due_for_prewarm`:
   `prewarm_at <= now < teardown_at`. **Prewarm and teardown are mutually
   exclusive by construction** — an earlier unbounded version made both true and
   the loop oscillated launch/terminate every 30 s for three days.
4. `provision_workshop_fleet()`:
   - `planned_seats()` = **`maxSeats` + the trainer team** — the capacity
     *booked*, never the roster. Learners join with a code and never touch a
     roster, so roster-based sizing returned 1 for a 40-seat class.
   - `plan_workshop_capacity()` = `ceil(seats / units_per_worker)` from
     `shared/capacity_units.py`, `WORKSHOP_REDUNDANCY = 0`.
   - ≤ `WORKSHOP_STANDING_MAX_SEATS` (7) → **standing lane, launch nothing**.
   - otherwise launch into pool `ws-{workshopId}`, on-demand (never spot —
     a spot reclaim costs a learner their session with two minutes' notice).
   - each machine is armed with `shutdown -h +N`, N = `_workshop_lifetime_minutes`
     = `(teardown_at − prewarm_at) + 60`. **Derived, never recomputed** — a
     recomputed value once fired *before* the loop's own teardown.
   - fleet record state → `warming`.

For this incident: `prewarm_at` = 06:00 − 60 = **05:00Z**, 72 seats → 6 workers
(10 instances were requested across two launch batches).

#### Phase 3 — Ready

- `warming` → `ready` as soon as `_pool_workers_ready()` reports any worker up.
- If still not warm after `WORKSHOP_WARMING_TIMEOUT_MINUTES` (20), the loop
  **delivers degraded** and says so at ERROR level: partial capacity beats a
  room that never opens.
- A `ready` workshop still on the standing lane is **upgraded** if
  `needs_bigger_fleet` (booked capacity grew past 7). Never downgraded.

#### Phase 4 — Open room, Start

Two independent gates:

- **Open Classroom** (`roomOpen=1`) → chat, board, raise-hand.
- **Start** (`state: scheduled|open → running`, sets `startedAt`) → environment
  and lab steps.

`startedAt` is recorded and **used only for display** (`WorkshopTimeBanner`).
No scheduling decision reads it.

#### Phase 5 — Environments

Three entry points, all converging on the same provision handler:

- trainer **Provision all** (`app.py:6747`),
- trainer **per-learner reprovision** (`app.py:7231`),
- learner self-provision through the pull channel when they open the app.

All three compute lifetime identically (`app.py:4734-4752`):

```python
session_hours = int(os.environ.get("ORBITAL_SESSION_HOURS", "2"))
if getattr(body, "sessionHours", 0):
    session_hours = max(session_hours, min(int(body.sessionHours), MAX_SESSION_HOURS))
if ws_id:
    ws_session = await pool.hgetall(ws_key) or {}
    if ws_session and not getattr(body, "sessionHours", 0):
        ws_hours = workshop_session_hours(ws_session)   # ← reads durationMinutes
        if ws_hours:
            session_hours = max(session_hours, ws_hours)
expires_at = now + timedelta(hours=session_hours)
```

and `workshop_session_hours` (`app.py:4579`):

```python
minutes = int(str((session or {}).get("durationMinutes") or "").strip() or 0)
if minutes <= 0:
    return 0            # ← ALWAYS, for every app-created workshop
```

**Nothing on the app side ever sends `sessionHours`.** So every environment in
every workshop gets exactly 2 h. That 2 h also sets:

- the minted DT token lifetime (`app.py:4792`, `expires_in_hours=session_hours`),
- the `job:running:{id}` Redis key TTL (`app.py:4905`).

`holdMinutes` — the field the trainer actually configured — is **never read on
this path**.

#### Phase 6 — Environment death (three independent ways)

1. **Expiry reaper** (`workers/manager.py:585`) — every `RECONCILE_INTERVAL`,
   scans `job:running:*`, flags `terminating=1` on anything past `expires_at`.
   The owning worker's reconciler force-kills the Sysbox container.
   *Fired once on 2026-08-19.*
2. **Trainer terminate-all** (`app.py:7037`) — matches on
   `(arena_user, training_id)` against roster ∪ trainer. Idempotent.
   *Fired once on 2026-08-19 and killed 41.*
3. **Fleet teardown** — the machine goes, so everything on it goes with it.

#### Phase 7 — Teardown

`due_for_teardown` (`workshop_fleet.py:365`):

```python
if session.get("state") in ("ended", "cancelled", "deleted"):
    return True                       # explicit end always wins
due = teardown_at(session, grace_minutes=grace_minutes)
return due is not None and now >= due
```

`teardown_at` (`:260`):

```python
start = parse_iso(session.get("scheduledAt", ""))       # ← the defect
end   = workshop_end(session)                           # start + (durationMinutes or 120)
by_duration = end + timedelta(minutes=grace_minutes)    # start + 150 by default
by_hold     = start + timedelta(minutes=session_hold_minutes(session))
return max(by_duration, by_hold)
```

`teardown_workshop_fleet` then:

- terminates every session in the pool,
- **defers** if any pool worker still reports active jobs — bounded by
  `TEARDOWN_DEFER_MAX_MINUTES` (15), record parked in `draining` so the next
  tick re-enters. *An earlier version fell through to `done`, which the function
  returns from immediately — "wait and retry" silently became "never" and three
  `m6a.4xlarge` outlived their workshop.*
- `fleet.scale_down()` (refuses anything not tagged `orbital-role=worker`),
- unbinds the pool, drops worker records, state → `done`.

`done` is **re-armable** — a rescheduled workshop prewarms again.

#### Phase 8 — Orphan reaper

A separate pass driven by the **fleet hash**, not the index, because deleting a
workshop removes it from the index and leaves its machines invisible. Two
fail-safe properties that must be preserved by any change here:

- a failed index read reaps **nothing** (an empty index makes every live
  workshop look abandoned);
- `session_exists` is **tri-state** — `None` means the check failed and is *not*
  evidence of deletion.

### 1.4 The two state machines

**Workshop** (`live_sessions.apply_transition`):

```
scheduled ──open──▶ open ──start──▶ running ──end──▶ ended
     └────────────── cancel ──────────────▶ cancelled
```

`end` (`app.py:6378`) is the only path that: freezes the board, stores the pad
export and completion record, **terminates every environment**
(`_stop_workshop_sessions`), emits `EVENT_ENDED`, applies a 7-day TTL to the
session keys.

**Fleet record** (`workshop:fleet[{id}]`):

```
(none) ──prewarm──▶ warming ──ready──▶ ready ──teardown──▶ draining ──▶ done
                                          │                              │
                                          └──── upgrade (standing) ──────┘
                              done is PREWARMABLE again (reschedule)
```

### 1.5 Defects

| ID | Severity | Defect |
|---|---|---|
| **D1** | **High** | `teardown_at` anchors the hold floor on `scheduledAt`, not `startedAt`. A workshop that starts late silently loses that much machine time. Cost on 2026-08-19: **1 h 32**. |
| **D2** | **High** | Environment TTL (`expires_at`) is a flat 2 h and ignores `holdMinutes` entirely. A trainer who configures a 4 h hold gets 2 h environments on a 4 h fleet — idle machines and dead clusters at the same time. |
| **D3** | Medium | `durationMinutes` is unsettable from the UI but defaults to **120** server-side (`workshop_end`). It can only ever *shorten* an explicit hold: `holdMinutes=60` still yields a 150-minute window. The helper text says *"A minimum"*, true upward, false downward. |
| **D4** | Medium | No warning before teardown. Machines and environments vanish with no message on any surface. |
| **D5** | Medium | A workshop nobody ends stays `running` forever. Its fleet is torn down on the clock, but the workshop record, the board and the completion record are never finalised — `_store_pad_export` / `_store_completion_record` only run on the `end` path. |
| **D6** | Low | `WorkshopTimeBanner` shows "X left of Y" only when `durationMinutes` is set (`:108`), so for every app-created workshop the trainer's remaining-time indicator is dead code. |
| **D7** | Low | `terminate-all` logs the count but **not the caller** (`app.py:7064`). 41 environments died on 2026-08-19 and Orbital cannot say who pressed the button. |

---

## 2. User stories

### US-1 — The window I configure is the window I get

> **As a trainer**, when I set "Hold after start", I want the machines held that
> long **after I actually start the workshop**, so a late start does not silently
> shorten my class.

**Acceptance criteria**

- **Given** a workshop with `scheduledAt = 06:00Z` and `holdMinutes = 240`,
  **when** it is still `scheduled` or `open`, **then**
  `teardown_at == 10:00Z` (anchored on `scheduledAt`, unchanged — there is no
  actual start yet).
- **Given** the same workshop, **when** the trainer starts it at `07:31Z`,
  **then** `teardown_at == 11:31Z` (anchored on `startedAt`).
- **Given** a trainer who starts *early*, at `05:50Z`, **then** `teardown_at`
  is `max(scheduledAt, startedAt) + hold == 10:00Z` — starting early must never
  shorten the window either.
- **Given** a workshop `running` for 20 h with `holdMinutes = 240`, **then** the
  window is still bounded: `startedAt + min(hold, HOLD_MINUTES_CAP)`. The anchor
  moves; the ceiling does not.
- **Invariant:** `due_for_prewarm` and `due_for_teardown` are never both true,
  at any anchor, at either ceiling.

### US-2 — My environment lives as long as the machines do

> **As a learner**, I want my environment to live for the whole workshop, so it
> does not die under me while the machines it runs on are still up and idle.

**Acceptance criteria**

- **Given** a workshop with `holdMinutes = 240` and no `durationMinutes`,
  **when** a learner provisions, **then** `expires_at` is derived from the
  workshop's own teardown point, not from the flat 2 h default.
- **Given** a learner who provisions 10 minutes before teardown, **then**
  `expires_at <= teardown_at` — an environment must never outlive its machine,
  and must never *claim* to.
- **Given** no workshop at all (self-paced `/sessions/:trainingId`), **then**
  the flat `ORBITAL_SESSION_HOURS` default still applies. This story changes
  workshop-bound provisioning only.
- **Given** a computed lifetime above `MAX_SESSION_HOURS` (12), **then** it is
  clamped, and the clamp is logged.
- Minted DT tokens (`app.py:4792`) and the `job:running` key TTL (`:4905`)
  follow the same value — one lifetime, three consumers.

### US-3 — I am told before my environment is taken away

> **As a learner**, I want a clear message a few minutes before my environment
> is destroyed, so I can save my work and know the workshop is finishing.

**Acceptance criteria**

- **Given** a workshop whose `teardown_at` is `T`, **when** the control loop
  ticks at or after `T − WARNING_LEAD_MINUTES` (default **5**), **then** exactly
  one system announcement is written to `live:session:{id}:broadcast`.
- The announcement names the wall-clock time in the workshop's own `timezone`,
  says what is ending, and says what to do:
  *"This workshop finishes at 11:31 (Atlantic/Canary). Your environment will be
  shut down in 5 minutes — save anything you need now."*
- **Idempotent.** A flag on the fleet record (`warned_at`) means a 30-second
  tick cannot emit ten copies. Re-arming after a reschedule clears it.
- The announcement is **attributed to the system**, not to a trainer, and is
  visually distinct from a trainer broadcast.
- It is visible to a learner **in the lab**, not only in the classroom tab —
  today `LiveTeachingBar` renders only when `roomOpen && !finished`
  (`LearnerWorkshopView.tsx:293`), so a closed room or a finished workshop
  silently swallows it. That gate must not apply to system warnings.
- An `EVENT_TEARDOWN_WARNED` audit entry is appended to
  `live:session:{id}:events`.
- **Given** the loop is in dry run (`CONTROL_LOOP_APPLY=0`), **then** the
  warning is logged and **not** sent.

### US-4 — A workshop nobody ends closes itself

> **As an operator**, I want Orbital to close a workshop that is still `running`
> when its machines are taken back, so the board, the completion record and the
> catalog agree with reality.

**Acceptance criteria**

- **Given** a workshop in state `running` (or `open`), **when** teardown
  executes, **then** Orbital applies the *same* end path a trainer would:
  `state → ended`, `endedAt` set, pad export stored, completion record stored,
  `EVENT_ENDED` emitted with `actor = "system"`, 7-day TTL applied.
- The end must be **factored out of the HTTP handler** (`api_live_session_end`)
  into a function the control loop can call. The loop must not call its own
  route over HTTP.
- **Given** a workshop already `ended` or `cancelled`, **then** the auto-close
  is a no-op (`apply_transition` raises; the loop treats that as success).
- **Given** the auto-close raises, **then** teardown still proceeds. A failed
  bookkeeping write must never leave machines running — same reasoning as
  `_stop_workshop_sessions`, which "never raises" by design.
- The trainer sees *"Ended automatically when the environment window closed"*,
  not a bare `ended`, so an auto-close is distinguishable from their own action.

### US-5 — I can see and move the window while the workshop runs

> **As a trainer**, when my class overruns, I want to extend the window from the
> app, so I do not need an operator with Redis access.

**Acceptance criteria**

- **Given** a `running` workshop, **when** the trainer opens the Fleet panel,
  **then** *Adjust* is available. Today `PATCH /api/live/sessions/{id}` 409s with
  *"a running workshop cannot be edited"* (`app.py:6392`), and the Fleet chip
  hides *Adjust* once running — so on 2026-08-19 there was **no** supported way
  to extend a live class.
- Editing while running is restricted to `holdMinutes` **only**. Title, roster,
  training and `scheduledAt` stay locked — the cohort has already acted on them.
- **Given** an extension, **then** teardown moves, the in-instance
  `shutdown -h +N` backstop is **re-armed** on every machine, and every live
  environment's `expires_at` is extended to match. An extension that moves only
  one of the three clocks is worse than none.
- **Given** a value above `HOLD_MINUTES_CAP`, **then** 400 with the cap named.
- **Given** the fleet is already `draining` or `done`, **then** 409 — extending
  past the point of no return must fail loudly, not appear to work.

### US-6 — The trainer can see the real remaining time

> **As a trainer**, I want the banner to show how long the environments have
> left, so I can pace the class against the truth.

**Acceptance criteria**

- The remaining-time banner is driven by **`teardown_at`** (from
  `GET /api/workshops/{id}/fleet`), not by `durationMinutes`.
- **Given** under 15 minutes remain, **then** the banner switches to a warning
  treatment and names the exact wall-clock end time.
- It renders for both trainer and learner.

### US-7 — Every destructive action names who did it

> **As an operator**, I want the audit trail to name the human behind a mass
> termination.

**Acceptance criteria**

- `terminate-all` logs `scrub_for_log(trainer)` alongside the count.
- An `EVENT_ENV_TERMINATED` audit entry records `actor` and the count.
- Applies equally to per-learner terminate and to auto-close (`actor=system`).

### US-8 — `durationMinutes` stops lying

> **As a maintainer**, I want one source of truth for "how long is this
> workshop", so a field nobody can set cannot shorten a field they can.

**Acceptance criteria** — pick **one**, do not keep both:

- **Option A (preferred): remove the phantom default.** `workshop_end()` returns
  `None` when `durationMinutes` is absent, so `teardown_at` reduces to the hold
  floor alone. A workshop that *does* carry a duration (API-created, imported)
  keeps today's `max()` behaviour.
- **Option B: surface it.** Add a "Planned duration" field to the form; keep
  `max(duration + grace, hold)`.
- Either way: **`holdMinutes = 60` must produce a 60-minute window**, not 150.

---

## 3. Design

### 3.1 Anchor — `effective_start()`

One new pure function; every clock reads it.

```python
def effective_start(session: dict):
    """The instant the workshop's window is measured from.

    `scheduledAt` before it starts (there is no actual start yet, and prewarm
    must happen against the booking). `max(scheduledAt, startedAt)` once it is
    running, so a late start moves the window and an early one never shortens it.
    """
    scheduled = parse_iso(session.get("scheduledAt", ""))
    started = parse_iso(session.get("startedAt", ""))
    if scheduled is None:
        return started
    if started is None or session.get("state") not in ("running",):
        return scheduled
    return max(scheduled, started)
```

- `prewarm_at` keeps using **`scheduledAt`** — unchanged, deliberately. Prewarm
  must fire before any start exists.
- `teardown_at` uses `effective_start()` for the hold floor.
- `workshop_end` uses `effective_start()` too, so the two terms of the `max()`
  share an origin.
- `_workshop_lifetime_minutes` keeps **deriving** from `teardown_at − prewarm_at`.
  It must be re-armed when the window moves (US-5).

**Oscillation check.** `due_for_prewarm` is bounded above by `teardown_at`.
Moving `teardown_at` *later* only widens the prewarm window, so the two
predicates stay mutually exclusive. The swept-timeline test (§4.2) pins this at
both ceilings and at both anchors.

### 3.2 Environment lifetime — derive it from the window

Replace `workshop_session_hours()` with a minutes-based function that prefers
the *fleet window* and keeps `durationMinutes` only as a legacy input:

```python
def workshop_session_minutes(session: dict, now=None) -> int:
    """Minutes an environment provisioned NOW should live, 0 = use the default.

    Bounded by the workshop's own teardown point: an environment must never
    outlive the machine under it, and must never advertise a lifetime the fleet
    will not honour.
    """
    due = workshop_fleet.teardown_at(session)
    if due is None:
        return 0
    remaining = int((due - (now or utcnow())).total_seconds() // 60)
    return max(0, min(remaining, MAX_SESSION_HOURS * 60))
```

Call sites (`app.py:4748`, `:6747`, `:7231`) take `max(default, computed)` as
today. Three consumers follow automatically: `expires_at`, the DT token
lifetime, the `job:running` key TTL.

> **Trap:** a learner who provisions 3 minutes before teardown gets a 3-minute
> environment. That is *correct* and *honest* — but the UI must say so rather
> than hand them a cluster that dies during `postCreate`. Below a floor
> (`MIN_USEFUL_SESSION_MINUTES`, ~15) the provision should be **refused** with
> "this workshop is finishing", not silently truncated.

### 3.3 The warning

Mechanism already exists — the broadcast stream. The loop XADDs directly
(server-side; the HTTP route's trainer gate does not apply):

```python
WARNING_LEAD_MINUTES = int(os.environ.get("TEARDOWN_WARNING_LEAD_MINUTES", "5"))

def due_for_warning(state, rec: dict, session: dict, now) -> bool:
    if state not in (READY, WARMING):
        return False
    if rec.get("warned_at"):
        return False
    due = teardown_at(session)
    return due is not None and now >= due - timedelta(minutes=WARNING_LEAD_MINUTES)
```

Placed in `tick()` **before** the teardown branch and **after** prewarm, as a
fourth predicate. Per the standing rule in this module, it is checked against
both existing predicates: it fires strictly inside the last
`WARNING_LEAD_MINUTES` of the prewarm window, sets a flag that makes it
one-shot, and takes no action on machines — so it cannot oscillate with either.

`warned_at` is cleared whenever the record re-arms (`done → warming`) and
whenever the window is extended (US-5), so an extended workshop is warned again
before its new end.

**Client side.** Add a `system: "1"` field to the broadcast XADD, shape it
through `live_pad.shape_broadcasts`, and render system broadcasts:

- outside the `roomOpen && !finished` gate in `LearnerWorkshopView.tsx:293`,
- in the trainer view,
- in the lab surface, where a learner deep in a step actually is.

### 3.4 Auto-close

Extract the body of `api_live_session_end` into:

```python
async def _end_workshop(session_id: str, session: dict, actor: str) -> bool:
    """The end path, callable without an HTTP request. Returns True if it moved."""
```

`api_live_session_end` becomes auth + 404 + a call to it.
`teardown_workshop_fleet` calls it with `actor="system"` **before** terminating
machines, wrapped so a failure logs loudly and never blocks teardown.

Add `endedBy` to the session hash (`"system"` | trainer email) so the UI can say
*"Ended automatically when the environment window closed."*

### 3.5 New configuration

| Name | Default | Meaning |
|---|---|---|
| `TEARDOWN_WARNING_LEAD_MINUTES` | 5 | how early the system announcement fires |
| `MIN_USEFUL_SESSION_MINUTES` | 15 | below this, refuse to provision into a finishing workshop |
| `WORKSHOP_AUTO_END` | 1 | auto-close a `running` workshop at teardown |

All three are env-overridable so a **compressed-clock rehearsal** (§4.6) can run
a whole delivery in minutes.

---

## 4. Verification

### 4.1 The meta-lesson — fixtures must be shaped like the UI's output

`test_workshop_fleet.py:19` builds every fixture as:

```python
def _session(start_offset_min=0, duration=120, state="scheduled"):
    return {"state": state,
            "scheduledAt": ...,
            "durationMinutes": str(duration)}     # ← the UI NEVER writes this
```

Every scheduling test in the suite passes a `durationMinutes` **that no workshop
created through the app has ever had**, and none passes a `startedAt`. The suite
is green and has been green throughout. It could not have caught D1, D2, D3 or
D6, because it never constructs the object the system actually stores.

**This is the first change to make, before any behaviour change:**

1. Add `_ui_session(**overrides)` — a fixture built from *exactly* the fields
   `LiveSessionsAdmin.tsx` sends: `state`, `scheduledAt`, `timezone`,
   `maxSeats`, `prewarmLeadMinutes`, `holdMinutes`, `trainers`, and — once
   running — `startedAt`. **No `durationMinutes`.**
2. Rename the existing helper `_legacy_session()` and keep it only for the
   API-created / imported path.
3. Add a **contract test** that fails if the two drift:

```python
def test_ui_fixture_matches_the_fields_the_app_actually_sends():
    """Guards the class of bug this whole RFE came from: a unit suite that
    tests an object the product cannot produce."""
    assert set(_ui_session()) == UI_WRITTEN_FIELDS
    assert "durationMinutes" not in _ui_session()
```

`UI_WRITTEN_FIELDS` is asserted against the app's own form model in §4.4, so
adding a form field without updating the server fixtures fails a test.

### 4.2 Unit tests — `dashboard/test_workshop_fleet.py`

Pure, no Redis, no AWS. Run:

```bash
/home/ops/ops-venv/bin/python -m pytest dashboard/test_workshop_fleet.py -q
```

**Anchor (US-1)**

| Test | Asserts |
|---|---|
| `test_teardown_anchors_on_scheduled_before_start` | `scheduled` state → `teardown_at == scheduledAt + hold` |
| `test_teardown_follows_a_late_start` | `startedAt = +92 min`, running → `teardown_at == startedAt + hold`. **The 2026-08-19 regression, pinned.** |
| `test_an_early_start_never_shortens_the_window` | `startedAt < scheduledAt` → anchor stays `scheduledAt` |
| `test_hold_ceiling_still_binds_at_the_new_anchor` | `holdMinutes = 99999` → clamped to `HOLD_MINUTES_CAP` from `startedAt` |
| `test_prewarm_still_anchors_on_scheduled` | moving `startedAt` never moves `prewarm_at` |
| `test_prewarm_and_teardown_never_both_due_swept` | sweep offsets −4320..+360 × {not started, started early, started late} × {lead, hold} at default **and** ceiling. Extends the existing oscillation guard to the new anchor. |
| `test_lifetime_backstop_still_derives_from_the_window` | `_workshop_lifetime_minutes == (teardown_at − prewarm_at) + margin` at every anchor — the invariant whose violation once killed machines mid-session |

**Duration (US-8, option A)**

| Test | Asserts |
|---|---|
| `test_a_ui_session_has_no_phantom_duration` | `workshop_end(_ui_session()) is None` |
| `test_a_short_hold_is_honoured` | `holdMinutes = 60` → window is 60 min, **not 150**. Pins D3. |
| `test_a_legacy_duration_still_wins_when_longer` | `_legacy_session(duration=360)` → `max()` behaviour preserved |

**Warning (US-3)**

| Test | Asserts |
|---|---|
| `test_warning_fires_exactly_one_lead_before_teardown` | true at `T−5`, false at `T−6` |
| `test_warning_is_one_shot` | `warned_at` set → false forever after |
| `test_warning_rearms_when_the_window_is_extended` | extend → `warned_at` cleared → fires again |
| `test_warning_does_not_fire_for_a_done_or_draining_fleet` | state gate |
| `test_warning_never_collides_with_prewarm_or_teardown` | the third-predicate rule from `CLAUDE.md`, applied |

**Lifetime (US-2)** — in `dashboard/test_workshop_env.py`:

| Test | Asserts |
|---|---|
| `test_env_lifetime_follows_the_workshop_window` | 4 h hold → ~4 h lifetime, not 2 h |
| `test_env_never_outlives_its_machine` | provision at `T−30` → `expires_at <= teardown_at` for a sweep of provision times |
| `test_env_lifetime_is_clamped_to_max_session_hours` | 24 h hold → 12 h |
| `test_self_paced_provision_keeps_the_flat_default` | no `workshopId` → `ORBITAL_SESSION_HOURS` |
| `test_provision_is_refused_below_the_useful_floor` | 3 min left → refuse with a named reason, do not truncate |

### 4.3 Integration tests — fake Redis, real handlers

Convention already in the repo (`test_live_room.py:35`, `test_live_learner_env.py:39`).
New file `dashboard/test_workshop_lifecycle.py`:

| Test | Asserts |
|---|---|
| `test_tick_warns_then_tears_down_in_order` | drive a fake clock across `T−6 → T−5 → T`: exactly one broadcast, then teardown. Order and count both pinned. |
| `test_tick_auto_ends_a_running_workshop` | after teardown: `state == ended`, `endedAt` set, `endedBy == "system"`, pad export + completion record written, `EVENT_ENDED` in the stream |
| `test_auto_end_is_a_noop_for_an_already_ended_workshop` | idempotent |
| `test_teardown_proceeds_when_auto_end_raises` | inject a failing `_end_workshop` → machines still terminated, error logged. **The rule that matters: bookkeeping must never hold machines.** |
| `test_extending_a_running_workshop_moves_all_three_clocks` | PATCH `holdMinutes` → `teardown_at`, every `expires_at`, and the re-armed `shutdown -h +N` all move |
| `test_extension_is_refused_once_draining` | 409 |
| `test_a_failed_index_read_still_reaps_nothing` | existing fail-safe preserved under the new predicate |
| `test_warning_is_not_sent_in_dry_run` | `CONTROL_LOOP_APPLY=0` → logged, not written |

### 4.4 App-side unit tests — `jest`

```bash
cd dynatrace-app-enablements && npx jest ui/app/training/utils ui/app/workshop
```

| Test | File | Asserts |
|---|---|---|
| `the create payload carries every field the server schedules from` | `workshopForm.test.ts` | payload keys == `UI_WRITTEN_FIELDS`. **The counterpart of §4.1's contract test — the pair is what stops the two sides drifting again.** |
| `the time banner counts down to teardown, not to durationMinutes` | `WorkshopTimeBanner.test.tsx` | remaining derived from `fleet.teardown_at` (US-6) |
| `a system broadcast renders with the room closed` | `LearnerWorkshopView.test.tsx` | the `roomOpen && !finished` gate does not suppress `system: "1"` |
| `a system broadcast renders inside the lab` | `WorkshopLabPanel.test.tsx` | reaches a learner mid-step |
| `Adjust is offered while running` | `FleetInfrastructureChip.test.tsx` | US-5 |

### 4.5 End-to-end — real browser, two tenants

Add `e2e/scenarios/09-teardown-warning-and-auto-close.mjs`. Picked up
automatically by `run.sh` in lexical order.

```
Setup (API, service bearer):
  create workshop, scheduledAt = now + 2 min, prewarmLeadMinutes = 2,
  holdMinutes = 6, maxSeats = 2
  server env for the run: TEARDOWN_WARNING_LEAD_MINUTES=1, CONTROL_TICK_S=10

Trainer (SRO)          Learner (sprint)
─────────────────────  ──────────────────────────────
open room
start workshop         join with code
                       provision environment
assert: expiresAt is within 60s of the fleet teardown_at   ← US-2
wait until T−1 min
                       assert: system announcement visible IN THE LAB TAB,
                               names the local wall-clock end time            ← US-3
                       assert: it is styled as a system message, not a
                               trainer broadcast
wait past T
assert: workshop state == ended, endedBy == system                            ← US-4
assert: the board is frozen and the completion record exists
assert: GET /api/workshops/{id}/fleet reports state done, no instances
assert: EC2 describe-instances shows every pool instance terminated
```

Second scenario, `10-extend-a-running-workshop.mjs`:

```
start a workshop with holdMinutes = 6
trainer opens Fleet → Adjust → 20                                             ← US-5
assert: teardown_at moved
assert: the learner's expiresAt moved with it
assert: at the ORIGINAL teardown time nothing was torn down
assert: the learner is warned again before the NEW end
```

Credentials come from the environment (`.env-qa`, `/home/ops/.env`) — never
hardcoded. See `e2e/README.md`.

### 4.6 Compressed-clock rehearsal (live, real EC2)

The E2E above uses the real control loop but a compressed clock. Run it against
a **narrowed** loop so it cannot touch a real cohort:

```bash
CONTROL_LOOP_WORKSHOPS=ws_<rehearsal-id> \
CONTROL_TICK_S=10 \
TEARDOWN_WARNING_LEAD_MINUTES=1 \
  sudo systemctl restart ops-dashboard
```

`CONTROL_LOOP_WORKSHOPS` exists for exactly this. Verify afterwards that
`aws ec2 describe-instances` shows the pool terminated and no orphan record
remains in `workshop:fleet`.

### 4.7 Regression guard — replay the decisions, not the code

Standing rule in `ops-server/CLAUDE.md`, and it applies here more than to any
previous change, because this one **moves an anchor**:

> Before restarting with a scheduling change, diff the DECISIONS, not the code.
> Replay old-vs-new `due_for_prewarm` / `due_for_teardown` over every indexed
> workshop and require 0 changes.

Ship a script `tools/replay_scheduling_decisions.py` that, for every workshop in
`live:sessions:index`, prints `(old_prewarm, new_prewarm, old_teardown,
new_teardown)` and exits non-zero on any difference for a workshop that is
**not** `running`. Differences for `running` workshops are the intended effect
of US-1 and must be listed explicitly for a human to approve before restart.

This is the only cheap check that proves the first tick after the restart will
not launch or kill anything unintended.

### 4.8 Manual acceptance — the incident, replayed

Re-run the exact 2026-08-19 shape as the sign-off:

| Step | Expected |
|---|---|
| workshop booked 06:00, hold 240, started 07:31 | `teardown_at == 11:31`, not 10:00 |
| learner provisions 07:35 | `expiresAt ≈ 11:31`, not 09:35 |
| learner reprovisions 09:21 | `expiresAt ≈ 11:31`, **not** 11:21 |
| 11:26 | system announcement on every learner surface |
| 11:31 | machines terminated, workshop `ended`, `endedBy: system`, board frozen |

---

## 5. Rollout

1. **Fixtures first** (§4.1). No behaviour change. Proves the suite can see the
   bugs.
2. **D3 / US-8 option A** — remove the phantom 120. Smallest change, immediately
   makes `holdMinutes` mean what the label says.
3. **D1 / US-1** — the anchor. Ship behind `WORKSHOP_ANCHOR_ON_START=1`, run
   §4.7 replay, enable, remove the flag one week later.
4. **D2 / US-2** — environment lifetime, with the `MIN_USEFUL_SESSION_MINUTES`
   refusal.
5. **D4 / US-3** — the warning. Server + all three client surfaces together;
   a warning that lands in only one surface is worse than none, because it
   trains people to trust a channel that does not always fire.
6. **D5 / US-4** — auto-close.
7. **US-5** — extend while running.
8. **D6 / US-6, D7 / US-7** — banner and audit. Independent, ship any time.

Steps 2–4 are the ones that would have changed the 2026-08-19 outcome. Steps 5–6
are what make the next one visible rather than mysterious.

## 6. Open questions

1. **Should a late start also move `prewarm_at`?** No — but it does mean a
   workshop started 3 h late has had machines idle for 3 h. Worth a separate
   *"start it or lose it"* nudge to the trainer at `scheduledAt + 30`.
2. **Should the warning be per-environment as well as per-workshop?** An
   environment reaped by TTL alone (not by teardown) gets no warning today. If
   US-2 lands, the two coincide and this stops mattering — until someone sets
   `sessionHours` explicitly.
3. **What does auto-close do to a workshop whose trainer is mid-sentence?** The
   window is a hard resource boundary; the answer is the warning plus US-5's
   extend. Confirm the extend affordance is discoverable enough that a trainer
   never learns about it from an incident.
4. **`WORKSHOP_AUTO_END` default.** Proposed `1`. A trainer who wants the board
   left open should end the workshop themselves, which already keeps the record
   for 7 days.

"""The control loop: workshop machines on a schedule, daily machines on demand.

Until now the autoscaler was four HTTP endpoints a human clicked -- plan,
apply, scale-down, reap. Nothing read a calendar and nothing watched
utilisation, so "the machines should be up before the workshop" and "scale when
a worker gets hot" were both unbuilt rather than merely unwired. This is that
loop.

TWO POOLS, TWO DIFFERENT CONTROL PROBLEMS
-----------------------------------------
They are not the same problem with different numbers, and treating them alike
is what makes fleets either wasteful or unreliable.

*Workshop* machines are **planned**. The roster size, the repo and the start
time are all known in advance, so the right control is a schedule: launch
enough capacity before the doors open, keep everything else off those machines,
and give it all back when the room empties. Reacting to load here would be
strictly worse -- by the time a workshop looks busy, seventy people are already
waiting.

*Daily* machines are **reactive**. Nobody files a ticket before starting a
self-service lab, so the control is a feedback loop on free seats.

WHY CPU IS NOT THE SCALE SIGNAL
-------------------------------
The instinct is to scale at 70% CPU. Measured, a *completely full* 30-seat
worker sits at about **24% CPU** -- 0.127 vCPU per session against 16 vCPUs. A
70% CPU trigger would therefore never fire for occupancy at all. It would fire
only during install bursts, which are transient and self-resolving, and would
add a machine five to ten minutes later, after the burst had passed, that could
not help the sessions already placed -- a Sysbox session cannot be moved.

Memory behaves the opposite way and makes an excellent signal. Thirty sessions
at 1,609 MiB plus host overhead is roughly 85% of a 64 GiB worker, so 70%
memory is reached at about **25 of 30 seats**: an early warning that arrives
while there is still time to act.

So the three signals do three different jobs:

===============  ===================================  ==========================
signal           meaning                              action
===============  ===================================  ==========================
free seats low   demand                               launch a worker
memory >= 70%    the repo profile is optimistic       launch AND shrink this
                                                      worker's advertised seats
cpu/io pressure  a transient burst                    stop admitting here;
                                                      do NOT scale
===============  ===================================  ==========================

The second row's second half matters most. If memory reaches 70% at twenty
seats on a box that advertised thirty, the profile is wrong, and adding a
machine while still selling the remaining ten seats overfills anyway. Shrinking
the advertisement is what makes the fleet self-correct when a profile is wrong
-- which, for every repo except Kubernetes-101, it currently is, because no
other repo has been measured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from dashboard import fleet_policy, live_sessions, pools, repo_profiles
from shared import capacity_units
from shared.log_safety import scrub_for_log

log = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────────────
# How far ahead of a workshop to start its machines. A worker takes ~5-10 min
# to boot and warm, so the default leaves real margin. Deliberately in MINUTES
# and env-configurable so a rehearsal can compress a whole delivery into a few
# minutes instead of waiting an hour to find out the loop works.
PREWARM_LEAD_MINUTES = int(os.environ.get("PREWARM_LEAD_MINUTES", "45"))
# Grace after a workshop's scheduled end before its machines are taken back.
# Overruns are normal; a trainer finishing ten minutes late must not have the
# room's environments deleted underneath them.
TEARDOWN_GRACE_MINUTES = int(os.environ.get("TEARDOWN_GRACE_MINUTES", "30"))
# Minimum time from a workshop's START that its machines are held, regardless of
# the booked duration. A FLOOR on top of duration+grace, never a replacement:
# `teardown_at` takes the later of the two, so a 6h workshop still gets its
# 6h30 rather than being truncated to 4h.
#
# It exists because the app's create form does not send durationMinutes at all,
# so every workshop it creates falls back to the server's 120 and would have
# lost its machines 2h30 after the start — long before a cohort that started
# late, or ran long, was finished with them.
WORKSHOP_HOLD_MINUTES = int(os.environ.get("WORKSHOP_HOLD_MINUTES", "240"))
# Ceilings on the PER-WORKSHOP overrides of the two windows above. Applied when
# the value is READ, not only when written: a value hand-edited into Redis, or
# stored before a ceiling was lowered, must not be able to hold a fleet for a
# week. Mirrors live_sessions.MAX_PREWARM_LEAD_MINUTES / MAX_HOLD_MINUTES, which
# reject the same values at the API boundary.
LEAD_MINUTES_CAP = 360     # 6h
HOLD_MINUTES_CAP = 1440    # 24h
CONTROL_TICK_S = float(os.environ.get("CONTROL_TICK_S", "30"))
CONTROL_LOOP_ENABLED = os.environ.get("CONTROL_LOOP_ENABLED", "1").strip().lower() \
    in ("1", "true", "yes")

# ── Rollout guards ──────────────────────────────────────────────────────────
# This loop launches and terminates EC2 instances on its own. Enabling it
# against a Redis that already holds 33 historical workshops would have it act
# on all of them within one tick — so it starts in DRY RUN and says what it
# would have done. Flip CONTROL_LOOP_APPLY=1 once the log reads correctly.
#
# This is a deliberate no-op default, not an accidental one: the log line at
# startup states it plainly, and every skipped action is logged with the word
# DRY-RUN. A silent no-op would be the drain-cordon bug over again.
CONTROL_LOOP_APPLY = os.environ.get("CONTROL_LOOP_APPLY", "0").strip().lower() \
    in ("1", "true", "yes")
# Comma-separated workshop ids the loop may manage; "*" means all of them.
# Narrow this during a rehearsal so one test workshop can be driven end to end
# without touching a real cohort that happens to be running.
CONTROL_LOOP_WORKSHOPS = os.environ.get("CONTROL_LOOP_WORKSHOPS", "*").strip()


def manages(ws_id: str) -> bool:
    if CONTROL_LOOP_WORKSHOPS in ("*", ""):
        return True
    allowed = {w.strip() for w in CONTROL_LOOP_WORKSHOPS.split(",") if w.strip()}
    return ws_id in allowed

# Planning safety on top of the profile arithmetic. 0.55 turns the 30 seats the
# memory model allows on an m6a.4xlarge into 20 -- an explicit instruction
# rather than a hedge: "I would rather provision 20 per machine than fit 30
# exactly and lose the delivery to two or three failures." Being wrong in this
# direction costs a few dollars; being wrong in the other costs the workshop.
WORKSHOP_SEAT_SAFETY = float(os.environ.get("WORKSHOP_SEAT_SAFETY", "0.55"))
WORKSHOP_INSTANCE_TYPE = os.environ.get("WORKSHOP_INSTANCE_TYPE", "m6a.4xlarge")
# At or below this many seats a workshop runs on the standing workshop box and
# launches nothing. Set to 7 rather than the full reserve (10 of a 20-slot box)
# so a small room still leaves headroom for a second one -- the reserve exists
# to absorb rooms that open with no notice, and a threshold equal to it would
# let the first small workshop consume all of it.
WORKSHOP_STANDING_MAX_SEATS = int(os.environ.get("WORKSHOP_STANDING_MAX_SEATS", "7"))
# Spare machines on top of the arithmetic, for workshops that get their own.
# ZERO by design (2026-08-17). The spare was bought against "a host dies
# mid-delivery and the loop will not re-plan a bound workshop" — but it never
# bought what that implies: the sessions on a dead host die with it either way,
# containers and all, so the spare only ever offered somewhere to RE-provision.
# Replacing a machine takes minutes, which is the same order as re-provisioning
# onto a spare that was already paid for. What actually has to survive is the
# lifetime of the containers a connected class is sitting on, and nothing about
# the spare affects that.
#
# The cost it was quietly carrying: `seats_per_worker` is 20 for k8s-101 on an
# m6a.4xlarge, so EVERY dedicated workshop from 8 to 20 seats was 1 real machine
# + 1 spare — 100% overhead across the whole band, held for the entire window
# (prewarm + duration + hold), not just the class.
#
# Still an env var: a delivery that genuinely cannot tolerate a re-provision can
# set WORKSHOP_REDUNDANCY=1 for that fleet without a code change.
WORKSHOP_REDUNDANCY = int(os.environ.get("WORKSHOP_REDUNDANCY", "0"))
# Extra minutes on top of prewarm + duration + grace before a workshop machine
# kills itself. Wide enough that the self-destruct never races a workshop that
# is merely overrunning — the loop's teardown should always win the race.
WORKSHOP_LIFETIME_MARGIN_MINUTES = int(os.environ.get("WORKSHOP_LIFETIME_MARGIN_MINUTES", "60"))
# How long a pool may sit in WARMING before the loop stops waiting for the last
# slot and delivers with what came up. A worker that comes back short (measured:
# "SysboxPool: 18/30 slots ready" while reporting fully warm) would otherwise
# hold the workshop at `warming` forever, which reads to a trainer as a hung
# fleet and blocks the room from opening at all.
WORKSHOP_WARMING_TIMEOUT_MINUTES = int(os.environ.get("WORKSHOP_WARMING_TIMEOUT_MINUTES", "20"))
# How long teardown will keep deferring termination for workers that still
# report active jobs. Long enough for a real teardown to finish (a 30-seat
# teardown took ~2.5 min measured), short enough that a stuck job count cannot
# keep a machine alive indefinitely.
TEARDOWN_DEFER_MAX_MINUTES = int(os.environ.get("TEARDOWN_DEFER_MAX_MINUTES", "15"))
# Workshops run on-demand, never spot. A spot reclamation costs a learner their
# session with two minutes' notice and a Sysbox session cannot be migrated.
WORKSHOP_PURCHASING = os.environ.get("WORKSHOP_PURCHASING", "on-demand")
# Branch a launched worker syncs its agent code from. "main" in production;
# overridable so fleet changes can be validated on a real launched worker
# before being merged, rather than proving the code by shipping it.
WORKER_CODE_BRANCH = os.environ.get("WORKER_CODE_BRANCH", "main")

# Daily pool.
DAILY_MIN_FREE_SEATS = int(os.environ.get("DAILY_MIN_FREE_SEATS", "4"))
DAILY_MAX_WORKERS = int(os.environ.get("DAILY_MAX_WORKERS", "4"))
DAILY_INSTANCE_TYPE = os.environ.get("DAILY_INSTANCE_TYPE", "m6a.2xlarge")
# Sustained memory fraction that means "this worker's seats are overpriced".
MEMORY_PRESSURE_THRESHOLD = float(os.environ.get("MEMORY_PRESSURE_THRESHOLD", "0.70"))
# CPU fraction that means "stop admitting here", NOT "scale".
CPU_BRAKE_THRESHOLD = float(os.environ.get("CPU_BRAKE_THRESHOLD", "0.70"))
# Consecutive ticks above threshold before acting. A single hot sample during a
# k3d bring-up is normal and must not move the fleet.
PRESSURE_SUSTAIN_TICKS = int(os.environ.get("PRESSURE_SUSTAIN_TICKS", "4"))

FLEET_KEY = "workshop:fleet"          # hash: ws_id -> json record
# NOT "worker:pressure" — that matched the worker:* scan used by /api/workers
# and by _daily_workers, so the pressure counter itself was listed as a worker
# ("pressure  status=None  None/None") and would have been fed to the scale
# planner as a box with no free seats. Caught live on the first apply run.
PRESSURE_KEY = "fleet:pressure"       # hash: worker_id -> consecutive tick count

# Workshop fleet lifecycle.
WARMING, READY, DRAINING, DONE = "warming", "ready", "draining", "done"


# ── Pure decisions (no Redis, no AWS — unit-testable) ────────────────────────

def parse_iso(value: str):
    """Tolerant ISO8601 → aware datetime, or None.

    Returns None rather than raising: a workshop with an unparseable start time
    must be skipped by the scheduler, never crash the loop that also serves
    every other workshop.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clamped_minutes(session: dict, field: str, cap: int, default: int) -> int:
    """A per-workshop minute override, or the loop default when unset/unusable.

    Missing, empty, zero and unparseable all mean "the trainer did not choose",
    which is the default — not zero minutes. Negative and over-cap values are
    clamped rather than rejected: this runs inside the control loop, where
    raising on one bad workshop would stop the tick for every other one.
    """
    raw = session.get(field, "")
    if raw in (None, "", 0, "0"):
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, cap))


def session_lead_minutes(session: dict) -> int:
    """How far ahead of its start this workshop's machines are launched."""
    return _clamped_minutes(session, "prewarmLeadMinutes",
                            LEAD_MINUTES_CAP, PREWARM_LEAD_MINUTES)


def session_hold_minutes(session: dict) -> int:
    """Minimum minutes from START that this workshop's machines are held."""
    return _clamped_minutes(session, "holdMinutes",
                            HOLD_MINUTES_CAP, WORKSHOP_HOLD_MINUTES)


def prewarm_at(session: dict):
    """When the loop will (or did) start this workshop's machines."""
    start = parse_iso(session.get("scheduledAt", ""))
    if start is None:
        return None
    return start - timedelta(minutes=session_lead_minutes(session))


def teardown_at(session: dict, grace_minutes: int = TEARDOWN_GRACE_MINUTES):
    """When the loop will give this workshop's machines back.

    The LATER of the two windows — the booked end plus grace, and the hold floor
    measured from the start. Taking the later is what makes `holdMinutes` a
    floor rather than a replacement: raising it never shortens a long workshop,
    and a long `durationMinutes` is never truncated by a shorter hold.

    An explicit end (`state` ended/cancelled/deleted) is NOT considered here —
    that short-circuit lives in :func:`due_for_teardown`, so a trainer who
    finishes early still gets an immediate teardown regardless of this window.
    """
    start = parse_iso(session.get("scheduledAt", ""))
    if start is None:
        return None
    end = workshop_end(session)
    by_duration = (end + timedelta(minutes=grace_minutes)) if end else start
    by_hold = start + timedelta(minutes=session_hold_minutes(session))
    return max(by_duration, by_hold)


def due_for_prewarm(session: dict, now: datetime,
                    lead_minutes: int | None = None) -> bool:
    """Is it time to start this workshop's machines?

    A workshop already past its start time is still due: a trainer who opens
    the room late, or a loop that was restarted, must still get machines rather
    than being silently skipped for having missed the window.

    But only until its teardown point. Being unbounded on the late side made this
    predicate and :func:`due_for_teardown` both true at once for any workshop whose
    trainer never pressed end — and the control loop acts on prewarm first. With
    CONTROL_LOOP_APPLY on that is an infinite launch/terminate cycle: tick 1 launches
    the machines, a later tick tears them down for being past the window, the next tick
    launches them again, forever, for a workshop nobody is attending. Found with a real
    one — a room opened 2026-08-13 and never ended, which the loop had been asking to
    prewarm every 30 seconds for three days.

    Making the two mutually exclusive is what stops the oscillation, and it costs the
    late trainer nothing: teardown is not due until the scheduled end PLUS grace.

    `lead_minutes` defaults to this workshop's own :func:`session_lead_minutes`;
    pass it explicitly only to test the predicate at a chosen lead.
    """
    if session.get("state") in ("ended", "cancelled", "deleted"):
        return False
    start = parse_iso(session.get("scheduledAt", ""))
    if start is None:
        return False
    if lead_minutes is None:
        lead_minutes = session_lead_minutes(session)
    if now < start - timedelta(minutes=lead_minutes):
        return False
    return not due_for_teardown(session, now)


def workshop_repo(session: dict) -> str:
    """Which repo a workshop delivers, for profile lookup.

    ``repoUrl`` is preferred because it is the unambiguous one. ``trainingId``
    is a catalog id and does NOT match the repo name — a live workshop stores
    ``kubernetes-101`` for ``enablement-kubernetes-101`` — so relying on it
    alone silently sent every workshop to the heavy default.
    """
    return (session.get("repoUrl") or session.get("trainingId")
            or session.get("repo") or "")


def _workshop_lifetime_minutes(session: dict,
                               margin_minutes: int = WORKSHOP_LIFETIME_MARGIN_MINUTES) -> int:
    """Hard self-destruct offset for a workshop's machines.

    The whole provisioning window — prewarm point to teardown point — plus a
    margin, so the timer can only ever fire AFTER the loop's own teardown would
    have. It is a backstop against the loop not running at all, not a second
    schedule competing with it.

    Derived from :func:`prewarm_at` / :func:`teardown_at` rather than recomputed
    from the same parts, because the two must not be able to drift: when the
    hold floor pushed teardown past duration+grace, a lifetime still computed as
    lead+duration+grace armed `shutdown -h +N` BEFORE the loop meant to tear the
    machines down, and the workshop would have lost them mid-session.
    """
    window = None
    start, end = prewarm_at(session), teardown_at(session)
    if start is not None and end is not None:
        window = int((end - start).total_seconds() // 60)
    if window is None:
        # Unscheduled workshop: no window to measure, so fall back to the
        # widest one it could legitimately have asked for.
        window = session_lead_minutes(session) + session_hold_minutes(session)
    return max(0, window) + max(0, margin_minutes)


def workshop_end(session: dict):
    start = parse_iso(session.get("scheduledAt", ""))
    if start is None:
        return None
    try:
        minutes = int(session.get("durationMinutes") or 120)
    except (TypeError, ValueError):
        minutes = 120
    return start + timedelta(minutes=minutes)


def due_for_teardown(session: dict, now: datetime,
                     grace_minutes: int = TEARDOWN_GRACE_MINUTES) -> bool:
    """Give the machines back — either the workshop ended, or it outlived its
    provisioning window.

    An explicit end always wins over the clock: a trainer who finishes early
    should not pay for an hour of idle machines.

    The window itself is :func:`teardown_at` — the later of the booked end plus
    grace and the workshop's hold floor.
    """
    if session.get("state") in ("ended", "cancelled", "deleted"):
        return True
    due = teardown_at(session, grace_minutes=grace_minutes)
    return due is not None and now >= due


# Fleet-record states from which a workshop may be (re)provisioned. DONE is in
# here deliberately — see the comment at the call site in `tick`. Kept as a
# module constant, and paired with the pure predicates below, because the bug it
# fixes lived for weeks as an inline tuple inside `tick`, where nothing but a
# full fake-Redis harness could reach it and so nothing ever did.
PREWARMABLE_STATES = (None, "", "failed", DONE)
# DRAINING is in here because a deferred teardown parks the record there and its
# own comment promises "the next tick DOES re-enter" — which was false, since
# this tuple was the gate and did not list it. A workshop whose workers still
# reported sessions was therefore deferred exactly ONCE and then never looked at
# again: the deferral bound (TEARDOWN_DEFER_MAX_MINUTES) could not expire,
# because nothing ever came back to check it. Found 2026-08-17 on a record that
# had been DRAINING since 00:19.
#
# Re-entering is safe and converges: `teardown_workshop_fleet` returns early only
# on DONE, unbinding and cordoning are idempotent, `deferred_since` persists in
# the record so the bound measures from the FIRST deferral, and past that bound
# the machines go regardless. So the state either terminates or expires — it
# cannot sit still.
TEARDOWNABLE_STATES = (WARMING, READY, DRAINING)


def should_prewarm(state, session: dict, now: datetime) -> bool:
    """Should this tick launch machines for the workshop?"""
    return state in PREWARMABLE_STATES and due_for_prewarm(session, now)


def should_teardown(state, session: dict, now: datetime) -> bool:
    """Should this tick give the workshop's machines back?"""
    return state in TEARDOWNABLE_STATES and due_for_teardown(session, now)


def planned_seats(session: dict, roster_count: int = 0) -> int:
    """Seats to BUY for a workshop: the capacity it BOOKED, not its turnout.

    A workshop is provisioned for the room it reserved. Sizing it from the
    roster instead — which is what this did — made every workshop that fills at
    the door plan for one person: learners join with a code and never appear on
    a roster, so `scard(roster) + 1` returned 1 for a 40-seat class and the
    planner put it on the standing lane with nothing launched. The machines have
    to be up *before* anyone arrives, which is precisely the moment there is
    nobody to count.

    `maxSeats` is the trainer's own number and wins whenever it is set. 0 means
    "unlimited", which cannot be planned, so it falls back to the roster. Either
    way the trainer team is added on top: `maxSeats` caps the ROSTER only, and
    every trainer takes an environment as well.

    Clamped on read, like the window minutes: a `maxSeats` edited straight into
    Redis must not be able to buy an unbounded number of machines.
    """
    team = max(1, len(live_sessions.trainers_of(session)))
    try:
        booked = int(session.get("maxSeats") or 0)
    except (TypeError, ValueError):
        booked = 0
    booked = max(0, min(booked, live_sessions.MAX_SEATS))
    return (booked if booked > 0 else max(0, roster_count)) + team


def needs_bigger_fleet(state, rec: dict, seats: int,
                       standing_max: int = WORKSHOP_STANDING_MAX_SEATS) -> bool:
    """Has a workshop on the standing lane outgrown it?

    The lane decision was only ever made once, at prewarm, from whatever the
    numbers said then. They move afterwards — a trainer raises `maxSeats`, or a
    roster fills — and without this the workshop rode the standing box's reserve
    for its whole delivery no matter how big it got.

    Upgrades only. The reverse would terminate machines a room may already be
    sitting on, to save the cost of a few hours.
    """
    return state == READY and bool(rec.get("standing")) and seats > standing_max


def orphan_candidates(fleet_ids, indexed_ids, index_ok: bool) -> list[str]:
    """Fleet records with no workshop left in the index.

    ``index_ok`` is the whole reason this is a function. The reaper's input is
    "every fleet record the index does not mention", and a FAILED index read
    produces an empty index — under which every workshop in the fleet, including
    the ones running a class right now, looks abandoned. One Redis blip would
    then terminate the entire fleet. So a failed read yields nothing at all: the
    reaper is an optimisation on cost, and doing nothing costs only money.
    """
    if not index_ok:
        return []
    indexed = set(indexed_ids or ())
    return [w for w in (fleet_ids or ()) if w not in indexed]


def is_orphaned(state, session_exists) -> bool:
    """Should this fleet record's machines be given back?

    Only when the workshop is provably gone. ``session_exists`` is tri-state on
    purpose: ``None`` means the check itself failed, which is NOT evidence of
    deletion — treating an unreadable key as a missing one is how a reaper turns
    a Redis hiccup into a terminated fleet.

    ``DONE`` records are skipped so a torn-down workshop is not re-torn every
    tick; they are cheap to leave, and they are the audit trail.
    """
    if session_exists is not False:
        return False
    return state not in (None, "", DONE)


def plan_workshop_capacity(seats: int, profile: repo_profiles.RepoProfile,
                           instance_type: str = WORKSHOP_INSTANCE_TYPE,
                           safety: float = WORKSHOP_SEAT_SAFETY,
                           standing_max_seats: int = WORKSHOP_STANDING_MAX_SEATS,
                           redundancy: int = WORKSHOP_REDUNDANCY) -> dict:
    """Machines and per-machine seats for a workshop of ``seats`` people.

    Two tiers, decided by size:

    * **At or under ``standing_max_seats``** the workshop launches NOTHING and
      runs on the standing workshop box's reserved half. That is what makes a
      small room openable now instead of in the ~8 minutes it takes to boot and
      warm an instance, and it is the reason the box keeps a reserve at all.
    * **Above it**, the workshop gets its own machines, sized for ALL of its
      seats. It deliberately does not lean on the standing box: the reserve has
      to stay free for the next room that opens with no notice.

    Returns ``workers``, ``seats_per_worker``, ``total_seats``, ``pool_kind``
    and whether the profile behind it was estimated. ``workers == 0`` with
    ``pool_kind == "dedicated"`` means refuse to plan -- an unknown instance
    type must never be guessed at.
    """
    per = repo_profiles.seats_per_worker(profile, instance_type, safety)
    if seats <= standing_max_seats:
        return {
            "workers": 0,
            "seats_per_worker": per,
            "total_seats": seats,
            "estimated": profile.estimated,
            "pool_kind": "standing",
            "reason": (f"{seats} seats ≤ {standing_max_seats} — runs on the "
                       f"standing {pools.WORKSHOP_POOL} box, no machines launched"),
        }
    if per <= 0:
        return {"workers": 0, "seats_per_worker": 0, "total_seats": 0,
                "estimated": profile.estimated, "pool_kind": "dedicated",
                "reason": f"no capacity model for {instance_type}"}
    # Round UP. A workshop one seat short is a person without an environment in
    # front of a room, so the division never truncates. `redundancy` is 0 by
    # default -- see WORKSHOP_REDUNDANCY for why the spare stopped being bought.
    workers = -(-max(0, seats) // per) + max(0, redundancy)
    return {
        "workers": workers,
        "seats_per_worker": per,
        "total_seats": workers * per,
        "estimated": profile.estimated,
        "pool_kind": "dedicated",
        "reason": (f"{seats} seats ÷ {per}/worker"
                   + (f" +{redundancy} spare" if redundancy else "")
                   + (" (profile is an ESTIMATE, not a measurement)"
                      if profile.estimated else "")),
    }


def _frac(hash_value, key: str) -> float:
    try:
        return float(hash_value.get(key, 0) or 0) / 100.0
    except (TypeError, ValueError):
        return 0.0


def daily_scale_decision(workers: list[dict], pressure_ticks: dict[str, int],
                         min_free: int = DAILY_MIN_FREE_SEATS,
                         max_workers: int = DAILY_MAX_WORKERS,
                         lenders: list[dict] | None = None) -> dict:
    """What the daily pool should do this tick.

    ``workers`` are heartbeat hashes for the DAILY pool only -- workshop
    machines must never be counted as available self-service capacity, which is
    the whole reason the pool field exists.

    Returns ``{"scale_up": n, "shrink": [...], "brake": [...], "why": str}``.
    Each is a separate lever on purpose: shrinking an over-advertised worker is
    what stops a scale-up from overfilling anyway, and braking is what protects
    the sessions already on a hot box, which adding a machine cannot do.
    """
    if not workers:
        return {"scale_up": 0, "shrink": [], "brake": [],
                "why": "no daily workers registered"}

    free = sum(int(w.get("slots_free", 0) or 0) for w in workers
               if w.get("status") == "ready" and not _truthy(w.get("draining")))
    # Seats a workshop box is willing to lend ARE daily capacity: the lending
    # worker reads the daily queue while it is under its cap. They arrive as a
    # SEPARATE list rather than in `workers`, because a lender must contribute
    # its lendable seats WITHOUT counting toward DAILY_MAX_WORKERS or being
    # eligible for the shrink/brake decisions below — those belong to whoever
    # owns the machine, and that is the workshop lane.
    lent_free = sum(int(w.get("borrow_free", 0) or 0) for w in (lenders or [])
                    if w.get("status") == "ready" and not _truthy(w.get("draining")))
    free += lent_free
    shrink, brake, reasons = [], [], []

    for w in workers:
        wid = w.get("worker_id", "?")
        sustained = pressure_ticks.get(wid, 0) >= PRESSURE_SUSTAIN_TICKS
        if not sustained:
            continue
        if _frac(w, "mem_pct") >= MEMORY_PRESSURE_THRESHOLD:
            # Memory is the honest occupancy proxy, so sustained pressure means
            # the seats this worker advertises cost more than the profile says.
            # Stop selling the rest of them.
            shrink.append(wid)
            reasons.append(f"{wid} mem {w.get('mem_pct')}% sustained")
        if _frac(w, "cpu_pct") >= CPU_BRAKE_THRESHOLD:
            # NOT a scale trigger. A new machine arrives minutes late and cannot
            # take work off this one; refusing new admissions here can.
            brake.append(wid)
            reasons.append(f"{wid} cpu {w.get('cpu_pct')}% sustained — braking")

    # A warming worker contributes 0 free seats, so a pool that is merely
    # restarting looks exactly like a pool that is full. It is not: those seats
    # are temporarily ABSENT, not taken, and they return in minutes — whereas a
    # machine launched now needs its own boot plus warm-up and lands about when
    # it stops being needed. Unguarded, every worker restart bought a spare
    # instance and a fleet-wide deploy bought one per worker.
    #
    # The guard is deliberately narrow: only wait when the seats already coming
    # back would COVER the shortfall. A warming worker does not excuse a pool
    # that will still be short once it lands — that is real demand, and the
    # machine should be on its way now rather than one warm-up later.
    #
    # Count `slots_total`, NOT `capacity`. The agent publishes `capacity` as the
    # number of slots ALREADY WARM, which is 0 for most of a warm-up — so this
    # guard summed zero at exactly the moment it existed for, and never fired.
    # Measured 2026-08-16: amd001 restarted at 13:51:46 and the loop launched
    # spot instances at 13:52:16 and 13:52:48, with both pets warming at
    # capacity=0 and nothing queued. `slots_total` is the nominal figure and is
    # published from registration onward, which is the seats that are genuinely
    # on their way back.
    incoming = sum(int(w.get("slots_total", 0) or w.get("capacity", 0) or 0)
                   for w in workers
                   if w.get("status") == "warming" and not _truthy(w.get("draining")))

    scale_up = 0
    if free < min_free and free + incoming >= min_free:
        reasons.append(f"{free} free seats < {min_free}, but {incoming} seat(s) are "
                       f"warming back up — waiting for capacity already paid for")
    elif free < min_free:
        reasons.append(f"{free} free seats < {min_free}"
                       + (f" (only {incoming} warming)" if incoming else ""))
        scale_up = 1
    elif shrink:
        # Capacity is about to be withdrawn from the pool, so replace it.
        reasons.append("replacing seats withdrawn by memory pressure")
        scale_up = 1
    if len(workers) >= max_workers and scale_up:
        reasons.append(f"at DAILY_MAX_WORKERS={max_workers} — NOT scaling")
        scale_up = 0

    return {"scale_up": scale_up, "shrink": shrink, "brake": brake,
            "why": "; ".join(reasons) or f"{free} free seats — steady"}


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _int(value, default: int = 0) -> int:
    """Redis hash field -> int, never raising.

    Every value in a fleet record arrives as a string, and this runs inside the
    control loop: a ValueError here would abort the tick for EVERY workshop,
    not just the one with the bad field. Total-function by construction.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def update_pressure(pressure_ticks: dict[str, int], workers: list[dict]) -> dict[str, int]:
    """Count consecutive ticks a worker has been hot; reset the moment it is not.

    Sustain counting is what separates "a k3d bring-up is running" from "this
    box is genuinely full". A single hot sample must never move the fleet.
    """
    out = dict(pressure_ticks)
    for w in workers:
        wid = w.get("worker_id", "?")
        hot = (_frac(w, "mem_pct") >= MEMORY_PRESSURE_THRESHOLD
               or _frac(w, "cpu_pct") >= CPU_BRAKE_THRESHOLD)
        out[wid] = out.get(wid, 0) + 1 if hot else 0
    return out


# ── Effects (Redis + AWS) ───────────────────────────────────────────────────

LIVE_INDEX_KEY = "live:sessions:index"


async def _fleet_record(redis, ws_id: str) -> dict:
    raw = await redis.hget(FLEET_KEY, ws_id)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


async def _save_fleet_record(redis, ws_id: str, rec: dict) -> None:
    await redis.hset(FLEET_KEY, ws_id, json.dumps(rec))


async def _planned_seats(redis, ws_id: str, session: dict) -> int:
    """Seats to plan for — see :func:`planned_seats`. Reads the roster only as
    the fallback for a workshop that booked no capacity."""
    try:
        roster = int(await redis.scard(f"live:session:{ws_id}:roster"))
    except Exception:
        roster = 0
    return planned_seats(session, roster)


async def provision_workshop_fleet(redis, ws_id: str, session: dict) -> dict:
    """Launch dedicated machines for a workshop and bind it to its pool.

    Order matters: the pool binding is written BEFORE the instances exist. A
    learner who arrives early then queues on the pool queue and waits for its
    machines, instead of being scheduled onto the daily pool where they would
    consume a self-service seat and escape the workshop's isolation.
    """
    from dashboard import fleet, pools

    repo = workshop_repo(session)
    seats = await _planned_seats(redis, ws_id, session)

    existing = await _fleet_record(redis, ws_id)
    # An existing fleet is left alone -- UNLESS it is a standing-lane record the
    # workshop has since outgrown, which is the one case where re-planning buys
    # something the room does not already have.
    if existing and existing.get("state") in (WARMING, READY) \
            and not needs_bigger_fleet(existing.get("state"), existing, seats):
        return existing
    if existing and existing.get("standing"):
        log.info("workshop %s: outgrew the standing lane (%d seats > %d) — "
                 "planning dedicated machines", ws_id, seats,
                 WORKSHOP_STANDING_MAX_SEATS)

    profile = await repo_profiles.load(redis, repo)
    plan = plan_workshop_capacity(seats, profile)

    # Small enough for the standing box: bind the shared workshop lane and stop.
    # Nothing is launched, so the room is deliverable the moment it opens.
    if plan["pool_kind"] == "standing":
        await pools.bind_workshop_pool(redis, ws_id, pools.WORKSHOP_POOL)
        rec = {
            "state": READY,
            "pool": pools.WORKSHOP_POOL,
            "repo": repo,
            "seats": seats,
            "workers": 0,
            "seats_per_worker": plan["seats_per_worker"],
            "profile_estimated": plan["estimated"],
            "instances": [],
            "standing": True,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "ready_at": datetime.now(timezone.utc).isoformat(),
        }
        await _save_fleet_record(redis, ws_id, rec)
        log.info("workshop %s: %s", ws_id, plan["reason"])
        return rec

    if plan["workers"] <= 0:
        log.error("workshop %s: cannot plan capacity (%s)", ws_id, plan["reason"])
        return {"state": "failed", "reason": plan["reason"]}

    pool_name = f"ws-{ws_id}"
    await pools.bind_workshop_pool(redis, ws_id, pool_name)

    rec = {
        "state": WARMING,
        "pool": pool_name,
        "repo": repo,
        "seats": seats,
        "workers": plan["workers"],
        "seats_per_worker": plan["seats_per_worker"],
        "profile_estimated": plan["estimated"],
        "instances": [],
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    await _save_fleet_record(redis, ws_id, rec)

    log.info("workshop %s: launching %d × %s for %d seats (%s)",
             ws_id, plan["workers"], WORKSHOP_INSTANCE_TYPE, seats, plan["reason"])
    if plan["estimated"]:
        # Say it out loud. A plan built on a guess should never read like a
        # plan built on a measurement.
        log.warning("workshop %s sized from an ESTIMATED profile for %s — "
                    "measure this repo before relying on the number", ws_id, repo)
    try:
        # scale_up refuses more than MAX_SCALE_UP (4) per call, and 70 seats
        # needs exactly 4 — so the bootcamp sits ON the limit and anything
        # larger would raise ValueError, be caught below, and retry forever
        # without ever succeeding. Batch instead: the cap is a per-call safety
        # rail, not a fleet ceiling.
        launched = []
        remaining = plan["workers"]
        while remaining > 0:
            batch = min(remaining, fleet.MAX_SCALE_UP)
            launched += await fleet.scale_up(
                batch,
                instance_type=WORKSHOP_INSTANCE_TYPE,
                purchasing=WORKSHOP_PURCHASING,
                pool=pool_name,
                capacity=plan["seats_per_worker"],
                # Self-destruct, armed inside the instance at boot. Until now the
                # ONLY thing that ever terminated a workshop machine was this
                # loop's teardown, so a lost fleet record, a deleted (rather than
                # ended) workshop, or a dashboard that never comes back left the
                # machines running until somebody noticed. `shutdown -h +N` needs
                # nothing from Orbital, Redis, AWS credentials or the network —
                # it is the only cost guarantee that survives losing all of them.
                # Generous on purpose: it is a backstop, not a schedule.
                lifetime_minutes=_workshop_lifetime_minutes(session),
                # A workshop worker serves one repo, so its slots can be capped
                # for that repo specifically rather than at a flat figure chosen
                # for the lightest one.
                slot_memory_mb=repo_profiles.slot_memory_cap_mb(profile),
                code_branch=WORKER_CODE_BRANCH,
            )
            remaining -= batch
        # scale_up returns snake_case ``instance_id``; accept both spellings so a
        # future change to either side cannot silently empty this list. It was
        # ``InstanceId`` only, which produced an empty list on every launch and
        # therefore a teardown that terminated nothing — invisible in unit tests
        # because they never call the real scale_up.
        rec["instances"] = [i.get("instance_id") or i.get("InstanceId")
                            for i in launched
                            if i.get("instance_id") or i.get("InstanceId")]
    except Exception as exc:
        # Leave the binding in place. Learners queue on the pool rather than
        # leaking onto the daily workers, and the next tick retries the launch.
        log.error("workshop %s: launch failed (%s) — will retry next tick", ws_id, exc)
        rec["state"] = WARMING
        rec["last_error"] = str(exc)[:300]
    await _save_fleet_record(redis, ws_id, rec)
    return rec


async def teardown_workshop_fleet(redis, ws_id: str) -> dict:
    """Give a finished workshop's machines back, in the only safe order.

    Drain, then terminate the sessions, then terminate the instances. Draining
    first is what stops a straggler being scheduled onto a machine that is
    about to disappear; terminating sessions before instances is what gives the
    per-slot teardown (and the reaper) a chance to run rather than having
    everything vanish with the host.
    """
    from dashboard import fleet, pools

    rec = await _fleet_record(redis, ws_id)
    if not rec or rec.get("state") == DONE:
        return rec or {}

    rec["state"] = DRAINING
    await _save_fleet_record(redis, ws_id, rec)

    # Unbind first: no further learner can be routed to these machines.
    await pools.unbind_workshop_pool(redis, ws_id)

    # A workshop that ran on the STANDING box owns no machines. Its sessions
    # must still be terminated, but every step below this one would act on a
    # long-lived worker shared with every other small workshop: cordoning it
    # would stop it serving them, and deleting its record would remove a live
    # box from the fleet's view entirely.
    if rec.get("standing") or rec.get("pool") == pools.WORKSHOP_POOL:
        terminated = await _terminate_workshop_sessions(redis, ws_id)
        rec["state"] = DONE
        rec["torn_down_at"] = datetime.now(timezone.utc).isoformat()
        await _save_fleet_record(redis, ws_id, rec)
        log.info("workshop %s: ended on the standing %s box — %d session(s) "
                 "terminated, no machines to return",
                 scrub_for_log(ws_id), pools.WORKSHOP_POOL, terminated)
        return rec

    # Remember who they were before cordoning: once the instances are gone their
    # heartbeats stop, and the scan that finds them by pool would find nothing.
    pool_workers = await _pool_worker_ids(redis, rec.get("pool", ""))
    for wid in pool_workers:
        try:
            await redis.hset(f"worker:{wid}", "draining", "1")
        except Exception as exc:
            log.warning("workshop %s: could not cordon %s: %s", ws_id, wid, exc)

    terminated = await _terminate_workshop_sessions(redis, ws_id)
    log.info("workshop %s: requested termination of %d session(s)", ws_id, terminated)

    # The record is a convenience, NOT the source of truth. Instances are tagged
    # with their pool at launch, so ask EC2 what actually exists rather than
    # trusting a list that a bug, a lost write or a restart could have emptied.
    # This is the difference between a workshop that gives its machines back and
    # one that leaks them silently — and the record WAS empty on the first live
    # run, so this path is load-bearing, not belt-and-braces.
    instances = sorted(set(rec.get("instances") or []) |
                       set(await _instances_tagged(rec.get("pool", ""))))

    # Never terminate a host that still has sessions on it: this workshop's own
    # sessions were just asked to stop, so anything still running is either
    # mid-teardown or belongs to someone else. Leave it cordoned and come back.
    #
    # DEFERRING MUST CONVERGE. The first version of this set the deferral flag
    # and then fell through to state=DONE, and `teardown_workshop_fleet` returns
    # immediately for a DONE record — so "wait and retry" silently became
    # "never", and three m6a.4xlarge ran on after their workshop ended. Measured
    # 2026-08-16, on the very run that was meant to prove teardown was clean.
    #
    # So a deferral now leaves the record in DRAINING (which the next tick DOES
    # re-enter) and is bounded: past the deadline the machines go regardless.
    # A worker whose active_jobs never returns to zero is the known wedged-reaper
    # bug, and a disposable machine must not outlive its workshop waiting for it.
    busy = await _busy_pool_workers(redis, rec.get("pool", ""))
    if busy and instances:
        first = parse_iso(rec.get("deferred_since", "")) or datetime.now(timezone.utc)
        rec["deferred_since"] = rec.get("deferred_since") or first.isoformat()
        expired = datetime.now(timezone.utc) >= first + timedelta(
            minutes=TEARDOWN_DEFER_MAX_MINUTES)
        if expired:
            log.error("workshop %s: %d worker(s) STILL report sessions after "
                      "%d min (%s) — terminating anyway; a machine must not "
                      "outlive its workshop waiting for a stuck job count",
                      scrub_for_log(ws_id), len(busy), TEARDOWN_DEFER_MAX_MINUTES,
                      ", ".join(sorted(busy)))
        else:
            log.warning("workshop %s: %d worker(s) still hold sessions (%s) — "
                        "cordoned, retrying next tick (deferred since %s)",
                        scrub_for_log(ws_id), len(busy), ", ".join(sorted(busy)),
                        rec["deferred_since"])
            rec["state"] = DRAINING
            rec["deferred_termination"] = True
            await _save_fleet_record(redis, ws_id, rec)
            return rec

    if instances:
        try:
            # scale_down refuses anything not tagged orbital-role=worker, so a
            # bug in the record here cannot terminate an unrelated instance.
            await fleet.scale_down(instances)
            log.info("workshop %s: terminated %s", ws_id, ", ".join(instances))
        except Exception as exc:
            # The instances still carry a self-destruct timer and the
            # ManagedBy tag, so a failure here delays the refund rather than
            # leaking a machine forever -- but it must be visible.
            log.error("workshop %s: instance termination FAILED (%s) — "
                      "instances %s need manual review", ws_id, exc, instances)
            rec["last_error"] = str(exc)[:300]

    # Drop the heartbeats of machines that no longer exist. A terminated instance
    # leaves its `worker:{id}` hash behind for ever, frozen at whatever it last
    # published — the first workshop machine the loop ever tore down left one at
    # `status: warming` / `capacity: 0`, which every consumer read as a live
    # worker that would never contribute a seat. `_daily_workers` now also skips
    # stale records, so this is hygiene rather than the only defence, but leaving
    # them accumulating makes every fleet view progressively less true.
    for wid in pool_workers:
        try:
            await redis.delete(f"worker:{wid}")
        except Exception as exc:
            log.warning("workshop %s: could not drop the heartbeat for %s: %s",
                        ws_id, wid, exc)
    if pool_workers:
        log.info("workshop %s: dropped %d worker record(s)", ws_id, len(pool_workers))

    rec["state"] = DONE
    rec["ended_at"] = datetime.now(timezone.utc).isoformat()
    await _save_fleet_record(redis, ws_id, rec)
    return rec


async def _pool_workers_ready(redis, pool_name: str) -> int:
    """How many of this pool's workers report ready with a full slot pool.

    ``slots_degraded`` is deliberately part of the test: a worker that came up
    short is not "ready" for a workshop that was sized on its full seat count.
    """
    if not pool_name:
        return 0
    ready = 0
    async for key in redis.scan_iter(match="worker:*", count=200):
        if key.count(":") != 1:
            continue
        try:
            h = await redis.hgetall(key)
        except Exception:
            continue
        if h.get("pool") != pool_name:
            continue
        if h.get("status") == "ready" and int(h.get("slots_degraded", 0) or 0) == 0:
            ready += 1
    return ready


def _warming_too_long(rec: dict, now: datetime,
                      timeout_minutes: int = 0) -> bool:
    """Has this pool been WARMING past the point where waiting still helps?

    Measured from ``requested_at`` — the launch — because that is when the clock
    a trainer cares about started. Returns False when the timestamp is missing
    or unparseable: a record we cannot date must not be declared degraded.
    """
    timeout_minutes = timeout_minutes or WORKSHOP_WARMING_TIMEOUT_MINUTES
    started = parse_iso(rec.get("requested_at", ""))
    if started is None:
        return False
    return now >= started + timedelta(minutes=timeout_minutes)


async def _pool_workers_any(redis, pool_name: str) -> int:
    """Workers in ``pool_name`` reporting ANY warm slot.

    The degraded counterpart to ``_pool_workers_ready``: it answers "how much
    can this pool actually serve right now", not "did every slot come up".
    """
    if not pool_name:
        return 0
    count = 0
    async for key in redis.scan_iter(match="worker:*", count=200):
        if key.count(":") != 1:
            continue
        try:
            h = await redis.hgetall(key)
        except Exception:
            continue
        if not h or h.get("pool") != pool_name:
            continue
        try:
            if int(h.get("slots_ready", 0) or 0) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


async def _busy_pool_workers(redis, pool_name: str) -> list[str]:
    """Workers in ``pool_name`` still reporting active jobs.

    Read AFTER this workshop's sessions have been asked to stop, so a non-empty
    answer means work that is not ours.
    """
    if not pool_name:
        return []
    busy = []
    async for key in redis.scan_iter(match="worker:*", count=200):
        if key.count(":") != 1:
            continue
        try:
            h = await redis.hgetall(key)
        except Exception:
            continue
        if not h or h.get("pool") != pool_name:
            continue
        try:
            if int(h.get("active_jobs", 0) or 0) > 0:
                busy.append(h.get("worker_id") or key.split(":", 1)[1])
        except (TypeError, ValueError):
            continue
    return busy


async def _instances_tagged(pool_name: str) -> list[str]:
    """Live EC2 instance ids carrying this pool's tag, in THIS environment.

    Deliberately queries AWS rather than Redis: when a workshop's machines need
    giving back, the question is "what is actually still running", and Redis is
    the component most likely to be the reason the record is wrong.

    The environment scope is not optional here. This list feeds termination,
    and pool names are not unique across environments — ``daily`` is the same
    string in staging as in production. Without it, a staging reaper handing
    back its own daily pool would hand back production's as well.

    Scoped client-side for the same reason as ``fleet.list_fleet``: EC2 filters
    cannot express "tag absent", and an untagged legacy machine must read as
    production rather than vanish from it.
    """
    if not pool_name:
        return []
    from dashboard import fleet
    from shared import environment
    env_name = environment.current().name
    try:
        data = await fleet._aws(
            "ec2", "describe-instances",
            "--filters", f"Name=tag:orbital-pool,Values={pool_name}",
            "Name=instance-state-name,Values=pending,running,stopping,stopped")
    except Exception as exc:
        log.warning("could not list instances for pool %s: %s", pool_name, exc)
        return []
    out = []
    for r in (data or {}).get("Reservations", []):
        for i in r.get("Instances", []):
            iid = i.get("InstanceId")
            if not iid:
                continue
            tags = {t.get("Key"): t.get("Value") for t in i.get("Tags", []) or []}
            if environment.owns(tags, env_name):
                out.append(iid)
    return out


async def _pool_worker_ids(redis, pool_name: str) -> list[str]:
    if not pool_name:
        return []
    out = []
    async for key in redis.scan_iter(match="worker:*", count=200):
        if key.count(":") != 1:
            continue
        try:
            if (await redis.hget(key, "pool")) == pool_name:
                out.append(key.split(":", 1)[1])
        except Exception:
            continue
    return out


async def terminate_workshop_sessions(redis, ws_id: str) -> int:
    """Public name for :func:`_terminate_workshop_sessions`.

    Ending a workshop has to stop its environments, and until 2026-08-14 the
    only caller was the control loop's scheduled teardown — which is off by
    default and driven by the clock, not by the trainer. So a trainer pressing
    "end" got a 200, a completion record, and twelve learner environments still
    running: measured on a 12-seat load test, still 12/12 active ten minutes
    after the end call returned.

    That is not merely untidy. A seat held by an ended workshop is a seat the
    next workshop's capacity plan already counted on, so the whole unit model
    is only as honest as this function being called.
    """
    return await _terminate_workshop_sessions(redis, ws_id)


async def _terminate_workshop_sessions(redis, ws_id: str) -> int:
    """Ask every session belonging to this workshop to stop.

    Sets the durable ``terminating`` flag and publishes on ``ops:terminate``.
    The flag is what makes this survive a worker that is restarting or briefly
    disconnected: the pub/sub message is fire-and-forget, the flag is not.
    """
    count = 0
    job_ids: set[str] = set()
    async for key in redis.scan_iter(match="job:running:*", count=500):
        try:
            if await redis.type(key) != "hash":
                continue
            rec = await redis.hgetall(key)
            if rec.get("workshop_id") != ws_id:
                continue
            job_id = key.split("job:running:", 1)[1]
            job_ids.add(job_id)
            await redis.hset(key, "terminating", "1")
            await redis.publish("ops:terminate", job_id)
            count += 1
        except Exception as exc:
            log.warning("workshop %s: could not terminate %s: %s",
                        scrub_for_log(ws_id), scrub_for_log(key), scrub_for_log(exc))

    count += await _drop_queued_jobs(redis, ws_id, job_ids)
    return count


async def _drop_queued_jobs(redis, ws_id: str, job_ids: set[str]) -> int:
    """Remove the workshop's not-yet-started jobs from the queues.

    Flagging ``terminating`` only reaches a job a worker has already claimed.
    Paced admission means a workshop can end with learners still parked, and
    those payloads would be admitted afterwards and build environments for a
    workshop nobody is attending — measured 2026-08-14: a workshop ended with
    three learners still in the pending list.

    Matched by job id rather than by payload content, because the queued
    payload carries no workshop id — only the ``job:running`` record does.
    """
    if not job_ids:
        return 0
    dropped = 0
    patterns = ("queue:pending:*", "queue:pool:*", "queue:test:*",
                "queue:direct:*")
    for pattern in patterns:
        async for key in redis.scan_iter(match=pattern, count=200):
            try:
                if await redis.type(key) != "list":
                    continue
                for payload in await redis.lrange(key, 0, -1):
                    try:
                        job_id = json.loads(payload).get("job_id", "")
                    except (ValueError, TypeError):
                        continue
                    if job_id in job_ids:
                        # LREM by exact value: the payload is unique, and this
                        # cannot disturb another learner's position in the queue.
                        removed = await redis.lrem(key, 1, payload)
                        if removed:
                            dropped += removed
                            log.info("workshop %s: dropped queued job %s from %s",
                                     scrub_for_log(ws_id), scrub_for_log(job_id),
                                     scrub_for_log(key))
            except Exception as exc:
                log.warning("workshop %s: could not scan %s: %s",
                            scrub_for_log(ws_id), scrub_for_log(key), scrub_for_log(exc))
    # And drop their job records. A learner who never started has no environment
    # to reap, so nothing else will ever clean these up: the terminate reconciler
    # deliberately no longer treats worker_id="queued" as an orphan (that bug
    # deleted every paced learner's record mid-session), which means ending the
    # workshop is now the only moment these can go. Left behind, they read as
    # running sessions for ever — five survived a workshop that had ended, with
    # no environment anywhere.
    for job_id in job_ids:
        try:
            rec = await redis.hgetall(f"job:running:{job_id}")
            # ONLY the explicit marker. "queued" is what api_arena_provision
            # writes before enqueueing, so it means "no worker has ever touched
            # this". An ABSENT worker_id is merely unknown, and deleting on
            # unknown would take a live learner's record with it — the opposite
            # asymmetry to _dead_worker_candidate, which treats both as
            # not-dead because there the safe answer is to leave things alone.
            if rec and (rec.get("worker_id") or "") == "queued":
                await redis.delete(f"job:running:{job_id}")
                log.info("workshop %s: dropped the record of never-started job %s",
                         scrub_for_log(ws_id), scrub_for_log(job_id))
        except Exception as exc:
            log.warning("workshop %s: could not drop the record for %s: %s",
                        scrub_for_log(ws_id), scrub_for_log(job_id), scrub_for_log(exc))

    return dropped


async def _lending_workers(redis) -> list[dict]:
    """Workers OUTSIDE the daily pool that lend seats INTO it.

    The standing workshop box reads the daily queue while it is under its lend
    cap, so its ``borrow_free`` seats are genuinely available to self-service —
    but it is not a daily worker, and ``_daily_workers`` correctly excludes it.
    Returned separately so the planner can count the seats without also counting
    the machine: a lender must not fill a slot in DAILY_MAX_WORKERS, and its
    memory/CPU pressure belongs to the lane that owns it.
    """
    out = []
    async for key in redis.scan_iter(match="worker:*", count=200):
        if key.count(":") != 1:
            continue
        try:
            h = await redis.hgetall(key)
        except Exception:
            continue
        if not h or "capacity" not in h:
            continue
        if (h.get("pool") or "daily") == "daily":
            continue                       # already counted by _daily_workers
        if (h.get("borrow_pool") or "") != "daily":
            continue
        wid = h.get("worker_id") or key.split(":", 1)[1]
        h.setdefault("worker_id", wid)
        # Same staleness rule as the daily scan: a terminated machine's record
        # can outlive it, and a frozen record would advertise seats forever.
        if fleet_policy.normalize_worker(wid, h, datetime.now(timezone.utc).timestamp())["stale"]:
            continue
        out.append(h)
    return out


async def _daily_workers(redis) -> list[dict]:
    """Heartbeats for the DAILY pool only.

    A missing ``pool`` field means daily: every worker predating pools is in the
    shared pool, and reading absence as "unknown" would exclude the entire
    existing fleet from its own autoscaler.

    Records whose heartbeat has stopped are skipped. A terminated machine leaves
    its ``worker:{id}`` hash behind, and a hash is not a worker: the one from a
    torn-down workshop pool sat at ``status: warming`` / ``capacity: 0``
    for ever, which reads as a daily worker that will never contribute a seat and
    would have had the loop scaling up against it indefinitely. Measured
    2026-08-16, on the first workshop machine the loop launched and terminated.
    """
    now = datetime.now(timezone.utc).timestamp()
    out = []
    async for key in redis.scan_iter(match="worker:*", count=200):
        if key.count(":") != 1:
            continue
        try:
            h = await redis.hgetall(key)
        except Exception:
            continue
        if not h or h.get("role") == "master":
            continue
        if (h.get("pool") or "daily") != "daily":
            continue
        wid = h.get("worker_id") or key.split(":", 1)[1]
        if fleet_policy.normalize_worker(wid, h, now)["stale"]:
            log.debug("skipping %s in the daily pool: heartbeat is stale", wid)
            continue
        h["worker_id"] = wid
        out.append(h)
    return out


async def tick(redis) -> dict:
    """One control pass. Returns a summary for logs and tests."""
    from dashboard import fleet

    now = datetime.now(timezone.utc)
    summary = {"prewarmed": [], "torn_down": [], "upgraded": [], "reaped": [],
               "daily": {}}

    # ── workshops ───────────────────────────────────────────────────────────
    index_ok = True
    try:
        ws_ids = await redis.zrevrange(LIVE_INDEX_KEY, 0, -1)
    except Exception as exc:
        log.warning("control loop: cannot read workshop index: %s", exc)
        ws_ids, index_ok = [], False

    for ws_id in ws_ids:
        try:
            session = await redis.hgetall(f"live:session:{ws_id}")
            if not session:
                continue
            if not manages(ws_id):
                continue
            rec = await _fleet_record(redis, ws_id)
            state = rec.get("state")

            # WARMING → READY once the pool's workers actually report ready.
            # Without this the record sits at "warming" for the whole workshop,
            # so the one field an operator reads to answer "are the machines up
            # yet" never answers it. Teardown worked anyway (it accepts either
            # state), which is exactly why the gap was easy to miss.
            if state == WARMING:
                pool_name = rec.get("pool", "")
                ready = await _pool_workers_ready(redis, pool_name)
                if ready:
                    rec["state"] = READY
                    rec["ready_at"] = now.isoformat()
                    rec["ready_workers"] = ready
                    await _save_fleet_record(redis, ws_id, rec)
                    state = READY
                    log.info("workshop %s: %d worker(s) ready", ws_id, ready)
                elif _warming_too_long(rec, now):
                    # Readiness requires slots_degraded == 0, so ONE worker that
                    # came up short holds the whole workshop at `warming` with no
                    # deadline — indistinguishable, to a trainer, from a hung
                    # fleet. Past the timeout, deliver with whatever warmed:
                    # partial capacity that learners can actually use beats a
                    # room that never opens. Said out loud, because a degraded
                    # pool must never look like a healthy one.
                    degraded = await _pool_workers_any(redis, pool_name)
                    rec["state"] = READY
                    rec["ready_at"] = now.isoformat()
                    rec["ready_workers"] = degraded
                    rec["degraded"] = True
                    await _save_fleet_record(redis, ws_id, rec)
                    state = READY
                    log.error("workshop %s: pool %s still not fully warm after "
                              "%d min — proceeding DEGRADED with %d worker(s); "
                              "seats may be short",
                              scrub_for_log(ws_id), scrub_for_log(pool_name),
                              WORKSHOP_WARMING_TIMEOUT_MINUTES, degraded)

            elif state == READY:
                # `ready_workers` was written ONCE, at the WARMING → READY
                # transition above, and never again. Machines that warm up a
                # moment later never got counted, so the number stayed at
                # whatever the first tick happened to catch — the APAC bootcamp
                # showed a trainer "1 ready" for the whole delivery while six
                # workers were up at 15/15 slots each.
                #
                # It is worth refreshing precisely because of WHEN a trainer
                # reads it: during provisioning, to decide whether to start. A
                # stale low number there reads as a broken fleet and invites
                # exactly the wrong action.
                #
                # Recount with the SAME predicate the record was written with —
                # a degraded pool must not silently start reporting the strict
                # count, or "degraded" and "ready_workers" would contradict each
                # other. A degraded pool that fully warms later clears the flag.
                degraded_rec = _truthy(rec.get("degraded"))
                pool_name = rec.get("pool", "")
                strict = await _pool_workers_ready(redis, pool_name)
                fresh = strict if not degraded_rec else max(
                    strict, await _pool_workers_any(redis, pool_name))
                changed = {}
                if fresh != _int(rec.get("ready_workers")):
                    changed["ready_workers"] = fresh
                if degraded_rec and strict and strict >= _int(rec.get("workers")):
                    # Every machine eventually came up: stop calling it degraded.
                    changed["degraded"] = ""
                if changed:
                    rec.update(changed)
                    await _save_fleet_record(redis, ws_id, rec)
                    log.info("workshop %s: %d worker(s) ready%s", scrub_for_log(ws_id),
                             fresh, " (degraded cleared)" if "degraded" in changed else "")

            # DONE is re-armable, and that is the whole point of listing it.
            # Teardown leaves the record at DONE and deletes the pool binding.
            # While DONE matched neither this tuple nor the teardown branch
            # below, a torn-down workshop became invisible to the loop FOREVER:
            # rescheduling it did nothing, no machines were ever launched again,
            # and — because the binding was gone too — its learners silently
            # routed to the shared daily queue and ate self-service capacity.
            # Seen live 2026-08-16 on a workshop rescheduled after its window.
            #
            # This cannot oscillate with the teardown branch. Re-arming needs
            # `due_for_prewarm`, which is false whenever `due_for_teardown` is
            # true, and a workshop that has just been torn down is by definition
            # past its end + grace. So DONE → WARMING only ever follows a real
            # reschedule, which is exactly the operator action it should follow.
            if should_prewarm(state, session, now):
                if CONTROL_LOOP_APPLY:
                    await provision_workshop_fleet(redis, ws_id, session)
                else:
                    seats = await _planned_seats(redis, ws_id, session)
                    profile = await repo_profiles.load(redis, workshop_repo(session))
                    plan = plan_workshop_capacity(seats, profile)
                    log.info("DRY-RUN workshop %s (%s): would launch %d × %s "
                             "for %d seats — %s", ws_id, session.get("title", ""),
                             plan["workers"], WORKSHOP_INSTANCE_TYPE, seats,
                             plan["reason"])
                summary["prewarmed"].append(ws_id)
            elif should_teardown(state, session, now):
                if CONTROL_LOOP_APPLY:
                    await teardown_workshop_fleet(redis, ws_id)
                else:
                    log.info("DRY-RUN workshop %s: would tear down %s",
                             ws_id, rec.get("instances") or "(no instances)")
                summary["torn_down"].append(ws_id)
            elif state == READY and rec.get("standing"):
                # Third scheduling predicate, and the comment above about
                # prewarm/teardown being mutually exclusive applies to it too.
                # It cannot oscillate with either: it fires only on a record
                # that launched NOTHING, and the moment it acts the record stops
                # being standing, so it can never fire on the same workshop
                # twice. It is checked last so a workshop past its window is
                # torn down rather than upgraded on its way out.
                seats = await _planned_seats(redis, ws_id, session)
                if needs_bigger_fleet(state, rec, seats):
                    if CONTROL_LOOP_APPLY:
                        await provision_workshop_fleet(redis, ws_id, session)
                    else:
                        log.info("DRY-RUN workshop %s: would UPGRADE off the "
                                 "standing lane — booked capacity is now %d "
                                 "seats (> %d)", ws_id, seats,
                                 WORKSHOP_STANDING_MAX_SEATS)
                    summary["upgraded"].append(ws_id)
        except Exception as exc:
            # One malformed workshop must not stop the loop serving the others.
            log.warning("control loop: workshop %s failed this tick: %s", ws_id, exc)

    # ── orphaned fleets ─────────────────────────────────────────────────────
    # The loop above is driven by the workshop INDEX. Deleting a workshop takes
    # it out of that index and deletes its hash, but leaves its fleet record —
    # and its machines — behind, where nothing could ever see them again. They
    # then ran until the in-instance `shutdown -h +N` backstop, hours later.
    # Measured 2026-08-17: 2 × m6a.4xlarge for a workshop that no longer existed,
    # plus four more records holding stale pool bindings.
    #
    # Driven from the FLEET HASH for exactly that reason: after a delete it is
    # the only structure that still knows the machines exist. Deliberately a
    # SEPARATE pass rather than a fourth branch in the loop above, which cannot
    # reach a workshop it cannot enumerate.
    #
    # It cannot fight the other three predicates: they only ever act on indexed
    # workshops, and this only ever acts on unindexed ones.
    try:
        fleet_ids = await redis.hkeys(FLEET_KEY)
    except Exception as exc:
        log.warning("control loop: cannot read the fleet hash: %s", exc)
        fleet_ids = []
    for ws_id in orphan_candidates(fleet_ids, ws_ids, index_ok):
        try:
            if not manages(ws_id):
                continue
            # Absent from the index AND absent as a hash. The index self-heals by
            # dropping members whose hash is gone, so the two normally agree —
            # requiring both means a half-finished delete cannot cost machines.
            try:
                exists = bool(await redis.exists(f"live:session:{ws_id}"))
            except Exception:
                exists = None          # unreadable is NOT deleted — see is_orphaned
            rec = await _fleet_record(redis, ws_id)
            if not is_orphaned(rec.get("state"), exists):
                continue
            instances = rec.get("instances") or []
            log.warning("workshop %s no longer exists but still holds a fleet "
                        "(%s, %d instance(s)) — reaping",
                        scrub_for_log(ws_id), rec.get("state"), len(instances))
            if CONTROL_LOOP_APPLY:
                await teardown_workshop_fleet(redis, ws_id)
            else:
                log.info("DRY-RUN workshop %s: would reap %s",
                         ws_id, instances or "(no instances)")
            summary["reaped"].append(ws_id)
        except Exception as exc:
            log.warning("control loop: reaping %s failed this tick: %s",
                        scrub_for_log(ws_id), exc)

    # ── daily pool ──────────────────────────────────────────────────────────
    try:
        workers = await _daily_workers(redis)
        raw = await redis.hgetall(PRESSURE_KEY) or {}
        ticks = {k: int(v or 0) for k, v in raw.items()}
        ticks = update_pressure(ticks, workers)
        await redis.delete(PRESSURE_KEY)
        if ticks:
            await redis.hset(PRESSURE_KEY, mapping={k: str(v) for k, v in ticks.items()})

        decision = daily_scale_decision(workers, ticks,
                                        lenders=await _lending_workers(redis))
        summary["daily"] = decision

        if not CONTROL_LOOP_APPLY:
            if decision["scale_up"] or decision["shrink"] or decision["brake"]:
                log.info("DRY-RUN daily: would scale_up=%d shrink=%s brake=%s — %s",
                         decision["scale_up"], decision["shrink"],
                         decision["brake"], decision["why"])
            return summary

        for wid in decision["shrink"]:
            # Withdraw the seats this worker cannot actually back. Capacity is
            # what the scale planner consumes, so lowering it here is what stops
            # a scale-up from overfilling the same box again.
            try:
                current = int((await redis.hget(f"worker:{wid}", "capacity")) or 0)
                if current > 1:
                    await redis.hset(f"worker:{wid}", "capacity", str(current - 1))
                    log.warning("daily: shrank %s to %d seats — sustained memory "
                                "pressure means its profile is optimistic",
                                wid, current - 1)
            except Exception as exc:
                log.warning("daily: could not shrink %s: %s", wid, exc)

        for wid in decision["brake"]:
            # Not a cordon: the worker keeps its sessions and its seats, it just
            # stops taking NEW ones until the burst passes.
            try:
                await redis.hset(f"worker:{wid}", "admission_brake", "1")
            except Exception:
                pass
        for w in workers:
            wid = w.get("worker_id", "")
            if wid and wid not in decision["brake"] and w.get("admission_brake"):
                await redis.hset(f"worker:{wid}", "admission_brake", "0")

        if decision["scale_up"]:
            log.info("daily: scaling up %d (%s)", decision["scale_up"], decision["why"])
            try:
                # Pin the slot count from the unit model. Without an explicit
                # capacity the worker keeps the AMI's baked WORKER_CAPACITY,
                # which an explicit env var makes win over the unit derivation
                # — the golden AMI carries 6 from the c5.2xlarge era, so every
                # autoscaled m6a.2xlarge advertised 6 seats instead of 10 and
                # the pool kept buying more of them. Measured live 2026-08-16.
                await fleet.scale_up(decision["scale_up"],
                                     instance_type=DAILY_INSTANCE_TYPE,
                                     purchasing="spot", pool="daily",
                                     capacity=capacity_units.units_for_instance(
                                         DAILY_INSTANCE_TYPE) or None)
            except Exception as exc:
                log.error("daily: scale-up failed: %s", exc)
    except Exception as exc:
        log.warning("control loop: daily pass failed: %s", exc)

    return summary


async def control_loop(redis) -> None:
    if not CONTROL_LOOP_ENABLED:
        log.info("Control loop disabled (CONTROL_LOOP_ENABLED=0)")
        return
    log.info("Control loop: prewarm %d min ahead (≤%d), teardown %d min after end "
             "or %d min after start (≤%d), whichever is later, tick %.0fs, "
             "workshop seats ×%.2f safety, workshops=%s",
             PREWARM_LEAD_MINUTES, LEAD_MINUTES_CAP, TEARDOWN_GRACE_MINUTES,
             WORKSHOP_HOLD_MINUTES, HOLD_MINUTES_CAP, CONTROL_TICK_S,
             WORKSHOP_SEAT_SAFETY, CONTROL_LOOP_WORKSHOPS)
    if not CONTROL_LOOP_APPLY:
        log.warning("Control loop is in DRY RUN — it will log what it would do "
                    "and launch NOTHING. Set CONTROL_LOOP_APPLY=1 to act.")
    while True:
        try:
            await tick(redis)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                              # pragma: no cover
            log.warning("control tick failed: %s", exc)
        await asyncio.sleep(CONTROL_TICK_S)

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

from dashboard import repo_profiles

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
PRESSURE_KEY = "worker:pressure"      # hash: worker_id -> consecutive tick count

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


def due_for_prewarm(session: dict, now: datetime,
                    lead_minutes: int = PREWARM_LEAD_MINUTES) -> bool:
    """Is it time to start this workshop's machines?

    A workshop already past its start time is still due: a trainer who opens
    the room late, or a loop that was restarted, must still get machines rather
    than being silently skipped for having missed the window.
    """
    if session.get("state") in ("ended", "cancelled", "deleted"):
        return False
    start = parse_iso(session.get("scheduledAt", ""))
    if start is None:
        return False
    return now >= start - timedelta(minutes=lead_minutes)


def workshop_repo(session: dict) -> str:
    """Which repo a workshop delivers, for profile lookup.

    ``repoUrl`` is preferred because it is the unambiguous one. ``trainingId``
    is a catalog id and does NOT match the repo name — a live workshop stores
    ``kubernetes-101`` for ``enablement-kubernetes-101`` — so relying on it
    alone silently sent every workshop to the heavy default.
    """
    return (session.get("repoUrl") or session.get("trainingId")
            or session.get("repo") or "")


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
    """Give the machines back — either the workshop ended, or it overran its
    scheduled window plus grace.

    An explicit end always wins over the clock: a trainer who finishes early
    should not pay for an hour of idle machines.
    """
    if session.get("state") in ("ended", "cancelled", "deleted"):
        return True
    end = workshop_end(session)
    return end is not None and now >= end + timedelta(minutes=grace_minutes)


def plan_workshop_capacity(seats: int, profile: repo_profiles.RepoProfile,
                           instance_type: str = WORKSHOP_INSTANCE_TYPE,
                           safety: float = WORKSHOP_SEAT_SAFETY) -> dict:
    """Machines and per-machine seats for a workshop of ``seats`` people.

    Returns ``workers``, ``seats_per_worker``, ``total_seats`` and whether the
    profile behind it was estimated. ``workers == 0`` means refuse to plan --
    an unknown instance type must never be guessed at.
    """
    per = repo_profiles.seats_per_worker(profile, instance_type, safety)
    if per <= 0:
        return {"workers": 0, "seats_per_worker": 0, "total_seats": 0,
                "estimated": profile.estimated,
                "reason": f"no capacity model for {instance_type}"}
    workers = -(-max(0, seats) // per)
    return {
        "workers": workers,
        "seats_per_worker": per,
        "total_seats": workers * per,
        "estimated": profile.estimated,
        "reason": (f"{seats} seats ÷ {per}/worker"
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
                         max_workers: int = DAILY_MAX_WORKERS) -> dict:
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

    scale_up = 0
    if free < min_free:
        reasons.append(f"{free} free seats < {min_free}")
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


async def _roster_size(redis, ws_id: str) -> int:
    """Seats to plan for: the roster plus the trainer, who also gets an
    environment and is otherwise the one person left without one."""
    try:
        return int(await redis.scard(f"live:session:{ws_id}:roster")) + 1
    except Exception:
        return 1


async def provision_workshop_fleet(redis, ws_id: str, session: dict) -> dict:
    """Launch dedicated machines for a workshop and bind it to its pool.

    Order matters: the pool binding is written BEFORE the instances exist. A
    learner who arrives early then queues on the pool queue and waits for its
    machines, instead of being scheduled onto the daily pool where they would
    consume a self-service seat and escape the workshop's isolation.
    """
    from dashboard import fleet, pools

    existing = await _fleet_record(redis, ws_id)
    if existing and existing.get("state") in (WARMING, READY):
        return existing

    repo = workshop_repo(session)
    seats = await _roster_size(redis, ws_id)
    profile = await repo_profiles.load(redis, repo)
    plan = plan_workshop_capacity(seats, profile)

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
        launched = await fleet.scale_up(
            plan["workers"],
            instance_type=WORKSHOP_INSTANCE_TYPE,
            purchasing=WORKSHOP_PURCHASING,
            pool=pool_name,
            capacity=plan["seats_per_worker"],
            # A workshop worker serves one repo, so its slots can be capped for
            # that repo specifically rather than at a flat figure chosen for
            # the lightest one.
            slot_memory_mb=repo_profiles.slot_memory_cap_mb(profile),
            code_branch=WORKER_CODE_BRANCH,
        )
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

    for wid in await _pool_worker_ids(redis, rec.get("pool", "")):
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

    rec["state"] = DONE
    rec["ended_at"] = datetime.now(timezone.utc).isoformat()
    await _save_fleet_record(redis, ws_id, rec)
    return rec


async def _instances_tagged(pool_name: str) -> list[str]:
    """Live EC2 instance ids carrying this pool's tag.

    Deliberately queries AWS rather than Redis: when a workshop's machines need
    giving back, the question is "what is actually still running", and Redis is
    the component most likely to be the reason the record is wrong.
    """
    if not pool_name:
        return []
    from dashboard import fleet
    try:
        data = await fleet._aws(
            "ec2", "describe-instances",
            "--filters", f"Name=tag:orbital-pool,Values={pool_name}",
            "Name=instance-state-name,Values=pending,running,stopping,stopped")
    except Exception as exc:
        log.warning("could not list instances for pool %s: %s", pool_name, exc)
        return []
    return [i.get("InstanceId") for r in (data or {}).get("Reservations", [])
            for i in r.get("Instances", []) if i.get("InstanceId")]


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


async def _terminate_workshop_sessions(redis, ws_id: str) -> int:
    """Ask every session belonging to this workshop to stop.

    Sets the durable ``terminating`` flag and publishes on ``ops:terminate``.
    The flag is what makes this survive a worker that is restarting or briefly
    disconnected: the pub/sub message is fire-and-forget, the flag is not.
    """
    count = 0
    async for key in redis.scan_iter(match="job:running:*", count=500):
        try:
            if await redis.type(key) != "hash":
                continue
            rec = await redis.hgetall(key)
            if rec.get("workshop_id") != ws_id:
                continue
            job_id = key.split("job:running:", 1)[1]
            await redis.hset(key, "terminating", "1")
            await redis.publish("ops:terminate", job_id)
            count += 1
        except Exception as exc:
            log.warning("workshop %s: could not terminate %s: %s", ws_id, key, exc)
    return count


async def _daily_workers(redis) -> list[dict]:
    """Heartbeats for the DAILY pool only.

    A missing ``pool`` field means daily: every worker predating pools is in the
    shared pool, and reading absence as "unknown" would exclude the entire
    existing fleet from its own autoscaler.
    """
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
        h["worker_id"] = h.get("worker_id") or key.split(":", 1)[1]
        out.append(h)
    return out


async def tick(redis) -> dict:
    """One control pass. Returns a summary for logs and tests."""
    from dashboard import fleet

    now = datetime.now(timezone.utc)
    summary = {"prewarmed": [], "torn_down": [], "daily": {}}

    # ── workshops ───────────────────────────────────────────────────────────
    try:
        ws_ids = await redis.zrevrange(LIVE_INDEX_KEY, 0, -1)
    except Exception as exc:
        log.warning("control loop: cannot read workshop index: %s", exc)
        ws_ids = []

    for ws_id in ws_ids:
        try:
            session = await redis.hgetall(f"live:session:{ws_id}")
            if not session:
                continue
            if not manages(ws_id):
                continue
            rec = await _fleet_record(redis, ws_id)
            state = rec.get("state")

            if state in (None, "", "failed") and due_for_prewarm(session, now):
                if CONTROL_LOOP_APPLY:
                    await provision_workshop_fleet(redis, ws_id, session)
                else:
                    seats = await _roster_size(redis, ws_id)
                    profile = await repo_profiles.load(redis, workshop_repo(session))
                    plan = plan_workshop_capacity(seats, profile)
                    log.info("DRY-RUN workshop %s (%s): would launch %d × %s "
                             "for %d seats — %s", ws_id, session.get("title", ""),
                             plan["workers"], WORKSHOP_INSTANCE_TYPE, seats,
                             plan["reason"])
                summary["prewarmed"].append(ws_id)
            elif state in (WARMING, READY) and due_for_teardown(session, now):
                if CONTROL_LOOP_APPLY:
                    await teardown_workshop_fleet(redis, ws_id)
                else:
                    log.info("DRY-RUN workshop %s: would tear down %s",
                             ws_id, rec.get("instances") or "(no instances)")
                summary["torn_down"].append(ws_id)
        except Exception as exc:
            # One malformed workshop must not stop the loop serving the others.
            log.warning("control loop: workshop %s failed this tick: %s", ws_id, exc)

    # ── daily pool ──────────────────────────────────────────────────────────
    try:
        workers = await _daily_workers(redis)
        raw = await redis.hgetall(PRESSURE_KEY) or {}
        ticks = {k: int(v or 0) for k, v in raw.items()}
        ticks = update_pressure(ticks, workers)
        await redis.delete(PRESSURE_KEY)
        if ticks:
            await redis.hset(PRESSURE_KEY, mapping={k: str(v) for k, v in ticks.items()})

        decision = daily_scale_decision(workers, ticks)
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
                await fleet.scale_up(decision["scale_up"],
                                     instance_type=DAILY_INSTANCE_TYPE,
                                     purchasing="spot", pool="daily")
            except Exception as exc:
                log.error("daily: scale-up failed: %s", exc)
    except Exception as exc:
        log.warning("control loop: daily pass failed: %s", exc)

    return summary


async def control_loop(redis) -> None:
    if not CONTROL_LOOP_ENABLED:
        log.info("Control loop disabled (CONTROL_LOOP_ENABLED=0)")
        return
    log.info("Control loop: prewarm %d min ahead, teardown %d min after, tick %.0fs, "
             "workshop seats ×%.2f safety, workshops=%s",
             PREWARM_LEAD_MINUTES, TEARDOWN_GRACE_MINUTES, CONTROL_TICK_S,
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

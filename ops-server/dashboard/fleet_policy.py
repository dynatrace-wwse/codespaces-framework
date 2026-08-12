"""Fleet autoscaling policy — pure decision logic, no AWS and no Redis.

Everything here is a plain function over plain data so the rules that decide
whether to spend money can be unit-tested without a network (see
``dashboard/test_fleet_policy.py``). ``dashboard/fleet.py`` does the AWS calls;
``dashboard/app.py`` wires them to endpoints.

WHY A SEAT TARGET RATHER THAN A UTILISATION TARGET
--------------------------------------------------
A training session is a 2-hour stateful unit that takes minutes to create and
cannot be moved once placed. That breaks the two reflexes people reach for:

* **Queue depth is a lagging signal.** A job sitting in ``queue:test:amd64``
  means a learner is *already* waiting. Worse, the usual backlog-per-instance
  recipe assumes capacity frees up when the item is processed — here the slot
  stays held for the next two hours.
* **CPU utilisation is anti-correlated with what we care about.** A worker
  holding six idle-but-occupied learner sessions shows almost no CPU and has
  zero free slots. Measured: 0.127 vCPU per session, so a fully packed
  c5.2xlarge sits near 6% CPU with nothing left to give.

So the controller tracks an absolute count of **usable free slots** and is
driven by a seat target the trainer actually knows in advance ("secure 70").

THE CAPACITY MODEL IS MEASURED, NOT ASSUMED
-------------------------------------------
From a load test on amd001 (c5.2xlarge) on 2026-08-12, running Kubernetes-101
all the way through the operator + DynaKube steps:

    committed per session (anon + slab)   1,609 MiB
    transient peak during provisioning    2.2 - 3.1 GiB
    CPU per session (65 min average)      0.127 vCPU
    base host overhead (OS+docker+agent)  ~1,260 MiB
    warm idle slot                        ~62 MiB

Twelve sessions were run on one c5.2xlarge to find the ceiling by breaking it:
memory pressure hit 35%, pods restarted 3-6 times, ActiveGate never pulled its
image, and all six lab installs timed out. Six recovered cleanly. The model
below reproduces that (7 for c5.2xlarge, so the production setting of 6 has one
slot of margin) which is the only validation a model like this can have.
"""

from __future__ import annotations

import math

# ── measured constants (MiB) ────────────────────────────────────────────────
# Committed (unreclaimable) memory a full Kubernetes-101 session holds.
SESSION_COMMITTED_MB = 1609
# OS + dockerd + sysbox + agent, before any slot exists.
HOST_BASE_OVERHEAD_MB = 1260
# Page cache + provisioning burst headroom kept free host-wide. Page cache is
# reclaimable and largely shared between slots (same image), so this is a flat
# reserve rather than a per-session term.
HOST_CACHE_RESERVE_MB = 1500
# Applied to the raw memory arithmetic. The 12-session test failed on memory
# pressure well before the arithmetic ceiling, and kernel limits (PIDs, inotify,
# conntrack) are unmeasured above ~12 slots — so plan below the ceiling.
SLOT_SAFETY_FACTOR = 0.8

# ── the CPU term (measured 2026-08-12 on r6a.2xlarge, 30 sessions) ──────────
# Memory is NOT the only ceiling. Running the Kubernetes-101 lab
# (dynatraceDeployOperator + deployApplicationMonitoring) on 30 sessions at once
# on 8 vCPU left memory completely comfortable — 41.7 GiB of 62.9, memory
# pressure ~0% — while CPU pressure sat at 98% for eighteen minutes. Every
# install eventually converged, but all 30 blew past the framework's own
# readiness gate (dynatraceDeployOperator waits 60 × 10s), which a learner sees
# as "Run solution" failing.
#
# So the binding constraint for an INSTRUCTOR-LED class — where everyone is told
# to run the same step at the same moment — is CPU, not RAM.
#
#   measured: ~265 CPU-seconds per lab install (8 vCPU × ~18 min × 98%, minus
#             the steady-state draw of 30 idle sessions)
#   gate:     600 s before the framework gives up
#   =>        N concurrent installs on V vCPU each get V/N cores, so
#             265·N/V <= 600  →  N <= 2.26·V
#
# Rounded down to 2.0 for margin.
LAB_INSTALL_CPU_SECONDS = 265
LAB_READINESS_GATE_S = 600
SESSIONS_PER_VCPU = 2.0

# Nominal usable RAM (MiB) as the OS reports it — i.e. after firmware/kernel
# reserve. c5.2xlarge and r6a.2xlarge are measured; the rest follow the same
# ~97.5% of nominal ratio.
INSTANCE_MEMORY_MB = {
    "c5.2xlarge":   15616,   # measured, amd001
    "c5.4xlarge":   31280,
    "m5.2xlarge":   31280,
    "m6a.2xlarge":  31690,
    "m6i.2xlarge":  31690,
    "m6a.4xlarge":  63380,
    "r5.2xlarge":   62919,
    "r6a.2xlarge":  62919,   # measured, density box i-0f6216c15887b791f
    "r6i.2xlarge":  62919,
    "r6a.4xlarge":  128560,
    "r6i.4xlarge":  128560,
    "r5.4xlarge":   128560,
}

INSTANCE_VCPUS = {
    "c5.2xlarge":    8,
    "c5.4xlarge":   16,
    "m5.2xlarge":    8,
    "m6a.2xlarge":   8,
    "m6i.2xlarge":   8,
    "m6a.4xlarge":  16,
    "r5.2xlarge":    8,
    "r6a.2xlarge":   8,      # measured
    "r6i.2xlarge":   8,
    "r6a.4xlarge":  16,
    "r6i.4xlarge":  16,
    "r5.4xlarge":   16,
}

DEFAULT_INSTANCE_TYPE = "r6a.2xlarge"

# ── regions ─────────────────────────────────────────────────────────────────
# Deliberately a short curated list, not all 30+ AWS regions: the choice that
# matters is "near which audience", and a long dropdown invites a mis-click that
# strands workers in a region with no AMI. Ordered by continent.
REGIONS = [
    {"id": "ap-southeast-1", "label": "Singapore",      "area": "Asia Pacific"},
    {"id": "ap-northeast-1", "label": "Tokyo",          "area": "Asia Pacific"},
    {"id": "ap-south-1",     "label": "Mumbai",         "area": "Asia Pacific"},
    {"id": "ap-southeast-2", "label": "Sydney",         "area": "Asia Pacific"},
    {"id": "eu-west-2",      "label": "London",         "area": "Europe"},
    {"id": "eu-central-1",   "label": "Frankfurt",      "area": "Europe"},
    {"id": "us-east-1",      "label": "N. Virginia",    "area": "North America"},
    {"id": "us-west-2",      "label": "Oregon",         "area": "North America"},
    {"id": "sa-east-1",      "label": "São Paulo",      "area": "Latin America"},
]
HOME_REGION = "eu-west-2"


def region_ids() -> list[str]:
    return [r["id"] for r in REGIONS]


def is_known_region(region: str) -> bool:
    return region in region_ids()


# ── capacity model ──────────────────────────────────────────────────────────

def memory_slots(instance_type: str, safety: float = SLOT_SAFETY_FACTOR) -> int:
    """Sessions an instance can HOLD, from the measured committed footprint."""
    mem = INSTANCE_MEMORY_MB.get(instance_type, 0)
    if mem <= 0:
        return 0
    usable = mem - HOST_BASE_OVERHEAD_MB - HOST_CACHE_RESERVE_MB
    if usable <= 0:
        return 0
    return max(0, int((usable / SESSION_COMMITTED_MB) * safety))


def cpu_slots(instance_type: str) -> int:
    """Sessions that can run the lab's heavy step SIMULTANEOUSLY and still pass
    the framework's readiness gate. See SESSIONS_PER_VCPU for the measurement.
    """
    vcpus = INSTANCE_VCPUS.get(instance_type, 0)
    return int(vcpus * SESSIONS_PER_VCPU) if vcpus > 0 else 0


def slots_for_instance(instance_type: str, safety: float = SLOT_SAFETY_FACTOR) -> int:
    """How many concurrent full-lab sessions one instance should be planned for.

    The lower of the memory ceiling and the CPU ceiling. Both are measured, and
    which one binds depends on the shape: a c5.2xlarge runs out of RAM first
    (6 vs 16), an r6a.2xlarge runs out of CPU first (16 vs 29). Planning on
    memory alone is what would have oversold an r-family box by nearly 2×.

    Returns 0 for an unknown type — callers must treat that as "refuse to plan"
    rather than guessing, because guessing high is how a class gets oversold.
    """
    mem_slots = memory_slots(instance_type, safety)
    cpu_cap = cpu_slots(instance_type)
    if mem_slots <= 0 or cpu_cap <= 0:
        return 0
    return min(mem_slots, cpu_cap)


def limiting_factor(instance_type: str) -> str:
    """Which ceiling binds for this shape — shown in the UI so the operator can
    see whether more RAM or more cores would actually buy anything."""
    m, c = memory_slots(instance_type), cpu_slots(instance_type)
    if m <= 0 or c <= 0:
        return "unknown"
    return "memory" if m < c else ("cpu" if c < m else "balanced")


def instances_for_seats(seats: int, instance_type: str = DEFAULT_INSTANCE_TYPE,
                        redundancy: bool = True) -> int:
    """Instances needed to seat ``seats`` learners.

    With ``redundancy`` (the default) one extra instance is added so the loss of
    any single host still leaves enough slots — on-demand removes spot reclaim,
    not hardware failure.
    """
    per = slots_for_instance(instance_type)
    if per <= 0 or seats <= 0:
        return 0
    return math.ceil(seats / per) + (1 if redundancy else 0)


# ── fleet state ─────────────────────────────────────────────────────────────

def _int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes")


def normalize_worker(worker_id: str, h: dict, now_epoch: float,
                     stale_after_s: int = 90) -> dict:
    """Flatten one ``worker:{id}`` hash into the fields a decision needs.

    ``slots_ready`` is used in preference to ``capacity`` and both fall back to
    each other, so this reads correctly against a worker still running the old
    agent (which publishes only ``capacity``) during a rolling deploy.
    """
    heartbeat_age = None
    raw_hb = h.get("last_heartbeat") or ""
    if raw_hb:
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(raw_hb.replace("Z", "+00:00"))
            heartbeat_age = max(0.0, now_epoch - ts.timestamp())
        except (ValueError, AttributeError):
            heartbeat_age = None

    ready = _int(h.get("slots_ready"), _int(h.get("capacity")))
    total = _int(h.get("slots_total"), _int(h.get("capacity")))
    active = _int(h.get("active_jobs"))
    free = _int(h.get("slots_free"), max(0, ready - active))
    status = (h.get("status") or "").strip().lower()

    return {
        "worker_id": worker_id,
        "role": (h.get("role") or "").strip().lower(),
        "status": status,
        "draining": _truthy(h.get("draining")),
        "slots_ready": ready,
        "slots_total": total,
        "slots_free": max(0, free),
        "active_jobs": active,
        "warming": status == "warming" or (total > 0 and ready < total),
        "stale": heartbeat_age is not None and heartbeat_age > stale_after_s,
        "heartbeat_age_s": heartbeat_age,
        "host": h.get("host") or "",
        "mem_available_mb": _int(h.get("mem_available_mb")),
        "time_to_ready_s": _int(h.get("time_to_ready_s")),
    }


def fleet_state(workers: list[dict], inflight: list[dict],
                slots_per_instance: int) -> dict:
    """Aggregate usable capacity.

    A worker contributes free slots only when it is live, not cordoned and not
    stale. A *warming* worker contributes the slots it has warmed so far — which
    is exactly why the agent had to stop advertising its nominal capacity from
    second zero.

    In-flight launches contribute their EXPECTED slots. Without that term a
    four-minute boot against a fifteen-second tick re-decides the same deficit
    sixteen times and launches sixteen times the capacity.
    """
    usable = [w for w in workers
              if not w["stale"] and not w["draining"] and w["role"] != "master"]
    free_ready = sum(w["slots_free"] for w in usable)
    free_inflight = sum(_int(i.get("expected_slots"), slots_per_instance)
                        for i in inflight)
    return {
        "workers_total": len(workers),
        "workers_usable": len(usable),
        "workers_warming": sum(1 for w in usable if w["warming"]),
        "workers_draining": sum(1 for w in workers if w["draining"]),
        "workers_stale": sum(1 for w in workers if w["stale"]),
        "slots_ready": sum(w["slots_ready"] for w in usable),
        "slots_active": sum(w["active_jobs"] for w in usable),
        "free_ready": free_ready,
        "free_inflight": free_inflight,
        "free_effective": free_ready + free_inflight,
        "inflight_launches": len(inflight),
    }


# ── decisions ───────────────────────────────────────────────────────────────

class PolicyRefusal(Exception):
    """A scale decision was refused by a guardrail. The message is user-facing."""


def plan_scale_up(target_seats: int, state: dict, *,
                  instance_type: str = DEFAULT_INSTANCE_TYPE,
                  workers_draining: list[dict] | None = None,
                  max_fleet: int = 40,
                  per_call_cap: int = 4,
                  quota_headroom_instances: int | None = None,
                  frozen: bool = False) -> dict:
    """Decide how to reach ``target_seats`` free slots.

    Order matters: un-draining a cordoned worker is instant and free, launching
    is minutes and costs money — so a cordoned worker is always reclaimed before
    an instance is bought.
    """
    if frozen:
        raise PolicyRefusal(
            "fleet is frozen (fleet:frozen=1) — clear the freeze before scaling"
        )
    if target_seats < 0:
        raise PolicyRefusal("target seats must be >= 0")

    per_instance = slots_for_instance(instance_type)
    if per_instance <= 0:
        raise PolicyRefusal(
            f"unknown instance type {instance_type!r} — refusing to plan "
            "capacity from a guessed session density"
        )

    deficit = target_seats - state["free_effective"]
    if deficit <= 0:
        return {
            "action": "none",
            "launch": 0, "undrain": [],
            "deficit": 0, "per_instance": per_instance,
            "reason": (
                f"{state['free_effective']} effective free slots already meet "
                f"target {target_seats}"
            ),
        }

    # Step 1 — reclaim cordoned workers first.
    undrain: list[str] = []
    recovered = 0
    for w in sorted(workers_draining or [], key=lambda x: -x["slots_free"]):
        if recovered >= deficit:
            break
        undrain.append(w["worker_id"])
        recovered += w["slots_free"]
    remaining = deficit - recovered

    if remaining <= 0:
        return {
            "action": "undrain",
            "launch": 0, "undrain": undrain,
            "deficit": deficit, "per_instance": per_instance,
            "reason": (
                f"un-cordoning {len(undrain)} worker(s) recovers {recovered} "
                f"slots — no launch needed"
            ),
        }

    # Step 2 — launch for what's left.
    wanted = math.ceil(remaining / per_instance)
    caps = {
        "per_call_cap": per_call_cap,
        "max_fleet": max(0, max_fleet - state["workers_total"] - state["inflight_launches"]),
    }
    if quota_headroom_instances is not None:
        caps["vcpu_quota"] = max(0, quota_headroom_instances)

    limiting = min(caps, key=lambda k: caps[k])
    launch = max(0, min(wanted, *caps.values()))

    if launch == 0:
        raise PolicyRefusal(
            f"need {wanted} more instance(s) for {remaining} slots but the "
            f"{limiting} guardrail allows 0"
        )

    reason = (
        f"{remaining} slots short after un-draining → launch {launch} × "
        f"{instance_type} ({per_instance} slots each)"
    )
    if launch < wanted:
        reason += f" — capped by {limiting} (wanted {wanted})"

    return {
        "action": "launch",
        "launch": launch,
        "undrain": undrain,
        "deficit": deficit,
        "per_instance": per_instance,
        "capped_by": limiting if launch < wanted else None,
        "reason": reason,
    }


def plan_scale_down(target_seats: int, state: dict, workers: list[dict], *,
                    min_workers: int = 1) -> dict:
    """Pick workers to cordon when there is more capacity than the target needs.

    Only ever cordons — termination is a separate step that happens once a
    cordoned worker reports ``active_jobs == 0``. A session cannot be moved, so
    an instance can only die empty.

    Candidates are emptiest-first: it is the one that will reach zero soonest,
    and cordoning it strands the fewest learners if the decision is reversed.
    """
    surplus = state["free_effective"] - target_seats
    candidates = [w for w in workers
                  if w["role"] != "master" and not w["draining"]
                  and not w["stale"] and not w["warming"]]
    live_count = len(candidates)

    if surplus <= 0 or live_count <= min_workers:
        return {"action": "none", "cordon": [], "surplus": max(0, surplus),
                "reason": "no surplus above target" if surplus <= 0
                          else f"at the {min_workers}-worker floor"}

    cordon: list[str] = []
    freed = 0
    for w in sorted(candidates, key=lambda x: (x["active_jobs"], -x["slots_free"])):
        if live_count - len(cordon) <= min_workers:
            break
        # Only cordon while the worker's whole contribution fits in the surplus,
        # otherwise removing it would drop us below target.
        if freed + w["slots_ready"] > surplus:
            continue
        cordon.append(w["worker_id"])
        freed += w["slots_ready"]

    if not cordon:
        return {"action": "none", "cordon": [], "surplus": surplus,
                "reason": f"{surplus} surplus slots, but no single worker fits "
                          "inside it without dropping below target"}

    return {
        "action": "cordon",
        "cordon": cordon,
        "surplus": surplus,
        "reason": (
            f"{surplus} slots above target → cordon {len(cordon)} worker(s), "
            f"terminating each once its sessions end"
        ),
    }


def terminatable(workers: list[dict], grace_s: int = 300) -> list[str]:
    """Cordoned workers that are empty and may now be terminated.

    ``grace_s`` is applied by the caller against ``drain_started_at``; this
    function only answers the "is it empty and cordoned" half.
    """
    return [w["worker_id"] for w in workers
            if w["draining"] and w["active_jobs"] == 0 and w["role"] != "master"]


def estimate_cost(instance_count: int, instance_type: str, hours: float,
                  price_per_hour: dict[str, float] | None = None) -> dict:
    """Rough spend for a planned fleet, so the UI can show it BEFORE committing.

    Prices are on-demand list, USD/hour. They are a planning aid, not a bill —
    spot differs, regions differ, and AWS changes them.
    """
    prices = price_per_hour or ON_DEMAND_USD_PER_HOUR.get(HOME_REGION, {})
    unit = prices.get(instance_type)
    if unit is None or instance_count <= 0:
        return {"known": False, "total_usd": None, "unit_usd": unit}
    total = unit * instance_count * hours
    return {
        "known": True,
        "unit_usd": unit,
        "hourly_usd": round(unit * instance_count, 4),
        "total_usd": round(total, 2),
        "daily_usd": round(unit * instance_count * 24, 2),
    }


# On-demand list prices, USD/hour, from the AWS pricing feed published
# 2026-08-10. Planning figures only — refresh when they drift.
ON_DEMAND_USD_PER_HOUR = {
    "eu-west-2": {
        "c5.2xlarge": 0.4040,
        "m6a.2xlarge": 0.3996,
        "m6a.4xlarge": 0.7992,
        "m6i.2xlarge": 0.4440,
        "r6a.2xlarge": 0.5328,
        "r6a.4xlarge": 1.0656,
        "r6i.2xlarge": 0.5920,
        "r5.2xlarge": 0.5920,
    },
    "ap-southeast-1": {
        "c5.2xlarge": 0.3920,
        "m6a.2xlarge": 0.4320,
        "m6a.4xlarge": 0.8640,
        "m6i.2xlarge": 0.4800,
        "r6a.2xlarge": 0.5472,
        "r6a.4xlarge": 1.0944,
        "r6i.2xlarge": 0.6080,
        "r5.2xlarge": 0.6080,
    },
}


def rank_by_cost(region: str, hours: float = 1.0) -> list[dict]:
    """Instance types ranked by $/session-hour, cheapest first.

    Uses the planning slot count (min of memory and CPU), so a shape whose RAM
    its cores cannot feed is priced on what it can actually deliver.
    """
    prices = ON_DEMAND_USD_PER_HOUR.get(region, {})
    rows = []
    for t, unit in prices.items():
        slots = slots_for_instance(t)
        if slots <= 0:
            continue
        rows.append({
            "instance_type": t,
            "slots": slots,
            "memory_slots": memory_slots(t),
            "cpu_slots": cpu_slots(t),
            "binds": limiting_factor(t),
            "usd_per_hour": unit,
            "usd_per_session_hour": round(unit / slots, 5),
            "usd_window": round(unit * hours, 2),
        })
    return sorted(rows, key=lambda r: r["usd_per_session_hour"])

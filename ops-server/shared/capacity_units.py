"""Capacity in UNITS — one number per machine, one number per training.

    seats = units(instance) // units(training)

That is the whole model. It replaces a four-term arithmetic (memory, CPU, disk
bandwidth, disk IOPS, take the minimum) that was correct and still produced
numbers nobody could act on, because every term moved independently and the
answer changed when a volume was reconfigured.

WHY UNITS, AND WHY NOW
----------------------
The four-ceiling model was built to answer "how much can we extract from this
box". That is the wrong question for training delivery. A learner does not care
that a machine ran at 24% CPU; they care that their environment came up. The
decision we actually make is "how many machines do we buy", and for that a
count of whole instances beats a utilisation percentage every time — it is what
gets ordered, it is what shows on the bill, and it is a number a trainer can
sanity-check in their head before a class.

So capacity is expressed the way Kubernetes expresses it: a **request** (the
units a session reserves, planned for) and a **limit** (the hard per-slot
memory ceiling that stops one runaway session hurting its neighbours). Nothing
is oversubscribed, and the reserve is deliberately generous.

THE UNIT
--------
One unit is **one Kubernetes-101 session** — the workload measured most, over
five load tests between 2026-08-12 and 2026-08-14. Everything else is priced
relative to it, which is what makes a heterogeneous daily pool plannable at all:
a box holding 8 Astroshop sessions and one holding 20 Kubernetes-101 sessions
are both "20 units of machine", and neither is a special case.

WHERE THE INSTANCE NUMBERS COME FROM
------------------------------------
``INSTANCE_UNITS`` is a table of measurements, not a formula. m6a.4xlarge = 20
is the anchor: a 20-session run on that shape passed 20/20 (worst session using
53 of the framework's 60 retries), and a 30-session run on the same shape passed
0/30. 20 is the largest number that has actually been observed to work.

Everything else in the table is that anchor scaled by size class, which is the
one property we are willing to assume: a 4xlarge is two 2xlarges. The assumption
is safe in the conservative direction because the fixed host overhead (~2.7 GiB
of OS, dockerd, sysbox and agent) is paid once on both, so a half-size box has
slightly *less* than half the usable memory — and the table already rounds down.

WHAT MAKES THIS SAFE RATHER THAN OPTIMISTIC
-------------------------------------------
Two rules, both of which only ever reduce a number:

* **An unprofiled training is priced as the heaviest one we know.** Adding a
  repo and forgetting to measure it makes a worker run below capacity. The
  opposite default oversells a workshop and fails in front of a room.
* **A derived figure is clamped by physical memory.** The size-class scaling is
  an assumption; available RAM is not. The clamp can lower a derived number and
  can never raise one.

WHAT THIS MODEL DELIBERATELY DOES NOT DO
----------------------------------------
It does not model *simultaneous installs*. The disk-bandwidth and IOPS ceilings
in ``fleet_policy`` are real, and they are what a 30-at-once run hits — but they
bound the rate at which sessions may START, not how many a box can HOLD. That
rate is now handled where it belongs, by the paced admission in ``pools.py``.
Keeping the two separate is what lets this file be four numbers instead of
twelve. ``fleet_policy`` keeps those terms as *diagnostics*, so the UI can still
say "raising IOPS would buy you seats" — they just no longer decide the plan.
"""

from __future__ import annotations

import json
import logging
import math
import os

# This module is imported TWO ways and must survive both:
#   - `from shared import capacity_units` on the master, and
#   - loaded BY PATH by the worker agent (`worker-agent/config.py`), whose sparse
#     checkout has no `ops-server/dashboard` and which therefore gets no package
#     context at all.
# A package-relative `from .log_safety import ...` raises under the second, and
# the loader treats any failure as "no unit table" — which silently halves a
# worker's advertised capacity rather than erroring. Caught by
# worker-agent/test_capacity_derivation.py, which is why that test exists.
try:                                     # normal package import
    from shared.log_safety import scrub_for_log
except ImportError:                      # loaded by path, next to log_safety.py
    import importlib.util as _ilu
    import pathlib as _pathlib
    _spec = _ilu.spec_from_file_location(
        "_capacity_log_safety",
        _pathlib.Path(__file__).resolve().parent / "log_safety.py")
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    scrub_for_log = _mod.scrub_for_log

log = logging.getLogger(__name__)

# ── the unit ────────────────────────────────────────────────────────────────
UNIT_REFERENCE_REPO = "enablement-kubernetes-101"
UNIT_DESCRIPTION = "one Kubernetes-101 session"

# Memory a single unit reserves. The measured committed footprint of a running
# Kubernetes-101 session (operator + DynaKube + demo app) is 1,609 MiB; this is
# that plus margin for the transient provisioning peak, which is where a tight
# reservation turns into an OOM kill of a perfectly healthy lab.
UNIT_MEMORY_MB = 2048

# OS + dockerd + sysbox + agent, before any slot exists (measured).
HOST_OVERHEAD_MB = 2760

# ── machines ────────────────────────────────────────────────────────────────
# MEASURED anchor, then scaled by size class. See the module docstring for why a
# table beats a formula here. Add a shape only when someone has run the lab on
# it, or accept the derived fallback below and its clamp.
INSTANCE_UNITS = {
    # measured 2026-08-13/14: 20/20 passed, 30/30 failed
    "m6a.4xlarge":  20,
    "m6i.4xlarge":  20,
    "m5.4xlarge":   20,
    # half the box, half the units
    "m6a.2xlarge":  10,
    "m6i.2xlarge":  10,
    "m5.2xlarge":   10,
    # memory-optimised: same cores as the 2xlarge m-family, so the same unit
    # count. The extra RAM buys nothing for a workload whose cores bind first —
    # measured 2026-08-12, 30 sessions at 98% CPU pressure with 21 GiB free.
    "r6a.2xlarge":  10,
    "r6i.2xlarge":  10,
    "r5.2xlarge":   10,
    "r6a.4xlarge":  20,
    "r6i.4xlarge":  20,
    "r5.4xlarge":   20,
    # compute-optimised: memory-starved for this workload. 6 is the number that
    # ran in production for months; 12 broke the box.
    "c5.2xlarge":    6,
    "c5.4xlarge":   12,
}

# vCPU counts, used only by the derived fallback for shapes not in the table.
INSTANCE_VCPUS = {
    "c5.2xlarge": 8, "c5.4xlarge": 16,
    "m5.2xlarge": 8, "m5.4xlarge": 16,
    "m6a.2xlarge": 8, "m6a.4xlarge": 16, "m6a.8xlarge": 32,
    "m6i.2xlarge": 8, "m6i.4xlarge": 16,
    "r5.2xlarge": 8, "r5.4xlarge": 16,
    "r6a.2xlarge": 8, "r6a.4xlarge": 16, "r6a.8xlarge": 32,
    "r6i.2xlarge": 8, "r6i.4xlarge": 16,
}

# Nominal usable RAM (MiB) as the OS reports it, i.e. after firmware/kernel
# reserve. Used only to clamp a derived figure — never to raise one.
INSTANCE_MEMORY_MB = {
    "c5.2xlarge": 15616, "c5.4xlarge": 31280,
    "m5.2xlarge": 31280, "m5.4xlarge": 63380,
    "m6a.2xlarge": 31690, "m6a.4xlarge": 63380, "m6a.8xlarge": 126760,
    "m6i.2xlarge": 31690, "m6i.4xlarge": 63380,
    "r5.2xlarge": 62919, "r5.4xlarge": 128560,
    "r6a.2xlarge": 62919, "r6a.4xlarge": 128560, "r6a.8xlarge": 257120,
    "r6i.2xlarge": 62919, "r6i.4xlarge": 128560,
}

# The anchor expressed per-vCPU (20 units on 16 vCPU), for shapes with no entry
# in INSTANCE_UNITS. Deliberately derived from the measurement rather than
# chosen, so the fallback and the table cannot disagree about the m-family.
UNITS_PER_VCPU = 20 / 16


def _memory_clamp(instance_type: str) -> int:
    """Units this shape's RAM can physically hold. 0 when unknown."""
    mem = INSTANCE_MEMORY_MB.get(instance_type, 0)
    if mem <= 0:
        return 0
    usable = mem - HOST_OVERHEAD_MB
    return max(0, usable // UNIT_MEMORY_MB)


def units_for_instance(instance_type: str) -> int:
    """Units of capacity one instance provides.

    Returns 0 for a shape we can neither look up nor derive. Callers must treat
    that as "refuse to plan" — guessing high is how a class gets oversold.
    """
    measured = INSTANCE_UNITS.get(instance_type)
    if measured:
        return measured

    vcpus = INSTANCE_VCPUS.get(instance_type, 0)
    if vcpus <= 0:
        return 0
    derived = int(vcpus * UNITS_PER_VCPU)
    clamp = _memory_clamp(instance_type)
    if clamp and clamp < derived:
        log.info("%s: derived %d units clamped to %d by memory",
                 scrub_for_log(instance_type), derived, clamp)
        return clamp
    return derived


def is_measured(instance_type: str) -> bool:
    """Whether this shape's unit count came from a run or from arithmetic.

    Surfaced so a plan built on a derived figure can say so out loud.
    """
    return instance_type in INSTANCE_UNITS


# ── trainings ───────────────────────────────────────────────────────────────
# What one session of a repo costs, in units. Measure at STEADY STATE with the
# lab deployed: post-create numbers understate by roughly half (a k8s-101
# session goes from 857 MiB to 1,609 MiB once the lab actually runs).
REPO_UNITS = {
    "enablement-kubernetes-101": 1,
    "enablement-dtwiz-101": 1,
    # Astroshop brings up a ten-component OpenTelemetry demo inside the same k3d
    # cluster. Its Helm values declare 6,320 MiB of pod limits against
    # Kubernetes-101's ~1.6 GiB, so 3 units is the declared-figure estimate.
    # DECLARED, NOT MEASURED — see ASTROSHOP note in docs.
    "demo-astroshop-problems": 3,
}

# A repo nobody has measured is priced as the heaviest thing we know about.
UNPROFILED_UNITS = 3

# Redis hash: repo short name → units. Lets a measurement take effect fleet-wide
# on the next control tick without a deploy, which matters because these numbers
# are supposed to be re-measured whenever a training changes shape.
UNITS_KEY = "repo:units"


def _short_name(repo: str) -> str:
    """Accepts 'owner/name', a full GitHub URL, or a bare name."""
    name = (repo or "").strip().rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def candidate_names(repo: str) -> list[str]:
    """Names to try, most specific first.

    A workshop stores its ``trainingId`` (``kubernetes-101``) while the repo is
    ``enablement-kubernetes-101``. Measured live: without this, every workshop
    missed its profile and was sized at the heavy default — safe, but a silent
    3x over-provision that nobody would question, because falling back is
    supposed to be the unusual path.
    """
    name = _short_name(repo)
    if not name:
        return []
    out = [name]
    if not name.startswith("enablement-"):
        out.append(f"enablement-{name}")
    else:
        out.append(name[len("enablement-"):])
    return out


def units_for_repo_static(repo: str) -> int:
    """Units per session, from the built-in table only (no Redis)."""
    for candidate in candidate_names(repo):
        if candidate in REPO_UNITS:
            return REPO_UNITS[candidate]
    return UNPROFILED_UNITS


async def units_for_repo(redis, repo: str) -> int:
    """Units per session: Redis override first, then built-in, then heavy default.

    Never raises and never returns 0 — a malformed override falls through to the
    safe default rather than producing a division by zero in a sizing path.
    """
    names = candidate_names(repo)
    if redis is not None:
        try:
            for candidate in names:
                raw = await redis.hget(UNITS_KEY, candidate)
                if not raw:
                    continue
                value = json.loads(raw) if raw.strip().startswith("{") else raw
                units = int(value["units"] if isinstance(value, dict) else value)
                if units > 0:
                    return units
                log.warning("repo:units[%s] = %s is not positive — ignoring",
                            candidate, units)
        except Exception as exc:
            log.warning("repo:units lookup failed for %s (%s) — using table",
                        repo, exc)
    return units_for_repo_static(repo)


async def publish_units(redis, repo: str, units: int, measured_on: str = "",
                        measured_with: str = "") -> None:
    """Store a measurement so it applies fleet-wide without a deploy."""
    if units <= 0:
        raise ValueError("units must be positive")
    await redis.hset(UNITS_KEY, _short_name(repo), json.dumps({
        "units": int(units),
        "measured_on": measured_on,
        "measured_with": measured_with,
    }))


# ── the two questions anyone actually asks ──────────────────────────────────

def seats_per_instance(instance_type: str, repo_units: int) -> int:
    """How many sessions of a training one machine holds.

    Integer division, so a machine is never planned to hold a fraction of a
    session. 0 when the shape is unknown.
    """
    capacity = units_for_instance(instance_type)
    if capacity <= 0 or repo_units <= 0:
        return 0
    return capacity // repo_units


def instances_for_seats(seats: int, instance_type: str, repo_units: int,
                        redundancy: int = 1) -> int:
    """Machines needed to seat ``seats`` learners of one training.

    Rounds UP and adds ``redundancy`` spare machines. A workshop one seat short
    is a person without an environment in front of a room, so the asymmetry is
    deliberate: the cost of a spare instance is a few dollars an hour, and the
    cost of being short is the class.
    """
    per = seats_per_instance(instance_type, repo_units)
    if per <= 0 or seats <= 0:
        return 0
    return math.ceil(seats / per) + max(0, redundancy)


# ── the limit half of request/limit ─────────────────────────────────────────
# The units above are the REQUEST — what a session reserves and what we plan
# against. This is the LIMIT: the hard per-slot memory ceiling that keeps one
# runaway session from taking its neighbours with it.
#
# It is deliberately well above the reservation. A limit set near the request
# turns every transient spike into an OOM kill of a healthy lab, which reads to
# a learner as a broken training rather than as a busy machine.
SLOT_CAP_MULTIPLIER = float(os.environ.get("SLOT_CAP_MULTIPLIER", "2.5"))
# Astroshop declares 6,320 MiB of pod limits across ten components. A flat
# 4,096 cap — the historical value — is below that, so a correct Astroshop
# session would be killed for being the size it is designed to be.
SLOT_CAP_FLOOR_MB = int(os.environ.get("SLOT_CAP_FLOOR_MB", "8192"))


def slot_memory_cap_mb(repo_units: int) -> int:
    """Per-slot hard memory ceiling for a training of this weight."""
    return max(SLOT_CAP_FLOOR_MB,
               int(repo_units * UNIT_MEMORY_MB * SLOT_CAP_MULTIPLIER))


def describe(instance_type: str, repo: str, repo_units: int) -> dict:
    """One row of the capacity table, for the UI and for logs.

    Includes ``measured`` so a plan built on a derived instance figure or an
    unprofiled repo says so, rather than presenting an assumption as a fact.
    """
    capacity = units_for_instance(instance_type)
    return {
        "instance_type": instance_type,
        "instance_units": capacity,
        "instance_measured": is_measured(instance_type),
        "repo": _short_name(repo),
        "repo_units": repo_units,
        "repo_measured": units_for_repo_static(repo) != UNPROFILED_UNITS
                         or _short_name(repo) in REPO_UNITS,
        "seats": seats_per_instance(instance_type, repo_units),
        "slot_memory_cap_mb": slot_memory_cap_mb(repo_units),
        "unit": UNIT_DESCRIPTION,
    }

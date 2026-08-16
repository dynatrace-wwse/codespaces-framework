"""Per-repo resource weights — how much of a worker one session actually costs.

WHY A COUNT IS THE WRONG UNIT
-----------------------------
``WORKER_CAPACITY=30`` means "thirty sessions", and that is only meaningful if
every session weighs the same. It does not. Kubernetes-101 brings up a handful
of pods; Astroshop brings up a twenty-service OpenTelemetry demo on the same
base image. Planning thirty seats is right for one and badly wrong for the
other, and nothing in the system currently knows the difference.

So capacity is expressed as a **weight budget** rather than a count. A worker
has memory and CPU; a repo declares what one of its sessions consumes; seats
fall out of the division. Two consequences worth stating:

* For a **workshop** — one repo by definition — budget ÷ weight is a clean seat
  count, which is the number a trainer plans against.
* For the **daily** pool, which is heterogeneous by nature, a seat count was
  never meaningful and a "don't mix repos" rule would be unenforceable. The
  same budget arithmetic handles it without a special case.

UNPROFILED MEANS HEAVY
----------------------
A repo nobody has measured is treated as the heaviest thing we know about, not
as an average. This is the one line that makes the system fail safe when
someone adds a repo and forgets to measure it: the cost of being wrong is a
worker running below capacity, versus a workshop that oversells and fails in
front of a room.

MEASURE AT STEADY STATE, WITH THE LAB DEPLOYED
----------------------------------------------
Post-create numbers understate by roughly half — a k8s-101 session goes from
857 MiB to 1,609 MiB once the lab actually runs. Any profile added here must
come from a session with its lab deployed, or it will oversell.
"""

from __future__ import annotations

import json
import logging

from shared import capacity_units

log = logging.getLogger(__name__)

# Redis hash for profiles published without a deploy. A measurement run can
# write here and take effect on the next control tick, which matters because
# these numbers are supposed to be re-measured whenever a repo changes weight.
PROFILE_KEY = "repo:profiles"


class RepoProfile:
    """What one session of a repo costs, and where the number came from.

    ``measured_on``/``measured_with`` are not decoration. Every capacity number
    in this system has been wrong at least once, and each time it was inferred
    rather than loaded — so a profile that cannot say when and on what it was
    measured should be read with suspicion.
    """

    __slots__ = ("repo", "steady_memory_mb", "steady_cpu", "install_burst_cost",
                 "disk_mb", "measured_on", "measured_with", "estimated")

    def __init__(self, repo: str, steady_memory_mb: int, steady_cpu: float,
                 install_burst_cost: int = 2, disk_mb: int = 3750,
                 measured_on: str = "", measured_with: str = "",
                 estimated: bool = False):
        self.repo = repo
        self.steady_memory_mb = int(steady_memory_mb)
        self.steady_cpu = float(steady_cpu)
        self.install_burst_cost = int(install_burst_cost)
        self.disk_mb = int(disk_mb)
        self.measured_on = measured_on
        self.measured_with = measured_with
        # True when this is a guess rather than a measurement. Surfaced to
        # callers so a plan built on estimates can say so out loud.
        self.estimated = estimated

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self) -> str:                                # pragma: no cover
        return f"<RepoProfile {self.repo} {self.steady_memory_mb}MiB " \
               f"{'estimated' if self.estimated else 'measured'}>"


# ── Measured ────────────────────────────────────────────────────────────────
# Kubernetes-101, amd001, 2026-08-12, running the operator + DynaKube steps
# through to a working lab. This is the only repo measured at steady state so
# far, and it is the bootcamp's repo.
K8S_101 = RepoProfile(
    repo="enablement-kubernetes-101",
    steady_memory_mb=1609,
    steady_cpu=0.127,
    install_burst_cost=4,
    disk_mb=3750,
    measured_on="2026-08-12",
    measured_with="c5.2xlarge / m6a.4xlarge, 12-30 sessions",
)

# ── The pessimistic default ─────────────────────────────────────────────────
# Deliberately ~3x the only repo we have measured. Astroshop is the reason the
# multiplier is not 1: same base image, but a twenty-service demo inside k3d.
# The true ratio is UNMEASURED — 3x is a placeholder chosen to fail safe, and
# it is flagged estimated=True so no plan can quietly present it as fact.
HEAVY_DEFAULT = RepoProfile(
    repo="(unprofiled)",
    steady_memory_mb=4800,
    steady_cpu=0.4,
    install_burst_cost=4,
    disk_mb=6000,
    measured_with="ESTIMATE — no measurement exists",
    estimated=True,
)

BUILTIN: dict[str, RepoProfile] = {
    "enablement-kubernetes-101": K8S_101,
}


def _short_name(repo: str) -> str:
    """Accepts 'owner/name', a full GitHub URL, or a bare name."""
    name = (repo or "").strip().rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def candidate_names(repo: str) -> list[str]:
    """Names to try, most specific first.

    A workshop stores ``trainingId`` as ``kubernetes-101`` while the repo is
    ``enablement-kubernetes-101``. Measured live: without this, EVERY workshop
    missed its profile and was sized as heavy — safe, but a silent 3x
    over-provision that nobody would have questioned because the fallback is
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


async def load(redis, repo: str) -> RepoProfile:
    """Profile for ``repo``: Redis override, then built-in, then heavy default.

    Redis wins so a fresh measurement takes effect without a deploy. Any
    malformed override falls through to the safe path rather than being
    partially applied — a half-read profile is how you oversell a box.
    """
    names = candidate_names(repo)
    name = names[0] if names else ""
    raw = None
    for candidate in names:
        try:
            raw = await redis.hget(PROFILE_KEY, candidate)
        except Exception as exc:                              # pragma: no cover
            log.warning("profile lookup failed for %s: %s", candidate, exc)
            raw = None
            break
        if raw:
            name = candidate
            break
    if raw:
        try:
            data = json.loads(raw)
            return RepoProfile(
                repo=name,
                steady_memory_mb=data["steady_memory_mb"],
                steady_cpu=data.get("steady_cpu", HEAVY_DEFAULT.steady_cpu),
                install_burst_cost=data.get("install_burst_cost", 4),
                disk_mb=data.get("disk_mb", HEAVY_DEFAULT.disk_mb),
                measured_on=data.get("measured_on", ""),
                measured_with=data.get("measured_with", ""),
                estimated=bool(data.get("estimated", False)),
            )
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("malformed profile for %s (%s) — using safe default",
                        name, exc)
    for candidate in names:
        if candidate in BUILTIN:
            return BUILTIN[candidate]
    log.info("no profile for %s (tried %s) — treating as heavy (%d MiB/session)",
             repo, ", ".join(names) or "nothing", HEAVY_DEFAULT.steady_memory_mb)
    return HEAVY_DEFAULT


async def publish(redis, repo: str, profile: RepoProfile) -> None:
    """Store a measurement so it applies fleet-wide without a deploy."""
    await redis.hset(PROFILE_KEY, _short_name(repo),
                     json.dumps(profile.as_dict()))


def units(profile: RepoProfile) -> int:
    """How many capacity units one session of this repo costs.

    The published table in ``capacity_units`` wins when the repo is in it — that
    is the number someone measured and wrote down deliberately. A profile that
    exists only as a memory figure is converted, rounding UP so a repo needing
    1.1 units is planned as 2 rather than silently oversold.
    """
    named = capacity_units.units_for_repo_static(profile.repo)
    if named != capacity_units.UNPROFILED_UNITS:
        return named
    return max(1, -(-int(profile.steady_memory_mb) // capacity_units.UNIT_MEMORY_MB))


def seats_per_worker(profile: RepoProfile, instance_type: str,
                     safety: float = 0.8) -> int:
    """Seats one instance should be planned for, for THIS repo.

    Delegates to the unit model — ``instance units // repo units``. The
    ``safety`` argument is accepted and ignored: safety is baked into the unit
    table itself (the m6a.4xlarge anchor is the largest count observed to pass,
    not the arithmetic ceiling), and a second multiplier on top of it was how
    the same margin ended up applied twice in some paths and not at all in
    others. Kept in the signature so existing callers do not have to change.

    Returns 0 for an unknown instance type. Callers must treat that as "refuse
    to plan" rather than guessing: guessing high is how a class gets oversold.
    """
    return capacity_units.seats_per_instance(instance_type, units(profile))


def slot_memory_cap_mb(profile: RepoProfile) -> int:
    """Per-slot hard memory ceiling for this repo — the *limit* to the unit
    model's *request*. See ``capacity_units.slot_memory_cap_mb``."""
    return capacity_units.slot_memory_cap_mb(units(profile))


def workers_for_seats(seats: int, profile: RepoProfile, instance_type: str,
                      safety: float = 0.8, redundancy: int = 0) -> int:
    """Instances needed to hold ``seats`` sessions of this repo.

    Rounds UP, always. A workshop one seat short is a person without an
    environment in front of a room. ``redundancy`` defaults to 0 here because
    the workshop planner adds its own spare machine explicitly; pass 1 to get a
    plan that survives losing a host.
    """
    return capacity_units.instances_for_seats(
        seats, instance_type, units(profile), redundancy=redundancy)

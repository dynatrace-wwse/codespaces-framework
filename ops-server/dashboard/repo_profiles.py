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


def seats_per_worker(profile: RepoProfile, instance_type: str,
                     safety: float = 0.8) -> int:
    """Seats one instance should be planned for, for THIS repo.

    Memory and CPU only. The disk ceilings in ``fleet_policy`` describe how many
    installs can run *simultaneously* and still clear the readiness gate — a
    concurrency limit, which the provisioning drip now handles separately. This
    is the steady-state holding capacity, which is the number a trainer needs
    when asking "how many machines for seventy people".

    Returns 0 for an unknown instance type. Callers must treat that as "refuse
    to plan" rather than guessing: guessing high is how a class gets oversold.
    """
    from dashboard import fleet_policy

    mem = fleet_policy.INSTANCE_MEMORY_MB.get(instance_type, 0)
    vcpus = fleet_policy.INSTANCE_VCPUS.get(instance_type, 0)
    if mem <= 0 or vcpus <= 0:
        return 0
    usable = mem - fleet_policy.HOST_BASE_OVERHEAD_MB - fleet_policy.HOST_CACHE_RESERVE_MB
    if usable <= 0:
        return 0
    by_memory = int((usable / profile.steady_memory_mb) * safety)
    # Steady-state CPU, not the install burst -- an idle-but-occupied session
    # draws almost nothing, which is exactly why CPU utilisation is a poor
    # occupancy signal and why this term rarely binds.
    by_cpu = int(vcpus / profile.steady_cpu) if profile.steady_cpu > 0 else by_memory
    return max(0, min(by_memory, by_cpu))


def workers_for_seats(seats: int, profile: RepoProfile, instance_type: str,
                      safety: float = 0.8) -> int:
    """Instances needed to hold ``seats`` sessions of this repo.

    Rounds UP, always. A workshop one seat short is a person without an
    environment in front of a room.
    """
    per = seats_per_worker(profile, instance_type, safety)
    if per <= 0:
        return 0
    return -(-max(0, seats) // per)

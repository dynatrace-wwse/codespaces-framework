"""Configuration for the remote worker agent."""

import os
import platform
import socket as _socket
import sys
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env")

def _detect_host_ip() -> str:
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return _socket.gethostname()

# Worker identity — ephemeral, recommissionable. Format: w{amd|arm}{NNN}
# (e.g. wamd001). Operators set WORKER_INSTANCE (e.g. "001") per box; if unset,
# a random 3-char suffix keeps it unique. A full WORKER_ID env override wins.
# The worker id is recorded in each job record (searchable by job id) and is
# NOT encoded in the app subdomain. master is the only non-"w…" worker id;
# the dashboard treats any worker_id != "master" as a remote SSH worker.
def _arch_family() -> str:
    return "arm" if platform.machine() in ("aarch64", "arm64") else "amd"

_WORKER_INSTANCE = os.environ.get("WORKER_INSTANCE") or uuid.uuid4().hex[:3]
WORKER_ID = os.environ.get("WORKER_ID", f"w{_arch_family()}{_WORKER_INSTANCE}")
WORKER_ARCH = os.environ.get("WORKER_ARCH", "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64")

def _instance_type() -> str:
    """This box's EC2 instance type, via IMDSv2. "" when not on EC2.

    Two seconds of timeout total: a worker that cannot reach IMDS must still
    start, it just falls back to the configured capacity.
    """
    try:
        import urllib.request

        def _fetch(url: str, headers: dict, method: str = "GET") -> str:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=1) as r:
                return r.read().decode().strip()

        token = _fetch("http://169.254.169.254/latest/api/token",
                       {"X-aws-ec2-metadata-token-ttl-seconds": "60"}, "PUT")
        return _fetch("http://169.254.169.254/latest/meta-data/instance-type",
                      {"X-aws-ec2-metadata-token": token})
    except Exception:
        return ""


INSTANCE_TYPE = os.environ.get("WORKER_INSTANCE_TYPE") or _instance_type()


def _units_for_instance(instance_type: str) -> int:
    """``capacity_units.units_for_instance``, loaded by PATH not by package.

    The table lives in ``shared/`` because a worker is a sparse checkout that
    does NOT clone ``ops-server/dashboard`` — measured 2026-08-14, two workers
    of the identical instance type derived 20 and 6, because one of them simply
    had no copy of the file and the fallback swallowed the error. A silent
    fallback to a *smaller* number is the benign direction, but it is still a
    machine quietly worth a third of what it cost.

    Loading by path rather than by package name removes the second half of that
    dependency: it no longer matters how the agent was started or what is on
    sys.path, only that the sparse pattern includes ``ops-server/shared/**``.
    """
    try:
        import importlib.util
        import pathlib

        path = (pathlib.Path(__file__).resolve().parent.parent
                / "shared" / "capacity_units.py")
        spec = importlib.util.spec_from_file_location("_capacity_units", path)
        if spec is None or spec.loader is None:
            return 0
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return int(module.units_for_instance(instance_type))
    except Exception as exc:                                  # pragma: no cover
        # Loud, not silent: this is the failure that cost amd002 two thirds of
        # its capacity without anything in the logs saying so.
        print(f"[config] could not derive capacity for {instance_type}: {exc}",
              file=sys.stderr)
        return 0


def _derive_capacity() -> int:
    """Slots this box should advertise.

    An explicit ``WORKER_CAPACITY`` always wins — the workshop planner sets it
    per launch, and an operator must be able to pin a box for a capacity test.
    Otherwise it comes from the unit model, so the number a machine advertises
    and the number the planner assumed cannot drift apart. They had: both daily
    workers were m6a.4xlarge advertising 30 while every measurement said 20, and
    nothing in the system could notice, because the 30 was typed into a file.

    Falls back to 6 — the smallest shape's figure — when the instance type is
    unknown. Advertising too few slots costs money; advertising too many costs
    a class.
    """
    explicit = os.environ.get("WORKER_CAPACITY")
    if explicit:
        return int(explicit)
    if INSTANCE_TYPE:
        derived = _units_for_instance(INSTANCE_TYPE)
        if derived > 0:
            return derived
    return 6


WORKER_CAPACITY = _derive_capacity()
# Weighted scheduler overrides (optional; derived from WORKER_CAPACITY when unset).
# WORKER_COST_BUDGET  — total in-flight cost units the worker admits at once.
# WORKER_MAX_HEAVY    — max concurrent heavy-lane jobs (e.g. dt-cnfs).
WORKER_COST_BUDGET = int(os.environ["WORKER_COST_BUDGET"]) if os.environ.get("WORKER_COST_BUDGET") else None
WORKER_MAX_HEAVY = int(os.environ["WORKER_MAX_HEAVY"]) if os.environ.get("WORKER_MAX_HEAVY") else None

# ── Pool membership ─────────────────────────────────────────────────────────
# Which queue this worker takes work from. This is the ONLY thing that keeps a
# self-service learner off a machine reserved for a workshop, and it is
# deliberately implemented as queue *topology* rather than as a filter:
#
#   daily worker     → BLPOP queue:direct:{id}, queue:test:{arch}
#   workshop worker  → BLPOP queue:direct:{id}, queue:pool:{POOL}
#
# A workshop worker never subscribes to the shared arch queue, so a self-service
# job cannot reach it — not "is filtered out of it". A filter can have a bug; a
# queue a process never reads from cannot deliver.
#
# "daily" is the shared, autoscaled pool that serves self-service. Any other
# value names a dedicated pool (e.g. a workshop id), and several workers may
# share it — they then load-balance over queue:pool:{POOL} for free, which
# pinning by WORKER_ID could not do.
WORKER_POOL = os.environ.get("WORKER_POOL", "daily").strip() or "daily"
DAILY_POOL = "daily"


def queue_keys(worker_id: str, arch: str, pool_name: str = "") -> list[str]:
    """BLPOP key list for this worker, highest priority first.

    ``queue:direct:{id}`` stays in front for both pool kinds: it is how a
    capacity test or an operator targets one specific box, and that must keep
    working regardless of pool membership.
    """
    pool = (pool_name or WORKER_POOL).strip() or DAILY_POOL
    shared = f"queue:test:{arch}" if pool == DAILY_POOL else f"queue:pool:{pool}"
    return [f"queue:direct:{worker_id}", shared]


# Short git SHA of the code this worker is actually running, stamped into
# /home/ops/.env by cloud-init after it syncs the checkout (see
# dashboard/fleet.py:_build_user_data). Published on every heartbeat so fleet
# drift is one glance rather than an SSH round-trip to each box.
#
# Empty means "nobody stamped it" — a hand-built worker, or a boot-time sync
# that failed. Both are worth seeing, which is why this is not defaulted to
# something reassuring.
WORKER_CODE_REF = os.environ.get("WORKER_CODE_REF", "").strip()

# ── Per-slot resource limits ────────────────────────────────────────────────
# Slots run untrusted learner workloads (a full k3d cluster, and whatever the
# lab tells the learner to install). Until now they were started with NO limits
# at all, so one slot could consume the whole host.
#
# Defaults are derived from measurement, not guesswork (amd001, 2026-08-12,
# Kubernetes-101 through the operator + DynaKube steps):
#   committed (anon+slab) 1.61 GiB · transient peak 2.2–3.1 GiB · 0.127 vCPU avg
# So 3 GiB is ~1.9× the committed working set and still above the worst observed
# peak — it stops a runaway without touching a healthy lab.
#
# Off by default: enable per worker with WORKER_SLOT_LIMITS=1, verify a full lab
# run, then roll out. Disabling is a single env var and a restart.
WORKER_SLOT_LIMITS = os.environ.get("WORKER_SLOT_LIMITS", "0").strip().lower() in ("1", "true", "yes")
# Hard ceiling per slot. OOM-kills inside the slot's own cgroup, so a runaway
# takes out one learner's cluster instead of the whole worker.
#
# 8192, NOT the session's committed 1,609 MiB. This is a runaway guard, not a
# packing device: a k8s-101 session's *transient* peak during the operator and
# DynaKube steps was measured at 2.2-3.1 GiB (2026-08-12), so a 3 GiB cap sits
# right on top of normal behaviour and would OOM-kill healthy labs. Capacity is
# planned from units (see shared/capacity_units.py); this number only has to
# be above any legitimate peak and below "ate the worker".
#
# Raised from 4096 on 2026-08-14. The DAILY pool is heterogeneous, so its cap
# has to clear the heaviest training that can land on it, not the one we
# measured: Astroshop declares 6,320 MiB of pod limits across ten components,
# which means a correct Astroshop session was being killed for being the size
# it is designed to be. A dedicated workshop pool gets a cap sized from its own
# repo's units instead (``capacity_units.slot_memory_cap_mb``).
#
# Raised again to 20480 on 2026-08-16, from the FIRST real Astroshop measurement
# — the workshop had never actually bootstrapped before, so every earlier figure
# described an empty container. Measured steady state is 7,158 MiB per session,
# not the 6,320 MiB its pod limits declare: declared limits omit k3d itself, the
# GitLab install and the load generator. Against the old 8,192 cap that is 87%
# used, so a slightly heavier run would have been OOM-killed mid-workshop.
#
# 20480 is ``slot_memory_cap_mb(4 units)`` — the model's own answer for the
# heaviest profiled training. It is a LIMIT, not a reservation: nothing is set
# aside, and what actually prevents overcommit is the unit model deciding how
# many sessions a box accepts. Sizing the cap to the heaviest training only
# removes a way for a correct session to die; it does not remove the runaway
# guard, which is what the cap is for.
WORKER_SLOT_MEMORY_MB = int(os.environ.get("WORKER_SLOT_MEMORY_MB", "20480"))
# Soft floor: under host pressure the kernel reclaims from slots above this
# first, so a heavy neighbour is squeezed before a well-behaved one.
WORKER_SLOT_MEMORY_RESERVATION_MB = int(os.environ.get("WORKER_SLOT_MEMORY_RESERVATION_MB", "2048"))
# CPU WEIGHT, not a quota. Deliberately not --cpus: a hard CFS quota would
# throttle exactly the k3d-creation burst and make every provision slower.
# Shares only arbitrate *contention*, so idle CPU stays fully available.
WORKER_SLOT_CPU_SHARES = int(os.environ.get("WORKER_SLOT_CPU_SHARES", "1024"))
# Optional HARD ceiling in whole vCPUs (--cpus). 0 = off, which keeps the
# reasoning above intact: by default nothing throttles.
#
# Shares alone bound a slot's share of a CONTESTED cpu, but place no limit at
# all on an idle host — so one wedged slot can still drive host CPU pressure
# for every one of its neighbours. That is the gap this closes, and the reason
# it is a separate knob rather than a change to the default: set it GENEROUSLY.
# Measured draw is 0.127 vCPU steady with a burst of roughly 1-2 cores during
# k3d creation, so a ceiling of 4 on a 16-vCPU box is ~2x the worst legitimate
# burst and still stops a runaway from taking a quarter of the machine.
# Too tight a value re-creates precisely the throttling the shares-only design
# set out to avoid.
WORKER_SLOT_CPU_QUOTA = float(os.environ.get("WORKER_SLOT_CPU_QUOTA", "0") or 0)
# A nested k3d cluster runs a few hundred processes; 4096 is far above normal
# and still bounds a fork bomb.
WORKER_SLOT_PIDS_LIMIT = int(os.environ.get("WORKER_SLOT_PIDS_LIMIT", "4096"))

# ── Warm-up pacing ──────────────────────────────────────────────────────────
# Slots used to be created all at once. On 2026-08-13 a restart taken while
# dockerd was still under teardown pressure produced 18/30 -- eight containers
# stuck in `created`, four never created -- and the pool logged "Worker fully
# warm" anyway. A second restart with a calm daemon gave 30/30, so the slots
# were fine; the instantaneous fan-out was not.
#
# 6 at a time warms 30 slots in five waves. Slower than the burst when the
# burst works, and vastly faster than the burst when it does not.
WARM_CONCURRENCY = int(os.environ.get("WARM_CONCURRENCY", "6"))
WARM_MAX_ATTEMPTS = int(os.environ.get("WARM_MAX_ATTEMPTS", "3"))
WARM_RETRY_BASE_S = int(os.environ.get("WARM_RETRY_BASE_S", "10"))

# Teardown is dripped for the same reason and at the same kind of rate: the
# trigger for the reaping bug was ~30 simultaneous `docker rm -fv` on nested
# Sysbox containers. The reaper makes a wedged teardown survivable; staggering
# makes it rare. Both are needed -- staggering alone leaves the bug latent for
# the next Docker hiccup, and the reaper alone means wedging the daemon on
# every workshop teardown.
TEARDOWN_CONCURRENCY = int(os.environ.get("TEARDOWN_CONCURRENCY", "4"))
TEARDOWN_STAGGER_S = float(os.environ.get("TEARDOWN_STAGGER_S", "1.0"))


def slot_limit_args() -> list[str]:
    """docker run flags enforcing the per-slot resource envelope.

    Returns [] when WORKER_SLOT_LIMITS is off, so the call site stays identical
    whether limits are enabled or not.

    Note there is deliberately no disk quota: the workers run overlayfs on ext4,
    where ``--storage-opt size=`` is unsupported (it needs XFS with prjquota).
    Per-slot disk is reported on the heartbeat for observability instead.
    """
    if not WORKER_SLOT_LIMITS:
        return []
    args = [
        "--memory", f"{WORKER_SLOT_MEMORY_MB}m",
        # Equal to --memory ⇒ swap disabled for the slot. Swapping a k3d
        # cluster degrades far worse than failing it outright.
        "--memory-swap", f"{WORKER_SLOT_MEMORY_MB}m",
        "--pids-limit", str(WORKER_SLOT_PIDS_LIMIT),
        "--cpu-shares", str(WORKER_SLOT_CPU_SHARES),
    ]
    if 0 < WORKER_SLOT_MEMORY_RESERVATION_MB < WORKER_SLOT_MEMORY_MB:
        args += ["--memory-reservation", f"{WORKER_SLOT_MEMORY_RESERVATION_MB}m"]
    if WORKER_SLOT_CPU_QUOTA > 0:
        args += ["--cpus", f"{WORKER_SLOT_CPU_QUOTA:g}"]
    return args
# Private IP of this worker (auto-detected; override via WORKER_HOST env var).
WORKER_HOST = os.environ.get("WORKER_HOST") or _detect_host_ip()
# Optional SSH alias the master uses to reach this worker (defaults to WORKER_HOST).
WORKER_SSH_HOST = os.environ.get("WORKER_SSH_HOST", "")

# Master Redis connection
MASTER_REDIS_URL = os.environ.get("MASTER_REDIS_URL", "redis://localhost:6379/0")
MASTER_REDIS_PASSWORD = os.environ.get("MASTER_REDIS_PASSWORD", "")

# Paths
REPOS_DIR = Path.home() / "repos"
LOGS_DIR = Path.home() / "logs"
WORKDIR = Path.home() / "workdir"

# Pre-warmed Sysbox slot directories live here (one subdir per slot index).
SLOT_BASE_DIR = Path(os.environ.get("SLOT_BASE_DIR", str(WORKDIR / "slots")))

# Dynatrace (for integration tests)
DT_ENVIRONMENT = os.environ.get("DT_ENVIRONMENT", "")
DT_OPERATOR_TOKEN = os.environ.get("DT_OPERATOR_TOKEN", "")
DT_INGEST_TOKEN = os.environ.get("DT_INGEST_TOKEN", "")
DT_LLM_TOKEN = os.environ.get("DT_LLM_TOKEN", "")
# Platform token (dt0s16) for the dtwiz suite + platform-token-native trainings.
DT_PLATFORM_TOKEN = os.environ.get("DT_PLATFORM_TOKEN", "")

# Timeouts
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "900"))  # 15 min
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))  # seconds
REGISTRATION_TTL = int(os.environ.get("REGISTRATION_TTL", "300"))  # seconds — must exceed pool init time (~90s)

# App proxy port pool — each Sysbox container publishes one port in this range
# so the ops-server dashboard can reverse-proxy to the k3d LB without an SSH tunnel.
# The corresponding SG rule on the worker must allow TCP 32000-32099 from the master.
APP_PROXY_PORT_START = int(os.environ.get("APP_PROXY_PORT_START", "32000"))
APP_PROXY_PORT_COUNT = int(os.environ.get("APP_PROXY_PORT_COUNT", "100"))
# k3d LB port inside the Sysbox. Workers default to 80 (framework default when
# K3D_LB_HTTP_PORT is unset); master overrides to 30080 to avoid nginx collision.
K3D_LB_HTTP_PORT = int(os.environ.get("K3D_LB_HTTP_PORT", "80"))

# Docker image for integration tests
TEST_IMAGE = os.environ.get("TEST_IMAGE", "shinojosa/dt-enablement:v1.2")

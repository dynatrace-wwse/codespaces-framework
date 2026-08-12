"""Configuration for the remote worker agent."""

import os
import platform
import socket as _socket
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
WORKER_CAPACITY = int(os.environ.get("WORKER_CAPACITY", "6"))
# Weighted scheduler overrides (optional; derived from WORKER_CAPACITY when unset).
# WORKER_COST_BUDGET  — total in-flight cost units the worker admits at once.
# WORKER_MAX_HEAVY    — max concurrent heavy-lane jobs (e.g. dt-cnfs).
WORKER_COST_BUDGET = int(os.environ["WORKER_COST_BUDGET"]) if os.environ.get("WORKER_COST_BUDGET") else None
WORKER_MAX_HEAVY = int(os.environ["WORKER_MAX_HEAVY"]) if os.environ.get("WORKER_MAX_HEAVY") else None

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
WORKER_SLOT_MEMORY_MB = int(os.environ.get("WORKER_SLOT_MEMORY_MB", "3072"))
# Soft floor: under host pressure the kernel reclaims from slots above this
# first, so a heavy neighbour is squeezed before a well-behaved one.
WORKER_SLOT_MEMORY_RESERVATION_MB = int(os.environ.get("WORKER_SLOT_MEMORY_RESERVATION_MB", "2048"))
# CPU WEIGHT, not a quota. Deliberately not --cpus: a hard CFS quota would
# throttle exactly the k3d-creation burst and make every provision slower.
# Shares only arbitrate *contention*, so idle CPU stays fully available.
WORKER_SLOT_CPU_SHARES = int(os.environ.get("WORKER_SLOT_CPU_SHARES", "1024"))
# A nested k3d cluster runs a few hundred processes; 4096 is far above normal
# and still bounds a fork bomb.
WORKER_SLOT_PIDS_LIMIT = int(os.environ.get("WORKER_SLOT_PIDS_LIMIT", "4096"))


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

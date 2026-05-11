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

# Worker identity
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{platform.machine()}-{uuid.uuid4().hex[:6]}")
WORKER_ARCH = os.environ.get("WORKER_ARCH", "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64")
WORKER_CAPACITY = int(os.environ.get("WORKER_CAPACITY", "6"))
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

# Dynatrace (for integration tests)
DT_ENVIRONMENT = os.environ.get("DT_ENVIRONMENT", "")
DT_OPERATOR_TOKEN = os.environ.get("DT_OPERATOR_TOKEN", "")
DT_INGEST_TOKEN = os.environ.get("DT_INGEST_TOKEN", "")

# Timeouts
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "900"))  # 15 min
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))  # seconds
REGISTRATION_TTL = int(os.environ.get("REGISTRATION_TTL", "120"))  # seconds

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

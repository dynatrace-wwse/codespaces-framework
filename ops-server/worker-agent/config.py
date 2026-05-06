"""Configuration for the remote worker agent."""

import os
import platform
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env")

# Worker identity
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{platform.machine()}-{uuid.uuid4().hex[:6]}")
WORKER_ARCH = os.environ.get("WORKER_ARCH", "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64")
WORKER_CAPACITY = int(os.environ.get("WORKER_CAPACITY", "6"))

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

# Docker image for integration tests
TEST_IMAGE = os.environ.get("TEST_IMAGE", "shinojosa/dt-enablement:v1.2")

"""Configuration for the ops webhook server."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env")

# GitHub webhook secret for signature verification
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Redis connection
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Paths
OPS_HOME = Path.home()
REPOS_DIR = OPS_HOME / "repos"
LOGS_DIR = OPS_HOME / "logs"
WORKDIR = OPS_HOME / "workdir"
FRAMEWORK_DIR = OPS_HOME / "enablement-framework" / "codespaces-framework"

# Concurrency
MAX_PARALLEL_WORKERS = int(os.environ.get("MAX_PARALLEL_WORKERS", "6"))
MAX_PARALLEL_AGENTS = int(os.environ.get("MAX_PARALLEL_AGENTS", "2"))

# Dynatrace
DT_ENVIRONMENT = os.environ.get("DT_ENVIRONMENT", "")
DT_OPERATOR_TOKEN = os.environ.get("DT_OPERATOR_TOKEN", "")
DT_INGEST_TOKEN = os.environ.get("DT_INGEST_TOKEN", "")

# Codespaces tracker
TRACKER_ENDPOINT = os.environ.get(
    "ENDPOINT_CODESPACES_TRACKER",
    "https://codespaces-tracker.whydevslovedynatrace.com/api/receive",
)
TRACKER_TOKEN = os.environ.get("CODESPACES_TRACKER_TOKEN", "")

# Server
HOST = os.environ.get("OPS_HOST", "0.0.0.0")
PORT = int(os.environ.get("OPS_PORT", "8443"))

#!/usr/bin/env python3
"""Push host + Redis metrics to DT metrics ingest API.

No OneAgent — reads /proc directly, pushes via HTTP.
Run as a systemd timer (every 60s) on master and workers.

Env vars (from /home/ops/.env):
  DT_ENVIRONMENT  — tenant URL (https://geu80787.live.dynatrace.com)
  DT_INGEST_TOKEN — API token with metrics.ingest scope
"""

import os
import socket
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env")

_dt_env_raw = os.environ.get("DT_ENVIRONMENT", "").rstrip("/")
# Metrics ingest API is on the live domain, not the apps domain.
DT_ENV = _dt_env_raw.replace(".apps.dynatrace.com", ".live.dynatrace.com")
DT_TOKEN = os.environ.get("DT_INGEST_TOKEN", "")
REDIS_AUTH = "50258583a5c8d515dc8a553a26e1a17d"
HOST = socket.gethostname().split(".")[0]  # short hostname


def _read_cpu_pct() -> float:
    """Compute CPU usage % over a 500ms sample window."""
    def read_stat():
        line = Path("/proc/stat").read_text().splitlines()[0].split()
        vals = [int(x) for x in line[1:]]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return total, idle

    t1, i1 = read_stat()
    time.sleep(0.5)
    t2, i2 = read_stat()
    dt = t2 - t1
    di = i2 - i1
    return round((1 - di / dt) * 100, 2) if dt > 0 else 0.0


def _read_memory() -> tuple[float, float]:
    """Returns (used_pct, used_gb)."""
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            mem[parts[0].rstrip(":")] = int(parts[1])
    total = mem.get("MemTotal", 1)
    available = mem.get("MemAvailable", 0)
    used = total - available
    used_pct = round(used / total * 100, 2)
    used_gb = round(used / 1024 / 1024, 2)
    return used_pct, used_gb


def _read_disk_pct(path: str = "/") -> float:
    """Returns disk used % for given path."""
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    used = total - free
    return round(used / total * 100, 2) if total > 0 else 0.0


def _redis_metrics() -> dict[str, float]:
    """Query Redis for queue depth and active job counts. Master only."""
    try:
        import redis  # type: ignore
        r = redis.Redis(host="localhost", port=6379, password=REDIS_AUTH, decode_responses=True, socket_timeout=3)
        amd64_depth = r.llen("queue:test:amd64") or 0
        arm64_depth = r.llen("queue:test:arm64") or 0
        # Count active job keys
        active_keys = len(r.keys("job:running:*"))
        # Count registered workers
        worker_keys = r.keys("worker:*")
        worker_count = len(worker_keys)
        # Completed jobs in last-500 ring
        completed = r.llen("jobs:completed") or 0
        return {
            "orbital.redis.queue.amd64": float(amd64_depth),
            "orbital.redis.queue.arm64": float(arm64_depth),
            "orbital.redis.active_jobs": float(active_keys),
            "orbital.redis.workers": float(worker_count),
            "orbital.redis.completed_ring": float(completed),
        }
    except Exception:
        return {}


def _build_payload(metrics: dict[str, float], host: str) -> str:
    """Build DT metrics ingest text format."""
    ts_ms = int(time.time() * 1000)
    lines = []
    for key, val in metrics.items():
        # Redis metrics: no host dimension (master-only)
        if key.startswith("orbital.redis."):
            lines.append(f"{key},host={host} {val} {ts_ms}")
        else:
            lines.append(f"{key},host={host} {val} {ts_ms}")
    return "\n".join(lines)


def push(payload: str) -> bool:
    if not DT_ENV or not DT_TOKEN:
        print("DT_ENVIRONMENT or DT_INGEST_TOKEN not set — skipping push")
        return False
    url = f"{DT_ENV}/api/v2/metrics/ingest"
    try:
        resp = httpx.post(
            url,
            content=payload,
            headers={
                "Authorization": f"Api-Token {DT_TOKEN}",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        if resp.status_code in (200, 202):
            print(f"Pushed {len(payload.splitlines())} metrics [{resp.status_code}]")
            return True
        print(f"Ingest failed {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"Push error: {e}")
        return False


def main():
    cpu = _read_cpu_pct()
    mem_pct, mem_gb = _read_memory()
    disk_pct = _read_disk_pct("/")

    metrics: dict[str, float] = {
        "orbital.host.cpu.pct": cpu,
        "orbital.host.memory.used_pct": mem_pct,
        "orbital.host.memory.used_gb": mem_gb,
        "orbital.host.disk.used_pct": disk_pct,
    }

    # Redis metrics — only works on master (has Redis)
    metrics.update(_redis_metrics())

    payload = _build_payload(metrics, HOST)
    print(payload)
    push(payload)


if __name__ == "__main__":
    main()

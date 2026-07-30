"""Worker host metrics → COE via the classic metrics-ingest line protocol.

Stdlib-only (workers have no OneAgent — removed by design — and no OTel SDK).
Reads /proc for CPU/memory, statvfs for disk, ships one batch per minute:

    orbital.worker.cpu.percent,worker=<id> <v>
    orbital.worker.mem.percent,worker=<id> <v>
    orbital.worker.disk.percent,worker=<id> <v>
    orbital.worker.load1,worker=<id> <v>

Started from the agent's main loop when DT_INGEST_TOKEN is present.
"""

import asyncio
import logging
import os
import urllib.request

log = logging.getLogger("worker-metrics")

INGEST_URL = os.environ.get(
    "DT_METRICS_INGEST_URL",
    "https://geu80787.live.dynatrace.com/api/v2/metrics/ingest",
)
INTERVAL = int(os.environ.get("WORKER_METRICS_INTERVAL", "60"))

_prev_cpu: tuple[int, int] | None = None


def _cpu_percent() -> float | None:
    """CPU busy % since the previous call (first call primes and returns None)."""
    global _prev_cpu
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:8]
    vals = [int(x) for x in parts]
    idle, total = vals[3] + vals[4], sum(vals)
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        return None
    didle, dtotal = idle - _prev_cpu[0], total - _prev_cpu[1]
    _prev_cpu = (idle, total)
    if dtotal <= 0:
        return None
    return round(100.0 * (1 - didle / dtotal), 1)


def _mem_percent() -> float:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            info[k] = int(rest.split()[0])
    total = info.get("MemTotal", 1)
    avail = info.get("MemAvailable", 0)
    return round(100.0 * (1 - avail / total), 1)


def _disk_percent() -> float:
    st = os.statvfs("/")
    used = (st.f_blocks - st.f_bavail) / st.f_blocks if st.f_blocks else 0
    return round(100.0 * used, 1)


def _ship(lines: list[str], token: str) -> None:
    req = urllib.request.Request(
        INGEST_URL,
        data="\n".join(lines).encode(),
        method="POST",
        headers={"Authorization": f"Api-Token {token}",
                 "Content-Type": "text/plain; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status >= 300:
            log.warning("metrics ingest HTTP %s", resp.status)


async def metrics_loop(worker_id: str) -> None:
    token = os.environ.get("DT_INGEST_TOKEN", "")
    if not token:
        log.info("worker metrics disabled (no DT_INGEST_TOKEN)")
        return
    log.info("worker metrics → COE every %ss (worker=%s)", INTERVAL, worker_id)
    _cpu_percent()  # prime the delta
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            dims = f"worker={worker_id}"
            lines = [
                f"orbital.worker.mem.percent,{dims} {_mem_percent()}",
                f"orbital.worker.disk.percent,{dims} {_disk_percent()}",
                f"orbital.worker.load1,{dims} {os.getloadavg()[0]:.2f}",
            ]
            cpu = _cpu_percent()
            if cpu is not None:
                lines.append(f"orbital.worker.cpu.percent,{dims} {cpu}")
            await asyncio.to_thread(_ship, lines, token)
        except Exception as exc:
            log.warning("worker metrics ship failed: %s", exc)

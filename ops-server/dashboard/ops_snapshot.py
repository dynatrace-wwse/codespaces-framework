"""Periodic ops snapshot → COE: registered tenants, trainings, workers, queues.

Numbers go out as OTel gauges (orbital.* metrics, exported by otel_setup's
reader); the full detail goes out as one JSON log line per cycle
(log.source="orbital.snapshot") so dashboard tables can show tenant/profile
lists without a bizevents-ingest scope.
"""

import asyncio
import json
import logging
import os
import time

import httpx

from dashboard import content_service

log = logging.getLogger("ops-snapshot")

SNAPSHOT_INTERVAL = int(os.environ.get("OPS_SNAPSHOT_INTERVAL", "300"))
_LOGS_URL = "https://geu80787.live.dynatrace.com/api/v2/logs/ingest"

# Latest snapshot, read by the observable gauges registered below.
_latest: dict = {}
_gauges_registered = False


def _register_gauges() -> None:
    global _gauges_registered
    if _gauges_registered:
        return
    from opentelemetry import metrics
    meter = metrics.get_meter("orbital.ops")

    def g(key):
        def cb(_):
            v = _latest.get(key)
            if v is not None:
                yield metrics.Observation(v)
        return cb

    meter.create_observable_gauge("orbital.tenants.registered", callbacks=[g("tenants")])
    meter.create_observable_gauge("orbital.trainings.sources", callbacks=[g("sources")])
    meter.create_observable_gauge("orbital.content.packs", callbacks=[g("packs")])
    meter.create_observable_gauge("orbital.workers.count", callbacks=[g("workers")])
    meter.create_observable_gauge("orbital.workers.active_jobs", callbacks=[g("active_jobs")])
    meter.create_observable_gauge("orbital.workers.capacity", callbacks=[g("capacity")])
    meter.create_observable_gauge("orbital.jobs.running", callbacks=[g("running")])
    meter.create_observable_gauge("orbital.queue.depth", callbacks=[g("queued")])
    _gauges_registered = True


async def _collect(pool) -> dict:
    tenant_map = content_service._load_tenant_map()
    tenants = tenant_map.get("tenants", {})
    profiles = {}
    if content_service.PROFILES_DIR.is_dir():
        for p in content_service.PROFILES_DIR.glob("*.json"):
            try:
                profiles[p.stem] = len(json.loads(p.read_text()).get("sources", []))
            except Exception:
                profiles[p.stem] = -1
    packs = len(list(content_service.PACKS_DIR.glob("*.json"))) if content_service.PACKS_DIR.is_dir() else 0

    workers, active, capacity = {}, 0, 0
    async for key in pool.scan_iter("worker:*"):
        try:
            h = await pool.hgetall(key)
        except Exception:
            continue  # not a hash (e.g. worker:<id>:something string keys)
        if not h:
            continue
        wid = key.split(":", 1)[1]
        workers[wid] = {"arch": h.get("arch", ""), "active_jobs": int(h.get("active_jobs", 0) or 0),
                        "capacity": int(h.get("capacity", 0) or 0), "status": h.get("status", "")}
        active += workers[wid]["active_jobs"]
        capacity += workers[wid]["capacity"]

    running = 0
    async for _ in pool.scan_iter("job:running:*"):
        running += 1
    queued = 0
    for q in ("queue:test:amd64", "queue:test:arm64", "queue:agent"):
        try:
            queued += await pool.llen(q)
        except Exception:
            pass

    default_profiles = tenant_map.get("defaults", {})
    return {
        "event.type": "ops.snapshot",
        "tenants": len(tenants),
        "tenant_map": tenants,
        "defaults": default_profiles,
        "profiles": profiles,
        "sources": profiles.get("all", 0),
        "packs": packs,
        "workers": len(workers),
        "worker_detail": workers,
        "active_jobs": active,
        "capacity": capacity,
        "running": running,
        "queued": queued,
        "ts": time.time(),
    }


async def _ship_log(snapshot: dict) -> None:
    token = os.environ.get("DT_INGEST_TOKEN", "")
    if not token:
        return
    line = {
        "content": json.dumps(snapshot),
        "log.source": "orbital.snapshot",
        "event.type": "ops.snapshot",
        "service.name": "orbital-dashboard",
        "severity": "info",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(_LOGS_URL, json=[line],
                              headers={"Authorization": f"Api-Token {token}"})
        if r.status_code >= 300:
            log.warning("snapshot log ingest %s: %s", r.status_code, r.text[:200])


async def snapshot_loop(pool) -> None:
    """Forever-loop started from the dashboard's startup hook."""
    _register_gauges()
    while True:
        try:
            snap = await _collect(pool)
            _latest.update({k: snap[k] for k in
                            ("tenants", "sources", "packs", "workers", "active_jobs",
                             "capacity", "running", "queued")})
            await _ship_log(snap)
        except Exception as exc:
            log.warning("ops snapshot failed: %s", exc)
        await asyncio.sleep(SNAPSHOT_INTERVAL)

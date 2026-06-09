#!/usr/bin/env python3
"""
Terminate all running daemon jobs on a specific worker and wait for it to go idle.
Usage: python3 cleanup_worker.py <worker_id>
"""
import os
import asyncio
import sys
import time

import redis.asyncio as aioredis

REDIS_PWD   = os.environ.get("REDIS_PASSWORD") or sys.exit("REDIS_PASSWORD not set in environment")
REDIS_URL   = f"redis://:{REDIS_PWD}@localhost:6379/0"
IDLE_TIMEOUT = 300   # max 5 min to drain
SLOT_TIMEOUT = 180   # max 3 min for slots to re-initialise


async def cleanup_worker(worker_id: str) -> int:
    pool = aioredis.from_url(REDIS_URL, decode_responses=True)

    # ── Find all running jobs on this worker ────────────────────────────────
    all_keys = await pool.keys("job:running:*")
    jobs_to_kill = []
    for key in all_keys:
        wid = await pool.hget(key, "worker_id")
        if wid == worker_id:
            job_id = key.removeprefix("job:running:")
            jobs_to_kill.append(job_id)

    print(f"  Found {len(jobs_to_kill)} job(s) on {worker_id}")
    for job_id in jobs_to_kill:
        print(f"  → terminating {job_id}")
        await pool.publish("ops:terminate", job_id)

    if not jobs_to_kill:
        print("  Nothing to terminate.")
        await pool.aclose()
        return 0

    # ── Wait for active_jobs == 0 ────────────────────────────────────────────
    print("  Waiting for active_jobs → 0 ...")
    deadline = time.time() + IDLE_TIMEOUT
    while time.time() < deadline:
        meta = await pool.hgetall(f"worker:{worker_id}")
        active = int(meta.get("active_jobs", 0))
        cpu    = meta.get("cpu_pct", "?")
        mem    = meta.get("mem_pct", "?")
        print(f"  active_jobs={active}  cpu={cpu}%  mem={mem}%")
        if active == 0:
            break
        await asyncio.sleep(10)
    else:
        print("  WARNING: drain timeout — continuing anyway")

    # ── Wait for all 6 pre-warm slots to be ready ────────────────────────────
    print("  Waiting for 6 pre-warm slots to reinitialise ...")
    import subprocess, json as _json
    deadline = time.time() + SLOT_TIMEOUT
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 "autonomous-enablements-worker",
                 "docker ps --filter 'name=sb-slot' --format '{{.Names}}'"],
                timeout=15, stderr=subprocess.DEVNULL
            ).decode().strip()
            n = len([l for l in out.splitlines() if l])
            print(f"  Pre-warm slots running: {n}/6")
            if n >= 6:
                break
        except Exception as e:
            print(f"  (slot check error: {e})")
        await asyncio.sleep(10)
    else:
        print("  WARNING: slots did not fully reinitialise within timeout")

    # ── Final status ─────────────────────────────────────────────────────────
    meta = await pool.hgetall(f"worker:{worker_id}")
    print(f"\n  Worker status:    {meta.get('status')}")
    print(f"  Active jobs:      {meta.get('active_jobs')}")
    print(f"  CPU:              {meta.get('cpu_pct')}%")
    print(f"  Memory:           {meta.get('mem_pct')}%")

    await pool.aclose()
    return len(jobs_to_kill)


def main():
    worker_id = sys.argv[1] if len(sys.argv) > 1 else "worker-x86_64-amd001"
    print(f"Cleaning up worker: {worker_id}")
    asyncio.run(cleanup_worker(worker_id))


if __name__ == "__main__":
    main()

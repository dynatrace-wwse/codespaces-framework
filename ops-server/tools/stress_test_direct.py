#!/usr/bin/env python3
"""
Worker stress test — direct Redis queuing, no OAuth cookie required.

Pushes integration-test jobs directly to Redis (same format as /api/builds/trigger)
so it can run from the server without browser authentication.

Monitors CPU/mem via the open GET /api/workers endpoint.

Saturation criteria (any one triggers stop):
  - CPU >= CPU_SAT% sustained 2 consecutive polls on any AMD worker
  - Mem >= MEM_SAT% sustained 2 consecutive polls on any AMD worker
  - Master CPU >= MASTER_CPU_MAX% (safety — must not hang master)
  - Worker heartbeat stale > 90s (worker died or overloaded)
  - Queue stops draining (no completions in DRAIN_TIMEOUT_MINUTES)

Usage:
  python3 ops-server/tools/stress_test_direct.py \
      --repo dynatrace-wwse/enablement-dql-fundamentals \
      --arch amd64 \
      --max-jobs 10 --step 2 --wave-minutes 5

Results written to stress-test-direct-<timestamp>.json
"""
from __future__ import annotations

import os
import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import redis
    import requests
except ImportError:
    print("Missing: pip install redis requests", file=sys.stderr)
    sys.exit(1)

ORBITAL_API = "https://autonomous-enablements.whydevslovedynatrace.com"
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or sys.exit("REDIS_PASSWORD not set in environment")

CPU_SAT_PCT   = 90.0   # AMD worker saturation threshold
MEM_SAT_PCT   = 90.0   # AMD worker saturation threshold
MASTER_CPU_MAX = 70.0  # Master must never exceed this (CPU)
MASTER_MEM_MAX = 70.0  # Master must never exceed this (Mem)
POLL_INTERVAL  = 30    # seconds between metric polls
DRAIN_TIMEOUT_MINUTES = 15


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_job(r: redis.Redis, repo: str, arch: str, run_id: str) -> str:
    """Push an integration-test job directly to the Redis queue. Returns job_id."""
    job_id = f"stress-{uuid.uuid4().hex[:12]}"
    job = {
        "type":          "integration-test",
        "repo":          repo,
        "arch":          arch,
        "queue":         f"test:{arch}",
        "ref":           "main",
        "timestamp":     ts(),
        "trigger":       "stress-test",
        "nightly_run_id": run_id,
        "requested_by":  "stress-test-direct",
        "job_id":        job_id,
    }
    r.rpush(f"queue:test:{arch}", json.dumps(job))
    return job_id


def get_workers(api: str) -> list[dict]:
    try:
        resp = requests.get(f"{api}/api/workers", timeout=10)
        resp.raise_for_status()
        return resp.json().get("workers", [])
    except Exception as exc:
        print(f"  [warn] Worker poll failed: {exc}", file=sys.stderr)
        return []


def heartbeat_age_s(w: dict) -> float:
    hb = w.get("last_heartbeat", "")
    if not hb:
        return 9999.0
    try:
        # Heartbeat may be stored as time-only or full ISO
        now = datetime.now(timezone.utc)
        # Try full ISO parse
        try:
            hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            return (now - hb_dt).total_seconds()
        except ValueError:
            pass
        # Time-only (HH:MM:SS.ffffff+HH:MM) — assume today
        from datetime import date
        hb_dt = datetime.strptime(hb[:8], "%H:%M:%S").replace(
            tzinfo=timezone.utc,
            year=now.year, month=now.month, day=now.day,
        )
        age = (now - hb_dt).total_seconds()
        if age < 0:
            age += 86400  # rolled past midnight
        return age
    except Exception:
        return 9999.0


def queue_depth(r: redis.Redis, arch: str) -> int:
    return r.llen(f"queue:test:{arch}")


def running_jobs(r: redis.Redis) -> list[str]:
    return [k for k in r.keys("job:running:stress-*")]


def main():
    p = argparse.ArgumentParser(description="Direct-Redis Orbital stress test")
    p.add_argument("--repo", default="dynatrace-wwse/enablement-dql-fundamentals",
                   help="GitHub repo owner/name to use as test workload")
    p.add_argument("--arch", default="amd64", choices=["amd64", "arm64"])
    p.add_argument("--max-jobs", type=int, default=10)
    p.add_argument("--step", type=int, default=2)
    p.add_argument("--wave-minutes", type=int, default=5)
    p.add_argument("--output", default=None)
    args = p.parse_args()

    # ARM64 test = master is both worker and master → cap sat thresholds at master limit
    if args.arch == "arm64":
        global CPU_SAT_PCT, MEM_SAT_PCT
        CPU_SAT_PCT = MASTER_CPU_MAX
        MEM_SAT_PCT = MASTER_MEM_MAX
        print(f"ARM64 mode: sat thresholds capped at {MASTER_CPU_MAX}% (master protection)")

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD,
                    decode_responses=True)
    r.ping()
    print("Redis connected.")

    run_id = f"stress-{int(time.time())}"
    out_path = Path(args.output) if args.output else Path(f"stress-test-direct-{int(time.time())}.json")

    results = {
        "started_at": ts(),
        "repo": args.repo,
        "arch": args.arch,
        "max_jobs": args.max_jobs,
        "step": args.step,
        "wave_minutes": args.wave_minutes,
        "cpu_sat_pct": CPU_SAT_PCT,
        "mem_sat_pct": MEM_SAT_PCT,
        "master_cpu_max": MASTER_CPU_MAX,
        "waves": [],
        "saturation_point": None,
        "saturation_reason": None,
        "finished_at": None,
    }

    print(f"Stress test: {args.repo} [{args.arch}]")
    print(f"Waves 1→{args.max_jobs} step={args.step} | {args.wave_minutes}min per wave")
    print(f"Sat: AMD CPU>{CPU_SAT_PCT}% or Mem>{MEM_SAT_PCT}% | Master CPU<{MASTER_CPU_MAX}%")
    print("─" * 72)

    saturated = False
    prev_high_cpu: dict[str, bool] = {}
    prev_high_mem: dict[str, bool] = {}
    last_completion_count = 0
    last_drain_check = time.monotonic()

    for concurrency in range(args.step, args.max_jobs + args.step, args.step):
        if saturated:
            break
        concurrency = min(concurrency, args.max_jobs)
        wave_info = {
            "concurrency": concurrency,
            "started_at": ts(),
            "jobs_queued": [],
            "polls": [],
            "saturation_triggered": False,
        }

        print(f"\nWave: {concurrency} concurrent jobs")
        for _ in range(concurrency):
            jid = queue_job(r, args.repo, args.arch, run_id)
            wave_info["jobs_queued"].append(jid)
            print(f"  → queued {jid} (queue depth: {queue_depth(r, args.arch)})")

        deadline = time.monotonic() + args.wave_minutes * 60
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            workers = get_workers(ORBITAL_API)
            if not workers:
                print("  [warn] No workers returned")
                continue

            poll_entry = {"timestamp": ts(), "workers": []}

            for w in workers:
                wid = w.get("worker_id", "?")
                arch = w.get("arch", "?")
                active = int(w.get("active_jobs") or 0)
                cpu = float(w.get("cpu_pct") or 0)
                mem = float(w.get("mem_pct") or 0)
                disk = float(w.get("disk_pct") or 0)
                age = heartbeat_age_s(w)

                poll_entry["workers"].append({
                    "worker_id": wid, "arch": arch,
                    "active_jobs": active, "cpu_pct": cpu, "mem_pct": mem,
                    "disk_pct": disk, "heartbeat_age_s": round(age, 1),
                })

                icon = "🖥️" if "master" in wid else "⚙️"
                print(f"  {icon} {wid[:24]} | arch={arch} jobs={active} "
                      f"CPU={cpu}% Mem={mem}% Disk={disk}% hb_age={age:.0f}s")

                # Master CPU + Mem guard — master must never exceed 70%
                if "master" in wid:
                    if cpu >= MASTER_CPU_MAX:
                        saturated = True
                        wave_info["saturation_triggered"] = True
                        results["saturation_point"] = concurrency
                        results["saturation_reason"] = f"Master CPU {cpu}% >= {MASTER_CPU_MAX}% limit"
                        print(f"  !! MASTER PROTECTION: CPU={cpu}% — stopping")
                        break
                    if mem >= MASTER_MEM_MAX:
                        saturated = True
                        wave_info["saturation_triggered"] = True
                        results["saturation_point"] = concurrency
                        results["saturation_reason"] = f"Master Mem {mem}% >= {MASTER_MEM_MAX}% limit"
                        print(f"  !! MASTER PROTECTION: Mem={mem}% — stopping")
                        break

                # Only apply sat checks to target arch workers
                if arch != args.arch:
                    continue

                # Heartbeat stale
                if age > 90:
                    saturated = True
                    wave_info["saturation_triggered"] = True
                    results["saturation_point"] = concurrency
                    results["saturation_reason"] = f"Worker {wid} heartbeat stale {age:.0f}s"
                    print(f"  !! SATURATION: heartbeat stale")
                    break

                # CPU sustained
                if cpu >= CPU_SAT_PCT:
                    if prev_high_cpu.get(wid):
                        saturated = True
                        wave_info["saturation_triggered"] = True
                        results["saturation_point"] = concurrency
                        results["saturation_reason"] = f"CPU sustained {cpu}% on {wid}"
                        print(f"  !! SATURATION: CPU {cpu}% sustained")
                    prev_high_cpu[wid] = True
                else:
                    prev_high_cpu[wid] = False

                # Mem sustained
                if mem >= MEM_SAT_PCT:
                    if prev_high_mem.get(wid):
                        saturated = True
                        wave_info["saturation_triggered"] = True
                        results["saturation_point"] = concurrency
                        results["saturation_reason"] = f"Mem sustained {mem}% on {wid}"
                        print(f"  !! SATURATION: Mem {mem}% sustained")
                    prev_high_mem[wid] = True
                else:
                    prev_high_mem[wid] = False

            poll_entry["queue_depth"] = queue_depth(r, args.arch)
            wave_info["polls"].append(poll_entry)
            if saturated:
                break

            # Drain timeout
            completed = len(r.lrange("jobs:completed", 0, -1))
            if completed > last_completion_count:
                last_completion_count = completed
                last_drain_check = time.monotonic()
            elif (time.monotonic() - last_drain_check) > DRAIN_TIMEOUT_MINUTES * 60:
                saturated = True
                wave_info["saturation_triggered"] = True
                results["saturation_point"] = concurrency
                results["saturation_reason"] = f"Queue drain timeout: no completions in {DRAIN_TIMEOUT_MINUTES}min"
                print(f"  !! SATURATION: drain timeout")
                break

        wave_info["finished_at"] = ts()
        results["waves"].append(wave_info)

    # Clear any remaining stress-test jobs from queue
    depth = queue_depth(r, args.arch)
    if depth > 0:
        print(f"\nClearing {depth} leftover stress jobs from queue...")
        popped = 0
        while True:
            item = r.lpop(f"queue:test:{args.arch}")
            if not item:
                break
            try:
                job = json.loads(item)
                if job.get("trigger") == "stress-test":
                    popped += 1
                else:
                    r.rpush(f"queue:test:{args.arch}", item)  # put non-stress back
            except Exception:
                r.rpush(f"queue:test:{args.arch}", item)
        print(f"Removed {popped} stress jobs from queue.")

    results["finished_at"] = ts()
    if not results["saturation_point"]:
        results["saturation_point"] = "not reached"
        results["saturation_reason"] = f"All waves up to {args.max_jobs} jobs completed without saturation"

    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n{'─'*72}")
    print(f"Saturation: {results['saturation_point']} — {results['saturation_reason']}")
    print(f"Results: {out_path}")


if __name__ == "__main__":
    main()

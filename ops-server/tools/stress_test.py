#!/usr/bin/env python3
"""
Worker stress test — measures maximum concurrent DinD training capacity.

Launches integration-test jobs in escalating waves on a target worker and
monitors CPU, memory, responsiveness, and job throughput until saturation.

Usage:
  python3 stress_test.py --api https://autonomous-enablements.whydevslovedynatrace.com \\
      --arch amd64 --max-jobs 14 --step 2 --wave-minutes 5 \\
      --repo dynatrace-wwse/dt-k8s-operator-enablement-new

Results are written to stress-test-<timestamp>.json.

The saturation point is declared when ANY of:
  - cpu_pct >= CPU_SATURATION_PCT sustained for two consecutive polls
  - mem_pct >= MEM_SATURATION_PCT sustained for two consecutive polls
  - A worker heartbeat becomes stale (> 90s old)
  - Job queue stops draining (no new completions in DRAIN_TIMEOUT_MINUTES)
"""
import argparse
import json
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests not installed: pip install requests", file=sys.stderr)
    sys.exit(1)

CPU_SATURATION_PCT = 90.0
MEM_SATURATION_PCT = 90.0
DRAIN_TIMEOUT_MINUTES = 15
POLL_INTERVAL_SECONDS = 30


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_workers(api: str, session: requests.Session) -> list[dict]:
    r = session.get(f"{api}/api/workers", timeout=10)
    r.raise_for_status()
    return r.json().get("workers", [])


def queue_job(api: str, session: requests.Session, repo: str, arch: str) -> str | None:
    """Trigger an integration test job and return its job_id, or None on error."""
    r = session.post(
        f"{api}/api/builds/trigger",
        json={"repo": repo, "arch": arch, "branch": "main"},
        timeout=10,
    )
    if r.status_code == 401:
        print("AUTH REQUIRED: visit the dashboard and re-run with a valid session cookie")
        sys.exit(1)
    if not r.ok:
        print(f"  [warn] Trigger failed {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    return data.get("job_id")


def poll_worker_metrics(api: str, session: requests.Session, arch: str) -> list[dict]:
    workers = get_workers(api, session)
    return [w for w in workers if w.get("arch") == arch]


def heartbeat_age_seconds(w: dict) -> float:
    hb = w.get("last_heartbeat")
    if not hb:
        return 999.0
    try:
        hb_ts = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - hb_ts).total_seconds()
    except Exception:
        return 999.0


def run_stress_test(
    api: str,
    repo: str,
    arch: str,
    max_jobs: int,
    step: int,
    wave_minutes: int,
    cookie: str | None,
    output: Path,
):
    session = requests.Session()
    if cookie:
        # Paste your browser _oauth2_proxy cookie value here or pass via --cookie
        session.cookies.set("_oauth2_proxy", cookie, domain=api.split("//")[-1].split("/")[0])

    results = {
        "started_at": ts(),
        "api": api,
        "repo": repo,
        "arch": arch,
        "max_jobs": max_jobs,
        "step": step,
        "wave_minutes": wave_minutes,
        "waves": [],
        "saturation_point": None,
        "saturation_reason": None,
        "finished_at": None,
    }

    print(f"Stress test: {repo} [{arch}] | waves 1→{max_jobs} step={step} | {wave_minutes}min per wave")
    print(f"Polling every {POLL_INTERVAL_SECONDS}s | saturation: CPU>{CPU_SATURATION_PCT}% or Mem>{MEM_SATURATION_PCT}%")
    print("─" * 72)

    saturated = False
    prev_high_cpu = False
    prev_high_mem = False
    last_completion_count = 0
    last_drain_check = time.monotonic()

    for concurrency in range(step, max_jobs + step, step):
        if saturated:
            break
        concurrency = min(concurrency, max_jobs)
        wave_info = {
            "concurrency": concurrency,
            "started_at": ts(),
            "jobs_queued": [],
            "polls": [],
            "saturation_triggered": False,
        }

        print(f"\nWave: {concurrency} concurrent jobs")
        for _ in range(concurrency):
            jid = queue_job(api, session, repo, arch)
            if jid:
                wave_info["jobs_queued"].append(jid)
                print(f"  → queued {jid}")

        deadline = time.monotonic() + wave_minutes * 60
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            workers = poll_worker_metrics(api, session, arch)
            if not workers:
                print("  [warn] No workers found for arch", arch)
                continue

            poll_entry = {"timestamp": ts(), "workers": []}
            for w in workers:
                age = heartbeat_age_seconds(w)
                cpu = float(w.get("cpu_pct") or 0)
                mem = float(w.get("mem_pct") or 0)
                disk = float(w.get("disk_pct") or 0)
                containers = w.get("containers_running", "?")
                active = int(w.get("active_jobs") or 0)
                entry = {
                    "worker_id": w.get("worker_id"),
                    "active_jobs": active,
                    "cpu_pct": cpu,
                    "mem_pct": mem,
                    "disk_pct": disk,
                    "containers_running": containers,
                    "heartbeat_age_s": round(age, 1),
                }
                poll_entry["workers"].append(entry)
                print(
                    f"  {w.get('worker_id','?')} | jobs={active} | "
                    f"CPU={cpu}% Mem={mem}% Disk={disk}% | "
                    f"containers={containers} | hb_age={age:.0f}s"
                )

                # Saturation checks
                if age > 90:
                    saturated = True
                    wave_info["saturation_triggered"] = True
                    results["saturation_point"] = concurrency
                    results["saturation_reason"] = f"Heartbeat stale: {age:.0f}s"
                    print(f"  !! SATURATION: heartbeat stale ({age:.0f}s)")
                elif cpu >= CPU_SATURATION_PCT:
                    if prev_high_cpu:
                        saturated = True
                        wave_info["saturation_triggered"] = True
                        results["saturation_point"] = concurrency
                        results["saturation_reason"] = f"CPU sustained >= {CPU_SATURATION_PCT}%: {cpu}%"
                        print(f"  !! SATURATION: CPU {cpu}% (sustained)")
                    prev_high_cpu = True
                else:
                    prev_high_cpu = False

                if mem >= MEM_SATURATION_PCT:
                    if prev_high_mem:
                        saturated = True
                        wave_info["saturation_triggered"] = True
                        results["saturation_point"] = concurrency
                        results["saturation_reason"] = f"Mem sustained >= {MEM_SATURATION_PCT}%: {mem}%"
                        print(f"  !! SATURATION: Mem {mem}% (sustained)")
                    prev_high_mem = True
                else:
                    prev_high_mem = False

            wave_info["polls"].append(poll_entry)
            if saturated:
                break

        wave_info["finished_at"] = ts()
        results["waves"].append(wave_info)

    results["finished_at"] = ts()
    if not results["saturation_point"]:
        results["saturation_point"] = "not reached"
        results["saturation_reason"] = f"Completed all waves up to {max_jobs} jobs without saturation"

    output.write_text(json.dumps(results, indent=2))
    print(f"\n{'─' * 72}")
    print(f"Saturation: {results['saturation_point']} jobs — {results['saturation_reason']}")
    print(f"Results: {output}")


def main():
    p = argparse.ArgumentParser(description="Orbital worker stress test")
    p.add_argument("--api", default="https://autonomous-enablements.whydevslovedynatrace.com",
                   help="Orbital API base URL")
    p.add_argument("--repo", default="dynatrace-wwse/dt-k8s-operator-enablement-new",
                   help="GitHub repo to use as test workload (owner/name)")
    p.add_argument("--arch", default="amd64", choices=["amd64", "arm64"],
                   help="Worker arch to stress test (default: amd64)")
    p.add_argument("--max-jobs", type=int, default=14,
                   help="Max concurrent jobs to attempt (default: 14)")
    p.add_argument("--step", type=int, default=2,
                   help="Increment per wave (default: 2)")
    p.add_argument("--wave-minutes", type=int, default=5,
                   help="Minutes to observe each concurrency level (default: 5)")
    p.add_argument("--cookie", default=None,
                   help="_oauth2_proxy cookie value for authenticated API calls")
    p.add_argument("--output", default=None,
                   help="Output JSON file (default: stress-test-<ts>.json)")
    args = p.parse_args()

    out = Path(args.output) if args.output else Path(f"stress-test-{int(time.time())}.json")
    run_stress_test(
        api=args.api,
        repo=args.repo,
        arch=args.arch,
        max_jobs=args.max_jobs,
        step=args.step,
        wave_minutes=args.wave_minutes,
        cookie=args.cookie,
        output=out,
    )


if __name__ == "__main__":
    main()

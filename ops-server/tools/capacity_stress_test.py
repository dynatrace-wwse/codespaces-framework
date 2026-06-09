#!/usr/bin/env python3
"""
Capacity Stress Test — measures max concurrent trainings per AMD worker.

Two variants:
  astroshop — heavy app (~25 pods, DT OneAgent, OTEL collector)
  todoapp   — light app (~3 pods)

Each container is started 30s after the previous one finishes its post-create.
kubectl response time and app curl time are measured every 10s.
Worker CPU/mem are sampled every 5s from Redis.
Test stops when pods fail to schedule, kubectl hangs, or thresholds are hit.

Usage:
  python3 capacity_stress_test.py --variant astroshop [--max-containers 10]
  python3 capacity_stress_test.py --variant todoapp   [--max-containers 10]
  python3 capacity_stress_test.py --variant astroshop --dry-run
"""

import os
import argparse
import asyncio
import json
import re
import shlex
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_PWD    = os.environ.get("REDIS_PASSWORD") or sys.exit("REDIS_PASSWORD not set in environment")
REDIS_URL    = f"redis://:{REDIS_PWD}@localhost:6379/0"
ORBITAL_BASE = "https://autonomous-enablements.whydevslovedynatrace.com"

REPO      = "dynatrace-wwse/codespaces-framework"
REPO_NAME = "codespaces-framework"

# Default: worker-x86_64-9fe515 = 172.31.10.70 = autonomous-enablements-worker
DEFAULT_WORKER_ID  = "worker-x86_64-amd001"
DEFAULT_WORKER_SSH = "autonomous-enablements-worker"

ASTROSHOP_BRANCH = "stress-test/astroshop"
TODOAPP_BRANCH   = "main"

# Thresholds — test aborts when exceeded
CPU_STOP_PCT = 90.0
MEM_STOP_PCT = 90.0

# Per-container stop conditions
KUBECTL_TIMEOUT_S        = 15   # if kubectl takes >15s = degraded
CURL_TIMEOUT_S           = 10   # curl max wait
MAX_KUBECTL_CONSECUTIVE_FAIL = 3
MAX_CURL_CONSECUTIVE_FAIL    = 3
BAD_POD_THRESHOLD_S          = 120   # pods in bad state for >2 min → stop

# Intervals
KUBECTL_INTERVAL_S = 10
CURL_INTERVAL_S    = 10
METRIC_INTERVAL_S  = 5

# Timeouts
STARTUP_READY_TIMEOUT_S = 1800   # 30 min max for post-create
JOB_APPEAR_TIMEOUT_S    = 120    # 2 min for job to start
NEXT_CONTAINER_DELAY_S  = 30     # gap between containers


@dataclass
class ContainerMetrics:
    container_num: int
    job_id: str
    sb_name: str = ""
    repo_name: str = REPO_NAME
    start_time: float = 0.0
    ready_time: float = 0.0          # epoch when "Daemon ready" seen
    setup_duration_s: float = 0.0   # ready_time - start_time
    app_url: str = ""
    kubectl_samples: list = field(default_factory=list)  # list of (ts, ms | None)
    curl_samples:    list = field(default_factory=list)  # list of (ts, ms | None)
    stop_reason: str = ""


@dataclass
class WorkerSample:
    ts: float
    cpu: float
    mem: float
    active_jobs: int


# ── Redis helpers ─────────────────────────────────────────────────────────────

async def push_direct_job(pool, worker_id: str, job_id: str, repo: str, branch: str) -> None:
    payload = {
        "type":       "daemon",
        "repo":       repo,
        "head_repo":  repo,
        "ref":        branch,
        "trigger":    "stress-test",
        "job_id":     job_id,
    }
    await pool.rpush(f"queue:direct:{worker_id}", json.dumps(payload))


async def wait_job_running(pool, job_id: str, timeout_s: int = JOB_APPEAR_TIMEOUT_S) -> Optional[dict]:
    """Wait until job:running:{job_id} exists and has sb_name. Returns meta dict."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        meta = await pool.hgetall(f"job:running:{job_id}")
        if meta and meta.get("sb_name"):
            return meta
        await asyncio.sleep(2)
    return None


async def wait_daemon_ready(pool, job_id: str, timeout_s: int = STARTUP_READY_TIMEOUT_S) -> bool:
    """Poll job:livelog:{job_id} until 'Daemon ready' appears."""
    deadline = time.time() + timeout_s
    key = f"job:livelog:{job_id}"
    while time.time() < deadline:
        log = await pool.get(key)
        if log and "Daemon ready" in log:
            return True
        if log and "sysbox container start failed" in log:
            return False
        await asyncio.sleep(5)
    return False


async def get_worker_metrics(pool, worker_id: str) -> Optional[WorkerSample]:
    meta = await pool.hgetall(f"worker:{worker_id}")
    if not meta:
        return None
    return WorkerSample(
        ts=time.time(),
        cpu=float(meta.get("cpu_pct", 0)),
        mem=float(meta.get("mem_pct", 0)),
        active_jobs=int(meta.get("active_jobs", 0)),
    )


# ── SSH exec helpers ──────────────────────────────────────────────────────────

async def run_ssh(ssh_host: str, *remote_cmd: str, timeout_s: float = 20) -> tuple[int, str, str]:
    """Run a command on the remote worker via SSH. Returns (rc, stdout, stderr).

    remote_cmd args are shlex-joined into a single quoted string so that
    semicolons and shell metacharacters in complex scripts (e.g. bash -lc '...')
    are preserved rather than interpreted by the remote login shell.
    """
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=5",
        ssh_host,
        shlex.join(remote_cmd),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


async def measure_kubectl(ssh_host: str, sb_name: str, repo_name: str) -> Optional[int]:
    """Returns kubectl get pod -A response time in ms, or None on failure."""
    script = (
        "start=$(date +%s%3N); "
        "kubectl get pod -A >/dev/null 2>&1; rc=$?; "
        "echo KUBECTL_MS=$(($(date +%s%3N) - start)); "
        "echo KUBECTL_RC=$rc"
    )
    rc, stdout, _ = await run_ssh(
        ssh_host,
        "docker", "exec", sb_name,
        "docker", "exec", "-w", f"/workspaces/{repo_name}", "dt",
        "bash", "-lc", script,
        timeout_s=KUBECTL_TIMEOUT_S + 5,
    )
    if rc != 0:
        return None
    ms_match = re.search(r"KUBECTL_MS=(\d+)", stdout)
    rc_match  = re.search(r"KUBECTL_RC=(\d+)", stdout)
    if not ms_match:
        return None
    kubectl_rc = int(rc_match.group(1)) if rc_match else 0
    if kubectl_rc != 0:
        return None
    return int(ms_match.group(1))


async def get_pod_health(ssh_host: str, sb_name: str, repo_name: str) -> str:
    """
    Returns "healthy", "degraded" (some bad pods), or "failed" (exec failed).
    """
    script = (
        "kubectl get pod -A --no-headers 2>/dev/null | "
        "grep -cE 'CrashLoopBackOff|Error|OOMKilled|Evicted|Failed|ImagePullBackOff' || echo 0"
    )
    rc, stdout, _ = await run_ssh(
        ssh_host,
        "docker", "exec", sb_name,
        "docker", "exec", "-w", f"/workspaces/{repo_name}", "dt",
        "bash", "-lc", script,
        timeout_s=20,
    )
    if rc != 0:
        return "failed"
    try:
        bad = int(stdout.strip())
        return "degraded" if bad > 0 else "healthy"
    except ValueError:
        return "healthy"


async def measure_curl(url: str) -> Optional[int]:
    """Returns curl response time in ms, or None on failure/timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-o", "/dev/null",
            "-w", "%{time_total}",
            "--max-time", str(CURL_TIMEOUT_S),
            "-L",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=CURL_TIMEOUT_S + 5)
        if proc.returncode != 0:
            return None
        return int(float(stdout.decode().strip()) * 1000)
    except Exception:
        return None


async def get_app_url(pool, job_id: str, sb_name: str, ssh_host: str, app_name: str) -> str:
    """
    Read the app registry from inside the container and return the orbital URL.
    Falls back to empty string if not found.
    """
    # Try reading app-registry from container
    registry_path = "/home/vscode/.cache/dt-framework/app-registry"
    rc, stdout, _ = await run_ssh(
        ssh_host,
        "docker", "exec", sb_name,
        "docker", "exec", "dt",
        "cat", registry_path,
        timeout_s=15,
    )
    if rc == 0:
        for line in stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 7 and parts[0] == app_name:
                orbital_subdomain = parts[6].strip()
                if orbital_subdomain:
                    return f"https://{orbital_subdomain}.autonomous-enablements.whydevslovedynatrace.com/"
    # Compute from job_id
    orbital_subdomain = _compute_subdomain(app_name, job_id)
    return f"https://{orbital_subdomain}.autonomous-enablements.whydevslovedynatrace.com/"


def _compute_subdomain(app_name: str, job_id: str) -> str:
    m = re.match(r"^worker-[^-]+-([a-f0-9]{6})-(.+)$", job_id)
    if m:
        slug_base = f"{m.group(1)}-{m.group(2)}"
    else:
        slug_base = job_id
    max_slug = 61 - len(app_name)
    slug = slug_base[:max(max_slug, 4)]
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower()).rstrip("-")
    return f"{app_name}--{slug}"


# ── Per-container monitors ────────────────────────────────────────────────────

async def kubectl_monitor(
    cm: ContainerMetrics,
    ssh_host: str,
    stop_event: asyncio.Event,
    warmup_done: Optional[asyncio.Event] = None,
):
    """Sample kubectl every KUBECTL_INTERVAL_S until stop_event set.

    While warmup_done is not yet set (post-create still running), failures are
    recorded but do NOT trigger a stop — K3s may still be initialising.
    Once warmup_done fires (post-create complete), consecutive failures and
    degraded pods can stop the test.
    """
    consecutive_fail = 0
    bad_pod_since: Optional[float] = None

    while not stop_event.is_set():
        ms = await measure_kubectl(ssh_host, cm.sb_name, cm.repo_name)
        cm.kubectl_samples.append((time.time(), ms))

        post_create_done = warmup_done is None or warmup_done.is_set()

        if ms is None:
            if post_create_done:
                consecutive_fail += 1
                if consecutive_fail >= MAX_KUBECTL_CONSECUTIVE_FAIL:
                    cm.stop_reason = f"kubectl unresponsive for {consecutive_fail} consecutive checks"
                    stop_event.set()
                    return
        else:
            consecutive_fail = 0

        if post_create_done:
            health = await get_pod_health(ssh_host, cm.sb_name, cm.repo_name)
            if health == "degraded":
                if bad_pod_since is None:
                    bad_pod_since = time.time()
                elif time.time() - bad_pod_since > BAD_POD_THRESHOLD_S:
                    cm.stop_reason = f"pods degraded for >{BAD_POD_THRESHOLD_S}s"
                    stop_event.set()
                    return
            elif health == "failed":
                consecutive_fail += 1
            else:
                bad_pod_since = None

        await asyncio.sleep(KUBECTL_INTERVAL_S)


async def curl_monitor(cm: ContainerMetrics, stop_event: asyncio.Event):
    """Sample curl every CURL_INTERVAL_S until stop_event set."""
    if not cm.app_url:
        return
    consecutive_fail = 0

    while not stop_event.is_set():
        ms = await measure_curl(cm.app_url)
        cm.curl_samples.append((time.time(), ms))

        if ms is None:
            consecutive_fail += 1
            if consecutive_fail >= MAX_CURL_CONSECUTIVE_FAIL:
                cm.stop_reason = f"app curl unresponsive for {consecutive_fail} consecutive checks"
                stop_event.set()
                return
        else:
            consecutive_fail = 0

        await asyncio.sleep(CURL_INTERVAL_S)


# ── Worker metric monitor (shared across all containers) ─────────────────────

CPU_CONSECUTIVE_STOP = 3   # samples above threshold before stopping

async def worker_monitor(
    pool,
    worker_id: str,
    samples: list,
    stop_event: asyncio.Event,
    stop_reason_holder: list,
    steady_state_gate: asyncio.Event,
):
    """Monitor worker CPU/mem. CPU/mem thresholds only enforced after
    steady_state_gate fires (first container post-create complete) so
    startup spikes don't prematurely abort the test."""
    cpu_high = 0
    mem_high = 0
    while not stop_event.is_set():
        s = await get_worker_metrics(pool, worker_id)
        if s:
            samples.append(s)
            if steady_state_gate.is_set():
                if s.cpu >= CPU_STOP_PCT:
                    cpu_high += 1
                    if cpu_high >= CPU_CONSECUTIVE_STOP:
                        reason = f"Worker CPU {s.cpu:.1f}% >= {CPU_STOP_PCT}% for {cpu_high} consecutive samples"
                        print(f"\n[!] {reason} — stopping")
                        stop_reason_holder.append(reason)
                        stop_event.set()
                        return
                else:
                    cpu_high = 0
                if s.mem >= MEM_STOP_PCT:
                    mem_high += 1
                    if mem_high >= CPU_CONSECUTIVE_STOP:
                        reason = f"Worker mem {s.mem:.1f}% >= {MEM_STOP_PCT}% for {mem_high} consecutive samples"
                        print(f"\n[!] {reason} — stopping")
                        stop_reason_holder.append(reason)
                        stop_event.set()
                        return
                else:
                    mem_high = 0
        await asyncio.sleep(METRIC_INTERVAL_S)


# ── Report ────────────────────────────────────────────────────────────────────

def _stats(vals: list) -> tuple:
    """(avg, p50, p99, max) or (None, ...) if empty."""
    clean = [v for v in vals if v is not None]
    if not clean:
        return None, None, None, None
    clean.sort()
    avg = sum(clean) / len(clean)
    p50 = clean[len(clean) // 2]
    p99 = clean[min(int(len(clean) * 0.99), len(clean) - 1)]
    return avg, p50, p99, max(clean)


def generate_report(
    variant: str,
    worker_id: str,
    containers: list,
    worker_samples: list,
    stop_reason: str,
    output_path: Optional[str] = None,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Capacity Stress Test Report — {variant.upper()}",
        f"Generated: {ts}",
        f"Worker: {worker_id}",
        f"Stop reason: {stop_reason or 'max containers reached'}",
        "",
        "## Per-Container Results",
        "",
        f"{'#':<4} {'Job ID':<52} {'Setup(s)':<10} {'kubectl avg':>12} {'kubectl p99':>12} {'curl avg':>10} {'curl p99':>10}",
        "-" * 115,
    ]
    for cm in containers:
        k_avg, _, k_p99, k_max = _stats([ms for _, ms in cm.kubectl_samples])
        c_avg, _, c_p99, c_max = _stats([ms for _, ms in cm.curl_samples])
        k_avg_s = f"{k_avg:.0f}ms" if k_avg else "—"
        k_p99_s = f"{k_p99:.0f}ms" if k_p99 else "—"
        c_avg_s = f"{c_avg:.0f}ms" if c_avg else "—"
        c_p99_s = f"{c_p99:.0f}ms" if c_p99 else "—"
        jid_short = cm.job_id[-48:] if len(cm.job_id) > 48 else cm.job_id
        lines.append(
            f"{cm.container_num:<4} {jid_short:<52} {cm.setup_duration_s:<10.0f}"
            f" {k_avg_s:>12} {k_p99_s:>12} {c_avg_s:>10} {c_p99_s:>10}"
        )
        if cm.stop_reason:
            lines.append(f"     ↳ STOP: {cm.stop_reason}")

    lines += [
        "",
        "## Worker Resource Progression",
        "",
        f"{'Time':>8}  {'CPU%':>6}  {'Mem%':>6}  {'Active':>6}",
        "-" * 35,
    ]
    if worker_samples:
        t0 = worker_samples[0].ts
        prev_active = -1
        for s in worker_samples:
            # Print a row when active_jobs changes or every ~60s
            elapsed = int(s.ts - t0)
            if s.active_jobs != prev_active or elapsed % 60 == 0:
                lines.append(f"{elapsed:>6}s   {s.cpu:>5.1f}  {s.mem:>5.1f}  {s.active_jobs:>6}")
                prev_active = s.active_jobs

    peak = max(s.active_jobs for s in worker_samples) if worker_samples else 0
    peak_cpu = max(s.cpu for s in worker_samples) if worker_samples else 0
    peak_mem = max(s.mem for s in worker_samples) if worker_samples else 0

    lines += [
        "",
        "## Summary",
        "",
        f"Containers provisioned:  {len(containers)}",
        f"Peak concurrent jobs:    {peak}",
        f"Peak CPU:                {peak_cpu:.1f}%",
        f"Peak memory:             {peak_mem:.1f}%",
        "",
    ]

    # Degradation analysis — find first container where kubectl p99 > 2x baseline
    if len(containers) >= 2:
        baseline_k = [ms for _, ms in containers[0].kubectl_samples if ms is not None]
        baseline_avg = sum(baseline_k) / len(baseline_k) if baseline_k else 0
        for cm in containers[1:]:
            k_vals = [ms for _, ms in cm.kubectl_samples if ms is not None]
            if k_vals and baseline_avg > 0:
                avg = sum(k_vals) / len(k_vals)
                if avg > baseline_avg * 2:
                    lines.append(
                        f"Degradation detected at container #{cm.container_num} "
                        f"(kubectl avg {avg:.0f}ms vs baseline {baseline_avg:.0f}ms)"
                    )
                    lines.append(
                        f"Recommendation: safe max concurrent = {cm.container_num - 1} containers on this worker"
                    )
                    break
    else:
        lines.append("Not enough data for degradation analysis (need >= 2 containers).")

    report = "\n".join(lines)
    if output_path:
        Path(output_path).write_text(report)
        print(f"\nReport saved to {output_path}")
    return report


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_stress_test(args):
    variant    = args.variant
    worker_id  = args.worker_id
    ssh_host   = args.ssh_host
    max_conts  = args.max_containers
    branch     = ASTROSHOP_BRANCH if variant == "astroshop" else TODOAPP_BRANCH
    app_name   = "astroshop" if variant == "astroshop" else "todoapp"

    print(f"=== Capacity Stress Test: {variant.upper()} ===")
    print(f"Worker: {worker_id} ({ssh_host})")
    print(f"Branch: {branch}")
    print(f"Max containers: {max_conts}")
    print()

    if args.dry_run:
        print("[dry-run] Would push daemon jobs to queue:direct:{worker_id}")
        return

    pool = aioredis.from_url(REDIS_URL, decode_responses=True)
    stop_event = asyncio.Event()
    worker_samples: list[WorkerSample] = []
    containers: list[ContainerMetrics] = []
    global_stop_reason = ""
    worker_stop_reason: list[str] = []   # populated by worker_monitor on threshold breach
    steady_state_gate = asyncio.Event()  # set after first container post-create completes

    # Start worker monitor
    wmon_task = asyncio.create_task(
        worker_monitor(pool, worker_id, worker_samples, stop_event, worker_stop_reason, steady_state_gate)
    )

    monitor_tasks: list[asyncio.Task] = []

    try:
        for container_num in range(1, max_conts + 1):
            if stop_event.is_set():
                break

            job_id = (
                f"{worker_id}-{REPO_NAME}"
                f"-stress-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
            )
            cm = ContainerMetrics(container_num=container_num, job_id=job_id)
            cm.start_time = time.time()
            containers.append(cm)

            print(f"\n── Container #{container_num} ──────────────────────────────")
            print(f"   job_id:  {job_id}")
            print(f"   time:    {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

            # Push to direct queue
            await push_direct_job(pool, worker_id, job_id, REPO, branch)
            print(f"   Pushed to queue:direct:{worker_id}")

            # Wait for job to appear in running state
            print(f"   Waiting for job to start (max {JOB_APPEAR_TIMEOUT_S}s)...")
            meta = await wait_job_running(pool, job_id)
            if not meta:
                cm.stop_reason = "job never appeared in running state"
                global_stop_reason = cm.stop_reason
                print(f"   ERROR: {cm.stop_reason}")
                stop_event.set()
                break

            cm.sb_name = meta.get("sb_name") or f"sb-{job_id[-32:]}"
            print(f"   sb_name: {cm.sb_name}")

            # Start kubectl monitoring immediately — K3s comes up during post-create,
            # so measurements begin as soon as the cluster is ready, not just after
            # post-create finishes. warmup_done gates the stop logic: failures and
            # degraded pods only trigger a stop once post-create completes.
            warmup_done = asyncio.Event()
            monitor_tasks.append(asyncio.create_task(
                kubectl_monitor(cm, ssh_host, stop_event, warmup_done=warmup_done)
            ))

            # Wait for post-create to complete
            print(f"   Waiting for daemon ready (max {STARTUP_READY_TIMEOUT_S // 60}min)...")
            ready = await wait_daemon_ready(pool, job_id)
            if not ready:
                cm.stop_reason = "post-create failed or timed out"
                global_stop_reason = cm.stop_reason
                print(f"   ERROR: {cm.stop_reason}")
                stop_event.set()
                break

            cm.ready_time = time.time()
            cm.setup_duration_s = cm.ready_time - cm.start_time
            print(f"   Ready in {cm.setup_duration_s:.0f}s")

            # Lift warmup — kubectl failures now trigger stop
            warmup_done.set()
            # First container ready: allow CPU/mem thresholds to enforce
            steady_state_gate.set()

            # Get app URL (app-registry written at end of post-create)
            cm.app_url = await get_app_url(pool, job_id, cm.sb_name, ssh_host, app_name)
            if cm.app_url:
                print(f"   App URL: {cm.app_url}")
                monitor_tasks.append(asyncio.create_task(
                    curl_monitor(cm, stop_event)
                ))

            # Next daemon starts 30s after this container finished its post-create.
            if stop_event.is_set():
                break
            if container_num < max_conts:
                print(f"   Waiting {NEXT_CONTAINER_DELAY_S}s from ready before next container...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=NEXT_CONTAINER_DELAY_S)
                    break  # stop_event fired during wait
                except asyncio.TimeoutError:
                    pass  # normal — proceed to next container

        if not global_stop_reason and stop_event.is_set():
            if worker_stop_reason:
                global_stop_reason = worker_stop_reason[0]
            else:
                for cm in containers:
                    if cm.stop_reason:
                        global_stop_reason = f"container #{cm.container_num}: {cm.stop_reason}"
                        break

        # Let monitors run a bit longer to capture final state
        if monitor_tasks:
            print(f"\nAll containers provisioned. Monitoring for 60s more...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            stop_event.set()

        print("\nStopping monitors...")
        for t in monitor_tasks:
            t.cancel()
        wmon_task.cancel()
        await asyncio.gather(*monitor_tasks, wmon_task, return_exceptions=True)

    finally:
        await pool.aclose()

    # Generate report
    report = generate_report(
        variant=variant,
        worker_id=worker_id,
        containers=containers,
        worker_samples=worker_samples,
        stop_reason=global_stop_reason,
        output_path=args.output,
    )
    print("\n" + "=" * 80)
    print(report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Capacity stress test for AMD workers")
    parser.add_argument(
        "--variant", choices=["astroshop", "todoapp"], default="astroshop",
        help="App variant to deploy (default: astroshop)"
    )
    parser.add_argument(
        "--max-containers", type=int, default=8,
        help="Max containers to start before stopping (default: 8)"
    )
    parser.add_argument(
        "--worker-id", default=DEFAULT_WORKER_ID,
        help=f"Worker ID to target (default: {DEFAULT_WORKER_ID})"
    )
    parser.add_argument(
        "--ssh-host", default=DEFAULT_WORKER_SSH,
        help=f"SSH host alias for the worker (default: {DEFAULT_WORKER_SSH})"
    )
    parser.add_argument(
        "--output", default=None,
        help="Write report to this file path"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without actually running"
    )
    args = parser.parse_args()
    asyncio.run(run_stress_test(args))


if __name__ == "__main__":
    main()

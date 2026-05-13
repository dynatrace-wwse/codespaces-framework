"""Nightly test scheduler — staggers integration tests across the fleet."""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as redis
import yaml

from webhook.config import REDIS_URL, FRAMEWORK_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ops-nightly")


def load_testable_repos() -> list[dict]:
    """Load repos from repos.yaml that have CI enabled."""
    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    repos = []
    for r in data.get("repos", []):
        if r.get("status") != "active":
            continue
        if not r.get("ci", True):
            continue
        repos.append({
            "name": r["name"],
            "repo": r["repo"],
            "duration": r.get("duration", "1h"),
            "arch": r.get("arch", "both"),  # arm64 | amd64 | both
        })

    return repos


def parse_duration_minutes(duration_str: str) -> int:
    """Parse duration string like '2h', '1.5h', '30m' to minutes."""
    d = duration_str.strip().lower()
    if d.endswith("h"):
        return int(float(d[:-1]) * 60)
    if d.endswith("m"):
        return int(d[:-1])
    return 60  # default 1h


def build_schedule(repos: list[dict], stagger_minutes: int = 5) -> list[dict]:
    """Build a staggered test schedule.

    Sorts repos by duration (shortest first) so quick canary tests run early
    and provide fast feedback. Staggers start times to avoid overwhelming
    the DT tenant with concurrent operator deployments.
    """
    # Sort: shortest duration first (canary), then alphabetical
    sorted_repos = sorted(repos, key=lambda r: (parse_duration_minutes(r["duration"]), r["name"]))

    schedule = []
    for i, repo in enumerate(sorted_repos):
        schedule.append({
            "repo": repo["repo"],
            "name": repo["name"],
            "duration": repo["duration"],
            "arch": repo.get("arch", "both"),
            "offset_minutes": i * stagger_minutes,
            "order": i + 1,
        })

    return schedule


async def run_nightly(
    stagger_minutes: int = 5,
    parallel: int = 6,
    dry_run: bool = False,
):
    """Execute the nightly test schedule."""
    repos = load_testable_repos()
    schedule = build_schedule(repos, stagger_minutes)
    run_id = f"nightly-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    log.info("=" * 60)
    log.info("Nightly test run: %s", run_id)
    log.info("Repos: %d | Stagger: %dm | Max parallel: %d", len(schedule), stagger_minutes, parallel)
    log.info("=" * 60)

    for entry in schedule:
        log.info(
            "  [%2d] +%3dm  %-50s (%s)",
            entry["order"],
            entry["offset_minutes"],
            entry["name"],
            entry["duration"],
        )

    if dry_run:
        log.info("Dry run — no jobs queued.")
        return

    pool = redis.from_url(REDIS_URL, decode_responses=True)

    # Record nightly run metadata
    run_meta = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_repos": len(schedule),
        "stagger_minutes": stagger_minutes,
        "parallel": parallel,
    }
    await pool.set(f"nightly:{run_id}:meta", json.dumps(run_meta))

    # Queue jobs with staggered delays — route to arch-specific queues
    for i, entry in enumerate(schedule):
        if i > 0:
            log.info(
                "[%d/%d] Waiting %dm before queueing %s...",
                i + 1, len(schedule), stagger_minutes, entry["name"],
            )
            await asyncio.sleep(stagger_minutes * 60)

        arch = entry.get("arch", "both")
        arches = [arch] if arch != "both" else ["arm64", "amd64"]

        for a in arches:
            job = {
                "type": "integration-test",
                "repo": entry["repo"],
                "arch": a,
                "queue": "test",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "nightly_run_id": run_id,
                "order": entry["order"],
            }
            await pool.rpush(f"queue:test:{a}", json.dumps(job))
            log.info("Queued: %s → queue:test:%s (order %d)", entry["name"], a, entry["order"])

    log.info("All %d repos queued for nightly run %s", len(schedule), run_id)
    await pool.aclose()


async def run_single(repo_name: str):
    """Queue a single repo for integration testing."""
    repos = load_testable_repos()
    match = [r for r in repos if r["name"] == repo_name or r["repo"] == repo_name]

    if not match:
        log.error("Repo not found: %s", repo_name)
        log.info("Available repos:")
        for r in repos:
            log.info("  - %s (%s)", r["name"], r["repo"])
        sys.exit(1)

    repo = match[0]
    pool = redis.from_url(REDIS_URL, decode_responses=True)

    job = {
        "type": "integration-test",
        "repo": repo["repo"],
        "queue": "test",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "nightly_run_id": "manual",
    }

    await pool.rpush("queue:test", json.dumps(job))
    log.info("Queued integration test for %s", repo["repo"])
    await pool.aclose()


def main():
    """CLI entry point for the nightly scheduler."""
    import argparse

    parser = argparse.ArgumentParser(description="Nightly test scheduler")
    sub = parser.add_subparsers(dest="command", required=True)

    # nightly
    n = sub.add_parser("nightly", help="Run full nightly test schedule")
    n.add_argument("--stagger", type=int, default=5, help="Minutes between test starts (default: 5)")
    n.add_argument("--parallel", type=int, default=6, help="Max parallel workers (default: 6)")
    n.add_argument("--dry-run", action="store_true", help="Print schedule without running")

    # single
    s = sub.add_parser("single", help="Run a single repo test")
    s.add_argument("repo", help="Repo name or owner/name")

    # schedule
    sc = sub.add_parser("schedule", help="Print the nightly schedule without running")

    # report
    rp = sub.add_parser("report", help="Show last nightly run results")

    args = parser.parse_args()

    if args.command == "nightly":
        asyncio.run(run_nightly(args.stagger, args.parallel, args.dry_run))
    elif args.command == "single":
        asyncio.run(run_single(args.repo))
    elif args.command == "schedule":
        repos = load_testable_repos()
        schedule = build_schedule(repos)
        total_minutes = schedule[-1]["offset_minutes"] if schedule else 0
        print(f"\nNightly schedule ({len(schedule)} repos, ~{total_minutes}m total):\n")
        for entry in schedule:
            print(
                f"  [{entry['order']:2d}] +{entry['offset_minutes']:3d}m  "
                f"{entry['name']:<50s} ({entry['duration']})"
            )
        print()
    elif args.command == "report":
        asyncio.run(show_report())


async def show_report():
    """Show the last nightly run results."""
    pool = redis.from_url(REDIS_URL, decode_responses=True)

    # Get all completed jobs
    completed = await pool.lrange("jobs:completed", -100, -1)
    nightly_jobs = []
    for j in completed:
        job = json.loads(j)
        if job.get("type") == "integration-test" and job.get("nightly_run_id", "").startswith("nightly-"):
            nightly_jobs.append(job)

    if not nightly_jobs:
        print("No nightly results found.")
        await pool.aclose()
        return

    # Group by run_id
    runs: dict[str, list] = {}
    for job in nightly_jobs:
        rid = job["nightly_run_id"]
        runs.setdefault(rid, []).append(job)

    # Show latest run
    latest_id = sorted(runs.keys())[-1]
    latest = runs[latest_id]

    passed = sum(1 for j in latest if j.get("result", {}).get("passed"))
    failed = sum(1 for j in latest if not j.get("result", {}).get("passed"))

    print(f"\nNightly run: {latest_id}")
    print(f"Results: {passed} passed, {failed} failed, {len(latest)} total\n")

    for job in sorted(latest, key=lambda j: j.get("repo", "")):
        result = job.get("result", {})
        status = "PASS" if result.get("passed") else "FAIL"
        duration = result.get("duration_seconds", 0)
        repo_name = job["repo"].split("/")[-1]
        print(f"  [{status}] {repo_name:<50s} ({duration}s)")

    print()
    await pool.aclose()


if __name__ == "__main__":
    main()

"""Test executor — runs integration tests in Docker/k3d on the worker node."""

import asyncio
import logging
import os
import time
from pathlib import Path

from .config import (
    REPOS_DIR,
    LOGS_DIR,
    DT_ENVIRONMENT,
    DT_OPERATOR_TOKEN,
    DT_INGEST_TOKEN,
    TEST_TIMEOUT,
    TEST_IMAGE,
    WORKER_ARCH,
)

log = logging.getLogger("ops-worker-agent")


async def execute_integration_test(job: dict) -> dict:
    """Run an integration test for a repo using devcontainer CI."""
    repo = job["repo"]
    repo_name = repo.split("/")[-1]
    repo_dir = REPOS_DIR / repo_name
    log_file = LOGS_DIR / f"{job['job_id']}.log"

    # Ensure repo is cloned and up to date
    await _ensure_repo(repo, repo_dir)

    # Run the integration test via Docker
    cmd = [
        "docker", "run",
        "--rm",
        "--privileged",
        "--network=host",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{repo_dir}:/workspace",
        "-e", f"DT_ENVIRONMENT={DT_ENVIRONMENT}",
        "-e", f"DT_OPERATOR_TOKEN={DT_OPERATOR_TOKEN}",
        "-e", f"DT_INGEST_TOKEN={DT_INGEST_TOKEN}",
        "-e", "INSTANTIATION_TYPE=ops-server",
        "-e", f"WORKER_ARCH={WORKER_ARCH}",
        "-w", "/workspace",
        TEST_IMAGE,
        "zsh", "-c", ".devcontainer/test/integration.sh",
    ]

    start_time = time.time()
    log.info("Executing test for %s (arch=%s)", repo_name, WORKER_ARCH)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=TEST_TIMEOUT
    )
    duration = int(time.time() - start_time)

    # Save logs
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        f"=== JOB: {job['job_id']} ===\n"
        f"=== REPO: {repo} | ARCH: {WORKER_ARCH} ===\n"
        f"=== DURATION: {duration}s ===\n\n"
        f"=== STDOUT ===\n{stdout.decode()}\n\n"
        f"=== STDERR ===\n{stderr.decode()}"
    )

    return {
        "test": "integration",
        "arch": WORKER_ARCH,
        "exit_code": proc.returncode,
        "duration_seconds": duration,
        "passed": proc.returncode == 0,
        "log_file": str(log_file),
    }


async def _ensure_repo(repo: str, repo_dir: Path):
    """Clone or pull latest for a repo."""
    if repo_dir.exists():
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--ff-only",
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    else:
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "gh", "repo", "clone", repo, str(repo_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

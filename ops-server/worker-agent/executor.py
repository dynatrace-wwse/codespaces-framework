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
    """Run an integration test by invoking the framework's own Makefile.

    Flow (mirrors what a developer runs locally):
      1. Clone/pull the repo
      2. Write .devcontainer/.env with DT credentials + per-worker port config
      3. cd .devcontainer && make clean-start  (k3d cluster + operator + apps)
      4.                       make integration (runs .devcontainer/test/integration.sh)
      5. make clean (always — even on test failure)

    The framework's makefile.sh handles the dev container, k3d cluster setup,
    operator deploy, app deploys, and test execution. We are a thin wrapper
    that captures stdout/stderr and turns the exit code into pass/fail.
    """
    repo = job["repo"]
    repo_name = repo.split("/")[-1]
    repo_dir = REPOS_DIR / repo_name
    devcontainer_dir = repo_dir / ".devcontainer"
    log_file = LOGS_DIR / f"{job['job_id']}.log"

    await _ensure_repo(repo, repo_dir)
    await _make_world_writable(repo_dir)
    await _write_devcontainer_env(devcontainer_dir)

    start_time = time.time()
    log.info("make clean-start && make integration for %s (arch=%s)", repo_name, WORKER_ARCH)

    # Always run `make clean` after, even if integration fails. Preserves the
    # integration exit code as the overall result.
    full_cmd = (
        "make clean-start && (make integration; rc=$?) || rc=1; "
        "make clean >/dev/null 2>&1 || true; "
        "exit ${rc:-1}"
    )
    proc = await asyncio.create_subprocess_shell(
        full_cmd,
        cwd=str(devcontainer_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TEST_TIMEOUT
        )
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        timed_out = True

    duration = int(time.time() - start_time)
    rc = proc.returncode if not timed_out else 124

    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        f"=== JOB: {job['job_id']} ===\n"
        f"=== REPO: {repo} | ARCH: {WORKER_ARCH} ===\n"
        f"=== DURATION: {duration}s | EXIT: {rc} | TIMED_OUT: {timed_out} ===\n\n"
        f"=== STDOUT ===\n{stdout.decode(errors='replace')}\n\n"
        f"=== STDERR ===\n{stderr.decode(errors='replace')}"
    )

    return {
        "test": "integration",
        "arch": WORKER_ARCH,
        "exit_code": rc,
        "duration_seconds": duration,
        "passed": rc == 0,
        "timed_out": timed_out,
        "log_file": str(log_file),
    }


async def _write_devcontainer_env(devcontainer_dir: Path):
    """Drop a .env file for the framework's makefile to source.

    Picks high ports so we don't collide with the master's nginx (80/443)
    or anything else on the host. Cluster name is per-arch so the worker
    never fights with a developer running ``make clean-start`` manually
    for their own debugging.
    """
    devcontainer_dir.mkdir(parents=True, exist_ok=True)
    env_path = devcontainer_dir / ".env"
    env_path.write_text(
        f"DT_ENVIRONMENT={DT_ENVIRONMENT}\n"
        f"DT_OPERATOR_TOKEN={DT_OPERATOR_TOKEN}\n"
        f"DT_INGEST_TOKEN={DT_INGEST_TOKEN}\n"
        f"INSTANTIATION_TYPE=ops-server\n"
        f"WORKER_ARCH={WORKER_ARCH}\n"
        f"K3D_CLUSTER_NAME=worker-{WORKER_ARCH}\n"
        f"K3D_LB_HTTP_PORT=30080\n"
        f"K3D_LB_HTTPS_PORT=30443\n"
        f"K3D_API_PORT=6444\n"
    )


async def _make_world_writable(repo_dir: Path):
    """Widen permissions so a container running as a different uid can write.

    The clone is transient (recloned per test), so loose perms here are fine.
    """
    proc = await asyncio.create_subprocess_exec(
        "chmod", "-R", "go+rwX", str(repo_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _ensure_repo(repo: str, repo_dir: Path):
    """Clone or pull latest for a repo.

    Handles three states:
      - dir exists with .git → pull
      - dir exists without .git (broken from a previous failed clone) → wipe and re-clone
      - dir doesn't exist → clone
    """
    import shutil
    is_git = (repo_dir / ".git").exists()
    if repo_dir.exists() and is_git:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--ff-only",
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() == 0:
            return
        log.warning("git pull failed for %s — wiping and re-cloning", repo)
        is_git = False

    if repo_dir.exists() and not is_git:
        shutil.rmtree(str(repo_dir), ignore_errors=True)

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", url, str(repo_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git clone {url} failed (rc={proc.returncode}): {stderr.decode()[:500]}"
        )

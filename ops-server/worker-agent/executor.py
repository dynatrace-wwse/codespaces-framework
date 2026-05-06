"""Test executor — mirrors the GitHub Actions integration-tests.yaml flow.

The repo's GHA workflow uses devcontainers/ci@v0.3 to:
  1. Build/pull the devcontainer image
  2. Run the container with the devcontainer.json runArgs + --env-file
  3. Execute postCreateCommand (sets up k3d cluster + DT operator + apps)
  4. Execute postStartCommand
  5. Execute the user runCmd: zsh .devcontainer/test/integration.sh

We replicate that as a single ``docker run`` that chains those steps so
each PR is tested the same way the GHA workflow tests it.
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path

from .config import (
    REPOS_DIR,
    LOGS_DIR,
    WORKDIR,
    DT_ENVIRONMENT,
    DT_OPERATOR_TOKEN,
    DT_INGEST_TOKEN,
    TEST_TIMEOUT,
    TEST_IMAGE,
    WORKER_ARCH,
)

log = logging.getLogger("ops-worker-agent")


async def execute_integration_test(job: dict) -> dict:
    """Run an integration test against the PR's branch (or main, for nightly)."""
    repo      = job["repo"]                       # base repo, e.g. dynatrace-wwse/codespaces-framework
    head_repo = job.get("head_repo") or repo      # for fork PRs (head.repo.full_name), else same as base
    ref       = job.get("ref") or job.get("head_branch") or "main"
    repo_name = repo.split("/")[-1]
    job_id    = job["job_id"]
    log_file  = LOGS_DIR / f"{job_id}.log"

    # Per-job working dir so concurrent tests can't trample each other
    work_dir = WORKDIR / job_id
    repo_dir = work_dir / repo_name
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Pre-test cleanup: ensure no stale clusters/containers from earlier runs.
    # Without this, kubeconfig and port bindings can leak between tests.
    await _cleanup_clusters()

    log.info("Cloning %s @ %s for job %s", head_repo, ref, job_id)
    await _git_clone(head_repo, ref, repo_dir)
    await _make_world_writable(repo_dir)
    _write_env_file(repo_dir / ".devcontainer" / ".env")

    start_time = time.time()
    log.info("Running integration test for %s (arch=%s, ref=%s)", repo_name, WORKER_ARCH, ref)

    # Single docker run that mirrors devcontainers/ci@v0.3:
    #   postCreateCommand → postStartCommand → integration.sh
    # Mounted at /workspaces/<name> to match devcontainer convention.
    workspace = f"/workspaces/{repo_name}"
    env_file = f"{repo_dir}/.devcontainer/.env"
    chained_cmd = (
        "set -e; "
        "./.devcontainer/post-create.sh; "
        "./.devcontainer/post-start.sh; "
        "zsh ./.devcontainer/test/integration.sh"
    )
    cmd = [
        "docker", "run",
        "--rm",
        "--init",
        "--privileged",
        "--network=host",
        "--env-file", env_file,
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", "/lib/modules:/lib/modules",
        "-v", f"{repo_dir}:{workspace}",
        "-w", workspace,
        # safe.directory inside the container so git ops on /workspaces/* don't fail
        # when the host clone was created by a different uid
        "-e", "GIT_CONFIG_COUNT=1",
        "-e", "GIT_CONFIG_KEY_0=safe.directory",
        "-e", "GIT_CONFIG_VALUE_0=*",
        TEST_IMAGE,
        "/usr/bin/zsh", "-c", chained_cmd,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
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
    body = (
        f"=== JOB: {job_id} ===\n"
        f"=== REPO: {head_repo}@{ref} (base: {repo}) | ARCH: {WORKER_ARCH} ===\n"
        f"=== DURATION: {duration}s | EXIT: {rc} | TIMED_OUT: {timed_out} ===\n\n"
        f"=== STDOUT ===\n{stdout.decode(errors='replace')}\n\n"
        f"=== STDERR ===\n{stderr.decode(errors='replace')}"
    )
    log_file.write_text(_mask_secrets(body))

    # Best-effort cleanup: clusters created by post-create.sh
    await _cleanup_clusters()
    # Wipe the per-job workspace
    shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "test": "integration",
        "arch": WORKER_ARCH,
        "ref": ref,
        "exit_code": rc,
        "duration_seconds": duration,
        "passed": rc == 0,
        "timed_out": timed_out,
        "log_file": str(log_file),
    }


def _mask_secrets(content: str) -> str:
    """Redact known DT tokens before writing/uploading the log.

    The dt-enablement image's entrypoint dumps the environment via ``set``
    for debugging after switching to the docker group, which leaks tokens
    if not masked. We also mask any literal token value found anywhere
    in stdout/stderr (e.g. helm install args, kubectl secrets dumps).
    """
    import re
    # Mask each known token value verbatim
    for secret in (DT_OPERATOR_TOKEN, DT_INGEST_TOKEN):
        if secret and len(secret) > 12:
            content = content.replace(secret, _redact(secret))
    # Catch-all for any dt0c01.*/dt0s01.*/dt0s16.* token shape we missed
    # (60+ chars, alphanumeric + dots/underscores)
    content = re.sub(
        r"\bdt0[cs]\d{2}\.[A-Z0-9]{24}\.[A-Z0-9]{60,80}\b",
        lambda m: _redact(m.group(0)),
        content,
    )
    return content


def _redact(token: str) -> str:
    """Keep the prefix (dt0c01.XXXXXXXX) so we can still tell which token
    is which; replace the secret part with stars."""
    if not token:
        return "***"
    # Show first 14 chars (dt0c01.XXXXXXXX) then mask the rest
    return token[:14] + "***REDACTED***"


def _write_env_file(env_path: Path):
    """Mirror what the GHA workflow writes to .devcontainer/.env."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        f"DT_ENVIRONMENT={DT_ENVIRONMENT}\n"
        f"DT_OPERATOR_TOKEN={DT_OPERATOR_TOKEN}\n"
        f"DT_INGEST_TOKEN={DT_INGEST_TOKEN}\n"
    )


async def _make_world_writable(repo_dir: Path):
    """Widen perms so the container's vscode user (uid 1000) can write a clone owned by ops (uid 1001)."""
    proc = await asyncio.create_subprocess_exec(
        "chmod", "-R", "go+rwX", str(repo_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def _git_clone(repo: str, ref: str, dest: Path):
    """Shallow-clone the given repo at the given ref (branch or tag) into ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    # --branch accepts branches and tags. Falls back to default branch if ref doesn't exist.
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "1", "--branch", ref, url, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Retry without --branch in case ref is a sha (rare for our workflow).
        log.warning("git clone --branch %s failed; retrying default branch", ref)
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", url, str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone {url} failed (rc={proc.returncode}): {stderr.decode()[:500]}"
            )


async def _cleanup_clusters():
    """Wipe stale clusters / containers / kubeconfig before or after a test.

    Failures are ignored — best-effort cleanup. The next test will pick up
    a clean slate either way.
    """
    cmds = [
        # Remove all k3d clusters (most common state from a previous test)
        ["bash", "-c", "k3d cluster list -o name 2>/dev/null | xargs -r -I{} k3d cluster delete {}"],
        # Remove all kind clusters (if framework was switched to kind engine)
        ["bash", "-c", "kind get clusters 2>/dev/null | xargs -r -I{} kind delete cluster --name {}"],
        # Force-remove any framework dev container with a known name
        ["bash", "-c", "docker rm -f dt-enablement 2>/dev/null || true"],
        # Stale rancher/k3s containers (k3d nodes that didn't get cleaned)
        ["bash", "-c", "docker ps -aq --filter 'ancestor=rancher/k3s' | xargs -r docker rm -f 2>/dev/null || true"],
        ["bash", "-c", "docker ps -aq --filter 'name=k3d-' | xargs -r docker rm -f 2>/dev/null || true"],
        # Stale kubeconfig — k3d/kind write here; next post-create.sh will rewrite cleanly
        ["bash", "-c", "rm -f ~/.kube/config 2>/dev/null || true"],
    ]
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:
            pass

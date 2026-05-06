"""Worker manager — consumes jobs from Redis queues and dispatches to handlers."""

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as redis

from webhook.config import (
    REDIS_URL,
    REPOS_DIR,
    LOGS_DIR,
    WORKDIR,
    MAX_PARALLEL_WORKERS,
    MAX_PARALLEL_AGENTS,
    DT_ENVIRONMENT,
    DT_OPERATOR_TOKEN,
    DT_INGEST_TOKEN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ops-worker")


class WorkerManager:
    """Manages concurrent job execution from Redis queues."""

    def __init__(self):
        self.pool: redis.Redis | None = None
        self.test_semaphore = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
        self.agent_semaphore = asyncio.Semaphore(MAX_PARALLEL_AGENTS)
        self.active_jobs: dict[str, dict] = {}

    async def start(self):
        """Connect to Redis and start consuming queues."""
        self.pool = redis.from_url(REDIS_URL, decode_responses=True)
        log.info(
            "Worker manager started (max_workers=%d, max_agents=%d)",
            MAX_PARALLEL_WORKERS,
            MAX_PARALLEL_AGENTS,
        )

        await asyncio.gather(
            self._consume_queue("agent", self.agent_semaphore),
            self._consume_queue("sync", self.agent_semaphore),
            # Local ARM worker consumes arch-specific queue
            self._consume_queue("test:arm64", self.test_semaphore),
            # Legacy queue for backwards compatibility
            self._consume_queue("test", self.test_semaphore),
        )

    async def _consume_queue(self, queue_name: str, semaphore: asyncio.Semaphore):
        """Consume jobs from a single queue with concurrency limiting."""
        queue_key = f"queue:{queue_name}"
        while True:
            try:
                # Blocking pop with 5s timeout
                result = await self.pool.blpop(queue_key, timeout=5)
                if result is None:
                    continue

                _, job_json = result
                job = json.loads(job_json)
                # Use ms precision + 6-char random suffix so 3+ jobs picked up
                # within the same second can't collide on workdir / container name.
                import uuid
                job_id = (
                    f"{job['type']}-{job['repo'].split('/')[-1]}"
                    f"-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
                )
                job["job_id"] = job_id

                # Acquire semaphore slot before dispatching
                asyncio.create_task(self._run_with_semaphore(semaphore, job))

            except redis.ConnectionError:
                log.error("Redis connection lost, retrying in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log.error("Queue consumer error on %s: %s", queue_name, e)
                await asyncio.sleep(1)

    async def _run_with_semaphore(self, semaphore: asyncio.Semaphore, job: dict):
        """Run a job within a semaphore-controlled slot."""
        async with semaphore:
            job_id = job["job_id"]
            self.active_jobs[job_id] = job
            log.info("Starting job: %s (%s on %s)", job_id, job["type"], job["repo"])

            # Track running state for the dashboard (only meaningful for tests).
            running_key = None
            if job.get("type") == "integration-test":
                running_key = f"job:running:{job['repo']}:{job.get('arch', 'arm64')}"
                await self.pool.set(running_key, json.dumps({
                    "job_id": job_id,
                    "ref": job.get("ref") or job.get("head_branch") or "main",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "worker_id": "master",
                }), ex=3600)

            try:
                result = await self._dispatch(job)
                job["result"] = result
                job["status"] = "completed"
            except Exception as e:
                log.error("Job %s failed: %s", job_id, e)
                job["result"] = {"error": str(e)}
                job["status"] = "failed"
            finally:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                await self._publish_log(job)
                await self.pool.rpush("jobs:completed", json.dumps(job))
                await self.pool.ltrim("jobs:completed", -500, -1)
                if running_key:
                    await self.pool.delete(running_key)
                self.active_jobs.pop(job_id, None)
                log.info("Finished job: %s → %s", job_id, job["status"])

    async def _publish_log(self, job: dict):
        """Upload the per-job log to Redis so the dashboard can serve it.

        Stored under ``job:log:<id>`` with a 7-day TTL, capped at 256KB.
        """
        result = job.get("result", {}) or {}
        log_path = result.get("log_file")
        if not log_path:
            return
        try:
            content = open(log_path, "r", errors="replace").read()
        except OSError as e:
            content = f"(log unavailable: {e})"
        max_bytes = 256 * 1024
        if len(content.encode()) > max_bytes:
            content = "... (truncated; see {} on master) ...\n\n".format(
                log_path
            ) + content[-max_bytes:]
        try:
            await self.pool.set(f"job:log:{job['job_id']}", content, ex=86400 * 7)
        except Exception as e:
            log.warning("Could not publish log for %s: %s", job["job_id"], e)

    async def _dispatch(self, job: dict) -> dict:
        """Dispatch a job to the appropriate handler."""
        job_type = job["type"]

        if job_type == "fix-issue":
            return await self._run_agent(job, "fix-issue")

        elif job_type == "fix-ci":
            return await self._run_agent(job, "fix-ci")

        elif job_type == "review-pr":
            return await self._run_agent(job, "review-pr")

        elif job_type == "migrate-gen3":
            return await self._run_agent(job, "migrate-gen3")

        elif job_type == "scaffold-lab":
            return await self._run_agent(job, "scaffold-lab")

        elif job_type == "validate-after-push":
            return await self._run_sync(job, "validate")

        elif job_type == "integration-test":
            return await self._run_integration_test(job)

        else:
            log.warning("Unknown job type: %s", job_type)
            return {"error": f"Unknown job type: {job_type}"}

    async def _run_agent(self, job: dict, agent_type: str) -> dict:
        """Run a Claude Code agent for the given job."""
        repo = job["repo"]
        repo_name = repo.split("/")[-1]
        repo_dir = REPOS_DIR / repo_name
        log_file = LOGS_DIR / f"{job['job_id']}.log"

        # Ensure repo is cloned and up to date
        await self._ensure_repo(repo, repo_dir)

        # Build the prompt for Claude
        prompt = self._build_agent_prompt(agent_type, job)

        # Run Claude Code in non-interactive mode
        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--max-turns", "30",
            prompt,
        ]

        log.info("Running Claude agent: %s in %s", agent_type, repo_dir)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "DT_ENVIRONMENT": DT_ENVIRONMENT,
                "DT_OPERATOR_TOKEN": DT_OPERATOR_TOKEN,
                "DT_INGEST_TOKEN": DT_INGEST_TOKEN,
            },
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=600  # 10 minute timeout
        )

        # Save logs
        log_file.write_text(
            f"=== STDOUT ===\n{stdout.decode()}\n\n=== STDERR ===\n{stderr.decode()}"
        )

        return {
            "agent_type": agent_type,
            "exit_code": proc.returncode,
            "log_file": str(log_file),
        }

    async def _run_integration_test(self, job: dict) -> dict:
        """Run an integration test, matching the GHA integration-tests.yaml flow.

        Single docker run that chains:
            ./.devcontainer/post-create.sh   → k3d cluster + DT operator + apps
            ./.devcontainer/post-start.sh    → greeting / final setup
            zsh ./.devcontainer/test/integration.sh  → the actual test
        Equivalent to what devcontainers/ci@v0.3 does on the GHA runner.
        """
        import shutil

        repo      = job["repo"]
        head_repo = job.get("head_repo") or repo
        ref       = job.get("ref") or job.get("head_branch") or "main"
        repo_name = repo.split("/")[-1]
        job_id    = job["job_id"]
        log_file  = LOGS_DIR / f"{job_id}.log"

        # Per-job working dir. Master nginx owns 80/443 so we override ingress
        # ports via .env (relies on framework supporting K3D_* env vars).
        work_dir = WORKDIR / job_id
        repo_dir = work_dir / repo_name
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        log.info("Cloning %s @ %s for job %s", head_repo, ref, job_id)
        await self._git_clone(head_repo, ref, repo_dir)
        await self._make_world_writable(repo_dir)
        self._write_env_file(repo_dir / ".devcontainer" / ".env", arch="arm64")

        # Sysbox-isolated nested containers (see executor.py for the same
        # architecture): outer Sysbox container runs docker:25-dind; inner
        # dockerd hosts the dt-enablement container which spins up its own
        # k3d cluster. Multiple jobs run in parallel without colliding on
        # ports, container names, or cluster names.
        workspace = f"/workspaces/{repo_name}"
        env_file_inside = f"{workspace}/.devcontainer/.env"
        sb_name = f"sb-{job_id[-32:]}"
        inner_name = "dt"

        sections: list[str] = []
        rc = 0
        timed_out = False
        TEST_TIMEOUT = 1800
        start_time = time.time()
        deadline = start_time + TEST_TIMEOUT

        log.info("Running integration test for %s (arch=arm64, ref=%s)", repo_name, ref)
        try:
            # 1. Outer Sysbox container running docker:25-dind
            run_cmd = [
                "docker", "run",
                "-d",
                "--runtime=sysbox-runc",
                "--name", sb_name,
                "-v", f"{repo_dir}:{workspace}",
                "docker:25-dind",
            ]
            proc = await asyncio.create_subprocess_exec(
                *run_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                sections.append(f"=== sysbox docker run failed (rc={proc.returncode}) ===\n{err.decode(errors='replace')}")
                rc = proc.returncode
                raise RuntimeError("sysbox container start failed")

            # 2. Wait for inner dockerd
            await self._wait_for_inner_docker(sb_name)

            # 3. Pull dt-enablement inside the Sysbox
            pull = await asyncio.create_subprocess_exec(
                "docker", "exec", sb_name,
                "docker", "pull", "shinojosa/dt-enablement:v1.2",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await pull.wait()

            # 4. Start dt-enablement detached inside the Sysbox
            inner_run = [
                "docker", "exec", sb_name,
                "docker", "run", "-d",
                "--init", "--privileged", "--network=host",
                "--name", inner_name,
                "--env-file", env_file_inside,
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", f"{workspace}:{workspace}",
                "-w", workspace,
                "-e", "GIT_CONFIG_COUNT=1",
                "-e", "GIT_CONFIG_KEY_0=safe.directory",
                "-e", "GIT_CONFIG_VALUE_0=*",
                "shinojosa/dt-enablement:v1.2",
                "sleep", "infinity",
            ]
            proc = await asyncio.create_subprocess_exec(
                *inner_run, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                sections.append(f"=== inner docker run failed (rc={proc.returncode}) ===\n{err.decode(errors='replace')}")
                rc = proc.returncode
                raise RuntimeError("inner container start failed")

            # 5. Wait for vscode inside dt-enablement to have docker access
            await self._wait_for_inner_dt_ready(sb_name, inner_name)

            steps = [
                ("postCreateCommand", "./.devcontainer/post-create.sh"),
                ("postStartCommand",  "./.devcontainer/post-start.sh"),
                ("integrationTest",   "zsh .devcontainer/test/integration.sh"),
            ]
            livelog_key = f"job:livelog:{job_id}"
            await self.pool.set(livelog_key, "", ex=3600)

            for label, script in steps:
                header = f"\n=== {label} ===\n"
                sections.append(header)
                await self.pool.append(livelog_key, header)
                remaining = max(60, int(deadline - time.time()))
                # docker exec <sysbox> docker exec <dt> bash -lc <script>
                exec_cmd = [
                    "docker", "exec", sb_name,
                    "docker", "exec",
                    "-w", workspace,
                    inner_name,
                    "bash", "-lc", script,
                ]
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *exec_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    step_out = await self._stream_to_redis(proc, livelog_key, remaining)
                    sections.append(step_out)
                    if proc.returncode != 0:
                        rc = proc.returncode
                        msg = f"\n=== {label} exited with rc={rc} — stopping ===\n"
                        sections.append(msg)
                        await self.pool.append(livelog_key, msg)
                        break
                except asyncio.TimeoutError:
                    proc.kill()
                    sections.append(f"\n=== {label} timed out ===\n")
                    rc = 124
                    timed_out = True
                    break
        except Exception as e:
            sections.append(f"\n=== executor error: {e} ===\n")
            if rc == 0:
                rc = 1
        finally:
            # Removing the outer Sysbox container takes the inner dockerd,
            # dt-enablement, and k3d cluster down with it.
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", sb_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(kill_proc.wait(), timeout=60)
            except Exception:
                pass

        duration = int(time.time() - start_time)
        header = (
            f"=== JOB: {job_id} ===\n"
            f"=== REPO: {head_repo}@{ref} (base: {repo}) | ARCH: arm64 (master) ===\n"
            f"=== DURATION: {duration}s | EXIT: {rc} | TIMED_OUT: {timed_out} ===\n"
        )
        log_file.write_text(self._mask_secrets(header + "".join(sections)))

        # No host-level cleanup needed — Sysbox tear-down (above) takes the
        # inner dockerd, dt-enablement, and k3d cluster down with it.
        shutil.rmtree(work_dir, ignore_errors=True)

        return {
            "test": "integration",
            "arch": "arm64",
            "ref": ref,
            "exit_code": rc,
            "duration_seconds": duration,
            "passed": rc == 0,
            "timed_out": timed_out,
            "log_file": str(log_file),
        }

    async def _stream_to_redis(self, proc, livelog_key: str, timeout_s: int) -> str:
        """Stream proc.stdout to ``livelog_key`` (~1s flush) for the dashboard."""
        full = []
        pending = []
        last_flush = time.time()
        deadline = last_flush + timeout_s
        MAX_LIVE_BYTES = 256 * 1024

        async def flush():
            if not pending:
                return
            chunk = self._mask_secrets("".join(pending))
            pending.clear()
            try:
                await self.pool.append(livelog_key, chunk)
                cur = await self.pool.strlen(livelog_key)
                if cur and cur > MAX_LIVE_BYTES:
                    tail = await self.pool.getrange(livelog_key, cur - MAX_LIVE_BYTES, cur)
                    await self.pool.set(livelog_key, tail, ex=3600)
            except Exception as e:
                log.warning("livelog flush failed: %s", e)

        while True:
            if time.time() > deadline:
                await flush()
                raise asyncio.TimeoutError()
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                line = b""
            if not line:
                if proc.returncode is not None or proc.stdout.at_eof():
                    break
                if time.time() - last_flush > 1.0:
                    await flush(); last_flush = time.time()
                continue
            decoded = line.decode(errors="replace")
            full.append(decoded); pending.append(decoded)
            if time.time() - last_flush > 1.0:
                await flush(); last_flush = time.time()
        await proc.wait()
        await flush()
        return "".join(full)

    async def _wait_for_inner_docker(self, sb_name: str, timeout_s: int = 60):
        """Wait until the Sysbox container's inner dockerd is responsive."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", sb_name,
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if (await proc.wait()) == 0:
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"inner dockerd never came up in {sb_name}")

    async def _wait_for_inner_dt_ready(self, sb_name: str, inner_name: str, timeout_s: int = 60):
        """Wait until vscode in the dt-enablement container can talk to docker."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", sb_name,
                "docker", "exec", inner_name,
                "sh", "-c", "docker info >/dev/null 2>&1",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if (await proc.wait()) == 0:
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"vscode never got docker access in {sb_name}/{inner_name}")

    def _mask_secrets(self, content: str) -> str:
        """Redact known DT tokens before writing the log."""
        import re
        for secret in (DT_OPERATOR_TOKEN, DT_INGEST_TOKEN):
            if secret and len(secret) > 12:
                content = content.replace(secret, secret[:14] + "***REDACTED***")
        # Catch-all for any dt0* token shape
        content = re.sub(
            r"\bdt0[cs]\d{2}\.[A-Z0-9]{24}\.[A-Z0-9]{60,80}\b",
            lambda m: m.group(0)[:14] + "***REDACTED***",
            content,
        )
        return content

    def _write_env_file(self, env_path: Path, arch: str):
        """Mirror the GHA workflow's .env writing.

        Adds K3D_* port overrides so the in-container k3d cluster doesn't
        try to bind to nginx's 80/443 on the master host.

        EXTERNAL_HOSTNAME is the master's hostname — passed through so that
        registerApp's hostname-based ingress route is stable across parallel
        workers (otherwise each worker's dt container would use its own
        random container hostname).
        """
        import socket
        external_hostname = os.environ.get("EXTERNAL_HOSTNAME") or socket.gethostname()

        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            f"DT_ENVIRONMENT={DT_ENVIRONMENT}\n"
            f"DT_OPERATOR_TOKEN={DT_OPERATOR_TOKEN}\n"
            f"DT_INGEST_TOKEN={DT_INGEST_TOKEN}\n"
            f"K3D_CLUSTER_NAME=master-{arch}\n"
            f"K3D_LB_HTTP_PORT=30080\n"
            f"K3D_LB_HTTPS_PORT=30443\n"
            f"K3D_API_PORT=6444\n"
            f"EXTERNAL_HOSTNAME={external_hostname}\n"
        )

    async def _git_clone(self, repo: str, ref: str, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{repo}.git"
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", "--branch", ref, url, str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
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

    async def _cleanup_clusters(self):
        """Wipe stale clusters / containers / kubeconfig (best-effort)."""
        cmds = [
            ["bash", "-c", "k3d cluster list -o name 2>/dev/null | xargs -r -I{} k3d cluster delete {}"],
            ["bash", "-c", "kind get clusters 2>/dev/null | xargs -r -I{} kind delete cluster --name {}"],
            ["bash", "-c", "docker rm -f dt-enablement 2>/dev/null || true"],
            ["bash", "-c", "docker ps -aq --filter 'ancestor=rancher/k3s' | xargs -r docker rm -f 2>/dev/null || true"],
            ["bash", "-c", "docker ps -aq --filter 'name=k3d-' | xargs -r docker rm -f 2>/dev/null || true"],
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

    async def _run_sync(self, job: dict, command: str) -> dict:
        """Run a sync CLI command."""
        repo = job["repo"]
        sync_dir = Path.home() / "enablement-framework" / "codespaces-framework"

        cmd = ["python3", "-m", "sync.cli", command]
        if repo != "unknown":
            cmd.extend(["--repo", repo])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(sync_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(sync_dir)},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        return {
            "command": command,
            "exit_code": proc.returncode,
            "output": stdout.decode()[-2000:],  # Last 2000 chars
        }

    async def _make_world_writable(self, repo_dir: Path):
        """Widen perms so a container running as a different uid can write."""
        proc = await asyncio.create_subprocess_exec(
            "chmod", "-R", "go+rwX", str(repo_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def _ensure_repo(self, repo: str, repo_dir: Path):
        """Clone or pull latest for a repo.

        Handles three states:
          - dir exists with .git → pull
          - dir exists without .git (broken from earlier failed clone) → wipe and re-clone
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

    def _build_agent_prompt(self, agent_type: str, job: dict) -> str:
        """Build a Claude Code prompt for the given agent type."""
        repo = job["repo"]

        if agent_type == "fix-issue":
            return (
                f"You are the enablement ops agent. A bug was reported in {repo}.\n\n"
                f"Issue #{job['issue_number']}: {job['title']}\n\n"
                f"{job.get('body', '')}\n\n"
                "Instructions:\n"
                "1. Investigate the issue — read the relevant code, check recent changes\n"
                "2. Query Dynatrace via MCP if the issue relates to observability or monitoring\n"
                "3. Implement a fix\n"
                "4. Run tests: make test (if available)\n"
                "5. Create a new branch, commit, and create a PR referencing the issue\n"
                "6. Use: gh pr create --title 'Fix: <summary>' "
                f"--body 'Fixes #{job['issue_number']}'\n"
            )

        elif agent_type == "fix-ci":
            return (
                f"You are the enablement ops agent. CI failed on {repo}.\n\n"
                f"Failed check suite for commit: {job.get('head_sha', 'unknown')}\n"
                f"PR numbers: {job.get('pr_numbers', [])}\n\n"
                "Instructions:\n"
                "1. Run: gh run list --limit 1 --json conclusion,databaseId\n"
                "2. Read the failed logs: gh run view <id> --log-failed\n"
                "3. Diagnose the root cause\n"
                "4. If it's a code issue, fix it and push to the PR branch\n"
                "5. If it's a flaky test, add a retry or note it in a comment\n"
                "6. If it's a framework issue, note it for manual review\n"
            )

        elif agent_type == "review-pr":
            return (
                f"You are the enablement ops agent. A PR was opened in {repo}.\n\n"
                f"PR #{job['pr_number']}: {job['title']}\n"
                f"URL: {job['pr_url']}\n\n"
                "Instructions:\n"
                "1. Read the diff: gh pr diff\n"
                "2. Check for: security issues, framework compliance, test coverage\n"
                "3. Verify devcontainer.json follows the framework spec\n"
                "4. Post a review comment with findings\n"
                "5. Approve if changes look good, request changes if not\n"
            )

        elif agent_type == "migrate-gen3":
            return (
                f"You are the enablement ops agent. Migrate {repo} from Gen2 to Gen3.\n\n"
                f"Issue #{job.get('issue_number', 'N/A')}: {job.get('title', '')}\n\n"
                "Instructions:\n"
                "1. Scan all docs/ markdown files for Gen2 (classic) references\n"
                "2. Use the dt-migration skill patterns to update:\n"
                "   - Classic entity selectors → Smartscape queries\n"
                "   - Old navigation paths → Native app navigation\n"
                "   - Deprecated DQL syntax → Current DQL\n"
                "3. Validate DQL queries by running them via dtctl or MCP\n"
                "4. Flag screenshots that need re-capture (add TODO comments)\n"
                "5. Create a PR with a detailed changelog of all changes\n"
            )

        elif agent_type == "scaffold-lab":
            return (
                f"You are the enablement ops agent. Create a new enablement lab.\n\n"
                f"Issue #{job.get('issue_number', 'N/A')}: {job.get('title', '')}\n\n"
                f"Description:\n{job.get('body', '')}\n\n"
                "Instructions:\n"
                "1. Parse the issue body for: topic, duration, tags, description\n"
                "2. Create the repo from template:\n"
                "   gh repo create dynatrace-wwse/<name> "
                "--template dynatrace-wwse/enablement-codespaces-template --public\n"
                "3. Clone it, configure devcontainer.json and post-create.sh\n"
                "4. Generate initial docs/ structure\n"
                "5. Add entry to repos.yaml in codespaces-framework\n"
                "6. Comment on the issue with the new repo URL\n"
            )

        return f"Unknown agent type: {agent_type}"


async def main():
    manager = WorkerManager()
    await manager.start()


if __name__ == "__main__":
    asyncio.run(main())

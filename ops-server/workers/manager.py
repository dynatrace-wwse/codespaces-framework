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
                job_id = f"{job['type']}-{job['repo'].split('/')[-1]}-{int(time.time())}"
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
                # Trim to keep last 500 completed jobs
                await self.pool.ltrim("jobs:completed", -500, -1)
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

        # Pre-test cleanup so leftover clusters / containers / kubeconfig
        # from earlier runs can't poison this run.
        await self._cleanup_clusters()

        log.info("Cloning %s @ %s for job %s", head_repo, ref, job_id)
        await self._git_clone(head_repo, ref, repo_dir)
        await self._make_world_writable(repo_dir)
        self._write_env_file(repo_dir / ".devcontainer" / ".env", arch="arm64")

        workspace = f"/workspaces/{repo_name}"
        env_file = f"{repo_dir}/.devcontainer/.env"
        container_name = f"test-{job_id[-32:]}"

        # Mirror devcontainers/ci@v0.3: detached `sleep infinity` container,
        # then docker exec for each step. Avoids the entrypoint's debug env
        # dump that triggers when $@ is anything other than `sleep`.
        sections: list[str] = []
        rc = 0
        timed_out = False
        TEST_TIMEOUT = 1800
        start_time = time.time()
        deadline = start_time + TEST_TIMEOUT

        log.info("Running integration test for %s (arch=arm64, ref=%s)", repo_name, ref)
        try:
            run_cmd = [
                "docker", "run",
                "-d",
                "--name", container_name,
                "--init",
                "--privileged",
                "--network=host",
                "--env-file", env_file,
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", "/lib/modules:/lib/modules",
                "-v", f"{repo_dir}:{workspace}",
                "-w", workspace,
                "-e", "GIT_CONFIG_COUNT=1",
                "-e", "GIT_CONFIG_KEY_0=safe.directory",
                "-e", "GIT_CONFIG_VALUE_0=*",
                "shinojosa/dt-enablement:v1.2",
                "sleep", "infinity",
            ]
            proc = await asyncio.create_subprocess_exec(
                *run_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                sections.append(f"=== docker run failed (rc={proc.returncode}) ===\n{err.decode(errors='replace')}")
                rc = proc.returncode
                raise RuntimeError("container start failed")

            steps = [
                ("postCreateCommand", "./.devcontainer/post-create.sh"),
                ("postStartCommand",  "./.devcontainer/post-start.sh"),
                ("integrationTest",   "zsh .devcontainer/test/integration.sh"),
            ]
            for label, script in steps:
                sections.append(f"\n=== {label} ===\n")
                remaining = max(60, int(deadline - time.time()))
                exec_cmd = [
                    "docker", "exec",
                    "-w", workspace,
                    container_name,
                    "bash", "-lc", script,
                ]
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *exec_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=remaining)
                    sections.append(out.decode(errors="replace"))
                    if proc.returncode != 0:
                        rc = proc.returncode
                        sections.append(f"\n=== {label} exited with rc={rc} — stopping ===\n")
                        break
                except asyncio.TimeoutError:
                    proc.kill()
                    out, _ = await proc.communicate()
                    sections.append(out.decode(errors="replace"))
                    sections.append(f"\n=== {label} timed out ===\n")
                    rc = 124
                    timed_out = True
                    break
        except Exception as e:
            sections.append(f"\n=== executor error: {e} ===\n")
            if rc == 0:
                rc = 1
        finally:
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", container_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(kill_proc.wait(), timeout=30)
            except Exception:
                pass

        duration = int(time.time() - start_time)
        header = (
            f"=== JOB: {job_id} ===\n"
            f"=== REPO: {head_repo}@{ref} (base: {repo}) | ARCH: arm64 (master) ===\n"
            f"=== DURATION: {duration}s | EXIT: {rc} | TIMED_OUT: {timed_out} ===\n"
        )
        log_file.write_text(self._mask_secrets(header + "".join(sections)))

        await self._cleanup_clusters()
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
        """
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            f"DT_ENVIRONMENT={DT_ENVIRONMENT}\n"
            f"DT_OPERATOR_TOKEN={DT_OPERATOR_TOKEN}\n"
            f"DT_INGEST_TOKEN={DT_INGEST_TOKEN}\n"
            f"K3D_CLUSTER_NAME=master-{arch}\n"
            f"K3D_LB_HTTP_PORT=30080\n"
            f"K3D_LB_HTTPS_PORT=30443\n"
            f"K3D_API_PORT=6444\n"
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

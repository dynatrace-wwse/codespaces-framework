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
                await self.pool.rpush("jobs:completed", json.dumps(job))
                # Trim to keep last 500 completed jobs
                await self.pool.ltrim("jobs:completed", -500, -1)
                self.active_jobs.pop(job_id, None)
                log.info("Finished job: %s → %s", job_id, job["status"])

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
        """Run an integration test for a repo using devcontainer CI."""
        repo = job["repo"]
        repo_name = repo.split("/")[-1]
        repo_dir = REPOS_DIR / repo_name
        log_file = LOGS_DIR / f"{job['job_id']}.log"

        await self._ensure_repo(repo, repo_dir)

        # Run the integration test via devcontainer
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
            "-w", "/workspace",
            "shinojosa/dt-enablement:v1.2",
            "zsh", "-c", ".devcontainer/test/integration.sh",
        ]

        start_time = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=900  # 15 minute timeout
        )
        duration = int(time.time() - start_time)

        log_file.write_text(
            f"=== STDOUT ===\n{stdout.decode()}\n\n=== STDERR ===\n{stderr.decode()}"
        )

        return {
            "test": "integration",
            "exit_code": proc.returncode,
            "duration_seconds": duration,
            "passed": proc.returncode == 0,
            "log_file": str(log_file),
        }

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

    async def _ensure_repo(self, repo: str, repo_dir: Path):
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

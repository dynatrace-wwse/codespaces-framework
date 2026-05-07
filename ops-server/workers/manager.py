"""Worker manager — consumes jobs from Redis queues and dispatches to handlers."""

import asyncio
import json
import logging
import os
import signal
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
from telemetry.reporter import (
    report_test_result,
    report_build_started,
    report_build_deferred,
    extract_framework_version,
)

# 2h: longer than any expected build. If a worker crashes mid-job, the lock
# auto-expires and the next enqueue for the same triple proceeds.
LOCK_TTL_SECONDS = 7200


def _branch_of(job: dict) -> str:
    return job.get("ref") or job.get("head_branch") or "main"


def _triple_of(job: dict) -> str:
    return f"{job['repo']}:{_branch_of(job)}:{job.get('arch', 'arm64')}"

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
        # Job IDs whose owners have requested termination. The job's finally
        # block consults this set to mark status='terminated' instead of 'failed'.
        self._terminated_jobs: set[str] = set()
        self._shutdown = False

    async def start(self):
        """Connect to Redis and start consuming queues."""
        self.pool = redis.from_url(REDIS_URL, decode_responses=True)
        log.info(
            "Worker manager started (max_workers=%d, max_agents=%d)",
            MAX_PARALLEL_WORKERS,
            MAX_PARALLEL_AGENTS,
        )

        await self._register_master()
        await self._recover_orphaned_deferred()

        # Install SIGTERM/SIGINT handlers for graceful shutdown — kills our
        # spawned Sysbox containers so they don't survive as zombies.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self._handle_shutdown(s))
                )
            except NotImplementedError:
                pass  # Windows / non-loop-supporting platforms — best effort

        await asyncio.gather(
            self._consume_queue("agent", self.agent_semaphore),
            self._consume_queue("sync", self.agent_semaphore),
            # Local ARM worker consumes arch-specific queue
            self._consume_queue("test:arm64", self.test_semaphore),
            # Legacy queue for backwards compatibility
            self._consume_queue("test", self.test_semaphore),
            self._terminate_listener(),
            self._master_heartbeat_loop(),
        )

    async def _register_master(self):
        """Write the master worker record so it shows up in the Workers tab.

        AMD agents register themselves to ``worker:{WORKER_ID}``; the master
        does the same here so the dashboard's /api/workers lists both.
        """
        await self.pool.hset("worker:master-arm64", mapping={
            "arch": "arm64",
            "role": "master",
            "capacity": str(MAX_PARALLEL_WORKERS),
            "active_jobs": "0",
            "status": "ready",
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        })
        await self.pool.expire("worker:master-arm64", 60)
        log.info("Registered master worker as worker:master-arm64")

    async def _master_heartbeat_loop(self):
        """Refresh the master's worker record every 15s so it never expires."""
        while not self._shutdown:
            try:
                await self.pool.hset("worker:master-arm64", mapping={
                    "active_jobs": str(len(self.active_jobs)),
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "status": (
                        "ready" if len(self.active_jobs) < MAX_PARALLEL_WORKERS
                        else "busy"
                    ),
                })
                await self.pool.expire("worker:master-arm64", 60)
            except Exception as e:
                log.warning("master heartbeat failed: %s", e)
            await asyncio.sleep(15)

    async def _terminate_listener(self):
        """Subscribe to ``ops:terminate`` and kill matching active jobs.

        Both workers receive every published job_id; only the worker that
        owns the job (membership in ``self.active_jobs``) acts on it.
        """
        pubsub = self.pool.pubsub()
        await pubsub.subscribe("ops:terminate")
        log.info("Subscribed to ops:terminate channel")
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            job_id = msg.get("data") if isinstance(msg.get("data"), str) \
                else (msg.get("data", b"").decode() if isinstance(msg.get("data"), bytes) else "")
            if not job_id or job_id not in self.active_jobs:
                continue
            log.info("Termination request received for %s — killing container", job_id)
            await self._kill_job_container(job_id)

    async def _kill_job_container(self, job_id: str):
        """Mark a job as terminated and force-remove its Sysbox container.

        The job's running asyncio task hits its ``finally`` block once the
        subprocess chain unwinds; that block consults ``_terminated_jobs``
        and sets status='terminated'.
        """
        self._terminated_jobs.add(job_id)
        sb_name = f"sb-{job_id[-32:]}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", sb_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log.warning(
                    "docker rm -f %s rc=%s: %s",
                    sb_name, proc.returncode, err.decode(errors="replace")[:200],
                )
        except Exception as e:
            log.warning("Failed to kill %s: %s", sb_name, e)

    async def _handle_shutdown(self, sig):
        """Graceful shutdown: kill active job containers, then exit."""
        if self._shutdown:
            return
        self._shutdown = True
        log.info("Received %s — terminating %d active job(s)", sig.name, len(self.active_jobs))
        # Snapshot ids — _run_with_semaphore mutates active_jobs in finally
        ids = list(self.active_jobs.keys())
        await asyncio.gather(
            *(self._kill_job_container(jid) for jid in ids),
            return_exceptions=True,
        )
        # Give the running tasks a moment to flush their finally blocks
        for _ in range(30):
            if not self.active_jobs:
                break
            await asyncio.sleep(1)
        log.info("Shutdown cleanup complete (active=%d)", len(self.active_jobs))
        # Don't sys.exit — let asyncio.gather unwind naturally so the systemd
        # restart cycle stays clean.
        loop = asyncio.get_running_loop()
        loop.stop()

    async def _recover_orphaned_deferred(self):
        """Drain ``deferred:{triple}`` lists whose lock has expired.

        Worker crashes between acquiring ``running:lock:{triple}`` and draining
        its deferred list leave deferred jobs stuck. The lock auto-expires
        after LOCK_TTL_SECONDS; on next worker startup we drain anything
        whose lock is gone.
        """
        recovered = 0
        async for key in self.pool.scan_iter(match="deferred:*"):
            triple = key.split(":", 1)[1]
            lock_key = f"running:lock:{triple}"
            if await self.pool.exists(lock_key):
                continue  # an active job still holds the lock
            while True:
                item = await self.pool.lpop(key)
                if item is None:
                    break
                try:
                    d_job = json.loads(item)
                    await self.pool.rpush(
                        f"queue:test:{d_job.get('arch', 'arm64')}", item
                    )
                    recovered += 1
                except Exception as e:
                    log.warning(
                        "Could not re-queue orphaned deferred for %s: %s",
                        triple, e,
                    )
        if recovered:
            log.info("Recovered %d orphaned deferred jobs at startup", recovered)

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
                # Honor a pre-set job_id (e.g. from /api/sync/run); otherwise
                # generate one with ms precision + 6-char random suffix so 3+
                # jobs picked up within the same second can't collide.
                if not job.get("job_id"):
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
            # Pickup-time concurrency lock per (repo, branch, arch). If held by
            # another job, defer this one and return; the holder drains the
            # deferred list on completion. See ops-server/design/2026-05-07-triage-queue.md.
            running_key = None
            lock_key = None
            triple = None
            arch = job.get("arch", "arm64")
            if job.get("type") == "integration-test":
                triple = _triple_of(job)
                lock_key = f"running:lock:{triple}"
                acquired = await self.pool.set(
                    lock_key, job_id, nx=True, ex=LOCK_TTL_SECONDS
                )
                if not acquired:
                    log.info(
                        "Lock held for %s — deferring job %s", triple, job_id
                    )
                    holder = await self.pool.get(lock_key) or ""
                    await self.pool.rpush(f"deferred:{triple}", json.dumps(job))
                    try:
                        await report_build_deferred(
                            repo=job["repo"], arch=arch, branch=_branch_of(job),
                            triggered_by=job.get("trigger") or job.get("nightly_run_id", ""),
                            worker_id="master", job_id=job_id,
                            holder_job_id=holder,
                        )
                    except Exception as e:
                        log.warning("telemetry report_build_deferred failed: %s", e)
                    self.active_jobs.pop(job_id, None)
                    return
                running_key = f"job:running:{job_id}"
                await self.pool.hset(running_key, mapping={
                    "job_id": job_id,
                    "repo": job["repo"],
                    "branch": _branch_of(job),
                    "arch": arch,
                    "ref": _branch_of(job),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "worker_id": "master",
                })
                await self.pool.expire(running_key, LOCK_TTL_SECONDS)
                # Best-effort build.started telemetry
                try:
                    await report_build_started(
                        repo=job["repo"], arch=arch, branch=_branch_of(job),
                        triggered_by=job.get("trigger") or job.get("nightly_run_id", ""),
                        worker_id="master", job_id=job_id,
                        nightly_run_id=job.get("nightly_run_id", ""),
                    )
                except Exception as e:
                    log.warning("telemetry report_build_started failed: %s", e)

            try:
                result = await self._dispatch(job)
                job["result"] = result
                job["status"] = "completed"
            except Exception as e:
                log.error("Job %s failed: %s", job_id, e)
                job["result"] = {"error": str(e)}
                job["status"] = "failed"
            finally:
                # Terminated jobs override the failed/completed status.
                if job_id in self._terminated_jobs:
                    job["status"] = "terminated"
                    job["result"] = job.get("result") or {}
                    job["result"]["terminated"] = True
                    self._terminated_jobs.discard(job_id)
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                await self._publish_log(job)
                await self.pool.rpush("jobs:completed", json.dumps(job))
                await self.pool.ltrim("jobs:completed", -500, -1)
                if running_key:
                    await self.pool.delete(running_key)
                if lock_key:
                    await self.pool.delete(lock_key)
                    # Drain anything deferred for this triple back into the queue.
                    deferred_key = f"deferred:{triple}"
                    while True:
                        item = await self.pool.lpop(deferred_key)
                        if item is None:
                            break
                        try:
                            d_job = json.loads(item)
                            await self.pool.rpush(
                                f"queue:test:{d_job.get('arch', arch)}", item
                            )
                        except Exception as e:
                            log.warning(
                                "Could not re-queue deferred job for %s: %s",
                                triple, e,
                            )
                self.active_jobs.pop(job_id, None)
                log.info("Finished job: %s → %s", job_id, job["status"])
                # Telemetry: integration-test results emit test.result with the
                # full schema. Best-effort — never block on tracker outage.
                if job.get("type") == "integration-test":
                    try:
                        result = job.get("result", {}) or {}
                        repo_name = job["repo"].split("/")[-1]
                        work_dir = WORKDIR / job_id / repo_name
                        fw_ver = extract_framework_version(work_dir)
                        await report_test_result(
                            repo=job["repo"],
                            passed=bool(result.get("passed")),
                            duration_seconds=int(result.get("duration_seconds", 0)),
                            error_detail=str(result.get("error", ""))[:500],
                            nightly_run_id=job.get("nightly_run_id", ""),
                            framework_version=fw_ver,
                            arch=arch,
                            branch=_branch_of(job),
                            commit_sha=result.get("commit_sha", ""),
                            triggered_by=job.get("trigger") or
                                        ("nightly" if job.get("nightly_run_id", "").startswith("nightly-")
                                         else "manual"),
                            worker_id="master",
                            job_id=job_id,
                            status=job.get("status", "completed"),
                            failed_step=str(result.get("failed_step", "") or ""),
                        )
                    except Exception as e:
                        log.warning("telemetry report_test_result failed: %s", e)

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

        elif job_type == "sync-command":
            return await self._run_sync_command(job)

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
        failed_step = None
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

            # Stage 1: BATS unit tests (fast, structural). Fails the pipeline
            # before we spin up k3d if the framework code is broken. Skipped if
            # the repo has no .bats files.
            bats_script = (
                "if ls .devcontainer/test/unit/*.bats >/dev/null 2>&1; then "
                "  command -v bats >/dev/null 2>&1 || "
                "    { apt-get update -qq && apt-get install -y -qq bats; }; "
                "  cd .devcontainer && bats test/unit/; "
                "else echo '(no .devcontainer/test/unit/*.bats found — skipping Stage 1)'; fi"
            )
            steps = [
                ("bats",              bats_script),
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
                        failed_step = label
                        msg = f"\n=== {label} exited with rc={rc} — stopping ===\n"
                        sections.append(msg)
                        await self.pool.append(livelog_key, msg)
                        break
                except asyncio.TimeoutError:
                    proc.kill()
                    sections.append(f"\n=== {label} timed out ===\n")
                    rc = 124
                    failed_step = label
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
            "failed_step": failed_step or "",
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

    async def _run_sync_command(self, job: dict) -> dict:
        """Run a sync CLI subcommand from the dashboard's curated catalog.

        Streams output to ``job:livelog:{job_id}`` so the dashboard can tail
        in the same modal/fullscreen viewer used for integration tests.
        Persists the final log under ``job:log:{job_id}`` (7-day TTL) and
        captures duration/exit_code into the result.
        """
        job_id = job["job_id"]
        args = job.get("args") or []
        sync_dir = Path.home() / "enablement-framework" / "codespaces-framework"
        log_file = LOGS_DIR / f"{job_id}.log"
        livelog_key = f"job:livelog:{job_id}"
        await self.pool.set(livelog_key, "", ex=3600)
        header = (
            f"=== sync.cli {' '.join(args)} ===\n"
            f"requested_by: {job.get('requested_by', '?')}\n"
            f"started: {datetime.now(timezone.utc).isoformat()}\n\n"
        )
        await self.pool.append(livelog_key, header)

        cmd = ["python3", "-u", "-m", "sync.cli", *args]
        start = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(sync_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONPATH": str(sync_dir), "PYTHONUNBUFFERED": "1"},
        )

        out_buf = []
        try:
            with open(log_file, "w") as logf:
                logf.write(header)
                logf.flush()
                while True:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=600,
                    )
                    if not line:
                        break
                    text = line.decode(errors="replace")
                    out_buf.append(text)
                    try:
                        await self.pool.append(livelog_key, text)
                    except Exception:
                        pass
                    logf.write(text)
                    logf.flush()
        except asyncio.TimeoutError:
            proc.kill()
            await self.pool.append(livelog_key, "\n[TIMEOUT — killed after 10m]\n")
        rc = await proc.wait()
        duration = int(time.time() - start)
        footer = f"\n=== exit_code={rc} duration={duration}s ===\n"
        await self.pool.append(livelog_key, footer)

        # Persist full log to job:log:{job_id} for the History view
        try:
            content = "".join(out_buf) + footer
            max_bytes = 256 * 1024
            if len(content.encode()) > max_bytes:
                content = "... (truncated) ...\n\n" + content[-max_bytes:]
            await self.pool.set(f"job:log:{job_id}", content, ex=86400 * 7)
        except Exception as e:
            log.warning("Could not publish sync log for %s: %s", job_id, e)

        return {
            "command": " ".join(args),
            "exit_code": rc,
            "duration_seconds": duration,
            "passed": rc == 0,
            "log_file": str(log_file),
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

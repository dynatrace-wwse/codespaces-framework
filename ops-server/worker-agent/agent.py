"""Remote worker agent — pulls jobs from master Redis and executes integration tests."""

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone

import redis.asyncio as redis

from .config import (
    WORKER_ID,
    WORKER_ARCH,
    WORKER_CAPACITY,
    WORKER_HOST,
    WORKER_SSH_HOST,
    MASTER_REDIS_URL,
    MASTER_REDIS_PASSWORD,
    HEARTBEAT_INTERVAL,
    REGISTRATION_TTL,
    APP_PROXY_PORT_START,
    APP_PROXY_PORT_COUNT,
)
from .executor import execute_integration_test, execute_daemon

# Keep in sync with workers/manager.py — see ops-server/design/2026-05-07-triage-queue.md
LOCK_TTL_SECONDS = 7200


def _branch_of(job: dict) -> str:
    return job.get("ref") or job.get("head_branch") or "main"


def _triple_of(job: dict, default_arch: str) -> str:
    return f"{job['repo']}:{_branch_of(job)}:{job.get('arch', default_arch)}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ops-worker-agent")


class WorkerAgent:
    """Lightweight worker that connects to master Redis, pulls and executes test jobs."""

    def __init__(self):
        self.pool: redis.Redis | None = None
        self.active_jobs: dict[str, dict] = {}
        self.semaphore = asyncio.Semaphore(WORKER_CAPACITY)
        self._running = True
        # Job IDs whose owners requested termination (via ops:terminate pub/sub
        # or local SIGTERM). The job's finally block sets status='terminated'.
        self._terminated_jobs: set[str] = set()
        self._shutdown = False

    async def start(self):
        """Connect to master Redis, register, and start consuming."""
        kwargs = {"decode_responses": True}
        if MASTER_REDIS_PASSWORD:
            kwargs["password"] = MASTER_REDIS_PASSWORD

        self.pool = redis.from_url(MASTER_REDIS_URL, **kwargs)

        # Verify connection
        await self.pool.ping()
        log.info(
            "Connected to master Redis. Worker: %s (arch=%s, capacity=%d)",
            WORKER_ID, WORKER_ARCH, WORKER_CAPACITY,
        )

        await self._register()

        # Install SIGTERM/SIGINT handlers for graceful shutdown.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self._handle_shutdown(s))
                )
            except NotImplementedError:
                pass

        # Run consumer, heartbeat, and termination listener concurrently
        await asyncio.gather(
            self._consume_queue(),
            self._heartbeat_loop(),
            self._terminate_listener(),
        )

    async def _terminate_listener(self):
        """Subscribe to ``ops:terminate`` and kill matching active jobs."""
        pubsub = self.pool.pubsub()
        await pubsub.subscribe("ops:terminate")
        log.info("Subscribed to ops:terminate channel")
        async for msg in pubsub.listen():
            if msg.get("type") != "message":
                continue
            data = msg.get("data")
            job_id = data if isinstance(data, str) else (
                data.decode() if isinstance(data, bytes) else ""
            )
            if not job_id or job_id not in self.active_jobs:
                continue
            log.info("Termination request received for %s — killing container", job_id)
            await self._kill_job_container(job_id)

    async def _kill_job_container(self, job_id: str):
        """Mark a job as terminated and force-remove its Sysbox container."""
        self._terminated_jobs.add(job_id)
        sb_name = f"sb-{job_id[-32:]}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-fv", sb_name,
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
        self._running = False
        log.info(
            "Received %s — terminating %d active job(s)", sig.name, len(self.active_jobs)
        )
        ids = list(self.active_jobs.keys())
        await asyncio.gather(
            *(self._kill_job_container(jid) for jid in ids),
            return_exceptions=True,
        )
        for _ in range(30):
            if not self.active_jobs:
                break
            await asyncio.sleep(1)
        log.info("Shutdown cleanup complete (active=%d)", len(self.active_jobs))
        loop = asyncio.get_running_loop()
        loop.stop()

    async def _register(self):
        """Register this worker with the master."""
        worker_key = f"worker:{WORKER_ID}"
        fields = {
            "arch": WORKER_ARCH,
            "capacity": str(WORKER_CAPACITY),
            "active_jobs": "0",
            "status": "ready",
            "host": WORKER_HOST,
            "ssh_host": WORKER_SSH_HOST or WORKER_HOST,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }
        await self.pool.hset(worker_key, mapping=fields)
        await self.pool.expire(worker_key, REGISTRATION_TTL)
        log.info("Registered as %s", worker_key)

        # (Re)initialise the app proxy port pool. Always recreate on startup so
        # stale entries from a previous run don't accumulate; graceful shutdown
        # kills all Sysbox containers first so no ports are in use at this point.
        port_pool_key = f"worker:{WORKER_ID}:app_ports_free"
        await self.pool.delete(port_pool_key)
        ports = list(range(APP_PROXY_PORT_START, APP_PROXY_PORT_START + APP_PROXY_PORT_COUNT))
        await self.pool.rpush(port_pool_key, *[str(p) for p in ports])
        log.info(
            "App proxy port pool initialised: %s (ports %d–%d)",
            port_pool_key, APP_PROXY_PORT_START, APP_PROXY_PORT_START + APP_PROXY_PORT_COUNT - 1,
        )

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to master."""
        worker_key = f"worker:{WORKER_ID}"
        while self._running:
            try:
                await self.pool.hset(worker_key, mapping={
                    "active_jobs": str(len(self.active_jobs)),
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "status": "ready" if len(self.active_jobs) < WORKER_CAPACITY else "busy",
                })
                await self.pool.expire(worker_key, REGISTRATION_TTL)
            except redis.ConnectionError:
                log.warning("Heartbeat failed — Redis connection lost")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _consume_queue(self):
        """Pull jobs from the arch-specific test queue.

        Only dequeues when a slot is available so jobs stay visible in Redis
        (and the dashboard queue counter stays accurate) until the worker is
        actually ready to run them.
        """
        queue_key = f"queue:test:{WORKER_ARCH}"
        log.info("Consuming from %s", queue_key)

        while self._running:
            try:
                # Back-pressure: leave the job in Redis until we have capacity.
                if len(self.active_jobs) >= WORKER_CAPACITY:
                    await asyncio.sleep(1)
                    continue

                result = await self.pool.blpop(queue_key, timeout=5)
                if result is None:
                    continue

                _, job_json = result
                job = json.loads(job_json)
                import uuid
                job_id = (
                    f"{WORKER_ID}-{job['repo'].split('/')[-1]}"
                    f"-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
                )
                job["job_id"] = job_id
                job["worker_id"] = WORKER_ID
                job["worker_arch"] = WORKER_ARCH

                asyncio.create_task(self._run_job(job))

            except redis.ConnectionError:
                log.error("Redis connection lost, retrying in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log.error("Consumer error: %s", e)
                await asyncio.sleep(1)

    async def _run_job(self, job: dict):
        """Execute a single job within the semaphore."""
        async with self.semaphore:
            job_id = job["job_id"]
            self.active_jobs[job_id] = job
            log.info("Starting: %s (%s)", job_id, job["repo"])

            # Pickup-time concurrency lock per (repo, branch, arch). If held,
            # defer this job; the holder drains the deferred list on completion.
            arch = job.get("arch", WORKER_ARCH)
            triple = _triple_of(job, WORKER_ARCH)
            lock_key = f"running:lock:{triple}"
            acquired = await self.pool.set(
                lock_key, job_id, nx=True, ex=LOCK_TTL_SECONDS
            )
            if not acquired:
                log.info("Lock held for %s — deferring job %s", triple, job_id)
                await self.pool.rpush(f"deferred:{triple}", json.dumps(job))
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
                "worker_id": WORKER_ID,
            })
            await self.pool.expire(running_key, LOCK_TTL_SECONDS)

            try:
                if job.get("type") == "daemon":
                    result = await execute_daemon(job, redis_pool=self.pool)
                else:
                    result = await execute_integration_test(job, redis_pool=self.pool)
                job["result"] = result
                job["status"] = "completed"
            except Exception as e:
                log.error("Job %s failed: %s", job_id, e)
                job["result"] = {"error": str(e)}
                job["status"] = "failed"
            finally:
                if job_id in self._terminated_jobs:
                    job["status"] = "terminated"
                    job["result"] = job.get("result") or {}
                    job["result"]["terminated"] = True
                    self._terminated_jobs.discard(job_id)
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                await self._publish_log(job)
                await self.pool.rpush("jobs:completed", json.dumps(job))
                await self.pool.ltrim("jobs:completed", -500, -1)
                await self.pool.delete(running_key)
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
                log.info("Finished: %s → %s", job_id, job["status"])

    async def _publish_log(self, job: dict):
        """Upload the per-job log to master Redis so the dashboard can serve it.

        The log file lives on the worker's local filesystem; we read it,
        truncate to the last 256KB, and store under ``job:log:<id>`` with a
        7-day TTL.
        """
        result = job.get("result", {}) or {}
        log_path = result.get("log_file")
        if not log_path:
            return
        try:
            content = open(log_path, "r", errors="replace").read()
        except OSError as e:
            content = f"(log unavailable: {e})"
        # Cap at 256KB — integration tests can be huge
        max_bytes = 256 * 1024
        if len(content.encode()) > max_bytes:
            content = "... (truncated; see /home/ops/logs/{}.log on worker) ...\n\n".format(
                job["job_id"]
            ) + content[-max_bytes:]
        try:
            await self.pool.set(f"job:log:{job['job_id']}", content, ex=86400 * 7)
        except Exception as e:
            log.warning("Could not publish log for %s: %s", job["job_id"], e)


async def main():
    agent = WorkerAgent()
    await agent.start()


if __name__ == "__main__":
    asyncio.run(main())

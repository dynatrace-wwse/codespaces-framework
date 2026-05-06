"""Remote worker agent — pulls jobs from master Redis and executes integration tests."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import redis.asyncio as redis

from .config import (
    WORKER_ID,
    WORKER_ARCH,
    WORKER_CAPACITY,
    MASTER_REDIS_URL,
    MASTER_REDIS_PASSWORD,
    HEARTBEAT_INTERVAL,
    REGISTRATION_TTL,
)
from .executor import execute_integration_test

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

        # Run consumer and heartbeat concurrently
        await asyncio.gather(
            self._consume_queue(),
            self._heartbeat_loop(),
        )

    async def _register(self):
        """Register this worker with the master."""
        worker_key = f"worker:{WORKER_ID}"
        await self.pool.hset(worker_key, mapping={
            "arch": WORKER_ARCH,
            "capacity": str(WORKER_CAPACITY),
            "active_jobs": "0",
            "status": "ready",
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        })
        await self.pool.expire(worker_key, REGISTRATION_TTL)
        log.info("Registered as %s", worker_key)

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
        """Pull jobs from the arch-specific test queue."""
        queue_key = f"queue:test:{WORKER_ARCH}"
        log.info("Consuming from %s", queue_key)

        while self._running:
            try:
                result = await self.pool.blpop(queue_key, timeout=5)
                if result is None:
                    continue

                _, job_json = result
                job = json.loads(job_json)
                job_id = f"{WORKER_ID}-{job['repo'].split('/')[-1]}-{int(time.time())}"
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

            try:
                result = await execute_integration_test(job)
                job["result"] = result
                job["status"] = "completed"
            except Exception as e:
                log.error("Job %s failed: %s", job_id, e)
                job["result"] = {"error": str(e)}
                job["status"] = "failed"
            finally:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                await self.pool.rpush("jobs:completed", json.dumps(job))
                await self.pool.ltrim("jobs:completed", -500, -1)
                self.active_jobs.pop(job_id, None)
                log.info("Finished: %s → %s", job_id, job["status"])


async def main():
    agent = WorkerAgent()
    await agent.start()


if __name__ == "__main__":
    asyncio.run(main())

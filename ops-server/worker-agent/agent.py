"""Remote worker agent — pulls jobs from master Redis and executes integration tests.

## Warm Sysbox Pool

At startup the agent pre-warms ``WORKER_CAPACITY`` Sysbox containers in
parallel. Each slot has its inner dockerd running and TEST_IMAGE already
loaded, eliminating the 60-120s startup overhead that previously occurred
at the start of every integration test or Arena training session.

Job flow with warm pool:
  1. Slot acquired from queue (~0s — blocks only if all capacity in use)
  2. git clone into slot workspace (~5-10s)
  3. docker exec sb → docker run dt (~3s)
  4. wait for vscode/docker group (~5s)
  5. postCreate + postStart + test

Between jobs the slot is cleaned (rm dt, volume/network prune inside the
Sysbox) and returned to the queue. The outer Sysbox and its cached image
stay alive. If a slot becomes unhealthy it is automatically re-initialized.
"""

import asyncio
import json
import logging
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

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
    K3D_LB_HTTP_PORT,
    SLOT_BASE_DIR,
    TEST_IMAGE,
)
from .executor import (
    execute_integration_test,
    execute_daemon,
    SysboxSlot,
    _wait_for_inner_docker,
    _pipe_image_to_sysbox,
)

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


class SysboxPool:
    """Pool of pre-warmed Sysbox containers, one per worker capacity slot.

    Each slot keeps a ``docker:25-dind`` container running with its inner
    dockerd ready and TEST_IMAGE pre-loaded. Jobs acquire a slot, use it,
    then release it. Between uses the inner ``dt`` container and its volumes/
    networks are pruned; the outer Sysbox stays alive.

    Port assignment: slot ``i`` always publishes ``APP_PROXY_PORT_START + i``
    on the host. This is fixed at Sysbox run time, so no dynamic allocation
    is needed.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        short_id = WORKER_ID[-6:]
        self.slots: list[SysboxSlot] = [
            SysboxSlot(
                index=i,
                sb_name=f"sb-slot-{short_id}-{i}",
                workspace=SLOT_BASE_DIR / str(i) / "workspace",
                port=APP_PROXY_PORT_START + i,
            )
            for i in range(capacity)
        ]
        self._queue: asyncio.Queue[SysboxSlot] = asyncio.Queue()

    async def init(self) -> int:
        """Start all slots concurrently. Returns the number of ready slots."""
        results = await asyncio.gather(
            *(self._init_slot(s) for s in self.slots),
            return_exceptions=True,
        )
        ready = sum(1 for r in results if r is True)
        log.info("SysboxPool: %d/%d slots ready", ready, self._capacity)
        return ready

    async def _init_slot(self, slot: SysboxSlot) -> bool:
        """(Re)initialize one slot: start outer Sysbox, wait for inner dockerd, load image."""
        # Clean up any orphan container from a previous agent run.
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-fv", slot.sb_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except Exception:
            pass

        # Prepare workspace directory (persistent across jobs; cleared between uses).
        try:
            if slot.workspace.exists():
                shutil.rmtree(slot.workspace, ignore_errors=True)
            slot.workspace.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            log.error("Slot %d: workspace mkdir failed: %s", slot.index, exc)
            return False

        # Start outer Sysbox with the workspace directory mounted at /workspaces.
        # The port is fixed per slot so no dynamic allocation is needed.
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "-d",
            "--runtime=sysbox-runc",
            "--name", slot.sb_name,
            "-p", f"{slot.port}:{K3D_LB_HTTP_PORT}",
            "-v", f"{slot.workspace}:/workspaces",
            "docker:25-dind",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            log.error(
                "Slot %d: sysbox start failed: %s",
                slot.index, err.decode(errors="replace")[:300],
            )
            return False

        # Wait for inner dockerd (Sysbox takes ~20-40s to initialize its inner namespaces).
        try:
            await _wait_for_inner_docker(slot.sb_name, timeout_s=90)
        except RuntimeError as exc:
            log.error("Slot %d: %s", slot.index, exc)
            await asyncio.create_subprocess_exec(
                "docker", "rm", "-fv", slot.sb_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            return False

        # Ensure TEST_IMAGE is present on the outer daemon, then load it into the slot.
        outer_inspect = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", TEST_IMAGE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if await outer_inspect.wait() != 0:
            log.info("Slot %d: pulling %s to outer daemon...", slot.index, TEST_IMAGE)
            pull = await asyncio.create_subprocess_exec(
                "docker", "pull", TEST_IMAGE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await pull.wait()

        try:
            await _pipe_image_to_sysbox(slot.sb_name)
        except Exception as exc:
            log.error("Slot %d: image load failed: %s", slot.index, exc)
            await asyncio.create_subprocess_exec(
                "docker", "rm", "-fv", slot.sb_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            return False

        # Record current image digest for staleness detection on release.
        slot.image_digest = await _image_digest(TEST_IMAGE)

        await self._queue.put(slot)
        log.info(
            "Slot %d (%s) ready — port %d, image %.12s",
            slot.index, slot.sb_name, slot.port, slot.image_digest,
        )
        return True

    async def acquire(self) -> SysboxSlot:
        """Block until a pre-warmed slot is available and return it."""
        return await self._queue.get()

    async def release(self, slot: SysboxSlot, healthy: bool = True) -> None:
        """Clean slot state and return it to the pool.

        If ``healthy`` is False (executor raised an exception), the Sysbox may
        be in an unknown state — the slot is re-initialized from scratch.
        Otherwise only the inner docker state is wiped; the outer Sysbox and
        its cached TEST_IMAGE remain.
        """
        if not healthy:
            log.warning("Slot %d unhealthy — re-initializing", slot.index)
            ok = await self._init_slot(slot)
            if not ok:
                log.error(
                    "Slot %d re-init failed — slot removed from pool until next agent restart",
                    slot.index,
                )
            # _init_slot puts slot back into the queue on success.
            return

        # Wipe inner docker state without touching the outer Sysbox or its image cache.
        for cmd in [
            ["docker", "exec", slot.sb_name, "docker", "rm", "-fv", "dt"],
            # Remove all remaining containers (k3d nodes, etc.) left from the previous job.
            # Must run after dt removal so the rm -fv dt succeeds by name first.
            ["docker", "exec", slot.sb_name, "sh", "-c",
             "docker ps -aq | xargs -r docker rm -f 2>/dev/null; true"],
            ["docker", "exec", slot.sb_name, "docker", "volume", "prune", "-f"],
            ["docker", "exec", slot.sb_name, "docker", "network", "prune", "-f"],
        ]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=30)
            except Exception:
                pass

        # Reload TEST_IMAGE into the slot if it was updated on the outer daemon.
        current_digest = await _image_digest(TEST_IMAGE)
        if current_digest and current_digest != slot.image_digest:
            log.info("Slot %d: TEST_IMAGE updated — reloading into inner docker", slot.index)
            try:
                await _pipe_image_to_sysbox(slot.sb_name)
                slot.image_digest = current_digest
            except Exception as exc:
                log.warning("Slot %d: image reload failed: %s — slot marked unhealthy", slot.index, exc)
                await self.release(slot, healthy=False)
                return

        await self._queue.put(slot)

    async def shutdown(self) -> None:
        """Kill all slot containers (called on agent shutdown)."""
        await asyncio.gather(
            *(self._kill_slot(s) for s in self.slots),
            return_exceptions=True,
        )
        log.info("SysboxPool: all slot containers removed")

    async def _kill_slot(self, slot: SysboxSlot) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-fv", slot.sb_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except Exception:
            pass


async def _image_digest(image: str) -> str:
    """Return the Id digest of a local Docker image, or empty string on error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", "--format={{index .Id}}", image,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return out.decode().strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


class WorkerAgent:
    """Lightweight worker that connects to master Redis, pulls and executes test jobs."""

    def __init__(self):
        self.pool: redis.Redis | None = None
        self.active_jobs: dict[str, dict] = {}
        self.semaphore = asyncio.Semaphore(WORKER_CAPACITY)
        self._running = True
        self._terminated_jobs: set[str] = set()
        self._shutdown = False
        self.sysbox_pool = SysboxPool(WORKER_CAPACITY)
        # Maps job_id → SysboxSlot so _kill_job_container can find the right container.
        self._job_slots: dict[str, SysboxSlot] = {}

    async def start(self):
        """Connect to master Redis, register, initialize pool, and start consuming."""
        kwargs = {"decode_responses": True}
        if MASTER_REDIS_PASSWORD:
            kwargs["password"] = MASTER_REDIS_PASSWORD

        self.pool = redis.from_url(MASTER_REDIS_URL, **kwargs)
        await self.pool.ping()
        log.info(
            "Connected to master Redis. Worker: %s (arch=%s, capacity=%d)",
            WORKER_ID, WORKER_ARCH, WORKER_CAPACITY,
        )

        await self._register()

        log.info("Initializing Sysbox pool (%d slots)...", WORKER_CAPACITY)
        await self.sysbox_pool.init()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self._handle_shutdown(s))
                )
            except NotImplementedError:
                pass

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
        """Mark a job as terminated and force-remove its Sysbox container.

        For slotted jobs, killing the outer Sysbox makes the executor's
        ``docker wait`` return, triggering the finally block. The pool's
        release() will re-initialize the slot since the container is gone.
        """
        self._terminated_jobs.add(job_id)
        slot = self._job_slots.get(job_id)
        sb_name = slot.sb_name if slot else f"sb-{job_id[-32:]}"
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
        """Graceful shutdown: kill active job containers, shut down pool, then exit."""
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
        await self.sysbox_pool.shutdown()
        log.info("Shutdown cleanup complete (active=%d)", len(self.active_jobs))
        asyncio.get_running_loop().stop()

    async def _register(self):
        """Register this worker with the master and prime the psutil CPU baseline."""
        # Prime psutil so the first heartbeat reports an accurate cpu_pct.
        # cpu_percent(interval=None) returns 0.0 on first call if never primed.
        try:
            import psutil as _ps
            _ps.cpu_percent(interval=0.1)
        except ImportError:
            pass

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

        # Keep the legacy Redis port pool for backward compatibility (used by the
        # non-slot code path and the co-located ARM worker manager.py).
        port_pool_key = f"worker:{WORKER_ID}:app_ports_free"
        await self.pool.delete(port_pool_key)
        ports = list(range(APP_PROXY_PORT_START, APP_PROXY_PORT_START + APP_PROXY_PORT_COUNT))
        await self.pool.rpush(port_pool_key, *[str(p) for p in ports])
        await self.pool.expire(port_pool_key, REGISTRATION_TTL)
        log.info(
            "App proxy port pool initialised: %s (ports %d–%d)",
            port_pool_key, APP_PROXY_PORT_START, APP_PROXY_PORT_START + APP_PROXY_PORT_COUNT - 1,
        )

    @staticmethod
    def _collect_metrics() -> dict:
        """Collect host CPU, memory, disk, and container metrics."""
        metrics = {}
        try:
            import psutil as _ps
            metrics["cpu_pct"] = str(round(_ps.cpu_percent(interval=None), 1))
            vm = _ps.virtual_memory()
            metrics["mem_pct"] = str(round(vm.percent, 1))
            metrics["mem_used_gb"] = str(round(vm.used / 1024 ** 3, 2))
            metrics["mem_total_gb"] = str(round(vm.total / 1024 ** 3, 2))
        except ImportError:
            # psutil not yet installed — read procfs directly
            try:
                with open("/proc/loadavg") as f:
                    load1 = float(f.read().split()[0])
                cpu_count = len([l for l in open("/proc/cpuinfo") if l.startswith("processor")])
                metrics["cpu_pct"] = str(round(min(load1 / max(cpu_count, 1) * 100, 100), 1))
            except Exception:
                pass
            try:
                info = {}
                with open("/proc/meminfo") as f:
                    for line in f:
                        k, v = line.split(":")
                        info[k.strip()] = int(v.split()[0])
                total = info.get("MemTotal", 0)
                avail = info.get("MemAvailable", 0)
                if total:
                    metrics["mem_pct"] = str(round((total - avail) / total * 100, 1))
                    metrics["mem_total_gb"] = str(round(total / 1024 ** 2, 2))
                    metrics["mem_used_gb"] = str(round((total - avail) / 1024 ** 2, 2))
            except Exception:
                pass
        try:
            du = shutil.disk_usage("/")
            metrics["disk_pct"] = str(round(du.used / du.total * 100, 1))
        except Exception:
            pass
        try:
            out = subprocess.check_output(
                ["docker", "ps", "-q"], timeout=3, stderr=subprocess.DEVNULL
            )
            metrics["containers_running"] = str(len([l for l in out.splitlines() if l]))
        except Exception:
            pass
        return metrics

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to master."""
        worker_key = f"worker:{WORKER_ID}"
        port_pool_key = f"worker:{WORKER_ID}:app_ports_free"
        # Static fields written every heartbeat so they survive key expiry + recreation.
        static_fields = {
            "arch": WORKER_ARCH,
            "capacity": str(WORKER_CAPACITY),
            "host": WORKER_HOST,
            "ssh_host": WORKER_SSH_HOST or WORKER_HOST,
        }
        while self._running:
            try:
                metrics = await asyncio.get_event_loop().run_in_executor(None, self._collect_metrics)
                await self.pool.hset(worker_key, mapping={
                    **static_fields,
                    "active_jobs": str(len(self.active_jobs)),
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "status": "ready" if len(self.active_jobs) < WORKER_CAPACITY else "busy",
                    **metrics,
                })
                await self.pool.expire(worker_key, REGISTRATION_TTL)
                await self.pool.expire(port_pool_key, REGISTRATION_TTL)
            except redis.ConnectionError:
                log.warning("Heartbeat failed — Redis connection lost")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _consume_queue(self):
        """Pull jobs from the arch-specific test queue with semaphore back-pressure."""
        queue_key = f"queue:test:{WORKER_ARCH}"
        log.info("Consuming from %s", queue_key)

        while self._running:
            try:
                if self.semaphore.locked():
                    await asyncio.sleep(1)
                    continue

                result = await self.pool.blpop(queue_key, timeout=5)
                if result is None:
                    continue

                _, job_json = result
                job = json.loads(job_json)
                import uuid
                if not job.get("job_id"):
                    job["job_id"] = (
                        f"{WORKER_ID}-{job['repo'].split('/')[-1]}"
                        f"-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
                    )
                job["worker_id"] = WORKER_ID
                job["worker_arch"] = WORKER_ARCH

                asyncio.create_task(self._run_job(job))
                await asyncio.sleep(0)

            except redis.ConnectionError:
                log.error("Redis connection lost, retrying in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                log.error("Consumer error: %s", e)
                await asyncio.sleep(1)

    async def _run_job(self, job: dict):
        """Execute a single job: acquire a warm Sysbox slot, run, then release."""
        async with self.semaphore:
            job_id = job["job_id"]
            self.active_jobs[job_id] = job
            log.info("Starting: %s (%s)", job_id, job["repo"])

            arch = job.get("arch", WORKER_ARCH)
            triple = _triple_of(job, WORKER_ARCH)
            lock_key = None

            if job.get("type") != "daemon":
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
                "job_id":     job_id,
                "repo":       job["repo"],
                "branch":     _branch_of(job),
                "arch":       arch,
                "ref":        _branch_of(job),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "worker_id":  WORKER_ID,
                "type":       job.get("type", "integration-test"),
            })
            ttl = 86400 if job.get("type") == "daemon" else LOCK_TTL_SECONDS
            await self.pool.expire(running_key, ttl)

            # Acquire a pre-warmed slot. Blocks if all capacity is in use.
            slot = await self.sysbox_pool.acquire()
            self._job_slots[job_id] = slot
            # Persist the actual Sysbox container name so the dashboard (app.py)
            # can resolve it without hardcoding the sb-{job_id} naming convention.
            await self.pool.hset(running_key, "sb_name", slot.sb_name)
            healthy = True

            try:
                if job.get("type") == "daemon":
                    result = await execute_daemon(job, redis_pool=self.pool, slot=slot)
                else:
                    result = await execute_integration_test(job, redis_pool=self.pool, slot=slot)
                job["result"] = result
                job["status"] = "completed"
            except Exception as e:
                log.error("Job %s failed: %s", job_id, e)
                job["result"] = {"error": str(e)}
                job["status"] = "failed"
                healthy = False
            finally:
                self._job_slots.pop(job_id, None)
                # Terminated daemon jobs have their Sysbox force-removed externally.
                # docker wait returns normally (no exception), so healthy stays True.
                # We must release as unhealthy so _init_slot gets a fresh container;
                # otherwise the dead slot re-enters the pool and the next git clone fails.
                was_terminated = job_id in self._terminated_jobs
                await self.sysbox_pool.release(slot, healthy=healthy and not was_terminated)

                if was_terminated:
                    job["status"] = "terminated"
                    job["result"] = job.get("result") or {}
                    job["result"]["terminated"] = True
                    self._terminated_jobs.discard(job_id)
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                await self._publish_log(job)
                await self.pool.rpush("jobs:completed", json.dumps(job))
                await self.pool.ltrim("jobs:completed", -500, -1)
                await self.pool.delete(running_key)
                if lock_key:
                    await self.pool.delete(lock_key)
                deferred_key = f"deferred:{triple}"
                while True:
                    item = await self.pool.lpop(deferred_key)
                    if item is None:
                        break
                    try:
                        d_job = json.loads(item)
                        await self.pool.rpush(f"queue:test:{d_job.get('arch', arch)}", item)
                    except Exception as e:
                        log.warning("Could not re-queue deferred job for %s: %s", triple, e)
                self.active_jobs.pop(job_id, None)
                log.info("Finished: %s → %s", job_id, job["status"])

    async def _publish_log(self, job: dict):
        """Upload the per-job log to master Redis so the dashboard can serve it."""
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

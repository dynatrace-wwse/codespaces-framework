"""Container reaper — one poller instead of N blocked ``docker wait`` processes.

THE BUG THIS REPLACES
---------------------
A daemon job used to hold the slot open with::

    docker wait sb-slot-amd001-18      # blocks until the container exits

and termination worked by force-removing that container, on the assumption that
doing so makes ``docker wait`` return. Measured on 2026-08-13, that assumption
fails under a mass teardown::

    docker rm -fv sb-slot-amd001-18 rc=1: could not kill container:
    tried to kill container, but did not receive an exit event

The kill fails, so nothing returns. The job coroutine blocks forever, its
``finally`` never runs, ``active_jobs`` never shrinks, and the heartbeat
publishes ``slots_free = 0`` indefinitely -- against thirty *healthy* warm
slots. Nothing alerts, ``status`` still reads ``ready``, and only an agent
restart clears it. Seventeen such waits were still alive twenty minutes after
the terminate.

THREE PROPERTIES THAT FIX IT
----------------------------
1. **Keyed on container ID, not name.** Slot names (``sb-slot-{worker}-{i}``)
   are stable and *recycled*, so a stale wait on a name can end up watching a
   later session's container and report the wrong session's exit. IDs are
   unique for all time.

2. **One poller, not one process per job.** A single ``docker ps -q`` per tick
   regardless of session count -- cheaper than today at any scale above one --
   and it demotes Docker's exit event from load-bearing to advisory. We no
   longer need Docker to *tell* us a container died; we look.

3. **The waiter never depends on the killer succeeding.** Terminate records
   *intent*. The reaper retries the kill with escalation and, after a bounded
   number of attempts, **resolves the waiter anyway** and reports the slot as
   abandoned so the pool rebuilds it. The job always exits and the slot is
   always reclaimed. Worst case costs one slot a rebuild, instead of stranding
   an entire worker.

Docker is injected as two callables so all of this is testable without a
daemon, and so a future runtime that is not Docker needs no changes here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable

log = logging.getLogger(__name__)

# How often to poll. 3 s is well inside the time a learner would notice, and a
# single cheap subprocess at this rate is negligible next to thirty blocked
# processes.
REAP_INTERVAL_S = 3.0
# Kill attempts on a container that is meant to be terminated but is still
# running, before giving up and freeing the job anyway.
MAX_KILL_ATTEMPTS = 3

# Why a watcher was resolved.
EXITED = "exited"          # container genuinely gone -- the normal path
ABANDONED = "abandoned"    # kill kept failing; freed anyway, slot needs rebuild
STOPPED = "stopped"        # reaper shut down while this was still watched


class ContainerReaper:
    """Watches container IDs and resolves a future when each stops running."""

    def __init__(
        self,
        list_running: Callable[[], Awaitable[Iterable[str]]],
        kill: Callable[[str, int], Awaitable[bool]],
        interval: float = REAP_INTERVAL_S,
        max_kill_attempts: int = MAX_KILL_ATTEMPTS,
    ):
        self._list_running = list_running
        self._kill = kill
        self._interval = interval
        self._max_kill_attempts = max_kill_attempts
        self._watched: dict[str, asyncio.Future] = {}
        # Container IDs a terminate has been requested for. Separate from
        # _watched because intent can arrive before or after the watch does,
        # and both orders must work.
        self._terminating: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.get_running_loop().create_task(self._loop())
            log.info("Container reaper started (poll %.1fs)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        # Never leave a coroutine blocked on a future nobody will resolve --
        # that is the very failure this class exists to remove.
        for cid, fut in list(self._watched.items()):
            if not fut.done():
                fut.set_result(STOPPED)
        self._watched.clear()

    # ── public API ──────────────────────────────────────────────────────────

    def watch(self, container_id: str) -> asyncio.Future:
        """Future resolved with a reason string once ``container_id`` stops."""
        if not container_id:
            raise ValueError("container_id is required — the whole point is not "
                             "to key on a recycled slot name")
        fut = self._watched.get(container_id)
        if fut is None or fut.done():
            fut = asyncio.get_running_loop().create_future()
            self._watched[container_id] = fut
        return fut

    def request_terminate(self, container_id: str) -> None:
        """Record the *intent* to kill. The reaper does the killing and, more
        importantly, guarantees the waiter is freed whether or not it works."""
        if container_id:
            self._terminating.add(container_id)

    def forget(self, container_id: str) -> None:
        self._watched.pop(container_id, None)
        self._terminating.discard(container_id)
        self._attempts.pop(container_id, None)

    @property
    def watched_count(self) -> int:
        return len(self._watched)

    # ── the loop ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # pragma: no cover
                # A reaper that dies recreates the original bug exactly, so no
                # error is allowed to end the loop.
                log.warning("reaper tick failed: %s", exc)
            await asyncio.sleep(self._interval)

    async def tick(self) -> dict[str, str]:
        """One pass. Returns {container_id: reason} for whatever was resolved."""
        if not self._watched:
            return {}
        try:
            running = set(await self._list_running())
        except Exception as exc:                              # pragma: no cover
            # Cannot see the truth this tick -- do nothing rather than guess.
            # Resolving on a failed listing would report live sessions as dead.
            log.warning("reaper could not list containers: %s", exc)
            return {}

        resolved: dict[str, str] = {}
        for cid, fut in list(self._watched.items()):
            if cid in running:
                continue
            if not fut.done():
                fut.set_result(EXITED)
            resolved[cid] = EXITED
            self.forget(cid)

        # Anything meant to be dead but still running: retry, then free anyway.
        for cid in list(self._terminating):
            if cid not in running or cid not in self._watched:
                continue
            attempts = self._attempts.get(cid, 0) + 1
            self._attempts[cid] = attempts
            try:
                await self._kill(cid, attempts)
            except Exception as exc:
                log.warning("reaper kill attempt %d for %s failed: %s",
                            attempts, cid[:12], exc)
            if attempts >= self._max_kill_attempts:
                fut = self._watched.get(cid)
                if fut and not fut.done():
                    # THE POINT OF THE WHOLE CLASS. Docker will not admit this
                    # container died, so we stop asking. The job exits, the slot
                    # is marked for rebuild, and one slot is lost instead of the
                    # worker advertising zero free slots forever.
                    log.error(
                        "reaper abandoning %s after %d failed kills — freeing the "
                        "job and marking the slot for rebuild", cid[:12], attempts)
                    fut.set_result(ABANDONED)
                resolved[cid] = ABANDONED
                self.forget(cid)
        return resolved


# ── Docker bindings ─────────────────────────────────────────────────────────

async def docker_list_running() -> list[str]:
    """Full (untruncated) IDs of every running container."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--no-trunc",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return [line for line in out.decode(errors="replace").split() if line]


async def docker_kill_escalating(container_id: str, attempt: int) -> bool:
    """Escalate across attempts: rm -f, then kill -9 then rm, then rm again.

    Escalation exists because the observed failure is Docker not receiving an
    exit event, not the container refusing a signal -- so a second identical
    ``rm -f`` is worth little, while SIGKILL direct to the container sometimes
    produces the event ``rm`` was waiting for.
    """
    if attempt == 2:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", "-s", "KILL", container_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    proc = await asyncio.create_subprocess_exec(
        "docker", "rm", "-fv", container_id,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode == 0:
        return True
    log.warning("kill attempt %d on %s: %s", attempt, container_id[:12],
                err.decode(errors="replace")[:160])
    return False


async def container_id_of(name: str) -> str:
    """Resolve a container NAME to its immutable ID.

    Called once at watch time. Everything downstream uses the ID, so a slot
    name being recycled later cannot redirect a wait onto another session.
    """
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "-f", "{{.Id}}", name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace").strip()

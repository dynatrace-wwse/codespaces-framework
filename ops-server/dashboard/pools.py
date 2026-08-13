"""Workshop pools and paced admission — where a session job goes, and when.

Two concerns that belong together because they act on the same object at the
same moment: the single ``rpush`` that puts a learner's session on a queue.

WHERE  (pool routing)
---------------------
A workshop's machines are sized ahead of time from a measured repo profile.
That planning is void if an unrelated self-service learner can land on one of
them mid-session and take a seat that was counted for an attendee.

Routing is by queue *topology*: a workshop's jobs are pushed to
``queue:pool:{pool}`` and only workers whose ``WORKER_POOL`` matches ever read
that key (see ``worker-agent/config.py``). Self-service jobs stay on
``queue:test:{arch}``, which workshop workers do not subscribe to. Neither side
can reach the other by accident, because there is no code path that could.

WHEN  (paced admission)
-----------------------
Thirty simultaneous provisions is the load that produced every capacity failure
so far: it exhausts the framework's own 600 s readiness gate, and the resulting
mass teardown has twice wedged a worker's container reaping. Spreading the same
thirty over ten minutes removes the burst without costing anything a learner
notices -- provisioning does not need to be fast, it needs to work.

The pacer is a **token bucket**, deliberately not a fixed timer. A lone
self-service learner arriving at a quiet moment finds a token and is admitted
instantly; only a burst drains the bucket and drips. A fixed timer would tax
the common case to fix the rare one.

Both provisioning paths converge here. A same-tenant roster is provisioned by a
server-side loop in ``provision-all``; foreign-tenant learners self-provision
when their own app notices ``provisionRequestedAt``. With 70 tenants that second
path is 70 independent clients firing inside one 10-second poll window, and
nothing on the trainer's side can pace it. They share this admission point, so
pacing it once covers both.

STATE
-----
The pending list is in Redis, so a restart cannot lose a learner's queued
provision. The bucket is in process memory, which is sound because the
dashboard runs as a single uvicorn process (``dashboard/app.py:start``); if it
ever runs multiple workers, the bucket must move to Redis or each worker will
independently admit a full burst.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

log = logging.getLogger(__name__)

# ── Pool identity ───────────────────────────────────────────────────────────
DAILY_POOL = "daily"

# Redis hash mapping workshop id → pool name, written when a workshop's
# machines are provisioned and deleted when they are torn down. A workshop with
# no entry falls back to the shared queue, which is the correct behaviour for
# every workshop that ran before pools existed and for small workshops that
# deliberately do not get dedicated machines.
WORKSHOP_POOL_KEY = "workshop:pools"


def pool_queue(pool: str) -> str:
    """Queue key a pool's workers consume. Mirrors worker-agent.config."""
    return f"queue:pool:{pool}"


def arch_queue(arch: str) -> str:
    return f"queue:test:{arch}"


def pending_key(target: str) -> str:
    """Holding list in front of ``target``. Redis-backed so a dashboard restart
    does not drop provisions that a trainer already asked for."""
    return f"queue:pending:{target}"


async def pool_for_workshop(redis, ws_id: str) -> str:
    """Pool serving ``ws_id``, or "" when it has no dedicated machines.

    Fails OPEN: any Redis problem returns "" and the job takes the shared
    queue. A workshop losing its isolation is a degraded delivery; a workshop
    whose learners cannot be provisioned at all is a failed one.
    """
    if not ws_id:
        return ""
    try:
        return (await redis.hget(WORKSHOP_POOL_KEY, ws_id)) or ""
    except Exception as exc:                                  # pragma: no cover
        log.warning("pool lookup failed for %s: %s — using shared queue", ws_id, exc)
        return ""


async def bind_workshop_pool(redis, ws_id: str, pool: str) -> None:
    await redis.hset(WORKSHOP_POOL_KEY, ws_id, pool)


async def unbind_workshop_pool(redis, ws_id: str) -> None:
    await redis.hdel(WORKSHOP_POOL_KEY, ws_id)


async def target_queue(redis, ws_id: str, arch: str = "amd64") -> str:
    """The queue a session job for this workshop belongs on."""
    pool = await pool_for_workshop(redis, ws_id)
    return pool_queue(pool) if pool else arch_queue(arch)


# ── Paced admission ─────────────────────────────────────────────────────────
# Defaults chosen from the measured failure, not from taste: 30 simultaneous
# installs exhausted a 600 s gate, 20 used 53 of 60 retries, 12 used 29. The
# aim is to keep the number of installs *starting together* well under ten.
#
# 2 every 30 s drips 30 sessions over roughly 7 minutes. Burst 2 means a pair of
# learners arriving at once is still instant.
PACE_ENABLED = os.environ.get("PACE_ENABLED", "1").strip().lower() in ("1", "true", "yes")
PACE_BATCH = int(os.environ.get("PACE_BATCH", "2"))
PACE_INTERVAL_S = float(os.environ.get("PACE_INTERVAL_S", "30"))
# How often the drainer looks at the pending lists. Independent of the refill
# interval: a short tick makes the drip smooth, the bucket controls the rate.
PACE_TICK_S = float(os.environ.get("PACE_TICK_S", "5"))


class TokenBucket:
    """Rate limiter that admits a small burst instantly, then drips.

    ``capacity`` tokens refill over ``interval`` seconds. Refill is computed
    from elapsed wall time rather than by a timer, so a slow or stalled event
    loop cannot silently change the rate.
    """

    def __init__(self, capacity: int, interval: float):
        self.capacity = max(1, capacity)
        self.interval = max(0.1, interval)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(float(self.capacity),
                           self._tokens + elapsed * (self.capacity / self.interval))

    def take(self, n: int = 1) -> int:
        """Consume up to ``n`` tokens; returns how many were actually granted."""
        self._refill()
        granted = min(n, int(self._tokens))
        self._tokens -= granted
        return granted

    @property
    def available(self) -> int:
        self._refill()
        return int(self._tokens)


# One bucket per target queue. Keyed by queue name so a workshop pool and the
# shared self-service queue drip independently -- a busy workshop must not
# throttle a single self-service learner on entirely different machines.
_buckets: dict[str, TokenBucket] = {}


def bucket_for(target: str) -> TokenBucket:
    if target not in _buckets:
        _buckets[target] = TokenBucket(PACE_BATCH, PACE_INTERVAL_S)
    return _buckets[target]


async def enqueue_paced(redis, target: str, job: dict) -> dict:
    """Admit ``job`` to ``target`` now, or park it in the pending list.

    Returns ``{"admitted": bool, "position": int, "queue": str}`` -- position is
    the depth of the pending list, so a caller can tell a waiting learner where
    they are instead of showing an unexplained spinner.

    Jumping the pending list would reorder learners, so a job is admitted
    directly ONLY when nothing is already waiting.
    """
    payload = json.dumps(job)
    if not PACE_ENABLED:
        await redis.rpush(target, payload)
        return {"admitted": True, "position": 0, "queue": target}

    pending = pending_key(target)
    waiting = await redis.llen(pending)
    if waiting == 0 and bucket_for(target).take(1):
        await redis.rpush(target, payload)
        return {"admitted": True, "position": 0, "queue": target}

    await redis.rpush(pending, payload)
    return {"admitted": False, "position": waiting + 1, "queue": target}


async def drain_pending(redis, target: str) -> int:
    """Move as many pending jobs onto ``target`` as the bucket allows.

    ``LMOVE ... LEFT RIGHT`` preserves arrival order and is atomic, so a crash
    mid-drain can neither duplicate a job nor drop one.
    """
    pending = pending_key(target)
    allowed = bucket_for(target).take(PACE_BATCH)
    moved = 0
    for _ in range(allowed):
        if not await redis.lmove(pending, target, "LEFT", "RIGHT"):
            break
        moved += 1
    return moved


async def known_targets(redis) -> list[str]:
    """Target queues that currently have someone waiting.

    Scans for pending lists rather than tracking targets in memory, so a
    dashboard restart resumes draining a workshop it never saw created.
    """
    out = []
    try:
        async for key in redis.scan_iter(match="queue:pending:*", count=100):
            out.append(key[len("queue:pending:"):])
    except Exception as exc:                                  # pragma: no cover
        log.warning("pending scan failed: %s", exc)
    return out


async def pacer_loop(redis) -> None:
    """Background drainer. One pass per tick over every queue with a backlog."""
    if not PACE_ENABLED:
        log.info("Provision pacer disabled (PACE_ENABLED=0)")
        return
    log.info("Provision pacer: %d job(s) per %.0fs per queue, tick %.0fs",
             PACE_BATCH, PACE_INTERVAL_S, PACE_TICK_S)
    while True:
        try:
            for target in await known_targets(redis):
                moved = await drain_pending(redis, target)
                if moved:
                    depth = await redis.llen(pending_key(target))
                    log.info("pacer: admitted %d to %s, %d still waiting",
                             moved, target, depth)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                              # pragma: no cover
            # Never let one bad tick kill the drainer -- a dead pacer means
            # provisions park in the pending list forever, which looks to a
            # trainer exactly like a hung fleet.
            log.warning("pacer tick failed: %s", exc)
        await asyncio.sleep(PACE_TICK_S)

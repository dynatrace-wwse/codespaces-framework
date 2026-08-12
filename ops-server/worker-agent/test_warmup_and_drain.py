"""Tests for honest warm-up capacity reporting and the drain cordon (phase 1).

Pure logic — no Redis/Docker — so it runs anywhere.

Runnable two ways (mirrors test_scheduler.py / test_slot_reinit.py):
  - pytest:     /home/ops/ops-venv/bin/python -m pytest worker-agent/test_warmup_and_drain.py
  - standalone: cd ops-server && /home/ops/ops-venv/bin/python -m worker-agent.test_warmup_and_drain

Two regression contracts, both observed live on 2026-08-12:

1. WARM-UP HONESTY. ``_register()`` used to write ``capacity=WORKER_CAPACITY``
   before ``sysbox_pool.init()`` ran, so a booting worker advertised its full
   nominal capacity with zero usable slots — measured at 4m35s of lying for a
   12-slot pool. Anything sizing on that field (fleet controller, workshop
   capacity check, dashboard) systematically over-counted the fleet. Now
   ``capacity``/``slots_ready`` track ``SysboxPool.ready_count``, which rises
   one slot at a time, and ``release()`` re-initialising a slot must NOT
   inflate it.

2. CORDON. ``draining=1`` is set by the master on scale-down. The string
   ``draining`` appeared zero times in the whole worker-agent package, so the
   agent kept claiming jobs onto a host about to be terminated. The cordon is
   now read every consume tick, and deliberately fails OPEN so a Redis blip
   cannot silently idle the fleet.
"""

import asyncio

from .agent import SysboxPool
from .executor import SysboxSlot


# ── helpers ──────────────────────────────────────────────────────────────────

def _pool(capacity: int) -> SysboxPool:
    return SysboxPool(capacity)


async def _mark_ready(pool: SysboxPool, slot: SysboxSlot) -> None:
    """Replicate _init_slot's success tail without Docker."""
    if not slot.ever_ready:
        slot.ever_ready = True
        pool.ready_count += 1
        pool.first_ready.set()
    await pool._queue.put(slot)


class _FakeRedis:
    """Minimal stand-in for the redis.asyncio client used by _refresh_draining."""

    def __init__(self, value=None, raises: bool = False):
        self.value = value
        self.raises = raises
        self.calls = 0

    async def hget(self, key, field):
        self.calls += 1
        if self.raises:
            raise ConnectionError("redis unreachable")
        return self.value


class _Agent:
    """Just enough of WorkerAgent to exercise _refresh_draining unchanged."""

    def __init__(self, redis_client):
        self.pool = redis_client
        self._draining = False

    # bind the real implementation
    from .agent import WorkerAgent as _WA
    _refresh_draining = _WA._refresh_draining


# ── warm-up honesty ──────────────────────────────────────────────────────────

def test_pool_starts_with_zero_ready():
    # The core of the bug: at construction NOTHING is usable yet.
    pool = _pool(12)
    assert pool.ready_count == 0
    assert not pool.first_ready.is_set()
    assert not pool.warm_complete.is_set()


def test_ready_count_rises_one_slot_at_a_time():
    pool = _pool(6)
    seen = []
    for slot in pool.slots:
        asyncio.run(_mark_ready(pool, slot))
        seen.append(pool.ready_count)
    # Monotonic 1..6 — never jumps to nominal capacity up front.
    assert seen == [1, 2, 3, 4, 5, 6]
    assert pool.free_slots() == 6


def test_first_ready_set_by_first_slot_not_the_last():
    # This is what lets _consume_queue start ~2 min before the pool finishes.
    pool = _pool(12)
    asyncio.run(_mark_ready(pool, pool.slots[0]))
    assert pool.first_ready.is_set()
    assert pool.ready_count == 1
    assert not pool.warm_complete.is_set()


def test_reinit_of_same_slot_does_not_inflate_ready_count():
    # release(healthy=False) re-runs _init_slot on a slot that is already part
    # of the warm pool. Counting it again would re-create the over-count bug
    # from the other direction.
    pool = _pool(3)
    for slot in pool.slots:
        asyncio.run(_mark_ready(pool, slot))
    assert pool.ready_count == 3

    recycled = pool.slots[1]
    for _ in range(5):
        asyncio.run(_mark_ready(pool, recycled))
    assert pool.ready_count == 3          # unchanged
    assert pool.free_slots() == 8         # queue does grow — 3 + 5 re-puts


def test_free_slots_tracks_claimed_slots():
    pool = _pool(4)
    for slot in pool.slots:
        asyncio.run(_mark_ready(pool, slot))
    claimed = asyncio.run(pool.acquire())
    assert claimed is not None
    assert pool.ready_count == 4          # still warm...
    assert pool.free_slots() == 3         # ...but one is in use


def test_total_warm_failure_unblocks_consumers():
    # If every slot fails to warm, init() sets first_ready anyway so
    # _consume_queue can proceed to its own error handling instead of
    # hanging forever on an event that will never fire.
    pool = _pool(2)

    async def _all_fail(_slot):
        return False

    pool._init_slot = _all_fail
    ready = asyncio.run(pool.init())
    assert ready == 0
    assert pool.warm_complete.is_set()
    assert pool.first_ready.is_set()
    assert pool.ready_count == 0


# ── cordon ───────────────────────────────────────────────────────────────────

def test_draining_flag_read_as_true():
    for raw in ("1", "true", "TRUE", "yes", " 1 "):
        agent = _Agent(_FakeRedis(raw))
        assert asyncio.run(agent._refresh_draining()) is True, raw
        assert agent._draining is True


def test_draining_flag_read_as_false():
    for raw in ("0", "", None, "no", "false"):
        agent = _Agent(_FakeRedis(raw))
        assert asyncio.run(agent._refresh_draining()) is False, raw
        assert agent._draining is False


def test_cordon_clears_again():
    # Un-drain must be as immediate as drain — the fleet controller prefers
    # un-cordoning a live worker over launching a new one.
    redis = _FakeRedis("1")
    agent = _Agent(redis)
    assert asyncio.run(agent._refresh_draining()) is True
    redis.value = "0"
    assert asyncio.run(agent._refresh_draining()) is False


def test_redis_failure_fails_open_preserving_last_state():
    # Not draining + Redis down → keep serving (a blip must not idle the fleet).
    agent = _Agent(_FakeRedis(raises=True))
    assert asyncio.run(agent._refresh_draining()) is False

    # Already draining + Redis down → stay drained (don't resurrect a worker
    # that is mid-termination).
    agent2 = _Agent(_FakeRedis(raises=True))
    agent2._draining = True
    assert asyncio.run(agent2._refresh_draining()) is True


def test_draining_is_checked_every_call():
    # It is a per-tick read, not a cached-at-startup value.
    redis = _FakeRedis("0")
    agent = _Agent(redis)
    for _ in range(3):
        asyncio.run(agent._refresh_draining())
    assert redis.calls == 3


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    print(f"{'FAILED' if failures else 'OK'} ({failures} failures)")
    sys.exit(1 if failures else 0)

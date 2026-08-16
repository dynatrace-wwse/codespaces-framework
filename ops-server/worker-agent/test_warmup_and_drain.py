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

from . import agent as agent_mod
from .agent import SysboxPool
from .executor import SysboxSlot


# ── helpers ──────────────────────────────────────────────────────────────────

def _pool(capacity: int) -> SysboxPool:
    pool = SysboxPool(capacity)
    # acquire() probes the slot's container with `docker inspect` before handing
    # it out (see SysboxPool._slot_is_alive). These are pure queue-accounting
    # tests with no containers behind the slots, so the probe would report every
    # slot dead and send acquire() into a real `docker run` rebuild — turning a
    # 0.4s suite into a 2-minute one that mutates the host. Stub it: liveness is
    # covered by test_slot_workspace.py, this file tests the bookkeeping.
    async def _always_alive(_slot):
        return True

    pool._slot_is_alive = _always_alive
    return pool


async def _mark_ready(pool: SysboxPool, slot: SysboxSlot) -> None:
    """Replicate _init_slot's success tail without Docker."""
    if not slot.ever_ready:
        slot.ever_ready = True
        pool.ready_count += 1
        pool.first_ready.set()
    await pool._queue.put(slot)


class _FakeRedis:
    """Minimal stand-in for the redis.asyncio client used by _refresh_draining."""

    def __init__(self, value=None, raises: bool = False, brake=None):
        self.value = value
        self.brake = brake
        self.raises = raises
        self.calls = 0

    async def hget(self, key, field):
        self.calls += 1
        if self.raises:
            raise ConnectionError("redis unreachable")
        return self.value

    async def hmget(self, key, *fields):
        self.calls += 1
        if self.raises:
            raise ConnectionError("redis unreachable")
        return [self.value if f == "draining" else self.brake for f in fields]


class _Agent:
    """Just enough of WorkerAgent to exercise _refresh_draining unchanged."""

    def __init__(self, redis_client):
        self.pool = redis_client
        self._draining = False
        self._braked = False

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


def test_total_warm_failure_unblocks_consumers(monkeypatch):
    # If every slot fails to warm, init() sets first_ready anyway so
    # _consume_queue can proceed to its own error handling instead of
    # hanging forever on an event that will never fire.
    #
    # Backoff is shortened here rather than left at the production value: init()
    # now retries failed slots, and the real 10s/20s waits would put 30 seconds
    # of sleep into the unit suite for no extra coverage.
    monkeypatch.setattr(agent_mod, "WARM_RETRY_BASE_S", 0)
    pool = _pool(2)

    async def _all_fail(_slot):
        return False

    pool._init_slot = _all_fail
    ready = asyncio.run(pool.init())
    assert ready == 0
    assert pool.warm_complete.is_set()
    assert pool.first_ready.is_set()
    assert pool.ready_count == 0
    assert pool.degraded == 2, "a pool with nothing ready must report itself degraded"


def test_warmup_retries_slots_that_failed_the_first_time(monkeypatch):
    """The recovery restart that returned 18/30 did so because a slot that
    failed once was never tried again. Transient dockerd pressure is exactly
    the case retrying fixes."""
    monkeypatch.setattr(agent_mod, "WARM_RETRY_BASE_S", 0)
    pool = _pool(4)
    attempts = {}

    async def _fail_once(slot):
        attempts[slot.index] = attempts.get(slot.index, 0) + 1
        return attempts[slot.index] > 1      # fails first time, succeeds after

    pool._init_slot = _fail_once
    ready = asyncio.run(pool.init())
    assert ready == 4, "every slot should recover on the retry"
    assert pool.degraded == 0


def test_warmup_reports_a_short_pool_instead_of_absorbing_it(monkeypatch):
    """'Worker fully warm' was logged at 18/30. A pool that cannot reach
    capacity has to say so — the scale planner cannot infer it."""
    monkeypatch.setattr(agent_mod, "WARM_RETRY_BASE_S", 0)
    pool = _pool(4)

    async def _one_always_fails(slot):
        return slot.index != 3

    pool._init_slot = _one_always_fails
    ready = asyncio.run(pool.init())
    assert ready == 3
    assert pool.degraded == 1


def test_warmup_never_fires_every_slot_at_once(monkeypatch):
    """The 30-at-once init burst is what failed while dockerd was busy.

    Asserts the observed peak concurrency stays within WARM_CONCURRENCY.
    """
    monkeypatch.setattr(agent_mod, "WARM_RETRY_BASE_S", 0)
    monkeypatch.setattr(agent_mod, "WARM_CONCURRENCY", 3)
    pool = _pool(12)
    live = {"now": 0, "peak": 0}

    async def _slow(_slot):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0)
        live["now"] -= 1
        return True

    pool._init_slot = _slow
    assert asyncio.run(pool.init()) == 12
    assert live["peak"] <= 3, f"warmed {live['peak']} at once, cap is 3"


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


# ── acquire() must not hand out a dead slot (2026-08-13, 4 of 60 provisions) ─
# Being in the queue only proves the container was running when it was
# *enqueued*. In a burst after a mass teardown, 4 of 60 jobs claimed a slot
# whose container was already gone and failed for a reason that had nothing to
# do with the learner's training.

def test_acquire_rebuilds_a_dead_slot_and_returns_a_live_one():
    pool = SysboxPool(2)
    dead, alive = pool.slots[0], pool.slots[1]
    rebuilt = []

    async def _liveness(slot):
        return slot is not dead

    async def _init(slot):
        rebuilt.append(slot)
        return True                      # rebuilt, but deliberately NOT re-queued

    pool._slot_is_alive = _liveness
    pool._init_slot = _init

    async def scenario():
        await pool._queue.put(dead)
        await pool._queue.put(alive)
        return await pool.acquire()

    got = asyncio.run(scenario())
    assert got is alive, "a dead slot must never be handed to a job"
    assert rebuilt == [dead], "the dead slot must be rebuilt, not silently dropped"


def test_liveness_probe_fails_open_when_docker_itself_errors():
    # A flaky docker CLI must not empty the pool. If the probe cannot run we
    # report the slot alive and let the executor's own checks catch a real
    # problem — refusing every slot would be a worse failure than the one being
    # prevented. The fail-open lives inside the probe, so exercise the probe.
    from . import agent as agent_mod
    pool = SysboxPool(1)
    orig = agent_mod.asyncio.create_subprocess_exec

    async def _boom(*_a, **_k):
        raise RuntimeError("docker daemon unreachable")

    agent_mod.asyncio.create_subprocess_exec = _boom
    try:
        assert asyncio.run(pool._slot_is_alive(pool.slots[0])) is True
    finally:
        agent_mod.asyncio.create_subprocess_exec = orig


def test_liveness_probe_reads_docker_inspect_state():
    from . import agent as agent_mod
    pool = SysboxPool(1)
    orig = agent_mod.asyncio.create_subprocess_exec

    class _P:
        def __init__(self, out):
            self._out = out

        async def communicate(self):
            return self._out, b""

    def _stub(out):
        async def _exec(*_a, **_k):
            return _P(out)
        return _exec

    try:
        agent_mod.asyncio.create_subprocess_exec = _stub(b"true\n")
        assert asyncio.run(pool._slot_is_alive(pool.slots[0])) is True
        agent_mod.asyncio.create_subprocess_exec = _stub(b"false\n")
        assert asyncio.run(pool._slot_is_alive(pool.slots[0])) is False
        # A missing container prints nothing on stdout — must read as dead.
        agent_mod.asyncio.create_subprocess_exec = _stub(b"")
        assert asyncio.run(pool._slot_is_alive(pool.slots[0])) is False
    finally:
        agent_mod.asyncio.create_subprocess_exec = orig


# ── admission brake ──────────────────────────────────────────────────────────
# The control loop sets admission_brake on a worker under sustained CPU
# pressure. A flag nothing reads is worse than no flag at all -- the drain
# cordon was exactly that until 2026-08-12 -- so these pin that it bites.

def test_admission_brake_stops_new_intake():
    agent = _Agent(_FakeRedis("0", brake="1"))
    assert asyncio.run(agent._refresh_draining()) is True
    assert agent._braked is True
    assert agent._draining is False, "a brake must not masquerade as a cordon"


def test_brake_clears_when_the_worker_cools():
    agent = _Agent(_FakeRedis("0", brake="1"))
    assert asyncio.run(agent._refresh_draining()) is True
    agent.pool.brake = "0"
    assert asyncio.run(agent._refresh_draining()) is False
    assert agent._braked is False


def test_cordon_and_brake_are_tracked_separately():
    """Distinct lifetimes: a cordon precedes termination, a brake is temporary
    back-pressure. A braked worker must not look like one being scaled down."""
    agent = _Agent(_FakeRedis("1", brake="0"))
    assert asyncio.run(agent._refresh_draining()) is True
    assert agent._draining is True and agent._braked is False


def test_brake_fails_open_on_a_redis_blip():
    """Same reasoning as the cordon: a transient blip must not quietly idle
    the fleet."""
    agent = _Agent(_FakeRedis(raises=True))
    assert asyncio.run(agent._refresh_draining()) is False

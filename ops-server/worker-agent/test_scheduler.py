"""Tests for the worker's weighted scheduler + lane routing.

Pure logic — no Redis, Docker, or psutil — so it runs anywhere.

Runnable two ways (mirrors dashboard/test_content_service.py):
  - pytest:     /home/ops/ops-venv/bin/python -m pytest worker-agent/test_scheduler.py
  - standalone: cd ops-server && /home/ops/ops-venv/bin/python -m worker-agent.test_scheduler
"""

import asyncio

from .scheduler import (
    WeightedScheduler,
    cost_of,
    lane_of,
    classify,
    COST_LIGHT,
    COST_MEDIUM,
    COST_HEAVY,
)


# ── cost classification ──────────────────────────────────────────────────────

def test_cost_of_by_suite():
    assert cost_of({"suite": "bats"}) == COST_LIGHT
    assert cost_of({"suite": "engines"}) == COST_MEDIUM
    assert cost_of({"suite": "dt-apponly"}) == COST_MEDIUM
    assert cost_of({"suite": "dt-cnfs"}) == COST_HEAVY
    # framework_suite is an accepted alias for suite
    assert cost_of({"framework_suite": "dt-cnfs"}) == COST_HEAVY


def test_cost_of_explicit_wins_and_is_floored():
    assert cost_of({"cost": 7, "suite": "bats"}) == 7      # explicit beats suite
    assert cost_of({"cost": "3"}) == 3                     # string coerced
    assert cost_of({"cost": 0}) == 1                       # floored to >= 1
    assert cost_of({"cost": -5}) == 1
    assert cost_of({"cost": "bad", "suite": "bats"}) == COST_LIGHT  # bad → fallback


def test_cost_of_by_type_and_default():
    assert cost_of({"type": "integration-test"}) == COST_MEDIUM
    assert cost_of({"type": "daemon"}) == COST_MEDIUM
    assert cost_of({"type": "framework-test"}) == COST_MEDIUM
    assert cost_of({}) == COST_LIGHT
    assert cost_of({"type": "unknown-type"}) == COST_LIGHT


# ── lane classification ──────────────────────────────────────────────────────

def test_lane_of():
    assert lane_of({"suite": "dt-cnfs"}) == "heavy"
    assert lane_of({"suite": "bats"}) == "light"
    assert lane_of({"requires_native": True}) == "heavy"
    assert lane_of({"cost": COST_HEAVY}) == "heavy"        # at threshold
    assert lane_of({"cost": COST_MEDIUM}) == "light"
    assert lane_of({}) == "light"


def test_lane_explicit_overrides():
    assert lane_of({"lane": "heavy", "suite": "bats"}) == "heavy"
    assert lane_of({"lane": "light", "suite": "dt-cnfs"}) == "light"


def test_classify():
    assert classify({"suite": "dt-cnfs"}) == (COST_HEAVY, "heavy")
    assert classify({"suite": "bats"}) == (COST_LIGHT, "light")


# ── from_capacity defaults + clamps ──────────────────────────────────────────

def test_from_capacity_defaults():
    s = WeightedScheduler.from_capacity(6)
    assert s.budget == 6 * COST_MEDIUM          # 12
    assert s.max_heavy == 2                      # 6 // 3


def test_from_capacity_clamps():
    # capacity 1 → budget 2 clamped up to COST_HEAVY; max_heavy floored to 1
    s = WeightedScheduler.from_capacity(1)
    assert s.budget == COST_HEAVY
    assert s.max_heavy == 1
    # explicit too-small values are clamped
    s2 = WeightedScheduler.from_capacity(6, budget=3, max_heavy=0)
    assert s2.budget == COST_HEAVY
    assert s2.max_heavy == 1


# ── can_admit (pure predicate) ───────────────────────────────────────────────

def test_can_admit_respects_budget():
    s = WeightedScheduler(budget=4, max_heavy=5)
    a = {"cost": 2, "lane": "light"}
    asyncio.run(s.acquire(a))
    asyncio.run(s.acquire({"cost": 2, "lane": "light"}))   # in_flight == 4 == budget
    assert not s.can_admit({"cost": 2, "lane": "light"})   # would exceed
    assert not s.can_admit({"cost": 1, "lane": "light"})


def test_can_admit_progress_when_idle():
    # An oversized job (cost > budget) is still admitted when nothing is in flight.
    s = WeightedScheduler(budget=4, max_heavy=2)
    assert s.can_admit({"cost": 99, "lane": "light"})


def test_can_admit_heavy_lane_cap():
    s = WeightedScheduler(budget=100, max_heavy=1)   # budget wide; isolate lane
    asyncio.run(s.acquire({"suite": "dt-cnfs"}))     # lane full
    assert not s.can_admit({"suite": "dt-cnfs"})     # second heavy blocked
    assert s.can_admit({"suite": "bats"})            # light unaffected


# ── async admission: blocks then unblocks ────────────────────────────────────

def test_heavy_lane_blocks_until_release():
    async def run():
        s = WeightedScheduler(budget=100, max_heavy=1)
        await s.acquire({"suite": "dt-cnfs"})
        assert s.stats()["sched_heavy_in_flight"] == 1

        waiter = asyncio.create_task(s.acquire({"suite": "dt-cnfs"}))
        await asyncio.sleep(0.02)
        assert not waiter.done()                     # blocked on the heavy lane

        await s.release({"suite": "dt-cnfs"})
        await asyncio.wait_for(waiter, 1)            # released → admitted
        assert s.stats()["sched_heavy_in_flight"] == 1
    asyncio.run(run())


def test_budget_blocks_until_release():
    async def run():
        s = WeightedScheduler(budget=4, max_heavy=5)
        a = {"cost": 2, "lane": "light"}
        await s.acquire(a)
        await s.acquire({"cost": 2, "lane": "light"})   # in_flight == budget

        waiter = asyncio.create_task(s.acquire({"cost": 2, "lane": "light"}))
        await asyncio.sleep(0.02)
        assert not waiter.done()                        # blocked on budget

        await s.release(a)                              # frees 2 units
        await asyncio.wait_for(waiter, 1)
        assert s.stats()["sched_in_flight_cost"] == 4
    asyncio.run(run())


def test_light_not_blocked_by_full_heavy_lane():
    async def run():
        s = WeightedScheduler(budget=100, max_heavy=1)
        await s.acquire({"suite": "dt-cnfs"})           # heavy lane saturated
        # A light job must still be admitted immediately.
        await asyncio.wait_for(s.acquire({"suite": "bats"}), 1)
        assert s.stats()["sched_heavy_in_flight"] == 1
    asyncio.run(run())


# ── acquire/release bookkeeping ──────────────────────────────────────────────

def test_acquire_release_roundtrip():
    async def run():
        s = WeightedScheduler(budget=10, max_heavy=2)
        j = {"suite": "dt-cnfs"}
        await s.acquire(j)
        st = s.stats()
        assert st["sched_in_flight_cost"] == COST_HEAVY
        assert st["sched_heavy_in_flight"] == 1

        await s.release(j)
        st = s.stats()
        assert st["sched_in_flight_cost"] == 0
        assert st["sched_heavy_in_flight"] == 0
    asyncio.run(run())


def test_release_floors_at_zero():
    async def run():
        s = WeightedScheduler(budget=10, max_heavy=2)
        await s.release({"suite": "dt-cnfs"})           # release without acquire
        st = s.stats()
        assert st["sched_in_flight_cost"] == 0
        assert st["sched_heavy_in_flight"] == 0
    asyncio.run(run())


def test_mixed_packing_one_heavy_plus_lights():
    async def run():
        # 6-slot worker defaults: budget 12, heavy lane 2.
        s = WeightedScheduler.from_capacity(6)
        await s.acquire({"suite": "dt-cnfs"})           # heavy: cost 4
        # Light unit tests (cost 1) keep filling the remaining budget headroom.
        for _ in range(8):                               # 4 + 8*1 = 12 == budget
            assert s.can_admit({"suite": "bats"})
            await s.acquire({"suite": "bats"})
        assert s.stats()["sched_in_flight_cost"] == 12
        assert not s.can_admit({"suite": "bats"})       # budget exhausted
        assert not s.can_admit({"suite": "dt-cnfs"})    # also over budget
    asyncio.run(run())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} scheduler tests passed")

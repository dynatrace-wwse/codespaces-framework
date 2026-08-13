"""Tests for pool membership — which queue a worker is allowed to eat from.

Pure logic, no Redis/Docker.

  pytest: /home/ops/ops-venv/bin/python -m pytest worker-agent/test_pool_topology.py \
              --import-mode=importlib

WHY THIS IS TESTED AT ALL

A workshop's machines are sized in advance from a measured repo profile. That
planning is worthless if a self-service learner can land on one of them mid-
workshop and consume a seat that was counted for an attendee. The isolation is
implemented by NOT SUBSCRIBING to the shared queue rather than by filtering
jobs after they arrive, because a filter is a piece of logic that can regress
silently, whereas a queue a process never reads from cannot deliver anything.

These tests pin that property: for any pool that is not "daily", the shared
arch queue must not appear in the worker's BLPOP key list at all.
"""

from .config import queue_keys, DAILY_POOL


def test_daily_worker_takes_shared_arch_queue():
    keys = queue_keys("worker-x86_64-amd001", "amd64", DAILY_POOL)
    assert keys == ["queue:direct:worker-x86_64-amd001", "queue:test:amd64"]


def test_workshop_worker_never_sees_the_shared_queue():
    keys = queue_keys("worker-x86_64-spot-abc123", "amd64", "ws_bootcamp01")
    assert "queue:test:amd64" not in keys, (
        "a workshop worker subscribed to the shared arch queue can be handed "
        "self-service work — this is the whole isolation guarantee"
    )
    assert keys == ["queue:direct:worker-x86_64-spot-abc123",
                    "queue:pool:ws_bootcamp01"]


def test_direct_queue_stays_highest_priority_in_both_pools():
    """queue:direct is how an operator or a capacity test targets one box.

    It must keep working irrespective of pool, and must stay ahead of the
    shared queue so a targeted job is not stuck behind ordinary backlog.
    """
    for pool in (DAILY_POOL, "ws_anything"):
        keys = queue_keys("w1", "arm64", pool)
        assert keys[0] == "queue:direct:w1"
        assert len(keys) == 2


def test_arch_is_honoured_for_daily_pool():
    assert queue_keys("w1", "arm64", DAILY_POOL)[1] == "queue:test:arm64"


def test_blank_pool_falls_back_to_daily():
    """An unset or whitespace WORKER_POOL must behave exactly like today.

    Every existing worker in the fleet has no WORKER_POOL in its .env, so the
    empty case is the upgrade path for the whole running fleet — if this
    resolved to a pool queue instead, every worker would go silent on restart.
    """
    assert queue_keys("w1", "amd64", "")[1] == "queue:test:amd64"
    assert queue_keys("w1", "amd64", "   ")[1] == "queue:test:amd64"

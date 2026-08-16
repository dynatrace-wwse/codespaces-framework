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


# ── Lending: the standing workshop box may sell HALF its seats to self-service ──
#
# A dedicated workshop machine is idle whenever no workshop is running, which is
# most of the time. Lending part of it back recovers that cost, but the lend has
# to be capped or the guarantee it exists to protect is gone: a Sysbox session
# cannot be migrated, so seats handed to self-service are gone until that learner
# finishes, and a workshop starting with no notice would find the box full.

import importlib
import os

from . import config as cfg


def _reload_with(**env):
    """Reload config with env overrides, and always restore the real one."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return importlib.reload(cfg)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_a_worker_with_no_borrow_pool_lends_nothing():
    """The default must be strict isolation — lending is opt-in per box."""
    c = _reload_with(WORKER_BORROW_POOL="", WORKER_POOL="workshop")
    try:
        assert c.borrow_capacity(20) == 0
        assert c.borrow_queue("amd64") == ""
        assert c.queue_keys("w1", "amd64", borrowing=True) == [
            "queue:direct:w1", "queue:pool:workshop"]
    finally:
        importlib.reload(cfg)


def test_lending_worker_reads_the_borrow_queue_last():
    """Key order is priority order in BLPOP, so the box's OWN lane must come
    first: a workshop learner is never left waiting behind borrowed work."""
    c = _reload_with(WORKER_BORROW_POOL="daily", WORKER_POOL="workshop",
                     WORKER_CAPACITY="20", WORKER_BORROW_FRACTION="0.5")
    try:
        keys = c.queue_keys("w-standing", "amd64", borrowing=True)
        assert keys == ["queue:direct:w-standing",
                        "queue:pool:workshop",
                        "queue:test:amd64"]
        assert keys.index("queue:pool:workshop") < keys.index("queue:test:amd64")
    finally:
        importlib.reload(cfg)


def test_borrow_queue_disappears_once_the_cap_is_reached():
    """Dropping the key from the BLPOP list IS the enforcement. There is no
    later point at which a borrowed session could be pushed back off the box."""
    c = _reload_with(WORKER_BORROW_POOL="daily", WORKER_POOL="workshop",
                     WORKER_CAPACITY="20", WORKER_BORROW_FRACTION="0.5")
    try:
        assert c.borrow_capacity(20) == 10
        at_cap = c.queue_keys("w-standing", "amd64", borrowing=False)
        assert "queue:test:amd64" not in at_cap
        assert at_cap == ["queue:direct:w-standing", "queue:pool:workshop"]
    finally:
        importlib.reload(cfg)


def test_half_of_twenty_is_ten_seats_kept_for_workshops():
    """The number the whole arrangement rests on: 20 slots, 10 lendable,
    10 always available for a workshop that starts with no notice."""
    c = _reload_with(WORKER_BORROW_POOL="daily", WORKER_CAPACITY="20",
                     WORKER_POOL="workshop", WORKER_BORROW_FRACTION="0.5")
    try:
        assert c.borrow_capacity(20) == 10
        assert 20 - c.borrow_capacity(20) == 10
    finally:
        importlib.reload(cfg)


def test_borrow_capacity_never_exceeds_the_box():
    """A fraction over 1.0 (fat finger in .env) must not lend seats that do not
    exist, and must not make the reserved half negative."""
    c = _reload_with(WORKER_BORROW_POOL="daily", WORKER_CAPACITY="20",
                     WORKER_POOL="workshop", WORKER_BORROW_FRACTION="5")
    try:
        assert c.borrow_capacity(20) == 20
    finally:
        importlib.reload(cfg)


def test_a_daily_worker_does_not_borrow_from_itself():
    """Borrowing from your own pool would just duplicate a key in the BLPOP
    list — harmless but nonsense, and it would double-count in the heartbeat."""
    c = _reload_with(WORKER_BORROW_POOL="daily", WORKER_POOL="daily",
                     WORKER_CAPACITY="20")
    try:
        keys = c.queue_keys("w-daily", "amd64", borrowing=True)
        assert keys == ["queue:direct:w-daily", "queue:test:amd64"]
    finally:
        importlib.reload(cfg)

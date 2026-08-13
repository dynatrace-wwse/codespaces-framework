"""Tests for workshop pool routing and paced admission.

  /home/ops/ops-venv/bin/python -m pytest dashboard/test_pools.py -q

Uses a tiny in-memory fake for the handful of Redis list/hash ops involved, so
these run with no server and no network.
"""

import asyncio

import pytest

from dashboard import pools


class FakeRedis:
    """Just enough Redis: hashes, lists, LMOVE, and a scan over key patterns."""

    def __init__(self):
        self.h: dict[str, dict[str, str]] = {}
        self.l: dict[str, list[str]] = {}
        self.fail_hget = False

    async def hget(self, key, field):
        if self.fail_hget:
            raise RuntimeError("redis down")
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)

    async def rpush(self, key, value):
        self.l.setdefault(key, []).append(value)
        return len(self.l[key])

    async def llen(self, key):
        return len(self.l.get(key, []))

    async def lmove(self, src, dst, whence, where):
        items = self.l.get(src) or []
        if not items:
            return None
        item = items.pop(0) if whence == "LEFT" else items.pop()
        self.l.setdefault(dst, []).append(item)
        return item

    async def scan_iter(self, match="*", count=100):
        prefix = match.rstrip("*")
        for key in list(self.l):
            if key.startswith(prefix):
                yield key


@pytest.fixture(autouse=True)
def _fresh_buckets():
    """Buckets are module-level; a test must not inherit another's tokens."""
    pools._buckets.clear()
    yield
    pools._buckets.clear()


# ── routing ─────────────────────────────────────────────────────────────────

def test_unbound_workshop_uses_the_shared_queue():
    """Every workshop that ran before pools existed has no binding, and small
    workshops deliberately never get dedicated machines. Both must keep working."""
    r = FakeRedis()
    assert asyncio.run(pools.target_queue(r, "ws_old", "amd64")) == "queue:test:amd64"


def test_self_service_session_never_lands_on_a_pool_queue():
    r = FakeRedis()
    assert asyncio.run(pools.target_queue(r, "", "amd64")) == "queue:test:amd64"


def test_bound_workshop_routes_to_its_pool():
    r = FakeRedis()

    async def go():
        await pools.bind_workshop_pool(r, "ws_bootcamp", "ws_bootcamp")
        return await pools.target_queue(r, "ws_bootcamp", "amd64")

    assert asyncio.run(go()) == "queue:pool:ws_bootcamp"


def test_pool_lookup_fails_open_to_the_shared_queue():
    """A workshop losing isolation is a degraded delivery. A workshop whose
    learners cannot be provisioned at all is a failed one -- so a Redis error
    must not block provisioning."""
    r = FakeRedis()
    r.fail_hget = True
    assert asyncio.run(pools.target_queue(r, "ws_x", "amd64")) == "queue:test:amd64"


def test_unbind_returns_workshop_to_shared_queue():
    r = FakeRedis()

    async def go():
        await pools.bind_workshop_pool(r, "ws_1", "ws_1")
        await pools.unbind_workshop_pool(r, "ws_1")
        return await pools.target_queue(r, "ws_1", "amd64")

    assert asyncio.run(go()) == "queue:test:amd64"


# ── pacing ──────────────────────────────────────────────────────────────────

def test_lone_learner_is_admitted_instantly():
    """The single most important pacing case.

    A self-service learner arriving at a quiet moment must not be taxed to
    solve a problem only bursts have. This is why the pacer is a token bucket
    and not a fixed timer.
    """
    r = FakeRedis()
    res = asyncio.run(pools.enqueue_paced(r, "queue:test:amd64", {"job_id": "a"}))
    assert res["admitted"] is True
    assert res["position"] == 0
    assert len(r.l["queue:test:amd64"]) == 1


def test_burst_beyond_the_bucket_is_parked_in_order():
    r = FakeRedis()

    async def go():
        return [await pools.enqueue_paced(r, "queue:test:amd64", {"job_id": str(i)})
                for i in range(10)]

    results = asyncio.run(go())
    admitted = [i for i, res in enumerate(results) if res["admitted"]]
    # PACE_BATCH tokens are available at rest; the rest queue.
    assert len(admitted) == pools.PACE_BATCH
    assert admitted == list(range(pools.PACE_BATCH)), "earliest arrivals go first"
    parked = r.l["queue:pending:queue:test:amd64"]
    assert len(parked) == 10 - pools.PACE_BATCH
    assert [p["position"] for p in results if not p["admitted"]] == \
        list(range(1, 10 - pools.PACE_BATCH + 1))


def test_a_waiting_queue_is_never_jumped():
    """Once anyone is waiting, a later arrival must queue behind them even if a
    token happens to be free -- otherwise learners are served out of order."""
    r = FakeRedis()

    async def go():
        for i in range(10):
            await pools.enqueue_paced(r, "queue:test:amd64", {"job_id": str(i)})
        pools._buckets["queue:test:amd64"] = pools.TokenBucket(5, 1)  # refill
        return await pools.enqueue_paced(r, "queue:test:amd64", {"job_id": "late"})

    res = asyncio.run(go())
    assert res["admitted"] is False


def test_drain_moves_in_arrival_order_and_respects_the_rate():
    r = FakeRedis()

    async def go():
        for i in range(10):
            await pools.enqueue_paced(r, "queue:test:amd64", {"job_id": str(i)})
        # Bucket is empty after the initial burst; give it exactly one batch.
        pools._buckets["queue:test:amd64"] = pools.TokenBucket(pools.PACE_BATCH, 3600)
        moved = await pools.drain_pending(r, "queue:test:amd64")
        return moved, r.l["queue:test:amd64"]

    moved, target = asyncio.run(go())
    assert moved == pools.PACE_BATCH
    ids = [__import__("json").loads(x)["job_id"] for x in target]
    assert ids == [str(i) for i in range(pools.PACE_BATCH * 2)], \
        "drained in arrival order, no reordering"


def test_drain_stops_cleanly_when_pending_is_empty():
    r = FakeRedis()
    assert asyncio.run(pools.drain_pending(r, "queue:test:amd64")) == 0


def test_pools_drip_independently():
    """A busy workshop must not throttle a self-service learner who is being
    scheduled onto entirely different machines."""
    r = FakeRedis()

    async def go():
        for i in range(10):
            await pools.enqueue_paced(r, "queue:pool:ws_1", {"job_id": str(i)})
        return await pools.enqueue_paced(r, "queue:test:amd64", {"job_id": "self-service"})

    assert asyncio.run(go())["admitted"] is True


def test_known_targets_finds_backlogs_after_a_restart():
    """Targets are discovered by scanning Redis, not remembered in memory, so a
    dashboard restart resumes draining a workshop it never saw created."""
    r = FakeRedis()

    async def go():
        for i in range(10):
            await pools.enqueue_paced(r, "queue:pool:ws_9", {"job_id": str(i)})
        return await pools.known_targets(r)

    assert "queue:pool:ws_9" in asyncio.run(go())


# ── the bucket itself ───────────────────────────────────────────────────────

def test_bucket_refills_over_time_not_on_a_timer():
    b = pools.TokenBucket(capacity=2, interval=1.0)
    assert b.take(5) == 2, "burst is capped at capacity"
    assert b.take(1) == 0, "empty until time passes"
    b._last -= 1.0                       # simulate a second elapsing
    assert b.take(5) == 2, "refills by elapsed wall time"


def test_bucket_never_exceeds_capacity_after_a_long_idle():
    b = pools.TokenBucket(capacity=2, interval=1.0)
    b._last -= 3600
    assert b.take(100) == 2, "an idle hour must not bank an hour of admissions"

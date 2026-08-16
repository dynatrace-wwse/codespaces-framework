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

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def scan_iter(self, match="*", count=100):
        prefix = match.rstrip("*")
        for key in list(self.l) + list(self.h):
            if key.startswith(prefix):
                yield key

    def add_workers(self, n, pool=pools.DAILY_POOL):
        """Register n live workers in a pool, so the pacer can rate off them."""
        for i in range(n):
            self.h[f"worker:w{pool}{i}"] = {"capacity": "20", "pool": pool,
                                            "role": "agent", "draining": "0"}


@pytest.fixture(autouse=True)
def _fresh_buckets():
    """Buckets and worker counts are module-level; a test must not inherit
    another's tokens or another's fleet size."""
    pools._buckets.clear()
    pools._worker_counts.clear()
    yield
    pools._buckets.clear()
    pools._worker_counts.clear()


# Burst available at rest for a single-worker queue -- the pacer's rate is
# derived per worker, so a test that hardcodes a number would silently stop
# testing the thing it names once the derivation changes.
BURST_1_WORKER = pools.rate_for_workers(1)[0]


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
    # One worker's burst is available at rest; the rest queue.
    assert len(admitted) == BURST_1_WORKER
    assert admitted == list(range(BURST_1_WORKER)), "earliest arrivals go first"
    parked = r.l["queue:pending:queue:test:amd64"]
    assert len(parked) == 10 - BURST_1_WORKER
    assert [p["position"] for p in results if not p["admitted"]] == \
        list(range(1, 10 - BURST_1_WORKER + 1))


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
        # A long interval means retune() cannot refill it mid-test.
        pools._buckets["queue:test:amd64"] = pools.TokenBucket(BURST_1_WORKER, 3600)
        moved = await pools.drain_pending(r, "queue:test:amd64")
        return moved, r.l["queue:test:amd64"]

    moved, target = asyncio.run(go())
    assert moved == BURST_1_WORKER
    ids = [__import__("json").loads(x)["job_id"] for x in target]
    assert ids == [str(i) for i in range(BURST_1_WORKER * 2)], \
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


# ── the rate scales with the fleet ──────────────────────────────────────────

def test_rate_is_per_worker_so_the_burst_each_volume_sees_is_constant():
    """The whole reason the rate is derived rather than fixed.

    A one-machine workshop and a five-machine bootcamp must put the SAME number
    of concurrent installs on each volume — that is what the 18-install disk
    ceiling is a property of. A fixed rate gets one of the two wrong: slow
    enough for five machines starves one, fast enough for one machine drowns
    all five.
    """
    per_min = lambda n: pools.rate_for_workers(n)[0] / pools.rate_for_workers(n)[1] * 60
    one, five = per_min(1), per_min(5)
    assert round(five / one, 6) == 5.0
    assert round(one, 4) == round(pools.PACE_PER_WORKER_PER_MIN, 4)


def test_rate_stays_under_the_measured_disk_ceiling():
    """1.5/min/worker x ~8 min per install is ~12 in flight — the largest N
    measured to finish comfortably (29 of 60 retries), with margin under the
    ~18 where a stock gp3 volume starts losing installs."""
    install_minutes = 8
    in_flight = pools.PACE_PER_WORKER_PER_MIN * install_minutes
    assert in_flight <= 12.5, "would exceed the comfortable measured N"
    assert in_flight < 18, "would exceed the measured failure point"


def test_a_lone_learner_is_still_admitted_instantly():
    """A fixed timer would tax the common case to fix the rare one."""
    r = FakeRedis()
    r.add_workers(1)
    res = asyncio.run(pools.enqueue_paced(r, "queue:test:amd64", {"job_id": "solo"}))
    assert res["admitted"] is True and res["position"] == 0


def test_a_bigger_pool_admits_more_at_rest():
    r = FakeRedis()
    r.add_workers(4)

    async def go():
        return [await pools.enqueue_paced(r, "queue:test:amd64", {"job_id": str(i)})
                for i in range(20)]

    admitted = [x for x in asyncio.run(go()) if x["admitted"]]
    assert len(admitted) == pools.rate_for_workers(4)[0]
    assert len(admitted) > BURST_1_WORKER


def test_worker_count_is_pool_scoped():
    """A workshop's machines must not speed up the daily queue, or the isolation
    the pool exists for leaks back in through the rate."""
    r = FakeRedis()
    r.add_workers(3, pool="ws-abc")
    r.add_workers(1)
    assert asyncio.run(pools.workers_serving(r, "queue:pool:ws-abc")) == 3
    assert asyncio.run(pools.workers_serving(r, "queue:test:amd64")) == 1


def test_a_worker_with_no_pool_field_counts_as_daily():
    """An older agent predates pools. It was serving daily before, and must keep
    being counted as daily during a rolling deploy."""
    r = FakeRedis()
    r.h["worker:old"] = {"capacity": "6", "role": "agent"}
    assert asyncio.run(pools.workers_serving(r, "queue:test:amd64")) == 1


def test_master_and_draining_workers_do_not_raise_the_rate():
    """Neither will take a new session, so neither may speed up admission."""
    r = FakeRedis()
    r.h["worker:master-arm64"] = {"capacity": "5", "role": "master"}
    r.h["worker:cordoned"] = {"capacity": "20", "role": "agent", "draining": "1"}
    r.add_workers(1)
    assert asyncio.run(pools.workers_serving(r, "queue:test:amd64")) == 1


def test_a_non_worker_key_does_not_inflate_the_rate():
    """`fleet:pressure` used to be `worker:pressure` and showed up in exactly
    this kind of scan as a worker called 'pressure'."""
    r = FakeRedis()
    r.h["worker:pressure"] = {"value": "3"}
    r.add_workers(2)
    assert asyncio.run(pools.workers_serving(r, "queue:test:amd64")) == 2


def test_redis_failure_slows_the_drip_but_never_stops_it():
    """A stopped drip looks to a trainer exactly like a hung fleet."""
    class Broken(FakeRedis):
        def scan_iter(self, match="*", count=100):
            raise RuntimeError("redis down")

    assert asyncio.run(pools.workers_serving(Broken(), "queue:test:amd64")) >= 1


def test_worker_count_is_cached_not_scanned_every_tick():
    r = FakeRedis()
    r.add_workers(2)
    calls = {"n": 0}
    inner = r.scan_iter

    def counting(match="*", count=100):
        calls["n"] += 1
        return inner(match=match, count=count)

    r.scan_iter = counting
    async def go():
        for _ in range(5):
            await pools.workers_serving(r, "queue:test:amd64")
    asyncio.run(go())
    assert calls["n"] == 1


def test_retune_does_not_hand_out_a_free_burst():
    """A worker leaving must not refill anyone's bucket."""
    b = pools.TokenBucket(8, 3600)
    b.take(8)
    b.retune(2, 3600)
    assert b.available == 0


def test_retune_keeps_earned_tokens_when_the_pool_grows():
    b = pools.TokenBucket(2, 3600)
    b.retune(8, 3600)
    assert b.available == 2, "tokens are kept, not reset, and not inflated"


def test_an_operator_can_still_pin_the_rate():
    """The derivation must be overridable — a capacity test needs a fixed rate."""
    saved = (pools.PACE_BATCH, pools.PACE_INTERVAL_S)
    try:
        pools.PACE_BATCH, pools.PACE_INTERVAL_S = 3, 90.0
        assert pools.rate_for_workers(1) == (3, 90.0)
        assert pools.rate_for_workers(9) == (3, 90.0)
    finally:
        pools.PACE_BATCH, pools.PACE_INTERVAL_S = saved


def test_a_wrongtype_key_costs_that_key_not_the_whole_count():
    """MEASURED 2026-08-14, and it made the fleet-scaled drip a no-op.

    `worker:{id}:app_ports_free` is a LIST. HGETALL on it raises WRONGTYPE,
    which aborted the entire scan, so every queue paced at the one-worker rate
    no matter how many machines were serving it. Benign in direction — it drips
    slower, never faster — which is exactly why nothing caught it.
    """
    class WithAList(FakeRedis):
        async def hgetall(self, key):
            if key.endswith(":app_ports_free"):
                raise RuntimeError(
                    "WRONGTYPE Operation against a key holding the wrong kind of value")
            return dict(self.h.get(key, {}))

    r = WithAList()
    r.add_workers(3)
    for i in range(3):
        r.l[f"worker:wdaily{i}:app_ports_free"] = ["32001", "32002"]
    assert asyncio.run(pools.workers_serving(r, "queue:test:amd64")) == 3


# ── fail-open must be visible, not just logged ──────────────────────────────

class _BrokenRedis:
    """Redis that cannot answer the pool lookup — the exact failure that makes
    routing fall back to the shared queue."""

    async def hget(self, *a, **kw):
        raise ConnectionError("redis is down")


def test_fail_open_is_counted_per_workshop():
    """A workshop delivered on the daily pool by a Redis blip looks identical to
    one delivered correctly. Counting the fallback is the only thing that makes
    the difference observable."""
    import asyncio
    before = pools.fail_open_counts().get("ws_counted", 0)
    got = asyncio.run(pools.pool_for_workshop(_BrokenRedis(), "ws_counted"))
    assert got == "", "must still fail OPEN — a workshop that cannot provision is worse"
    assert pools.fail_open_counts().get("ws_counted", 0) == before + 1


def test_the_counter_does_not_live_in_the_thing_that_broke():
    """It records Redis being unavailable, so storing it in Redis would leave it
    empty exactly when it matters."""
    import inspect
    src = inspect.getsource(pools.pool_for_workshop)
    assert "_FAIL_OPEN[ws_id]" in src
    assert "redis.incr" not in src and "redis.hincrby" not in src


def test_a_healthy_lookup_counts_nothing():
    import asyncio

    class _OkRedis:
        async def hget(self, *a, **kw):
            return "ws-abc"

    asyncio.run(pools.pool_for_workshop(_OkRedis(), "ws_healthy"))
    assert "ws_healthy" not in pools.fail_open_counts()

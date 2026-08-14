"""Tests for per-repo resource profiles.

  /home/ops/ops-venv/bin/python -m pytest dashboard/test_repo_profiles.py -q

The property that matters most: an unmeasured repo must be planned as HEAVY.
Every capacity number in this system has been wrong at least once, and each
time it was inferred rather than loaded — so the safe direction is a worker
running under capacity, never a workshop that oversells.
"""

import asyncio
import json

from dashboard import repo_profiles as rp


class FakeRedis:
    def __init__(self, values=None):
        self.h = {rp.PROFILE_KEY: dict(values or {})}
        self.fail = False

    async def hget(self, key, field):
        if self.fail:
            raise RuntimeError("redis down")
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value


def test_measured_repo_returns_its_measurement():
    p = asyncio.run(rp.load(FakeRedis(), "dynatrace-wwse/enablement-kubernetes-101"))
    assert p.steady_memory_mb == 1609
    assert p.estimated is False
    assert p.measured_on, "a measured profile must record when it was measured"


def test_unknown_repo_is_treated_as_heavy():
    p = asyncio.run(rp.load(FakeRedis(), "enablement-something-nobody-measured"))
    assert p.steady_memory_mb == rp.HEAVY_DEFAULT.steady_memory_mb
    assert p.estimated is True, "a guess must never be presented as a measurement"


def test_redis_override_wins_so_a_measurement_needs_no_deploy():
    r = FakeRedis({"enablement-kubernetes-101": json.dumps(
        {"steady_memory_mb": 2500, "steady_cpu": 0.2, "measured_on": "2026-08-14"})})
    p = asyncio.run(rp.load(r, "enablement-kubernetes-101"))
    assert p.steady_memory_mb == 2500


def test_malformed_override_falls_back_instead_of_partially_applying():
    """A half-read profile is how you oversell a box."""
    r = FakeRedis({"enablement-kubernetes-101": "{not json"})
    p = asyncio.run(rp.load(r, "enablement-kubernetes-101"))
    assert p.steady_memory_mb == 1609

    r2 = FakeRedis({"enablement-kubernetes-101": json.dumps({"steady_cpu": 0.2})})
    p2 = asyncio.run(rp.load(r2, "enablement-kubernetes-101"))
    assert p2.steady_memory_mb == 1609, "missing memory must not default to zero"


def test_redis_failure_falls_back_to_builtin():
    r = FakeRedis()
    r.fail = True
    p = asyncio.run(rp.load(r, "enablement-kubernetes-101"))
    assert p.steady_memory_mb == 1609


def test_owner_prefix_is_accepted():
    a = asyncio.run(rp.load(FakeRedis(), "enablement-kubernetes-101"))
    b = asyncio.run(rp.load(FakeRedis(), "dynatrace-wwse/enablement-kubernetes-101"))
    assert a.steady_memory_mb == b.steady_memory_mb


def test_heavier_profile_yields_fewer_seats():
    light = rp.seats_per_worker(rp.K8S_101, "m6a.4xlarge")
    heavy = rp.seats_per_worker(rp.HEAVY_DEFAULT, "m6a.4xlarge")
    assert heavy < light, "a heavier session must reduce the seat count"


def test_unknown_instance_type_yields_zero_not_a_guess():
    assert rp.seats_per_worker(rp.K8S_101, "nonsense.4xlarge") == 0
    assert rp.workers_for_seats(70, rp.K8S_101, "nonsense.4xlarge") == 0


def test_workers_for_seats_rounds_up():
    per = rp.seats_per_worker(rp.K8S_101, "m6a.4xlarge")
    assert rp.workers_for_seats(per, rp.K8S_101, "m6a.4xlarge") == 1
    assert rp.workers_for_seats(per + 1, rp.K8S_101, "m6a.4xlarge") == 2


def test_publish_round_trips():
    r = FakeRedis()
    measured = rp.RepoProfile("demo-astroshop-problems", 5200, 0.35,
                              measured_on="2026-08-14")
    asyncio.run(rp.publish(r, "demo-astroshop-problems", measured))
    back = asyncio.run(rp.load(r, "demo-astroshop-problems"))
    assert back.steady_memory_mb == 5200
    assert back.measured_on == "2026-08-14"

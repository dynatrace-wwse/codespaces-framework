"""Tests for the unit capacity model.

Run: /home/ops/ops-venv/bin/python -m dashboard.test_capacity_units
  or pytest dashboard/test_capacity_units.py

The properties asserted here are the ones a wrong answer would cost money or a
class, not the arithmetic itself: a bigger box is worth more, an unknown box is
worth nothing, an unmeasured training is priced heavy, and a plan always rounds
in the learner's favour.
"""

from __future__ import annotations

import asyncio
import unittest

from dashboard import capacity_units as cu


class TestInstanceUnits(unittest.TestCase):

    def test_measured_anchor(self):
        # 20 is the largest count observed to pass on this shape. If this
        # changes, it must be because someone ran the lab, not because the
        # arithmetic moved.
        self.assertEqual(cu.units_for_instance("m6a.4xlarge"), 20)
        self.assertTrue(cu.is_measured("m6a.4xlarge"))

    def test_four_xlarge_is_double_a_two_xlarge(self):
        """The property the whole model is built on."""
        for two, four in (("m6a.2xlarge", "m6a.4xlarge"),
                          ("m6i.2xlarge", "m6i.4xlarge"),
                          ("r6a.2xlarge", "r6a.4xlarge"),
                          ("c5.2xlarge", "c5.4xlarge")):
            self.assertEqual(cu.units_for_instance(four),
                             2 * cu.units_for_instance(two),
                             f"{four} should be worth two {two}")

    def test_unknown_instance_refuses_to_plan(self):
        self.assertEqual(cu.units_for_instance("bogus.9xlarge"), 0)
        self.assertEqual(cu.seats_per_instance("bogus.9xlarge", 1), 0)
        self.assertEqual(cu.instances_for_seats(30, "bogus.9xlarge", 1), 0)

    def test_derived_shape_is_flagged_not_measured(self):
        self.assertGreater(cu.units_for_instance("m6a.8xlarge"), 0)
        self.assertFalse(cu.is_measured("m6a.8xlarge"))

    def test_memory_clamp_only_ever_lowers(self):
        """The size-class scaling is an assumption; RAM is not."""
        for t in cu.INSTANCE_VCPUS:
            derived = int(cu.INSTANCE_VCPUS[t] * cu.UNITS_PER_VCPU)
            clamp = cu._memory_clamp(t)
            if clamp and t not in cu.INSTANCE_UNITS:
                self.assertLessEqual(cu.units_for_instance(t), derived)

    def test_compute_optimised_is_worth_less_than_general_purpose(self):
        """c5 is memory-starved for this workload — the model must say so."""
        self.assertLess(cu.units_for_instance("c5.2xlarge"),
                        cu.units_for_instance("m6a.2xlarge"))


class TestRepoUnits(unittest.TestCase):

    def test_reference_repo_is_one_unit(self):
        self.assertEqual(cu.units_for_repo_static(cu.UNIT_REFERENCE_REPO), 1)

    def test_training_id_resolves_to_repo(self):
        """A workshop stores 'kubernetes-101'; the repo is
        'enablement-kubernetes-101'. Missing this silently sized every workshop
        3x heavy."""
        self.assertEqual(cu.units_for_repo_static("kubernetes-101"), 1)
        self.assertEqual(cu.units_for_repo_static("dynatrace-wwse/kubernetes-101"), 1)
        self.assertEqual(
            cu.units_for_repo_static("https://github.com/wwse/kubernetes-101.git"), 1)

    def test_unprofiled_is_priced_heavy(self):
        self.assertEqual(cu.units_for_repo_static("brand-new-training"),
                         cu.UNPROFILED_UNITS)
        self.assertGreater(cu.UNPROFILED_UNITS, 1)

    def test_astroshop_costs_more_than_the_reference(self):
        self.assertGreater(cu.units_for_repo_static("demo-astroshop-problems"), 1)


class TestPlanning(unittest.TestCase):

    def test_seats_divide_cleanly(self):
        self.assertEqual(cu.seats_per_instance("m6a.4xlarge", 1), 20)
        self.assertEqual(cu.seats_per_instance("m6a.4xlarge", 3), 6)   # 20//3
        self.assertEqual(cu.seats_per_instance("m6a.2xlarge", 3), 3)   # 10//3

    def test_a_machine_never_holds_a_fraction_of_a_session(self):
        for units in range(1, 8):
            seats = cu.seats_per_instance("m6a.4xlarge", units)
            self.assertLessEqual(seats * units, cu.units_for_instance("m6a.4xlarge"))

    def test_plans_round_up_and_add_a_spare(self):
        # 21 seats of a 1-unit training does not fit on one 20-unit box.
        self.assertEqual(cu.instances_for_seats(21, "m6a.4xlarge", 1), 3)   # 2 + spare
        self.assertEqual(cu.instances_for_seats(20, "m6a.4xlarge", 1), 2)   # 1 + spare
        self.assertEqual(cu.instances_for_seats(1, "m6a.4xlarge", 1), 2)

    def test_redundancy_can_be_declined(self):
        self.assertEqual(cu.instances_for_seats(20, "m6a.4xlarge", 1, redundancy=0), 1)

    def test_zero_seats_buys_nothing(self):
        self.assertEqual(cu.instances_for_seats(0, "m6a.4xlarge", 1), 0)

    def test_heavier_training_needs_more_machines(self):
        light = cu.instances_for_seats(30, "m6a.4xlarge", 1)
        heavy = cu.instances_for_seats(30, "m6a.4xlarge", 3)
        self.assertGreater(heavy, light)


class TestSlotCap(unittest.TestCase):

    def test_cap_clears_astroshops_declared_limits(self):
        """6,320 MiB of declared pod limits — the reason 4096 was wrong."""
        astro = cu.units_for_repo_static("demo-astroshop-problems")
        self.assertGreater(cu.slot_memory_cap_mb(astro), 6320)

    def test_cap_is_above_the_reservation_not_at_it(self):
        """A limit at the request turns every transient spike into an OOM kill."""
        self.assertGreater(cu.slot_memory_cap_mb(1), cu.UNIT_MEMORY_MB)

    def test_cap_never_drops_below_the_floor(self):
        self.assertGreaterEqual(cu.slot_memory_cap_mb(1), cu.SLOT_CAP_FLOOR_MB)


class _FakeRedis:
    def __init__(self, data=None):
        self.data = data or {}

    async def hget(self, key, field):
        return self.data.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.data.setdefault(key, {})[field] = value


class TestRedisOverride(unittest.TestCase):

    def test_override_wins_so_a_measurement_needs_no_deploy(self):
        r = _FakeRedis({cu.UNITS_KEY: {"enablement-kubernetes-101": '{"units": 2}'}})
        self.assertEqual(asyncio.run(cu.units_for_repo(r, "kubernetes-101")), 2)

    def test_bare_integer_override_is_accepted(self):
        r = _FakeRedis({cu.UNITS_KEY: {"demo-astroshop-problems": "5"}})
        self.assertEqual(
            asyncio.run(cu.units_for_repo(r, "demo-astroshop-problems")), 5)

    def test_malformed_override_falls_through_to_the_table(self):
        """A half-read profile is how you oversell a box — and a 0 here would be
        a division by zero in a sizing path."""
        for bad in ("not json {", '{"units": 0}', '{"units": -3}', '{"nope": 1}'):
            r = _FakeRedis({cu.UNITS_KEY: {"enablement-kubernetes-101": bad}})
            self.assertEqual(asyncio.run(cu.units_for_repo(r, "kubernetes-101")), 1,
                             f"override {bad!r} should have been ignored")

    def test_redis_failure_does_not_break_sizing(self):
        class Broken:
            async def hget(self, *a):
                raise RuntimeError("redis is down")
        self.assertEqual(asyncio.run(cu.units_for_repo(Broken(), "kubernetes-101")), 1)

    def test_no_redis_at_all(self):
        self.assertEqual(asyncio.run(cu.units_for_repo(None, "kubernetes-101")), 1)

    def test_publish_refuses_a_nonsense_measurement(self):
        with self.assertRaises(ValueError):
            asyncio.run(cu.publish_units(_FakeRedis(), "x", 0))


class TestDescribe(unittest.TestCase):

    def test_describe_admits_what_it_does_not_know(self):
        row = cu.describe("m6a.8xlarge", "brand-new-training", cu.UNPROFILED_UNITS)
        self.assertFalse(row["instance_measured"])
        self.assertFalse(row["repo_measured"])
        self.assertEqual(row["repo_units"], cu.UNPROFILED_UNITS)

    def test_describe_of_the_known_case(self):
        row = cu.describe("m6a.4xlarge", "enablement-kubernetes-101", 1)
        self.assertTrue(row["instance_measured"])
        self.assertTrue(row["repo_measured"])
        self.assertEqual(row["seats"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)

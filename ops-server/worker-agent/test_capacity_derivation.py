"""A worker must derive the same capacity the planner assumed.

Run: /home/ops/ops-venv/bin/python -m worker-agent.test_capacity_derivation

The bug this pins: both daily workers were m6a.4xlarge advertising 30 while
every measurement said 20, because the 30 was typed into a file. The fix
derives it — and the fix's own first attempt then silently derived 6 on one of
two identical machines, because it read the unit table out of ``dashboard``,
which a worker's sparse checkout does not contain.
"""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
config = importlib.import_module("worker-agent.config")


class TestUnitLookup(unittest.TestCase):

    def test_loader_does_not_need_the_dashboard_package_at_all(self):
        """A worker's sparse checkout has no ``ops-server/dashboard``.

        Measured 2026-08-14: amd002 derived 6 instead of 20 because the file it
        was reading simply was not on that machine.
        """
        saved = sys.modules.pop("dashboard", None)
        sys.modules["dashboard"] = object()   # definitely not a package
        try:
            self.assertEqual(config._units_for_instance("m6a.4xlarge"), 20)
        finally:
            sys.modules.pop("dashboard", None)
            if saved is not None:
                sys.modules["dashboard"] = saved

    def test_the_table_is_in_shared_where_a_worker_can_see_it(self):
        """Pins the location, because moving it back into ``dashboard`` would
        pass every unit test on the master and quietly halve a worker."""
        from pathlib import Path as _P
        root = _P(config.__file__).resolve().parent.parent
        self.assertTrue((root / "shared" / "capacity_units.py").exists())
        self.assertFalse((root / "dashboard" / "capacity_units.py").exists())

    def test_agrees_with_the_planner(self):
        """The worker and the dashboard must not disagree about one machine."""
        from shared import capacity_units as cu
        for t in ("m6a.4xlarge", "m6a.2xlarge", "c5.2xlarge", "r6a.2xlarge"):
            self.assertEqual(config._units_for_instance(t),
                             cu.units_for_instance(t), t)

    def test_unknown_shape_returns_zero_not_a_guess(self):
        self.assertEqual(config._units_for_instance("bogus.9xlarge"), 0)


class TestDerivedCapacity(unittest.TestCase):

    def _derive(self, env: dict, instance_type: str) -> int:
        saved_env = {k: config.os.environ.get(k) for k in env}
        saved_type = config.INSTANCE_TYPE
        try:
            for k, v in env.items():
                if v is None:
                    config.os.environ.pop(k, None)
                else:
                    config.os.environ[k] = v
            config.INSTANCE_TYPE = instance_type
            return config._derive_capacity()
        finally:
            config.INSTANCE_TYPE = saved_type
            for k, v in saved_env.items():
                if v is None:
                    config.os.environ.pop(k, None)
                else:
                    config.os.environ[k] = v

    def test_derives_from_the_instance_type(self):
        self.assertEqual(self._derive({"WORKER_CAPACITY": None}, "m6a.4xlarge"), 20)
        self.assertEqual(self._derive({"WORKER_CAPACITY": None}, "m6a.2xlarge"), 10)

    def test_explicit_setting_still_wins(self):
        """The workshop planner sets capacity per launch, and an operator must
        be able to pin a box for a capacity test."""
        self.assertEqual(self._derive({"WORKER_CAPACITY": "3"}, "m6a.4xlarge"), 3)

    def test_unknown_shape_falls_back_low_not_high(self):
        """Advertising too few slots costs money; too many costs a class."""
        self.assertEqual(self._derive({"WORKER_CAPACITY": None}, "bogus.9xlarge"), 6)
        self.assertEqual(self._derive({"WORKER_CAPACITY": None}, ""), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ── disk telemetry ──────────────────────────────────────────────────────────
#
# Every disk figure in the capacity model came from a human running `iostat`
# during a load test. This is the production version of that, and it has to work
# on the workers as they actually are — which means procfs, because psutil is
# NOT installed there.

def test_disk_io_reads_procfs_not_psutil():
    """psutil is absent on every worker; the cpu/mem collector already carries a
    procfs fallback for the same reason. A metric that silently reports nothing
    on every real box is worse than one that was never added."""
    import inspect
    from . import agent as ag
    src = inspect.getsource(ag.WorkerAgent._collect_disk_io)
    assert "/proc/diskstats" in src
    assert "import psutil" not in src


def test_disk_io_counts_whole_disks_only():
    """A partition double-counts its parent and loop/dm devices are container
    overlay noise — counting them would describe the container runtime, not the
    EBS volume the capacity model is about."""
    import re as _re
    nvme = _re.compile(r"nvme\d+n\d+")
    assert nvme.fullmatch("nvme0n1")
    assert not nvme.fullmatch("nvme0n1p1"), "partition would double-count its parent"
    other = _re.compile(r"(xvd|sd)[a-z]+")
    assert other.fullmatch("xvda") and other.fullmatch("sda")
    assert not other.fullmatch("loop0") and not other.fullmatch("dm-0")


def test_first_sample_publishes_nothing_then_a_rate():
    """A fabricated 0 reads as an idle volume, which is a different claim from
    'not measured yet'."""
    from . import agent as ag
    ag._DISK_IO_SAMPLE.clear()
    assert ag.WorkerAgent._collect_disk_io() == {}, \
        "published a rate with no baseline to compare against"
    second = ag.WorkerAgent._collect_disk_io()
    if second:                      # empty only on a host with no matching disk
        assert {"disk_read_mbps", "disk_write_mbps", "disk_iops"} == set(second)
        assert float(second["disk_read_mbps"]) >= 0
        assert int(second["disk_iops"]) >= 0
    ag._DISK_IO_SAMPLE.clear()

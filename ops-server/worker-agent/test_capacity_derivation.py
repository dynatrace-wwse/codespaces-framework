"""A worker must derive the same capacity the planner assumed.

Run: /home/ops/ops-venv/bin/python -m worker-agent.test_capacity_derivation

The bug this pins: both daily workers were m6a.4xlarge advertising 30 while
every measurement said 20, because the 30 was typed into a file. The fix
derives it — and the fix's own first attempt then silently derived 6 on one of
two identical machines, because it reached the unit table through a package
import whose availability depended on how the agent was started.
"""

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
config = importlib.import_module("worker-agent.config")


class TestUnitLookup(unittest.TestCase):

    def test_loader_does_not_depend_on_the_dashboard_package_being_importable(self):
        """Loading by file path, not by package name.

        Measured 2026-08-14: ``import dashboard.capacity_units`` raised
        ModuleNotFoundError on amd002 and succeeded on amd001, same instance
        type, same service, same command line.
        """
        saved = sys.modules.pop("dashboard", None)
        blocker = object()   # something that is definitely not a package
        sys.modules["dashboard"] = blocker
        try:
            self.assertEqual(config._units_for_instance("m6a.4xlarge"), 20)
        finally:
            sys.modules.pop("dashboard", None)
            if saved is not None:
                sys.modules["dashboard"] = saved

    def test_agrees_with_the_planner(self):
        """The worker and the dashboard must not disagree about one machine."""
        from dashboard import capacity_units as cu
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

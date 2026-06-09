"""Weighted scheduler + lane routing for the worker agent.

Pure, dependency-free logic (asyncio only) so it can be unit-tested without
Redis or Docker.

Why this exists
---------------
The worker used to gate concurrency with a flat ``Semaphore(WORKER_CAPACITY)``
and a pool of identical Sysbox slots: every job cost exactly one slot, whether
it was a static shell unit test (``bats``, <0.5 GB) or a full Dynatrace
CloudNativeFullStack run (``dt-cnfs`` — Operator + ActiveGate + k3d, ~3-4 GB).
That meant the worker either ran too few slots (wasting capacity on light
jobs) or oversubscribed and OOMed when several heavy jobs landed together.

Two independent gates now sit in front of the physical slot:

1. **Cost budget** — each job has an integer *cost* (light=1, medium=2,
   heavy=4). Jobs are admitted while the sum of in-flight costs stays within
   ``budget``. Heavy jobs reserve more of the budget, so fewer run at once,
   while cheap jobs keep filling the remaining headroom — dense packing
   without OOM.

2. **Heavy lane** — jobs classified ``heavy`` additionally need a permit from a
   small lane of ``max_heavy`` concurrent slots. This stops several
   memory-hungry jobs from co-scheduling even when the cost budget alone would
   allow it. Light jobs never touch this lane, so they are never blocked by it.

Both reservations are released when the job finishes — any status (ok, fail,
timeout, terminated) — by the caller's ``finally`` block.

The physical Sysbox slot pool (``WORKER_CAPACITY`` slots) remains the hard cap
on simultaneous jobs; this scheduler only decides *which* queued jobs are
allowed to claim a slot next.
"""

from __future__ import annotations

import asyncio

# Cost tiers.
COST_LIGHT = 1
COST_MEDIUM = 2
COST_HEAVY = 4

# A job at or above this cost is treated as heavy-lane traffic.
HEAVY_THRESHOLD = COST_HEAVY

# Known framework suites → cost. Suite id is carried on the job as job["suite"].
SUITE_COST = {
    "bats": COST_LIGHT,            # static shell unit tests, no cluster
    "engines": COST_MEDIUM,        # k3d (+ kind) engine bring-up
    "k3d-apps": COST_MEDIUM,       # demo apps on k3d
    "dt-apponly": COST_MEDIUM,     # Operator + ActiveGate + CSI app injection
    "k3d-aitraveladvisor": COST_MEDIUM,
    "dt-cnfs": COST_HEAVY,         # CNFS: Operator + ActiveGate + k3d, ~3-4 GB
}

# Suites that must run in the constrained heavy lane regardless of cost lookups.
HEAVY_SUITES = {"dt-cnfs"}

# Fallback cost by job type when no suite is present (e.g. PR / nightly repo tests).
TYPE_COST = {
    "framework-test": COST_MEDIUM,
    "integration-test": COST_MEDIUM,
    "daemon": COST_MEDIUM,
}


def cost_of(job: dict) -> int:
    """Resolve a job's cost.

    Precedence: explicit ``job["cost"]`` → suite table → type table → light.
    Always at least 1.
    """
    explicit = job.get("cost")
    if explicit is not None:
        try:
            return max(1, int(explicit))
        except (TypeError, ValueError):
            pass
    suite = job.get("suite") or job.get("framework_suite")
    if suite in SUITE_COST:
        return SUITE_COST[suite]
    return TYPE_COST.get(job.get("type"), COST_LIGHT)


def lane_of(job: dict) -> str:
    """Classify a job into the ``"heavy"`` or ``"light"`` lane.

    Precedence: explicit ``job["lane"]`` → known heavy suite → ``requires_native``
    flag → cost at/above ``HEAVY_THRESHOLD`` → light.
    """
    explicit = job.get("lane")
    if explicit in ("heavy", "light"):
        return explicit
    suite = job.get("suite") or job.get("framework_suite")
    if suite in HEAVY_SUITES:
        return "heavy"
    if job.get("requires_native"):
        return "heavy"
    return "heavy" if cost_of(job) >= HEAVY_THRESHOLD else "light"


def classify(job: dict) -> tuple[int, str]:
    """Return ``(cost, lane)`` for a job — convenience for logging."""
    return cost_of(job), lane_of(job)


class WeightedScheduler:
    """Admission control by cost budget + a bounded heavy lane.

    Not tied to Redis/Docker — callers ``await acquire(job)`` before claiming a
    physical slot and ``await release(job)`` in their ``finally``.
    """

    def __init__(self, budget: int, max_heavy: int) -> None:
        # budget must allow at least one heavy job to ever run.
        self.budget = max(int(budget), COST_HEAVY)
        # at least one heavy permit, else heavy jobs would deadlock forever.
        self.max_heavy = max(int(max_heavy), 1)
        self._in_flight_cost = 0
        self._heavy_in_flight = 0
        self._cond = asyncio.Condition()

    @classmethod
    def from_capacity(
        cls,
        capacity: int,
        budget: int | None = None,
        max_heavy: int | None = None,
    ) -> "WeightedScheduler":
        """Derive sensible defaults from the worker's slot capacity.

        - ``budget``    defaults to ``capacity * COST_MEDIUM`` (light jobs can
          still fill every slot; heavy jobs are throttled by weight).
        - ``max_heavy`` defaults to ``capacity // 3`` (at least 1) — roughly one
          heavy lane per three slots.
        """
        capacity = max(int(capacity), 1)
        if budget is None:
            budget = capacity * COST_MEDIUM
        if max_heavy is None:
            max_heavy = max(1, capacity // 3)
        return cls(budget, max_heavy)

    # ── pure predicate (unit-testable without awaiting) ──────────────────────
    def can_admit(self, job: dict) -> bool:
        """Whether ``job`` could be admitted right now.

        Heavy-lane capacity is always enforced. Cost budget is enforced unless
        nothing is in flight, in which case a single job is always admitted so
        an oversized job (cost > budget) can still make progress alone.
        """
        if lane_of(job) == "heavy" and self._heavy_in_flight >= self.max_heavy:
            return False
        if self._in_flight_cost == 0:
            return True
        return self._in_flight_cost + cost_of(job) <= self.budget

    # ── async admission ──────────────────────────────────────────────────────
    async def acquire(self, job: dict) -> None:
        """Block until ``job`` can be admitted, then reserve its cost + lane."""
        async with self._cond:
            await self._cond.wait_for(lambda: self.can_admit(job))
            self._in_flight_cost += cost_of(job)
            if lane_of(job) == "heavy":
                self._heavy_in_flight += 1

    async def release(self, job: dict) -> None:
        """Release ``job``'s reservation and wake any waiters."""
        async with self._cond:
            self._in_flight_cost = max(0, self._in_flight_cost - cost_of(job))
            if lane_of(job) == "heavy":
                self._heavy_in_flight = max(0, self._heavy_in_flight - 1)
            self._cond.notify_all()

    def stats(self) -> dict:
        """Snapshot for heartbeat/observability."""
        return {
            "sched_budget": self.budget,
            "sched_in_flight_cost": self._in_flight_cost,
            "sched_max_heavy": self.max_heavy,
            "sched_heavy_in_flight": self._heavy_in_flight,
        }

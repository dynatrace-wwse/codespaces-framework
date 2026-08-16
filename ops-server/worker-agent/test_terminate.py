"""Tests for the terminate (kill-job) container resolution + rm verification.

Pure logic — no Redis/Docker — so it runs anywhere.

Runnable two ways (mirrors test_scheduler.py / test_daemon_status.py):
  - pytest:     /home/ops/ops-venv/bin/python -m pytest worker-agent/test_terminate.py
  - standalone: cd ops-server && /home/ops/ops-venv/bin/python -m worker-agent.test_terminate

Regression contract for the "Terminate did nothing" bug (job enablement-396267dba477):
a slot-pooled daemon's real container is ``sb-slot-{worker}-{i}``, NOT
``sb-{job_id}``. The old kill targeted ``sb-{job_id}`` which does not exist, and
``docker rm -f`` exits 0 on a missing container, so the no-op looked successful
while the daemon kept running. These tests pin both halves of the fix.
"""

from .agent import _terminate_candidate_names, _docker_rm_removed


# ── docker rm verification (the silent-success bug) ──────────────────────────

def test_rm_removed_on_clean_exit():
    assert _docker_rm_removed(0, "") is True
    assert _docker_rm_removed(0, None) is True


def test_rm_not_removed_when_container_missing():
    # `docker rm -f <missing>` exits 0 but prints this to stderr — the exact
    # silent no-op that let terminated daemons leak. Must be treated as a miss.
    err = "Error response from daemon: No such container: sb-enablement-396267dba477"
    assert _docker_rm_removed(0, err) is False


def test_rm_not_removed_on_nonzero_exit():
    assert _docker_rm_removed(1, "some other docker error") is False


# ── container name resolution ────────────────────────────────────────────────

def test_slot_name_takes_precedence():
    # Real slot container is tried first; legacy sb-{job_id} is last resort.
    names = _terminate_candidate_names(
        "enablement-396267dba477", slot_sb_name="sb-slot-amd001-4")
    assert names == ["sb-slot-amd001-4", "sb-enablement-396267dba477"]


def test_redis_sb_name_used_when_no_slot():
    # Post-restart orphan: no in-memory slot, resolve via persisted sb_name.
    names = _terminate_candidate_names(
        "enablement-396267dba477", slot_sb_name=None, redis_sb_name="sb-slot-amd001-4")
    assert names == ["sb-slot-amd001-4", "sb-enablement-396267dba477"]


def test_order_slot_then_redis_then_fallback():
    names = _terminate_candidate_names(
        "jobx", slot_sb_name="sb-slot-amd001-1", redis_sb_name="sb-slot-amd001-2")
    assert names == ["sb-slot-amd001-1", "sb-slot-amd001-2", "sb-jobx"]


def test_dedup_when_redis_equals_fallback():
    names = _terminate_candidate_names("jobx", redis_sb_name="sb-jobx")
    assert names == ["sb-jobx"]


def test_fallback_only_when_nothing_known():
    assert _terminate_candidate_names("jobx") == ["sb-jobx"]


def test_long_job_id_fallback_uses_last_32():
    jid = "a" * 40
    names = _terminate_candidate_names(jid)
    assert names == ["sb-" + "a" * 32]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


# ── staggered teardown ──────────────────────────────────────────────────────
# The reaping bug has never reproduced on a single terminate; it took ~30
# simultaneous `docker rm -fv` on nested Sysbox containers. The reaper makes a
# wedged teardown survivable, staggering makes it rare, and both are needed:
# staggering alone leaves the bug latent for the next Docker hiccup, and the
# reaper alone means wedging the daemon on every workshop teardown.

import asyncio

from . import agent as agent_mod


class _StaggerAgent:
    """Minimal stand-in exposing only what _kill_staggered touches."""

    def __init__(self):
        self.killed: list[str] = []
        self.peak = 0
        self._live = 0

    async def _kill_job_container(self, job_id, rec=None):
        self._live += 1
        self.peak = max(self.peak, self._live)
        await asyncio.sleep(0)
        self.killed.append(job_id)
        self._live -= 1
        return True

    _kill_staggered = agent_mod.WorkerAgent._kill_staggered


def test_teardown_never_fires_every_kill_at_once(monkeypatch):
    monkeypatch.setattr(agent_mod, "TEARDOWN_CONCURRENCY", 4)
    monkeypatch.setattr(agent_mod, "TEARDOWN_STAGGER_S", 0)
    a = _StaggerAgent()
    asyncio.run(a._kill_staggered([f"job{i}" for i in range(30)]))
    assert len(a.killed) == 30, "every job must still be terminated"
    assert a.peak <= 4, f"fired {a.peak} kills at once, cap is 4"


def test_one_stuck_container_does_not_block_the_rest(monkeypatch):
    """A container refusing to die must not stop the other twenty-nine from
    being asked -- that would turn one bad slot into a stalled teardown."""
    monkeypatch.setattr(agent_mod, "TEARDOWN_CONCURRENCY", 2)
    monkeypatch.setattr(agent_mod, "TEARDOWN_STAGGER_S", 0)
    a = _StaggerAgent()

    async def _boom(job_id, rec=None):
        if job_id == "job3":
            raise RuntimeError("did not receive an exit event")
        a.killed.append(job_id)
        return True

    a._kill_job_container = _boom
    asyncio.run(a._kill_staggered([f"job{i}" for i in range(10)]))
    assert len(a.killed) == 9
    assert "job3" not in a.killed


def test_empty_teardown_is_a_noop():
    a = _StaggerAgent()
    asyncio.run(a._kill_staggered([]))
    assert a.killed == []

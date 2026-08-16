"""Tests for the container reaper.

  /home/ops/ops-venv/bin/python -m pytest worker-agent/test_reaper.py -q \
      --import-mode=importlib

The measured failure these pin down (amd001, 2026-08-13): a mass teardown made
``docker rm -fv`` fail with "did not receive an exit event", so the per-job
``docker wait`` never returned, ``active_jobs`` never shrank, and the worker
advertised zero free slots while holding thirty healthy ones for twenty
minutes. Every test below is one property that failure violated.
"""

import asyncio

import pytest

from .reaper import ContainerReaper, EXITED, ABANDONED, STOPPED


def run(coro):
    return asyncio.run(coro)


class FakeDocker:
    def __init__(self, running=()):
        self.running = set(running)
        self.kill_calls: list[tuple[str, int]] = []
        # Models the actual bug: rm reports failure and the container survives.
        self.kills_fail = False
        self.list_error = False

    async def list_running(self):
        if self.list_error:
            raise RuntimeError("docker daemon unreachable")
        return list(self.running)

    async def kill(self, cid, attempt):
        self.kill_calls.append((cid, attempt))
        if self.kills_fail:
            return False
        self.running.discard(cid)
        return True


def test_watcher_resolves_when_the_container_is_gone():
    async def go():
        d = FakeDocker(running=["abc123"])
        r = ContainerReaper(d.list_running, d.kill)
        fut = r.watch("abc123")
        assert not fut.done(), "still running — must not resolve"
        d.running.discard("abc123")
        await r.tick()
        return fut

    assert run(go()).result() == EXITED


def test_watch_refuses_a_name():
    async def go():
        d = FakeDocker()
        r = ContainerReaper(d.list_running, d.kill)
        with pytest.raises(ValueError):
            r.watch("")
    run(go())


def test_a_failing_kill_still_frees_the_job():
    """THE regression test for the outage.

    Docker refuses to admit the container died, exactly as observed. The job
    must still exit, and must be told the slot is unusable so the pool rebuilds
    it -- losing one slot instead of stranding the worker.
    """
    async def go():
        d = FakeDocker(running=["stuck1"])
        d.kills_fail = True
        r = ContainerReaper(d.list_running, d.kill, max_kill_attempts=3)
        fut = r.watch("stuck1")
        r.request_terminate("stuck1")
        for _ in range(3):
            await r.tick()
        return fut, d

    fut, d = run(go())
    assert fut.done(), "a job blocked on a failing kill is the whole bug"
    assert fut.result() == ABANDONED
    assert [a for _, a in d.kill_calls] == [1, 2, 3], "escalating attempts"


def test_kill_is_retried_before_giving_up():
    """Give-up must be a last resort: a container that dies on the second
    attempt should be reported as a clean exit, not abandoned."""
    async def go():
        d = FakeDocker(running=["c1"])
        d.kills_fail = True
        r = ContainerReaper(d.list_running, d.kill, max_kill_attempts=4)
        fut = r.watch("c1")
        r.request_terminate("c1")
        await r.tick()                 # attempt 1 fails
        d.kills_fail = False
        await r.tick()                 # attempt 2 succeeds, container goes away
        await r.tick()                 # noticed gone
        return fut

    assert run(go()).result() == EXITED


def test_terminate_intent_before_watch_still_works():
    """Order must not matter: a terminate can race ahead of the watch."""
    async def go():
        d = FakeDocker(running=["c1"])
        d.kills_fail = True
        r = ContainerReaper(d.list_running, d.kill, max_kill_attempts=1)
        r.request_terminate("c1")
        fut = r.watch("c1")
        await r.tick()
        return fut

    assert run(go()).result() == ABANDONED


def test_a_failed_listing_resolves_nothing():
    """If we cannot see the truth, do nothing.

    Resolving on a failed listing would report every live session as dead --
    a worse outage than the one being fixed.
    """
    async def go():
        d = FakeDocker(running=["c1"])
        d.list_error = True
        r = ContainerReaper(d.list_running, d.kill)
        fut = r.watch("c1")
        await r.tick()
        return fut

    assert not run(go()).done()


def test_untargeted_containers_are_never_killed():
    """The reaper kills only what a terminate was requested for. A healthy
    session must survive any number of ticks."""
    async def go():
        d = FakeDocker(running=["healthy"])
        r = ContainerReaper(d.list_running, d.kill)
        r.watch("healthy")
        for _ in range(5):
            await r.tick()
        return d.kill_calls

    assert run(go()) == []


def test_one_listing_serves_every_watcher():
    """Cost is one docker call per tick regardless of session count -- the
    property that makes this cheaper than thirty blocked processes."""
    calls = {"n": 0}

    async def list_running():
        calls["n"] += 1
        return []

    async def kill(cid, attempt):
        return True

    async def go():
        r = ContainerReaper(list_running, kill)
        for i in range(30):
            r.watch(f"c{i}")
        await r.tick()
        return calls["n"]

    assert run(go()) == 1


def test_stop_never_leaves_a_coroutine_blocked():
    async def go():
        d = FakeDocker(running=["c1"])
        r = ContainerReaper(d.list_running, d.kill)
        fut = r.watch("c1")
        await r.stop()
        return fut

    assert run(go()).result() == STOPPED


def test_forget_drops_all_state_for_a_container():
    async def go():
        d = FakeDocker(running=["c1"])
        r = ContainerReaper(d.list_running, d.kill)
        r.watch("c1")
        r.request_terminate("c1")
        r.forget("c1")
        assert r.watched_count == 0
        await r.tick()
        return d.kill_calls

    assert run(go()) == [], "a forgotten container must not still be killed"

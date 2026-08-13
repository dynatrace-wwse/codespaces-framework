"""Tests for recycled-slot workspace hygiene (2026-08-13 15-session load test).

Pure logic — no Redis/Docker — so it runs anywhere.

Runnable two ways (mirrors test_slot_reinit.py):
  - pytest:     /home/ops/ops-venv/bin/python -m pytest worker-agent/test_slot_workspace.py \
                    --import-mode=importlib
  - standalone: cd ops-server && /home/ops/ops-venv/bin/python -m worker-agent.test_slot_workspace

Regression contract: provisioning 15 sessions onto a worker whose 30 slots had
just been recycled failed 3 times (20%) with

    git clone ... failed (rc=128): fatal: destination path
    '/home/ops/workdir/slots/21/workspace/enablement-kubernetes-101'
    already exists and is not an empty directory.

Two defects combined. ``release(healthy=True)`` wiped inner docker state but
never the workspace, so the next job's pre-clone ``rm -rf`` was the only
cleanup — and it discarded the rm's exit code and passed ``ignore_errors=True``
to rmtree, so a failed clean was invisible until the clone. The rm itself races
the previous inner ``dt`` container's bind-mount teardown (EBUSY on the
mountpoint). Now: release empties the workspace while it still owns the slot,
and the executor verifies emptiness (with bounded retries) before cloning.
"""

import asyncio
from pathlib import Path

from . import executor
from .executor import SlotWorkspaceDirty, _clear_slot_repo_dir


class FakeSlot:
    def __init__(self, tmp: Path, index: int = 21):
        self.index = index
        self.sb_name = f"sb-slot-amd001-{index}"
        self.workspace = tmp


class FakeProc:
    """Stands in for asyncio.create_subprocess_exec's return value."""

    def __init__(self, stdout: bytes = b"", hang: bool = False):
        self._stdout = stdout
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            # A never-resolving future, NOT asyncio.sleep — tests that neuter
            # sleep would otherwise turn a "hung" child into an instant return.
            await asyncio.get_running_loop().create_future()
        return self._stdout, b""

    def kill(self):
        self.killed = True
        self._hang = False

    async def wait(self):
        return 0


def _patch_exec(monkey_outputs, calls=None):
    """Return a create_subprocess_exec stub yielding the given outputs in order."""
    seq = list(monkey_outputs)

    async def fake_exec(*args, **kwargs):
        if calls is not None:
            calls.append(args)
        return seq.pop(0) if seq else FakeProc(b"")

    return fake_exec


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── the happy path ───────────────────────────────────────────────────────────

def test_empty_listing_clears_on_first_attempt(tmp_path):
    calls = []
    orig = executor.asyncio.create_subprocess_exec
    executor.asyncio.create_subprocess_exec = _patch_exec([FakeProc(b"")], calls)
    try:
        slot = FakeSlot(tmp_path)
        repo_dir = _run(_clear_slot_repo_dir(slot, "enablement-kubernetes-101"))
    finally:
        executor.asyncio.create_subprocess_exec = orig
    assert repo_dir == tmp_path / "enablement-kubernetes-101"
    assert repo_dir.is_dir() and not any(repo_dir.iterdir())
    assert len(calls) == 1, "a clean slot must not pay for retries"


def test_directory_is_created_when_absent(tmp_path):
    orig = executor.asyncio.create_subprocess_exec
    executor.asyncio.create_subprocess_exec = _patch_exec([FakeProc(b"")])
    try:
        repo_dir = _run(_clear_slot_repo_dir(FakeSlot(tmp_path), "repo-x"))
    finally:
        executor.asyncio.create_subprocess_exec = orig
    assert repo_dir.is_dir()


# ── the race we actually measured ────────────────────────────────────────────

def test_busy_mount_clears_on_retry(tmp_path):
    # First rm leaves the bind-mounted directory behind (EBUSY); the second,
    # after the mount has gone, comes back empty. This is the 3-of-15 case.
    calls = []
    orig = executor.asyncio.create_subprocess_exec
    executor.asyncio.create_subprocess_exec = _patch_exec(
        [FakeProc(b"enablement-kubernetes-101\n"), FakeProc(b"")], calls)
    orig_sleep = executor.asyncio.sleep

    async def no_sleep(_):
        return None

    executor.asyncio.sleep = no_sleep
    try:
        repo_dir = _run(_clear_slot_repo_dir(FakeSlot(tmp_path), "enablement-kubernetes-101"))
    finally:
        executor.asyncio.create_subprocess_exec = orig
        executor.asyncio.sleep = orig_sleep
    assert repo_dir.is_dir()
    assert len(calls) == 2


def test_persistently_dirty_raises_rather_than_cloning(tmp_path):
    # The whole point: fail here, loudly, naming the slot — so the caller
    # releases it unhealthy and it is re-initialized. Never clone into a
    # directory we only assume is empty.
    orig = executor.asyncio.create_subprocess_exec
    executor.asyncio.create_subprocess_exec = _patch_exec(
        [FakeProc(b".git\nsrc\n") for _ in range(3)])
    orig_sleep = executor.asyncio.sleep

    async def no_sleep(_):
        return None

    executor.asyncio.sleep = no_sleep
    try:
        raised = None
        try:
            _run(_clear_slot_repo_dir(FakeSlot(tmp_path, index=6), "enablement-kubernetes-101"))
        except SlotWorkspaceDirty as exc:
            raised = exc
    finally:
        executor.asyncio.create_subprocess_exec = orig
        executor.asyncio.sleep = orig_sleep
    assert raised is not None, "a dirty slot must not fall through to git clone"
    assert "slot 6" in str(raised)
    assert "enablement-kubernetes-101" in str(raised)


def test_hung_docker_exec_is_killed_and_retried(tmp_path):
    # A wedged `docker exec` must not hold a provision open forever, and the
    # timed-out child must be reaped rather than left as a zombie.
    orig = executor.asyncio.create_subprocess_exec
    hung = FakeProc(b"", hang=True)
    executor.asyncio.create_subprocess_exec = _patch_exec([hung, FakeProc(b"")])
    orig_sleep = executor.asyncio.sleep

    async def no_sleep(_):
        return None

    executor.asyncio.sleep = no_sleep
    try:
        repo_dir = _run(_clear_slot_repo_dir(FakeSlot(tmp_path), "repo-y", timeout=0.05))
    finally:
        executor.asyncio.create_subprocess_exec = orig
        executor.asyncio.sleep = orig_sleep
    assert hung.killed, "the timed-out child must be reaped, not leaked"
    assert repo_dir.is_dir()


# ── release() must leave the workspace clean ─────────────────────────────────

def test_release_wipes_the_workspace_after_removing_dt():
    """The wipe has to run AFTER `docker rm -fv dt`, or it races the mount.

    Asserting on ordering rather than mocking Docker: the sequence is the
    contract, and getting it backwards reintroduces the exact 20% failure.
    """
    from . import agent as agent_mod
    src = Path(agent_mod.__file__).read_text()
    body = src.split("async def release(", 1)[1].split("\n    async def ", 1)[0]
    rm_dt = body.index('"rm", "-fv", "dt"')
    wipe = body.index("rm -rf /workspaces/*")
    assert rm_dt < wipe, "workspace wipe must follow dt removal"
    # And it must be inside release()'s best-effort loop, not the unhealthy
    # branch — an unhealthy slot is re-initialized from scratch anyway.
    assert body.index("if not healthy:") < rm_dt


if __name__ == "__main__":
    import sys
    import tempfile
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {name}: {exc}")
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)

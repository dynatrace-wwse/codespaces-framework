"""job:running:{codespace} must never be resurrected by a status poll.

Redis `hset` CREATES a missing key. `_append_creation_log` writes flags
(`creation_log_fetched`, `ssh_ready`, `recovery`) on that hash and is fired
from `session_status` with `asyncio.ensure_future` whenever GitHub reports the
Codespace Available — including long after the session was terminated or
reaped. Without a guard those writes rebuilt a partial, TTL-less record, which
then flapped against the master reconciler (recreate → reap → recreate) and
made /api/codespace/orbital/{name} answer true or false depending on which side
of the 15 s loop the framework's post-create happened to ask on.

Regression cover for 2026-08-10. See also workers/test_dead_worker_candidate.py.

Run: /home/ops/ops-venv/bin/python -m pytest dashboard/test_codespace_record.py -q
"""

import asyncio

import pytest

from dashboard import codespace_service as cs


class _Pool:
    """Minimal async Redis stand-in that records every mutation."""

    def __init__(self, existing: bool, fields: dict | None = None):
        self._existing = existing
        self._fields = dict(fields or {})
        self.writes: list[tuple] = []

    async def exists(self, key):
        return 1 if self._existing else 0

    async def hget(self, key, field):
        return self._fields.get(field)

    async def hset(self, key, field=None, value=None, mapping=None):
        self.writes.append(("hset", key, field, value, mapping))
        return 1

    async def hdel(self, key, *fields):
        self.writes.append(("hdel", key, fields))
        return 1


def _run(pool, monkeypatch, name="didactic-goggles-abc"):
    monkeypatch.setattr(cs, "_pool", lambda: pool)
    asyncio.run(cs._append_creation_log("someone@example.com", name))


def test_no_writes_when_the_record_is_gone(monkeypatch):
    pool = _Pool(existing=False)
    _run(pool, monkeypatch)
    assert pool.writes == [], f"resurrected job:running with {pool.writes}"


def test_no_writes_when_already_fetched(monkeypatch):
    pool = _Pool(existing=True, fields={"creation_log_fetched": "1"})
    _run(pool, monkeypatch)
    assert pool.writes == []


def test_live_record_is_marked_before_fetching(monkeypatch):
    """A tracked, unfetched session still takes the at-most-once flag — the
    guard must not disable the normal path."""
    pool = _Pool(existing=True)
    # Fail the token lookup so the function stops right after the flag write
    # instead of shelling out to `gh` in a unit test.
    async def _boom(*a, **k):
        raise RuntimeError("no token in tests")
    monkeypatch.setattr(cs, "get_user_token", _boom)
    monkeypatch.setattr(cs, "_pool", lambda: pool)
    with pytest.raises(RuntimeError):
        asyncio.run(cs._append_creation_log("someone@example.com", "cs-live"))
    assert ("hset", "job:running:cs-live", "creation_log_fetched", "1", None) in pool.writes

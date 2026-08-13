"""Trainer registry — dashboard/trainer_registry.py.

Two halves, same approach as test_tenant_registry.py:
  - pure shaping (no Redis, no FastAPI);
  - the async helpers over an in-process fake, because the interesting
    behaviour lives there: re-add keeps the original attribution, and remove
    drops the INDEX before the hash so a half-failed delete cannot leave
    someone still passing the gate.

Runnable:
  - pytest:     python3 -m pytest dashboard/test_trainer_registry.py
  - standalone: python3 -m dashboard.test_trainer_registry
"""

import asyncio

from dashboard import trainer_registry as tr

SERGIO = "sergio.hinojosa@dynatrace.com"
ASAD = "asad.ali@dynatrace.com"


class FakeRedis:
    """Only the set/hash commands the registry uses."""

    def __init__(self):
        self.h: dict = {}
        self.s: dict = {}

    async def sismember(self, key, member):
        return member in self.s.get(key, set())

    async def smembers(self, key):
        return set(self.s.get(key, set()))

    async def sadd(self, key, *members):
        self.s.setdefault(key, set()).update(members)

    async def srem(self, key, *members):
        target = self.s.setdefault(key, set())
        removed = sum(1 for m in members if m in target)
        target.difference_update(members)
        return removed

    async def scard(self, key):
        return len(self.s.get(key, set()))

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hset(self, key, field=None, value=None, mapping=None):
        target = self.h.setdefault(key, {})
        if mapping:
            target.update(mapping)
        else:
            target[field] = value

    async def delete(self, *keys):
        for key in keys:
            self.h.pop(key, None)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── Pure shaping ─────────────────────────────────────────────────────────────

def test_registry_key_normalizes():
    assert tr.registry_key("  Sergio.Hinojosa@Dynatrace.com ") == \
        f"trainer:registry:{SERGIO}"


def test_validate_accepts_and_normalizes():
    assert tr.validate_trainer_email("  ASAD.Ali@Dynatrace.com  ") == ASAD


def test_validate_rejects_non_addresses():
    for bad in ("", None, "   ", "not-an-email", "sergio"):
        try:
            tr.validate_trainer_email(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not validate")


def test_shape_entry_drops_empty_fields():
    f = tr.shape_entry(SERGIO, now="2026-08-13T10:00:00+00:00")
    assert f == {"email": SERGIO, "addedAt": "2026-08-13T10:00:00+00:00"}


def test_shape_entry_keeps_attribution():
    f = tr.shape_entry("  Asad.Ali@Dynatrace.com ", name="Asad Ali",
                       added_by="sergiohinojosa", note="COE",
                       now="2026-08-13T10:00:00+00:00")
    assert f["email"] == ASAD
    assert f["name"] == "Asad Ali"
    assert f["addedBy"] == "sergiohinojosa"
    assert f["note"] == "COE"


# ── Async helpers ────────────────────────────────────────────────────────────

def test_add_then_is_trainer():
    fake = FakeRedis()

    async def go():
        await tr.add_entry(fake, "  Sergio.Hinojosa@Dynatrace.com ",
                           name="Sergio", added_by="sergiohinojosa")
        assert await tr.is_trainer(fake, SERGIO) is True
        # Lookup normalizes too — the app sends whatever Dynatrace reports.
        assert await tr.is_trainer(fake, " SERGIO.HINOJOSA@dynatrace.com ") is True
        assert await tr.is_trainer(fake, ASAD) is False

    run(go())


def test_is_trainer_false_for_empty_and_never_raises():
    class Broken:
        async def sismember(self, *_):
            raise RuntimeError("redis down")

    async def go():
        assert await tr.is_trainer(FakeRedis(), "") is False
        # An unreachable registry hides a button; it must not break a page.
        assert await tr.is_trainer(Broken(), SERGIO) is False

    run(go())


def test_readd_keeps_original_attribution():
    fake = FakeRedis()

    async def go():
        first = await tr.add_entry(fake, SERGIO, added_by="alice")
        again = await tr.add_entry(fake, SERGIO, name="Sergio H",
                                   added_by="bob")
        assert again["addedAt"] == first["addedAt"]
        assert again["addedBy"] == "alice", "a re-add is not a new grant"
        assert again["name"] == "Sergio H", "but details may be corrected"

    run(go())


def test_add_rejects_bad_email_before_touching_redis():
    fake = FakeRedis()

    async def go():
        try:
            await tr.add_entry(fake, "nope")
        except ValueError:
            assert fake.s == {} and fake.h == {}
            return
        raise AssertionError("expected ValueError")

    run(go())


def test_remove_returns_whether_they_were_registered():
    fake = FakeRedis()

    async def go():
        await tr.add_entry(fake, SERGIO)
        assert await tr.remove_entry(fake, " SERGIO.hinojosa@Dynatrace.com ") is True
        assert await tr.is_trainer(fake, SERGIO) is False
        assert await tr.remove_entry(fake, SERGIO) is False
        assert await tr.remove_entry(fake, "") is False

    run(go())


def test_list_entries_sorted_and_survives_a_missing_hash():
    fake = FakeRedis()

    async def go():
        await tr.add_entry(fake, SERGIO, name="Sergio")
        await tr.add_entry(fake, ASAD, name="Asad")
        # A half-written entry (index member, no hash) must still be listed so
        # it can be seen and removed rather than becoming invisible.
        fake.s[tr.INDEX_KEY].add("orphan@dynatrace.com")
        entries = await tr.list_entries(fake)
        assert [e["email"] for e in entries] == [
            ASAD, "orphan@dynatrace.com", SERGIO]
        assert entries[0]["name"] == "Asad"
        assert entries[1] == {"email": "orphan@dynatrace.com"}

    run(go())


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")

"""Global trainer registry — who is allowed to SCHEDULE workshops.

Until now there was no such list anywhere. A workshop's `trainers` field is
per-workshop MEMBERSHIP (live_sessions.py) and grants nothing outside that one
workshop, and the app's `isInstructor` gate is about CONTENT (import, solution
reveal). Neither answers "may this person create a workshop at all?", so in
practice anyone whose app boot reported `isInstructor || canWrite` saw the
+ New workshop button and Orbital accepted the create with no check whatsoever.

This module is that missing list, and it is deliberately narrow:

  * being in the registry lets you CREATE/schedule workshops, and flip the
    tenant-wide solutions toggle;
  * it does NOT make you an admin, and being an admin does not put you here
    (the two rights are split on purpose — an admin may see admin surfaces
    without being someone who delivers training);
  * it does NOT grant anything inside a workshop. Authority there is still
    workshop membership: see live_sessions.is_trainer / is_owner.

Redis layout (Orbital Redis, same instance as jobs):
  trainer:registry:index        set of normalized emails — the HOT path. Every
                                workshop-list call answers "is the caller a
                                trainer?" with one SISMEMBER, so that question
                                must never require reading a hash.
  trainer:registry:{email}      hash — email, name, addedBy, addedAt, note.
                                Attribution for the Trainers table only.

Two structures for one concept, for the same reason tenant_registry.py has
INDEX_KEY plus a per-entity hash.

The shaping logic is pure (no Redis) and unit-tested in
test_trainer_registry.py; the async helpers below are thin wrappers.
"""

import logging
from datetime import datetime, timezone

from dashboard.live_sessions import is_valid_email, normalize_email

log = logging.getLogger("ops-dashboard.trainer-registry")

INDEX_KEY = "trainer:registry:index"


def registry_key(email: str) -> str:
    return f"trainer:registry:{normalize_email(email)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Pure shaping ─────────────────────────────────────────────────────────────

def validate_trainer_email(email) -> str:
    """Normalized email, or ValueError. Same minimal rule the roster uses —
    Orbital cannot verify an address, only reject an obvious non-address."""
    normalized = normalize_email(email)
    if not is_valid_email(normalized):
        raise ValueError("a valid email is required")
    return normalized


def shape_entry(email: str, *, name: str = "", added_by: str = "",
                note: str = "", now: str | None = None) -> dict:
    """Fields for a new registry entry. Empty values are DROPPED so a later
    re-add never blanks attribution captured the first time (mirrors
    tenant_registry.shape_deploy)."""
    fields = {"email": normalize_email(email), "addedAt": now or _now_iso()}
    if name:
        fields["name"] = name.strip()
    if added_by:
        fields["addedBy"] = added_by.strip()
    if note:
        fields["note"] = note.strip()
    return fields


# ── Async Redis helpers ──────────────────────────────────────────────────────

async def is_trainer(pool, email) -> bool:
    """The hot path. One SISMEMBER; never raises — an unreachable registry
    means "not a trainer", which hides a button rather than breaking a page."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    try:
        return bool(await pool.sismember(INDEX_KEY, normalized))
    except Exception as exc:
        log.warning("trainer-registry lookup failed for %s: %s", normalized, exc)
        return False


async def list_entries(pool) -> list[dict]:
    """All trainers, sorted by email. Writer-gated by the caller — values are
    returned unmasked. An index member with no hash still appears (email only),
    so a half-written entry is visible and removable rather than invisible."""
    emails = sorted(await pool.smembers(INDEX_KEY) or [])
    out: list[dict] = []
    for email in emails:
        entry = await pool.hgetall(registry_key(email)) or {}
        out.append({"email": email, **entry})
    return out


async def add_entry(pool, email, *, name: str = "", added_by: str = "",
                    note: str = "") -> dict:
    """Add or update a trainer. Raises ValueError on a bad address."""
    normalized = validate_trainer_email(email)
    key = registry_key(normalized)
    existing = await pool.hgetall(key) or {}
    fields = shape_entry(normalized, name=name, added_by=added_by, note=note)
    if existing.get("addedAt"):
        # Keep the original attribution; a re-add is not a new grant.
        fields["addedAt"] = existing["addedAt"]
        fields.pop("addedBy", None)
    await pool.hset(key, mapping=fields)
    await pool.sadd(INDEX_KEY, normalized)
    return {**existing, **fields}


async def remove_entry(pool, email) -> bool:
    """Remove a trainer. True when they were actually in the registry.

    Removes from the index FIRST: if the delete then fails, the worst outcome
    is an orphan hash, not someone who still passes the gate.
    """
    normalized = normalize_email(email)
    if not normalized:
        return False
    removed = bool(await pool.srem(INDEX_KEY, normalized))
    await pool.delete(registry_key(normalized))
    return removed

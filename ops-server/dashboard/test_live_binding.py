"""Automatic tenant binding — POST /api/live/sessions/{id}/bind and friends.

Binding answers "which tenant will provision this learner". It replaced the
"Provision here" button, which was a label promising an action it never
performed. Two properties are load-bearing and both are asserted here:

  1. Binding is NOT attendance. It is allowed with the room closed and the
     workshop days away, and it must not write :joined.
  2. FIRST WRITE WINS. Walking into a second tenant must not silently move a
     learner who already has, or is about to get, an environment elsewhere.
     Only an explicit rebind, or provision-ack reporting ground truth, moves it.

Redis is an in-process fake.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_live_binding.py
"""

import json

import dashboard.app as a
from dashboard import live_sessions as ls
from dashboard import masking
from fastapi.testclient import TestClient

client = TestClient(a.app, raise_server_exceptions=False)

BEARER = {"Authorization": "Bearer test-service-token"}
SID = "ws_bind-1"
TRAINER = "trainer@dynatrace.com"
CO = "co@dynatrace.com"
LEARNER = "learner@customer.com"
STRANGER = "stranger@customer.com"
COE = "https://geu80787.apps.dynatrace.com"
SRO = "https://sro97894.apps.dynatrace.com"


class FakeRedis:
    def __init__(self):
        self.h, self.s, self.x, self.z = {}, {}, {}, {}
        self.seq = 0

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    async def hset(self, key, field=None, value=None, mapping=None):
        target = self.h.setdefault(key, {})
        if mapping:
            target.update(mapping)
        else:
            target[field] = value

    async def hsetnx(self, key, field, value):
        target = self.h.setdefault(key, {})
        if field in target:
            return 0
        target[field] = value
        return 1

    async def hdel(self, key, *fields):
        target = self.h.get(key, {})
        return sum(1 for f in fields if target.pop(f, None) is not None)

    async def hlen(self, key):
        return len(self.h.get(key, {}))

    async def smembers(self, key):
        return set(self.s.get(key, set()))

    async def sadd(self, key, *members):
        target = self.s.setdefault(key, set())
        added = sum(1 for m in members if m not in target)
        target.update(members)
        return added

    async def srem(self, key, *members):
        target = self.s.setdefault(key, set())
        removed = sum(1 for m in members if m in target)
        target.difference_update(members)
        return removed

    async def delete(self, *keys):
        for key in keys:
            self.h.pop(key, None)
            self.s.pop(key, None)
            self.x.pop(key, None)

    async def expire(self, key, ttl):
        return True

    async def exists(self, key):
        return 1 if (key in self.h or key in self.s) else 0

    async def xadd(self, key, mapping, maxlen=None, approximate=True):
        self.seq += 1
        self.x.setdefault(key, []).append((f"{1000 + self.seq}-0", dict(mapping)))
        return f"{1000 + self.seq}-0"

    async def xrange(self, key, min="-", max="+", count=None):
        return list(self.x.get(key, []))

    async def zrevrange(self, key, start, end):
        return list(self.z.get(key, []))

    async def zrem(self, key, *members):
        before = len(self.z.get(key, []))
        self.z[key] = [m for m in self.z.get(key, []) if m not in members]
        return before - len(self.z[key])

    async def get(self, key):
        return None

    async def scan_iter(self, match=None, count=None):
        for key in ():
            yield key


def setup_module(_module):
    _module._saved_tokens = a.ORBITAL_TOKENS
    a.ORBITAL_TOKENS = ("test-service-token",)


def teardown_module(_module):
    a.ORBITAL_TOKENS = _module._saved_tokens


def _seed(state="scheduled", room_open=""):
    a.pool = FakeRedis()
    a.pool.h[f"live:session:{SID}"] = {
        "title": "Kubernetes 101", "trainingId": "kubernetes-101",
        "trainers": json.dumps([TRAINER, CO]), "state": state,
        "roomOpen": room_open, "ownerTenant": COE,
        "createdAt": "2026-08-13T09:00:00+00:00",
        "scheduledAt": "2026-09-20T09:00:00+00:00", "maxSeats": "20",
    }
    a.pool.s[f"live:session:{SID}:roster"] = {LEARNER}
    a.pool.z["live:sessions:index"] = [SID]


def setup_function(_fn):
    _fn._saved_pool = a.pool
    _seed()


def teardown_function(_fn):
    a.pool = _fn._saved_pool


def _bind(email, tenant, rebind=False, sid=SID):
    return client.post(f"/api/live/sessions/{sid}/bind", headers=BEARER,
                       json={"email": email, "tenant": tenant, "rebind": rebind})


def _tenants():
    return a.pool.h.get(f"live:session:{SID}:tenants", {})


def _boundat():
    return a.pool.h.get(f"live:session:{SID}:boundat", {})


def _joined():
    return a.pool.h.get(f"live:session:{SID}:joined", {})


def _event_kinds():
    return [e[1].get("kind") for e in a.pool.x.get(f"live:session:{SID}:events", [])]


# ── Pure decisions ───────────────────────────────────────────────────────────

def test_bind_outcome_truth_table():
    assert ls.bind_outcome("", COE) == ls.BIND_BOUND
    assert ls.bind_outcome(COE, COE) == ls.BIND_KEPT          # same tenant
    assert ls.bind_outcome(COE, SRO) == ls.BIND_KEPT          # first write wins
    assert ls.bind_outcome(COE, SRO, rebind=True) == ls.BIND_REBOUND
    assert ls.bind_outcome(COE, "") == ls.BIND_KEPT           # never clear
    assert ls.bind_outcome("", "") == ls.BIND_KEPT
    # An explicit rebind to the tenant you are already on is still a no-op.
    assert ls.bind_outcome(COE, COE, rebind=True) == ls.BIND_KEPT


def test_bind_error_allows_a_closed_room_days_early():
    """The whole point: binding is not attendance."""
    roster = {LEARNER}
    for state in ("scheduled", "open", "running"):
        assert ls.bind_error(state, LEARNER, roster) is None, state


def test_bind_error_refuses_finished_workshops():
    roster = {LEARNER}
    assert ls.bind_error("ended", LEARNER, roster)[0] == 409
    assert ls.bind_error("cancelled", LEARNER, roster)[0] == 409


def test_bind_error_requires_membership_but_trainers_always_qualify():
    sess = {"trainers": json.dumps([TRAINER, CO])}
    assert ls.bind_error("scheduled", STRANGER, {LEARNER}, sess)[0] == 403
    assert ls.bind_error("scheduled", TRAINER, {LEARNER}, sess) is None
    assert ls.bind_error("scheduled", CO, {LEARNER}, sess) is None


def test_attendance_state_precedence():
    roster, joined, tenants = {LEARNER}, {LEARNER: "t"}, {LEARNER: COE}
    assert ls.attendance_state(LEARNER, roster, joined, tenants) == ls.ATTENDANCE_PRESENT
    assert ls.attendance_state(LEARNER, roster, {}, tenants) == ls.ATTENDANCE_BOUND
    assert ls.attendance_state(LEARNER, roster, {}, {}) == ls.ATTENDANCE_REGISTERED
    assert ls.attendance_state(STRANGER, roster, {}, {}) == ls.ATTENDANCE_NONE
    assert ls.attendance_state("", roster, {}, {}) == ls.ATTENDANCE_NONE
    # A binding with an empty value is not a binding.
    assert ls.attendance_state(LEARNER, roster, {}, {LEARNER: ""}) == ls.ATTENDANCE_REGISTERED


def test_env_tenant_mismatch_needs_both_sides():
    assert ls.env_tenant_mismatch(COE, SRO) is True
    assert ls.env_tenant_mismatch(COE, COE) is False
    assert ls.env_tenant_mismatch("", SRO) is False
    assert ls.env_tenant_mismatch(COE, "") is False
    assert ls.env_tenant_mismatch("", "") is False


# ── The route ────────────────────────────────────────────────────────────────

def test_bind_works_on_a_closed_room_days_before_the_workshop():
    r = _bind(LEARNER, COE)
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == ls.BIND_BOUND
    assert body["tenant"] == COE and body["boundHere"] is True
    assert body["boundAt"]
    assert _tenants() == {LEARNER: COE}


def test_binding_is_not_attendance():
    _bind(LEARNER, COE)
    assert _joined() == {}, "binding must never write the joined hash"


def test_second_tenant_is_kept_not_moved():
    _bind(LEARNER, COE)
    r = _bind(LEARNER, SRO)
    assert r.json()["outcome"] == ls.BIND_KEPT
    assert r.json()["tenant"] == COE
    assert r.json()["boundHere"] is False, "they are looking at SRO, bound to COE"
    assert _tenants() == {LEARNER: COE}


def test_explicit_rebind_moves_it():
    _bind(LEARNER, COE)
    r = _bind(LEARNER, SRO, rebind=True)
    assert r.json()["outcome"] == ls.BIND_REBOUND
    assert r.json()["tenant"] == SRO and r.json()["boundHere"] is True
    assert _tenants() == {LEARNER: SRO}


def test_rebinding_updates_the_timestamp():
    _bind(LEARNER, COE)
    first = _boundat()[LEARNER]
    _bind(LEARNER, SRO, rebind=True)
    assert _boundat()[LEARNER] >= first


def test_bind_emits_audit_events_only_on_a_real_change():
    _bind(LEARNER, COE)
    _bind(LEARNER, COE)            # same tenant — nothing happened
    _bind(LEARNER, SRO)            # kept — nothing happened
    assert _event_kinds() == [ls.EVENT_BOUND]
    _bind(LEARNER, SRO, rebind=True)
    assert _event_kinds() == [ls.EVENT_BOUND, ls.EVENT_REBOUND]


def test_bind_rejects_a_stranger_and_a_finished_workshop():
    assert _bind(STRANGER, COE).status_code == 403
    assert _bind(LEARNER, COE, sid="ws_nope").status_code == 404
    _seed(state="ended")
    assert _bind(LEARNER, COE).status_code == 409


def test_bind_requires_an_email():
    r = client.post(f"/api/live/sessions/{SID}/bind", headers=BEARER,
                    json={"tenant": COE})
    assert r.status_code == 400


def test_bind_is_authenticated():
    r = client.post(f"/api/live/sessions/{SID}/bind",
                    json={"email": LEARNER, "tenant": COE})
    assert r.status_code == 401


def test_a_trainer_can_bind_without_being_on_the_roster():
    """Trainers provision their own environment and are never on their own
    roster — the same carve-out join_error makes."""
    assert _bind(TRAINER, COE).json()["outcome"] == ls.BIND_BOUND
    assert _bind(CO, SRO).json()["outcome"] == ls.BIND_BOUND


# ── The other three writers all go through the same funnel ───────────────────

def test_join_binds_but_no_longer_moves_an_existing_binding():
    _bind(LEARNER, COE)
    r = client.post(f"/api/live/sessions/{SID}/join", headers=BEARER,
                    json={"email": LEARNER, "tenant": SRO})
    assert r.status_code == 200
    assert _tenants() == {LEARNER: COE}, "join must not silently rebind"
    assert LEARNER in _joined(), "join still checks in"


def test_join_still_binds_when_nothing_was_bound():
    client.post(f"/api/live/sessions/{SID}/join", headers=BEARER,
                json={"email": LEARNER, "tenant": SRO})
    assert _tenants() == {LEARNER: SRO}


def test_join_by_code_binds_and_registers_without_checking_in():
    a.pool.h[f"live:session:{SID}"]["state"] = "open"
    a.pool.h[f"live:session:{SID}"]["joinCode"] = "ABC123"
    a.pool.h["live:joincode:ABC123"] = {}       # presence only; route uses get()
    a.pool.k = {"live:joincode:ABC123": SID}

    async def _get(key):
        return a.pool.k.get(key)
    a.pool.get = _get

    r = client.post("/api/live/sessions/join-by-code", headers=BEARER,
                    json={"code": "ABC123", "email": STRANGER, "tenant": SRO})
    assert r.status_code == 200
    assert _tenants() == {STRANGER: SRO}
    assert _joined() == {}, "registering is not checking in"


def test_provision_ack_is_the_one_caller_that_overrides_a_binding():
    """Ground truth beats intent: the ack says where the environment actually
    landed, and the trainer's board has to show the truth."""
    _bind(LEARNER, COE)
    r = client.post(f"/api/live/sessions/{SID}/provision-ack", headers=BEARER,
                    json={"email": LEARNER, "tenant": SRO, "status": "queued"})
    assert r.status_code == 200
    assert _tenants() == {LEARNER: SRO}


# ── Payload shaping + masking ────────────────────────────────────────────────

def _detail(email):
    return client.get(f"/api/live/sessions/{SID}?email={email}",
                      headers=BEARER).json()


def test_trainer_sees_every_binding_and_the_seat_summary():
    _bind(LEARNER, COE)
    detail = _detail(TRAINER)
    assert detail["bindings"] == [
        {"email": LEARNER, "tenant": COE, "boundAt": _boundat()[LEARNER]}]
    assert detail["seats"]["seatsTaken"] == 1
    assert detail["seats"]["seatsOpen"] == 19


def test_bindings_cover_learners_who_never_checked_in():
    """Not derivable from `joined` — which is exactly why the split exists."""
    _bind(LEARNER, COE)
    assert _joined() == {}
    assert len(_detail(TRAINER)["bindings"]) == 1


def test_a_learner_sees_only_their_own_binding():
    _bind(LEARNER, COE)
    _bind(TRAINER, SRO)
    detail = _detail(LEARNER)
    assert detail["myTenant"] == COE
    assert detail["boundAt"] == _boundat()[LEARNER]
    assert "bindings" not in detail and "seats" not in detail


def test_my_provision_status_is_caller_scoped():
    _bind(LEARNER, COE)
    client.post(f"/api/live/sessions/{SID}/provision-ack", headers=BEARER,
                json={"email": LEARNER, "tenant": COE, "status": "queued"})
    assert _detail(LEARNER)["myProvisionStatus"] == "queued"
    assert _detail(TRAINER)["myProvisionStatus"] == ""


def test_masking_drops_bindings_and_seats():
    """Without this the anonymous read path — which returns a masked 200 by
    design — would disclose every learner's tenant."""
    item = {"sessionId": SID, "trainerEmail": TRAINER, "roster": [LEARNER],
            "joined": [], "seats": {"seatsTaken": 1},
            "bindings": [{"email": LEARNER, "tenant": COE, "boundAt": "x"}]}
    out = masking.mask_live_detail(item)
    assert "bindings" not in out and "seats" not in out
    assert "roster" not in out and "joined" not in out


def test_boundat_is_absent_for_a_pre_existing_binding():
    """Bindings written before this key existed must still count as bindings —
    bind_outcome keys off the tenant, never the timestamp."""
    a.pool.h[f"live:session:{SID}:tenants"] = {LEARNER: COE}   # no :boundat
    r = _bind(LEARNER, SRO)
    assert r.json()["outcome"] == ls.BIND_KEPT
    assert r.json()["tenant"] == COE
    assert _detail(LEARNER)["boundAt"] == ""

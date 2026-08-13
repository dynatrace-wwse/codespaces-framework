"""Workshops & Delivery admin API — /api/workshops/admin/*.

The load-bearing assertion in this file is not the CRUD: it is that a caller
presenting only the app's SERVICE BEARER is rejected. Every enablement app
install ships the same baked bearer, so if these routes accepted it, any
tenant's app could read every other tenant's workshops, rosters and trainers.
_require_writer demands X-Auth-User, which nginx sets only after oauth2-proxy
has validated a GitHub org session.

Redis is an in-process fake; _resolve_role is stubbed so no GitHub call is made.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_workshops_admin.py
"""

import json

import dashboard.app as a
from dashboard import trainer_registry as tr
from fastapi.testclient import TestClient

client = TestClient(a.app, raise_server_exceptions=False)

BEARER = {"Authorization": "Bearer test-service-token"}
WRITER = {"X-Auth-User": "sergiohinojosa"}
SERGIO = "sergio.hinojosa@dynatrace.com"
ASAD = "asad.ali@dynatrace.com"
LEARNER = "learner@customer.com"
COE = "https://geu80787.apps.dynatrace.com"

ADMIN_ROUTES = [
    ("get", "/api/workshops/admin/trainers"),
    ("post", "/api/workshops/admin/trainers"),
    ("delete", f"/api/workshops/admin/trainers/{SERGIO}"),
]


class FakeRedis:
    def __init__(self):
        self.h: dict = {}
        self.s: dict = {}
        self.z: dict = {}

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

    async def zrevrange(self, key, start, end):
        return list(self.z.get(key, []))

    async def zrem(self, key, *members):
        before = len(self.z.get(key, []))
        self.z[key] = [m for m in self.z.get(key, []) if m not in members]
        return before - len(self.z[key])

    async def exists(self, key):
        return 1 if (key in self.h or key in self.s) else 0


def setup_module(_module):
    _module._saved_tokens = a.ORBITAL_TOKENS
    _module._saved_role = a._resolve_role
    a.ORBITAL_TOKENS = ("test-service-token",)

    async def _fake_role(user):
        return {"role": "writer" if user else "guest", "user": user,
                "org_role": "member"}
    a._resolve_role = _fake_role


def teardown_module(_module):
    a.ORBITAL_TOKENS = _module._saved_tokens
    a._resolve_role = _module._saved_role


def setup_function(_fn):
    _fn._saved_pool = a.pool
    a.pool = FakeRedis()


def teardown_function(_fn):
    a.pool = _fn._saved_pool


# ── The gate ─────────────────────────────────────────────────────────────────

def _call(verb, path, headers=None):
    """TestClient.get/delete take no json= — only POST carries a body."""
    kwargs = {"headers": headers} if headers else {}
    if verb == "post":
        kwargs["json"] = {"email": SERGIO}
    return getattr(client, verb)(path, **kwargs)


def test_anonymous_gets_401_on_every_admin_route():
    for verb, path in ADMIN_ROUTES:
        r = _call(verb, path)
        assert r.status_code == 401, f"{verb} {path} -> {r.status_code}"


def test_service_bearer_alone_cannot_read_workshops_admin():
    """The whole security model: the baked app bearer is NOT enough here."""
    for verb, path in ADMIN_ROUTES:
        r = _call(verb, path, headers=BEARER)
        assert r.status_code == 401, f"{verb} {path} -> {r.status_code}"


def test_empty_x_auth_user_is_treated_as_anonymous():
    # nginx clears the header on un-gated paths; an empty value must not pass.
    r = client.get("/api/workshops/admin/trainers", headers={"X-Auth-User": ""})
    assert r.status_code == 401


def test_non_org_member_gets_403():
    saved = a._resolve_role

    async def _guest(user):
        return {"role": "guest", "user": user, "org_role": ""}
    a._resolve_role = _guest
    try:
        r = client.get("/api/workshops/admin/trainers", headers=WRITER)
        assert r.status_code == 403
    finally:
        a._resolve_role = saved


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_trainer_crud_round_trip():
    assert client.get("/api/workshops/admin/trainers",
                      headers=WRITER).json() == {"trainers": [], "count": 0}

    r = client.post("/api/workshops/admin/trainers", headers=WRITER,
                    json={"email": "  Sergio.Hinojosa@Dynatrace.com ",
                          "name": "Sergio", "note": "COE"})
    assert r.status_code == 200
    assert r.json()["trainer"]["email"] == SERGIO
    # addedBy is taken from the signed-in identity, never from the body.
    assert r.json()["trainer"]["addedBy"] == "sergiohinojosa"

    client.post("/api/workshops/admin/trainers", headers=WRITER,
                json={"email": ASAD})
    listed = client.get("/api/workshops/admin/trainers", headers=WRITER).json()
    assert listed["count"] == 2
    assert [t["email"] for t in listed["trainers"]] == [ASAD, SERGIO]

    r = client.delete(f"/api/workshops/admin/trainers/{SERGIO}", headers=WRITER)
    assert r.status_code == 200 and r.json()["removed"] == SERGIO
    assert client.get("/api/workshops/admin/trainers",
                      headers=WRITER).json()["count"] == 1


def test_add_rejects_a_non_address():
    r = client.post("/api/workshops/admin/trainers", headers=WRITER,
                    json={"email": "not-an-email"})
    assert r.status_code == 400


def test_remove_unknown_is_404():
    r = client.delete(f"/api/workshops/admin/trainers/{ASAD}", headers=WRITER)
    assert r.status_code == 404


def test_added_by_cannot_be_forged_through_the_body():
    client.post("/api/workshops/admin/trainers", headers=WRITER,
                json={"email": ASAD, "addedBy": "someone-else"})
    entry = client.get("/api/workshops/admin/trainers",
                       headers=WRITER).json()["trainers"][0]
    assert entry["addedBy"] == "sergiohinojosa"


# ── callerIsTrainer rides the existing list route ────────────────────────────

def _seed_workshop():
    a.pool.h["live:session:ws_x"] = {
        "title": "Kubernetes 101", "trainingId": "kubernetes-101",
        "trainers": json.dumps([SERGIO]), "state": "open",
        "createdAt": "2026-08-13T09:00:00+00:00", "ownerTenant": COE,
    }
    a.pool.s["live:session:ws_x:roster"] = {LEARNER}
    a.pool.z["live:sessions:index"] = ["ws_x"]


def test_caller_is_trainer_true_for_a_registered_trainer():
    _seed_workshop()
    client.post("/api/workshops/admin/trainers", headers=WRITER,
                json={"email": SERGIO})
    body = client.get(f"/api/live/sessions?email={SERGIO}",
                      headers=BEARER).json()
    assert body["callerIsTrainer"] is True


def test_caller_is_trainer_false_for_a_learner():
    _seed_workshop()
    client.post("/api/workshops/admin/trainers", headers=WRITER,
                json={"email": SERGIO})
    body = client.get(f"/api/live/sessions?email={LEARNER}",
                      headers=BEARER).json()
    assert body["callerIsTrainer"] is False
    # Being a workshop trainer is NOT the same thing as being registered: the
    # learner still sees their workshop, they just cannot schedule one.
    assert body["count"] == 1


def test_caller_is_trainer_false_when_registry_is_empty():
    _seed_workshop()
    body = client.get(f"/api/live/sessions?email={SERGIO}",
                      headers=BEARER).json()
    assert body["callerIsTrainer"] is False


# ── Owner-only delete, over HTTP ─────────────────────────────────────────────
# The one authority a co-trainer does not share. Everything else about the
# trainer team is deliberately flat — see live_sessions.is_owner.

def _seed_team_workshop():
    a.pool.h["live:session:ws_team"] = {
        "title": "Team", "trainingId": "k8s", "state": "scheduled",
        "trainers": json.dumps([SERGIO, ASAD]), "ownerTenant": COE,
        "createdAt": "2026-08-13T09:00:00+00:00",
    }
    a.pool.z["live:sessions:index"] = ["ws_team"]


def test_co_trainer_cannot_delete_the_workshop():
    _seed_team_workshop()
    r = client.request("DELETE", "/api/live/sessions/ws_team", headers=BEARER,
                       json={"trainerEmail": ASAD})
    assert r.status_code == 403
    assert "owner" in str(r.json().get("detail", "")).lower()
    assert "live:session:ws_team" in a.pool.h, "nothing may be deleted on a 403"


def test_owner_can_delete_the_workshop():
    _seed_team_workshop()
    r = client.request("DELETE", "/api/live/sessions/ws_team", headers=BEARER,
                       json={"trainerEmail": SERGIO})
    assert r.status_code == 200 and r.json()["deleted"] == "ws_team"
    assert "live:session:ws_team" not in a.pool.h
    assert a.pool.z["live:sessions:index"] == []


def test_a_stranger_still_gets_the_generic_trainer_error():
    """A non-trainer must not learn that an owner/co-trainer split exists —
    they get the same message as before."""
    _seed_team_workshop()
    r = client.request("DELETE", "/api/live/sessions/ws_team", headers=BEARER,
                       json={"trainerEmail": LEARNER})
    assert r.status_code == 403
    assert "owner" not in str(r.json().get("detail", "")).lower()


# ── Cross-tenant schedule ────────────────────────────────────────────────────

SRO = "https://sro97894.apps.dynatrace.com"


def _seed_many():
    """Three workshops on two tenants, deliberately indexed newest-created
    first so a correct scheduledAt sort has to reorder them."""
    a.pool.h["live:session:ws_late"] = {
        "title": "Late", "trainingId": "k8s", "state": "scheduled",
        "trainers": json.dumps([SERGIO, ASAD]), "ownerTenant": COE,
        "createdAt": "2026-08-13T09:00:00+00:00",
        "scheduledAt": "2026-09-20T09:00:00+00:00", "maxSeats": "10",
    }
    a.pool.s["live:session:ws_late:roster"] = {LEARNER, "b@x.com"}
    a.pool.h["live:session:ws_late:joined"] = {LEARNER: "2026-08-13T10:00:00+00:00"}
    a.pool.h["live:session:ws_late:tenants"] = {LEARNER: COE}

    a.pool.h["live:session:ws_early"] = {
        "title": "Early", "trainingId": "dtwiz", "state": "open",
        "trainers": json.dumps([ASAD]), "ownerTenant": SRO,
        "createdAt": "2026-08-12T09:00:00+00:00",
        "scheduledAt": "2026-09-01T09:00:00+00:00",
    }
    a.pool.s["live:session:ws_early:roster"] = set()

    # No scheduledAt at all — must fall back to createdAt, not vanish.
    a.pool.h["live:session:ws_unsched"] = {
        "title": "Unscheduled", "trainingId": "k8s", "state": "ended",
        "trainers": json.dumps([SERGIO]), "ownerTenant": COE,
        "createdAt": "2026-07-01T09:00:00+00:00",
    }
    a.pool.s["live:session:ws_unsched:roster"] = {LEARNER}
    a.pool.z["live:sessions:index"] = ["ws_late", "ws_early", "ws_unsched"]


def test_schedule_requires_a_writer():
    assert client.get("/api/workshops/admin/schedule").status_code == 401
    assert client.get("/api/workshops/admin/schedule",
                      headers=BEARER).status_code == 401


def test_schedule_returns_every_tenant_sorted_by_when_it_happens():
    _seed_many()
    body = client.get("/api/workshops/admin/schedule", headers=WRITER).json()
    assert [w["sessionId"] for w in body["workshops"]] == [
        "ws_unsched", "ws_early", "ws_late"]
    assert {w["ownerTenant"] for w in body["workshops"]} == {COE, SRO}
    assert body["count"] == 3 and body["total"] == 3


def test_schedule_splits_owner_from_co_trainers():
    _seed_many()
    row = next(w for w in client.get("/api/workshops/admin/schedule",
                                     headers=WRITER).json()["workshops"]
               if w["sessionId"] == "ws_late")
    assert row["owner"] == SERGIO
    assert row["coTrainers"] == [ASAD]
    assert row["trainers"] == [SERGIO, ASAD]


def test_schedule_seat_math_and_unlimited():
    _seed_many()
    rows = {w["sessionId"]: w for w in
            client.get("/api/workshops/admin/schedule",
                       headers=WRITER).json()["workshops"]}
    assert rows["ws_late"]["seatsTaken"] == 2
    assert rows["ws_late"]["seatsOpen"] == 8
    assert rows["ws_late"]["present"] == 1
    assert rows["ws_late"]["boundCount"] == 1
    # No maxSeats: "unlimited" must not be reported as "zero free".
    assert rows["ws_early"]["maxSeats"] == 0
    assert rows["ws_early"]["seatsOpen"] is None


def test_schedule_marks_which_rows_are_editable():
    _seed_many()
    rows = {w["sessionId"]: w for w in
            client.get("/api/workshops/admin/schedule",
                       headers=WRITER).json()["workshops"]}
    # PATCH refuses anything past `open`, so the editor must say so up front.
    assert rows["ws_late"]["editable"] is True
    assert rows["ws_early"]["editable"] is True
    assert rows["ws_unsched"]["editable"] is False


def test_schedule_state_filter():
    _seed_many()
    body = client.get("/api/workshops/admin/schedule?state=open,scheduled",
                      headers=WRITER).json()
    assert [w["sessionId"] for w in body["workshops"]] == ["ws_early", "ws_late"]


def test_schedule_self_heals_a_stale_index_member():
    _seed_many()
    a.pool.z["live:sessions:index"].append("ws_expired")  # hash already TTL'd
    body = client.get("/api/workshops/admin/schedule", headers=WRITER).json()
    assert body["count"] == 3
    assert "ws_expired" not in a.pool.z["live:sessions:index"], \
        "an index member with no hash must be dropped, not re-scanned forever"


def test_registry_membership_is_independent_of_workshop_membership():
    """A registered trainer with no workshops still gets the flag — that is
    what enables + New workshop on a brand-new trainer's first visit."""
    async def _add():
        await tr.add_entry(a.pool, ASAD)
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_add())
    body = client.get(f"/api/live/sessions?email={ASAD}", headers=BEARER).json()
    assert body["callerIsTrainer"] is True and body["count"] == 0

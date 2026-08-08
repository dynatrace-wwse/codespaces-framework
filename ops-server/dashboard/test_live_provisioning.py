"""Trainer-triggered cross-tenant provisioning (PASS 2).

Why this file exists: a trainer's app can only mint DT tokens for ITS OWN
tenant, so provision-all could never provision a learner sitting in a different
tenant — it reported them and moved on, and the trainer's board showed a gap
they had no way to close. The fix is a PULL channel: the trainer's intent is
recorded on the session hash, and the learner's own app instance — the only
thing that can mint in their tenant — picks it up on the session poll it is
already making, provisions there, and acks.

These tests pin the parts that decide whether an environment appears:

  * provision-all records the intent and reports foreign learners as
    "requested" rather than as a dead end
  * the session detail exposes provisionRequested scoped to the CALLER
  * provision-ack settles it so it does not fire twice
  * a re-click un-settles only the chunk it names (the app chunks big rosters)
  * the audit trail records the workflow and is masked for non-trainers

Redis is replaced by an in-process fake — like every other test file here, this
one touches no real Redis and no network.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_live_provisioning.py
"""

import json
from datetime import datetime, timezone

import dashboard.app as a
from fastapi.testclient import TestClient

client = TestClient(a.app, raise_server_exceptions=False)

BEARER = {"Authorization": "Bearer test-service-token"}
SID = "ws_test-prov-1"
TRAINER = "trainer@dynatrace.com"
SAME = "same@dynatrace.com"          # joined from the trainer's tenant
FOREIGN = "foreign@customer.com"     # joined from another tenant
ABSENT = "absent@customer.com"       # on the roster, never joined

COE = "https://geu80787.apps.dynatrace.com"
SRO = "https://sro97894.apps.dynatrace.com"


class FakeRedis:
    """The commands the provisioning helpers use, over plain dicts."""

    def __init__(self):
        self.h: dict = {}
        self.s: dict = {}
        self.k: dict = {}
        self.x: dict = {}
        self.lists: dict = {}
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
        self.s.setdefault(key, set()).update(members)

    async def get(self, key):
        return self.k.get(key)

    async def delete(self, *keys):
        for key in keys:
            self.h.pop(key, None)
            self.k.pop(key, None)
            self.x.pop(key, None)

    async def expire(self, key, ttl):
        return True

    async def scan(self, cursor, match=None, count=None):
        return 0, []

    async def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    async def xadd(self, key, mapping, maxlen=None, approximate=True):
        self.seq += 1
        entry_id = f"{1000 + self.seq}-0"
        self.x.setdefault(key, []).append((entry_id, dict(mapping)))
        return entry_id

    async def xrange(self, key, min="-", max="+", count=None):
        entries = list(self.x.get(key, []))
        if min not in ("-", ""):
            # Real Redis treats a leading "(" as exclusive.
            after = min[1:] if min.startswith("(") else None
            entries = ([e for e in entries if e[0] > after] if after
                       else [e for e in entries if e[0] >= min])
        return entries[:count] if count else entries


def setup_module(_module):
    _module._saved_tokens = a.ORBITAL_TOKENS
    a.ORBITAL_TOKENS = ("test-service-token",)


def teardown_module(_module):
    a.ORBITAL_TOKENS = _module._saved_tokens


def setup_function(_fn):
    """A running workshop with three learners: one in the trainer's tenant, one
    in another tenant, one who never showed up."""
    _fn._saved_pool = a.pool
    _fn._saved_provision = a.api_arena_provision
    fake = FakeRedis()
    a.pool = fake
    fake.h[f"live:session:{SID}"] = {
        "title": "Kubernetes 101", "trainingId": "kubernetes-101", "ref": "",
        "trainers": json.dumps([TRAINER]), "state": "running",
        "createdAt": "2026-08-07T09:00:00+00:00",
    }
    fake.s[f"live:session:{SID}:roster"] = {SAME, FOREIGN, ABSENT}
    now = datetime.now(timezone.utc).isoformat()
    fake.h[f"live:session:{SID}:joined"] = {SAME: now, FOREIGN: now}
    fake.h[f"live:session:{SID}:tenants"] = {SAME: COE, FOREIGN: SRO}

    async def _fake_provision(req, request):
        return {"jobId": f"enablement-{req.userId.split('@')[0]}", "deduped": False}
    a.api_arena_provision = _fake_provision


def teardown_function(_fn):
    a.pool = _fn._saved_pool
    a.api_arena_provision = _fn._saved_provision


def _provision_all(emails=None, include_trainer=False):
    body = {"trainerEmail": TRAINER, "tenant": COE, "perUser": {},
            "includeTrainer": include_trainer}
    if emails is not None:
        body["emails"] = emails
    return client.post(f"/api/live/sessions/{SID}/provision-all",
                       headers=BEARER, json=body)


def _detail(email):
    return client.get(f"/api/live/sessions/{SID}?email={email}", headers=BEARER)


def _ack(email, tenant=SRO, status="queued", **extra):
    return client.post(f"/api/live/sessions/{SID}/provision-ack", headers=BEARER,
                       json={"email": email, "tenant": tenant, "status": status, **extra})


def _events(email="", since=""):
    q = f"?email={email}&since={since}"
    return client.get(f"/api/live/sessions/{SID}/events{q}", headers=BEARER)


def _readiness(email=TRAINER, tenant=COE):
    return client.get(
        f"/api/live/sessions/{SID}/readiness?trainerEmail={email}&tenant={tenant}",
        headers=BEARER)


def _states(payload):
    return {r["email"]: r["state"] for r in payload["results"]}


def test_pool_is_faked():
    """Guard: if the app's startup handler ever rebinds pool under TestClient,
    every assertion below would silently run against production Redis."""
    assert isinstance(a.pool, FakeRedis)


# ── provision-all ────────────────────────────────────────────────────────────

def test_foreign_learner_is_requested_not_abandoned():
    """The whole point of the pass: a learner in another tenant used to be
    reported and dropped. Now the trainer's intent is recorded for their own
    tenant to act on."""
    body = _provision_all().json()
    status = {r["email"]: r["status"] for r in body["results"]}
    assert status[SAME] == "queued"           # same tenant — provisioned here
    assert status[FOREIGN] == "requested"     # their tenant will do it
    assert status[ABSENT] == "requested"      # and so will theirs, on arrival
    # The reason is kept so the board can still explain WHY it could not be
    # done here, without making it look like nothing will happen.
    reasons = {r["email"]: r.get("reason") for r in body["results"]}
    assert reasons[FOREIGN] == "foreign-tenant"
    assert reasons[ABSENT] == "not-joined"


def test_provision_all_records_the_intent_on_the_session():
    _provision_all()
    session = a.pool.h[f"live:session:{SID}"]
    assert session["provisionRequestedAt"]
    assert session["provisionRequestedBy"] == TRAINER


def test_same_tenant_learners_are_settled_immediately():
    """They were provisioned here, so their own app must not do it again."""
    _provision_all()
    done = a.pool.h[f"live:session:{SID}:provdone"]
    assert done[SAME] == "queued"
    assert FOREIGN not in done and ABSENT not in done


def test_a_failed_provision_stays_pending_for_the_learners_own_tenant():
    """An error here is not a settlement — the pull path is the retry."""
    async def _boom(req, request):
        raise RuntimeError("no capacity")
    a.api_arena_provision = _boom
    body = _provision_all().json()
    assert {r["email"]: r["status"] for r in body["results"]}[SAME] == "error"
    assert SAME not in a.pool.h.get(f"live:session:{SID}:provdone", {})


def test_a_reclick_unsettles_only_the_chunk_it_names():
    """The app chunks a big roster across several calls. Clearing the whole
    done-map would wipe the earlier chunks and re-provision everyone."""
    _provision_all()
    assert a.pool.h[f"live:session:{SID}:provdone"][SAME] == "queued"
    a.pool.h[f"live:session:{SID}:provdone"][FOREIGN] = "queued"
    # A second chunk that names only SAME must leave FOREIGN's marker alone.
    _provision_all(emails=[SAME])
    assert a.pool.h[f"live:session:{SID}:provdone"][FOREIGN] == "queued"


# ── the pull channel ─────────────────────────────────────────────────────────

def test_detail_tells_only_the_caller_who_needs_an_environment():
    """provisionRequested drives a silent auto-provision, so it must answer
    'do I need one', never 'does anyone'."""
    _provision_all()
    assert _detail(FOREIGN).json()["provisionRequested"] is True
    assert _detail(SAME).json()["provisionRequested"] is False


def test_no_request_means_no_auto_provision():
    """A workshop nobody asked to provision must never build a container."""
    assert _detail(FOREIGN).json()["provisionRequested"] is False


def test_ack_settles_the_request():
    _provision_all()
    assert _detail(FOREIGN).json()["provisionRequested"] is True
    assert _ack(FOREIGN).status_code == 200
    assert _detail(FOREIGN).json()["provisionRequested"] is False


def test_a_failed_ack_also_settles():
    """Otherwise a permanently failing provision retries on every 10s poll
    forever. The trainer's re-click is the retry."""
    _provision_all()
    _ack(FOREIGN, status="failed", error="no capacity")
    assert _detail(FOREIGN).json()["provisionRequested"] is False


def test_ack_rejects_a_bogus_email():
    assert _ack("not-an-email").status_code == 400


def test_ack_404s_on_an_unknown_workshop():
    r = client.post("/api/live/sessions/ws_nope/provision-ack", headers=BEARER,
                    json={"email": FOREIGN, "tenant": SRO, "status": "queued"})
    assert r.status_code == 404


# ── audit trail ──────────────────────────────────────────────────────────────

def test_the_trail_records_the_whole_workflow():
    _provision_all()
    _ack(FOREIGN, status="queued", jobId="enablement-abc")
    kinds = [e["kind"] for e in _events(TRAINER).json()["events"]]
    assert "provision-requested" in kinds     # trainer asked
    assert "provision-started" in kinds       # trainer's tenant did one itself
    assert "provision-accepted" in kinds      # the foreign tenant did the other


def test_a_join_is_recorded_once_so_the_trainer_is_toasted_once():
    """The toast reads this stream. A learner reloading their tab must not
    re-notify the trainer on every refresh."""
    for _ in range(3):
        client.post(f"/api/live/sessions/{SID}/join",
                    json={"email": ABSENT, "tenant": SRO})
    joins = [e for e in _events(TRAINER).json()["events"] if e["kind"] == "joined"]
    assert len(joins) == 1
    assert joins[0]["email"] == ABSENT


def test_since_pages_forward_exclusively():
    """Inclusive paging would re-deliver the last event every poll, which is
    exactly the duplicate-toast bug the id exists to prevent."""
    _provision_all()
    first = _events(TRAINER).json()["events"]
    assert first
    again = _events(TRAINER, since=first[-1]["id"]).json()["events"]
    assert again == []


def test_a_stale_since_does_not_break_the_room():
    assert _events(TRAINER, since="not-a-stream-id").status_code == 200


def test_a_learner_sees_the_trail_masked():
    """It is a cohort-wide record of who is where — the shape of disclosure
    BUG-MASK-1 was about. Kinds stay so the client can still page it."""
    _provision_all()
    rows = _events(FOREIGN).json()["events"]
    assert rows and all("@d***" in r["actor"] or "@c***" in r.get("email", "@c***")
                        for r in rows if r.get("actor"))
    assert not any(r.get("email") == FOREIGN for r in rows)
    assert not any(r.get("tenant") == COE for r in rows)
    assert [r["kind"] for r in rows]


def test_an_audit_failure_never_fails_the_action_it_records():
    """A trainer's provision-all must not 500 because Redis hiccuped on a log
    write."""
    async def _broken_xadd(*args, **kwargs):
        raise RuntimeError("redis down")
    a.pool.xadd = _broken_xadd
    assert _provision_all().status_code == 200


# ── readiness board ──────────────────────────────────────────────────────────

def test_readiness_shows_requested_once_the_trainer_has_asked():
    before = _states(_readiness().json())
    assert before[FOREIGN] == "foreign"
    assert before[ABSENT] == "not-joined"
    _provision_all()
    after = _states(_readiness().json())
    assert after[FOREIGN] == "requested"
    # Still honest about the empty seat — a trainer has to act on that one.
    assert after[ABSENT] == "not-joined"


def test_readiness_carries_the_bound_tenant():
    """So the board can show who runs where instead of only encoding it as a
    state string."""
    rows = {r["email"]: r.get("tenant") for r in _readiness().json()["results"]}
    assert rows[FOREIGN] == SRO
    assert rows[SAME] == COE
    assert rows[TRAINER] == COE      # the tenant they are asking from


def test_an_acked_learner_stops_showing_as_requested():
    _provision_all()
    _ack(FOREIGN)
    assert _states(_readiness().json())[FOREIGN] == "foreign"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup_function(fn)
            try:
                fn()
                print(f"ok {name}")
            finally:
                teardown_function(fn)
    print("all live-provisioning tests passed")

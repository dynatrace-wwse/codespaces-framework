"""Per-learner environment controls — terminate / reprovision one learner.

Before these routes the only tool was terminate-all, so "this one student is in
the wrong tenant" cost the whole cohort their environments.

The assertion that matters most is scoping: a job belonging to a DIFFERENT
workshop of the SAME training must not be touched. Same person, same training,
wrong room is a real configuration — the cohort-wide route matches on
(arena_user, training_id) and would hit it; _workshop_jobs matches on
workshop_id first, which is what makes a single-learner kill safe.

Gate is is_trainer, not is_owner: a co-trainer troubleshooting a stuck learner
is exactly what these buttons exist for.

Runnable: /home/ops/ops-venv/bin/python -m pytest dashboard/test_live_learner_env.py
"""

import json

import dashboard.app as a
from dashboard import live_sessions as ls
from dashboard import masking
from fastapi.testclient import TestClient

client = TestClient(a.app, raise_server_exceptions=False)

BEARER = {"Authorization": "Bearer test-service-token"}
SID = "ws_env-1"
OTHER_SID = "ws_env-2"
TRAINER = "trainer@dynatrace.com"
CO = "co@dynatrace.com"
LEARNER = "learner@customer.com"
OTHER = "other@customer.com"
STRANGER = "stranger@customer.com"
COE = "https://geu80787.apps.dynatrace.com"
SRO = "https://sro97894.apps.dynatrace.com"


class FakeRedis:
    def __init__(self):
        self.h, self.s, self.k, self.lists = {}, {}, {}, {}
        self.x, self.z = {}, {}
        self.published = []
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

    async def exists(self, key):
        return 1 if (key in self.h or key in self.k) else 0

    async def delete(self, *keys):
        for key in keys:
            self.h.pop(key, None)

    async def expire(self, key, ttl):
        return True

    async def publish(self, channel, message):
        self.published.append((channel, message))

    async def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    async def scan_iter(self, match=None, count=None):
        prefix = (match or "*").rstrip("*")
        for key in list(self.h):
            if key.startswith(prefix):
                yield key

    async def scan(self, cursor, match=None, count=None):
        prefix = (match or "*").rstrip("*")
        return 0, [k for k in self.h if k.startswith(prefix)]

    async def xadd(self, key, mapping, maxlen=None, approximate=True):
        self.seq += 1
        self.x.setdefault(key, []).append((f"{1000 + self.seq}-0", dict(mapping)))
        return f"{1000 + self.seq}-0"

    async def xrange(self, key, min="-", max="+", count=None):
        return list(self.x.get(key, []))

    async def zrevrange(self, key, start, end):
        return list(self.z.get(key, []))

    async def zrem(self, key, *members):
        return 0


def setup_module(_module):
    _module._saved_tokens = a.ORBITAL_TOKENS
    a.ORBITAL_TOKENS = ("test-service-token",)


def teardown_module(_module):
    a.ORBITAL_TOKENS = _module._saved_tokens


def _job(job_id, email, workshop_id, tenant=COE, training="kubernetes-101",
         terminating=""):
    a.pool.h[f"job:running:enablement-{job_id}"] = {
        "job_id": f"enablement-{job_id}", "arena_user": email,
        "arena_tenant": tenant, "training_id": training,
        "workshop_id": workshop_id, "terminating": terminating,
    }


def setup_function(_fn):
    _fn._saved_pool = a.pool
    _fn._saved_provision = a.api_arena_provision
    a.pool = FakeRedis()
    a.pool.h[f"live:session:{SID}"] = {
        "title": "Kubernetes 101", "trainingId": "kubernetes-101",
        "trainers": json.dumps([TRAINER, CO]), "state": "running",
        "ownerTenant": COE, "createdAt": "2026-08-13T09:00:00+00:00",
    }
    a.pool.s[f"live:session:{SID}:roster"] = {LEARNER, OTHER}
    a.pool.h[f"live:session:{SID}:joined"] = {LEARNER: "2026-08-13T10:00:00+00:00"}
    a.pool.h[f"live:session:{SID}:tenants"] = {LEARNER: COE}


def teardown_function(_fn):
    a.pool = _fn._saved_pool
    a.api_arena_provision = _fn._saved_provision


def _terminate(email, trainer=TRAINER, sid=SID):
    return client.post(f"/api/live/sessions/{sid}/learner/{email}/terminate",
                       headers=BEARER, json={"trainerEmail": trainer})


def _reprovision(email, trainer=TRAINER, tenant=COE):
    return client.post(f"/api/live/sessions/{SID}/learner/{email}/reprovision",
                       headers=BEARER, json={"trainerEmail": trainer,
                                             "tenant": tenant})


def _stub_provision(result=None, raises=None):
    async def _fake(req, request):
        if raises:
            raise raises
        _fake.calls.append(req)
        return result or {"jobId": "enablement-new1", "deduped": False}
    _fake.calls = []
    a.api_arena_provision = _fake
    return _fake


# ── Terminate ────────────────────────────────────────────────────────────────

def test_terminate_kills_only_that_learners_job():
    _job("a", LEARNER, SID)
    _job("b", OTHER, SID)
    r = _terminate(LEARNER)
    assert r.status_code == 200
    assert r.json()["terminated"] == ["enablement-a"]
    assert a.pool.h["job:running:enablement-a"]["terminating"] == "1"
    assert a.pool.h["job:running:enablement-b"]["terminating"] == ""
    assert ("ops:terminate", "enablement-a") in a.pool.published


def test_terminate_does_not_touch_another_workshop_of_the_same_training():
    """The scoping assertion. terminate-all matches (arena_user, training_id)
    and would kill this; workshop_id is what makes a per-learner kill safe."""
    _job("mine", LEARNER, SID)
    _job("elsewhere", LEARNER, OTHER_SID)
    r = _terminate(LEARNER)
    assert r.json()["terminated"] == ["enablement-mine"]
    assert a.pool.h["job:running:enablement-elsewhere"]["terminating"] == ""


def test_terminate_falls_back_to_training_for_pre_workshop_id_jobs():
    _job("legacy", LEARNER, workshop_id="")
    assert _terminate(LEARNER).json()["terminated"] == ["enablement-legacy"]


def test_terminate_is_idempotent():
    _job("a", LEARNER, SID, terminating="1")
    r = _terminate(LEARNER)
    assert r.json() == {"terminated": [], "count": 0, "alreadyTerminating": 1}


def test_terminate_with_no_environment_is_a_quiet_success():
    """Nothing running is not an error — the trainer asked for a state, and
    that state already holds."""
    r = _terminate(LEARNER)
    assert r.status_code == 200 and r.json()["count"] == 0


def test_co_trainer_may_terminate_a_learner():
    _job("a", LEARNER, SID)
    assert _terminate(LEARNER, trainer=CO).json()["count"] == 1


def test_non_trainer_gets_403_and_kills_nothing():
    _job("a", LEARNER, SID)
    assert _terminate(LEARNER, trainer=STRANGER).status_code == 403
    assert a.pool.h["job:running:enablement-a"]["terminating"] == ""


def test_terminate_404s_on_an_unknown_workshop():
    assert _terminate(LEARNER, sid="ws_nope").status_code == 404


def test_terminate_requires_authentication():
    r = client.post(f"/api/live/sessions/{SID}/learner/{LEARNER}/terminate",
                    json={"trainerEmail": TRAINER})
    assert r.status_code == 401


def test_terminate_emits_an_audit_event():
    _job("a", LEARNER, SID)
    _terminate(LEARNER)
    kinds = [e[1]["kind"] for e in a.pool.x[f"live:session:{SID}:events"]]
    assert ls.EVENT_ENV_TERMINATED in kinds


# ── Reprovision ──────────────────────────────────────────────────────────────

def test_reprovision_terminates_then_builds():
    _job("old", LEARNER, SID)
    fake = _stub_provision()
    r = _reprovision(LEARNER)
    assert r.status_code == 200
    body = r.json()
    assert body["terminated"] == ["enablement-old"]
    assert body["status"] == "queued" and body["jobId"] == "enablement-new1"
    assert len(fake.calls) == 1
    assert fake.calls[0].userId == LEARNER
    assert a.pool.h[f"live:session:{SID}:provdone"][LEARNER] == "queued"


def test_reprovision_reports_already_active_honestly():
    _stub_provision({"jobId": "enablement-x", "deduped": True})
    assert _reprovision(LEARNER).json()["status"] == "already-active"


def test_reprovision_of_a_foreign_bound_learner_goes_through_the_pull_channel():
    """The trainer's tenant cannot mint for a learner bound elsewhere. Rather
    than fail, re-arm the request their own tenant's app polls for."""
    a.pool.h[f"live:session:{SID}:tenants"][LEARNER] = SRO
    a.pool.h[f"live:session:{SID}:provdone"] = {LEARNER: "failed"}
    fake = _stub_provision()
    body = _reprovision(LEARNER, tenant=COE).json()
    assert body["status"] == "requested"
    assert body["tenant"] == SRO
    assert fake.calls == [], "must not provision into the wrong tenant"
    assert LEARNER not in a.pool.h[f"live:session:{SID}:provdone"], \
        "the settled marker must be cleared or their app will ignore the request"
    assert a.pool.h[f"live:session:{SID}"]["provisionRequestedAt"]


def test_reprovision_still_terminates_before_handing_off():
    a.pool.h[f"live:session:{SID}:tenants"][LEARNER] = SRO
    _job("old", LEARNER, SID, tenant=SRO)
    _stub_provision()
    body = _reprovision(LEARNER, tenant=COE).json()
    assert body["terminated"] == ["enablement-old"]
    assert body["status"] == "requested"


def test_reprovision_reports_a_provision_failure_instead_of_500ing():
    from fastapi import HTTPException
    _stub_provision(raises=HTTPException(status_code=503, detail="no capacity"))
    body = _reprovision(LEARNER).json()
    assert body["status"] == "error" and "no capacity" in body["error"]
    kinds = [e[1]["kind"] for e in a.pool.x[f"live:session:{SID}:events"]]
    assert ls.EVENT_PROVISION_FAILED in kinds


def test_co_trainer_may_reprovision_but_a_stranger_may_not():
    _stub_provision()
    assert _reprovision(LEARNER, trainer=CO).status_code == 200
    assert _reprovision(LEARNER, trainer=STRANGER).status_code == 403


def test_reprovision_rejects_a_malformed_email():
    r = client.post(f"/api/live/sessions/{SID}/learner/not-an-email/reprovision",
                    headers=BEARER, json={"trainerEmail": TRAINER})
    assert r.status_code == 400


# ── Readiness board: attendance + wrong-tenant flag ──────────────────────────

def _readiness(trainer=TRAINER, tenant=COE):
    return client.get(f"/api/live/sessions/{SID}/readiness"
                      f"?trainerEmail={trainer}&tenant={tenant}",
                      headers=BEARER).json()


def _row(payload, email):
    return next(r for r in payload["results"] if r["email"] == email)


def test_readiness_reports_attendance_states():
    a.pool.h[f"live:session:{SID}:tenants"][OTHER] = SRO   # bound, never present
    rows = _readiness()
    assert _row(rows, LEARNER)["attendance"] == ls.ATTENDANCE_PRESENT
    assert _row(rows, OTHER)["attendance"] == ls.ATTENDANCE_BOUND
    assert _row(rows, TRAINER)["attendance"] == "trainer"


def test_readiness_reports_registered_when_only_on_the_roster():
    del a.pool.h[f"live:session:{SID}:tenants"][LEARNER]
    a.pool.h[f"live:session:{SID}:joined"] = {}
    assert _row(_readiness(), LEARNER)["attendance"] == ls.ATTENDANCE_REGISTERED


def test_readiness_flags_a_learner_running_in_the_wrong_tenant():
    """Orbital sees every tenant's jobs (job:running:* is global), which is why
    it can spot this at all. Nothing is torn down — the trainer decides."""
    _job("a", LEARNER, SID, tenant=SRO)          # bound to COE, running on SRO
    row = _row(_readiness(), LEARNER)
    # envTenant is the canonical environment id, not the URL the job recorded —
    # arena_tenant arrives in several shapes and only the id compares.
    assert row["envTenant"] == "sro97894"
    assert row["tenantMismatch"] is True


def test_readiness_does_not_cry_wolf_when_there_is_no_environment():
    row = _row(_readiness(), LEARNER)
    assert row["tenantMismatch"] is False
    assert "envTenant" not in row, "a row without an environment gains no field"


def test_readiness_no_mismatch_when_the_environment_matches():
    _job("a", LEARNER, SID, tenant=COE)
    assert _row(_readiness(), LEARNER)["tenantMismatch"] is False


def test_masking_hides_env_tenant_but_keeps_attendance():
    payload = {"results": [{"email": LEARNER, "tenant": COE, "envTenant": SRO,
                            "attendance": "present", "tenantMismatch": True,
                            "state": "ready"}]}
    row = masking.mask_readiness(payload)["results"][0]
    assert row["envTenant"] != SRO and row["tenant"] != COE
    # Non-identifying: they say nothing about WHICH tenant.
    assert row["attendance"] == "present" and row["tenantMismatch"] is True
    assert row["state"] == "ready"

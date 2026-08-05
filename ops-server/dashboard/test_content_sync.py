"""Server-side content reconciliation (E6b).

The behaviours worth pinning are mostly about honesty: a tenant Orbital cannot
reach must not look like a tenant that synced fine, a failed import must not be
counted as an import, and the loop must not die on a bad pass.

Run: /home/ops/ops-venv/bin/python -m pytest dashboard/test_content_sync.py -q
"""

import asyncio

import pytest

from dashboard import content_sync as cs

MANIFEST = {"sources": [
    {"repo": "dynatrace-wwse/enablement-kubernetes-101", "version": "abc1234",
     "category": "hands-on"},
    {"repo": "dynatrace-wwse/enablement-dynatrace-log-ingest-101", "version": "def5678",
     "category": "hands-on"},
]}

CRED = {"token": "deploy-bearer", "source": "oauth"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("ORBITAL_TOKEN", "orb-secret")


def _sync(**kw):
    kw.setdefault("manifest", MANIFEST)
    kw.setdefault("credential", CRED)
    return asyncio.run(cs.sync_tenant("https://t.example.com", **kw))


def test_every_profile_source_is_imported():
    calls = []

    async def invoke(tenant, bearer, payload):
        calls.append((tenant, bearer, payload))
        return {"labId": "x"}

    out = _sync(invoke=invoke)
    assert out["status"] == "ok" and out["imported"] == 2 and out["failed"] == 0
    assert [c[2]["repoUrl"] for c in calls] == [
        "https://github.com/dynatrace-wwse/enablement-kubernetes-101",
        "https://github.com/dynatrace-wwse/enablement-dynatrace-log-ingest-101",
    ]
    assert all(c[1] == "deploy-bearer" for c in calls)


def test_the_import_carries_the_orbital_token_as_its_proof():
    # This is the whole authentication story: no user exists, so the request
    # proves itself with a token only Orbital knows.
    payload = cs.import_payload(MANIFEST["sources"][0], "orb-secret")
    assert payload["orbitalToken"] == "orb-secret"


def test_reconciliation_never_resets_learner_progress():
    # A scheduled sync that wiped progress would be a data-loss bug that only
    # shows up hours after the deploy that caused it.
    assert cs.import_payload(MANIFEST["sources"][0], "t")["resetProgress"] is False


def test_content_is_public_and_fetched_through_the_proxy():
    p = cs.import_payload(MANIFEST["sources"][0], "t")
    assert p["isPublic"] is True and p["useContentService"] is True
    assert p["contentSha"] == "abc1234"


def test_an_unreachable_tenant_is_named_not_counted_as_done():
    out = _sync(credential={"token": ""}, invoke=None)
    assert out["status"] == "no-credential"
    assert "imported" not in out  # nothing was imported; do not imply otherwise


def test_a_failed_import_is_reported_not_counted():
    async def invoke(tenant, bearer, payload):
        if "kubernetes" in payload["repoUrl"]:
            return {"error": "HTTP 500"}
        return {"labId": "x"}

    out = _sync(invoke=invoke)
    assert out["status"] == "partial" and out["imported"] == 1 and out["failed"] == 1
    assert "kubernetes" in out["errors"][0]


def test_a_thrown_import_does_not_abort_the_remaining_sources():
    seen = []

    async def invoke(tenant, bearer, payload):
        seen.append(payload["repoUrl"])
        if "kubernetes" in payload["repoUrl"]:
            raise RuntimeError("connection reset")
        return {"labId": "x"}

    out = _sync(invoke=invoke)
    assert len(seen) == 2 and out["failed"] == 1 and out["imported"] == 1


def test_no_orbital_token_disables_the_sync_rather_than_importing_unauthenticated(monkeypatch):
    monkeypatch.delenv("ORBITAL_TOKEN", raising=False)
    called = {"n": 0}

    async def invoke(*a):
        called["n"] += 1
        return {}

    out = _sync(invoke=invoke)
    assert out["status"] == "disabled" and called["n"] == 0


def test_a_broken_manifest_is_an_error_not_a_crash():
    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("profile missing")

    # Non-empty so it is truthy: an EMPTY mapping is legitimately falsy and takes
    # the "no sources" path, which is a different (and correct) outcome.
    out = _sync(manifest=Boom(sources=[]))
    assert out["status"] == "error" and "profile missing" in out["detail"]


def test_an_empty_profile_is_ok_with_nothing_done():
    out = _sync(manifest={"sources": []})
    assert out["status"] == "ok" and out["imported"] == 0


def test_sync_all_reports_one_row_per_tenant():
    async def invoke(tenant, bearer, payload):
        return {"labId": "x"}

    rows = asyncio.run(cs.sync_all(["https://a.example.com", "https://b.example.com"],
                                   manifest=MANIFEST, credential=CRED, invoke=invoke))
    assert [r["tenant"] for r in rows] == ["https://a.example.com", "https://b.example.com"]
    assert all(r["status"] == "ok" for r in rows)


def test_the_loop_survives_a_failing_pass(monkeypatch):
    # A reconciliation that stops silently is worse than one that fails loudly
    # every six hours, so a raising pass must not end the loop.
    passes = {"n": 0}

    async def boom(*a, **k):
        passes["n"] += 1
        if passes["n"] == 1:
            raise RuntimeError("redis down")
        raise asyncio.CancelledError

    monkeypatch.setattr(cs, "sync_all", boom)
    # The loop floors its sleep at 60s so a misconfigured interval cannot spin.
    # Without this the test would honestly wait that minute out.
    # `cs.asyncio` IS the asyncio module, so the replacement must close over the
    # original — referring to asyncio.sleep by name would call itself.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(cs.asyncio, "sleep", lambda _s: real_sleep(0))

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await cs.sync_loop(interval_s=60)

    asyncio.run(run())
    assert passes["n"] == 2  # it came back after the failure


def test_only_tenants_orbital_holds_credentials_for_are_scheduled():
    # The honest scope of this feature. If this list ever silently grows to
    # "every tenant", sync_tenant would start reporting no-credential rows for
    # tenants that were never meant to be in it.
    from dashboard import app_deploy as dep
    assert set(cs.auto_tenants()) == {
        t for t in (dep.COE_TENANT_URL, dep.SRO_TENANT_URL, dep.SPRINT_TENANT_URL) if t}

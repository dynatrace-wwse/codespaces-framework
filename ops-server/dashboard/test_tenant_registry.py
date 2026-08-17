"""Tenant-attribution registry (EPIC-002 §9) — dashboard/tenant_registry.py.

Two halves, same approach as test_masking.py / test_live_auth.py:
  - pure shaping/merge logic (no Redis, no FastAPI);
  - endpoint auth gating over HTTP with Starlette's TestClient (anonymous 401,
    wrong bearer 401, service bearer passes — asserted via pre-Redis validation
    responses so no Redis is needed).

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_tenant_registry.py
  - standalone (logic tests only): python3 -m dashboard.test_tenant_registry
"""

from dashboard import tenant_registry as tr


# ── shape_deploy ─────────────────────────────────────────────────────────────

def test_shape_deploy_drops_empty_fields():
    f = tr.shape_deploy("token", now="2026-08-03T10:00:00+00:00")
    assert f == {"via": "token", "lastDeploy": "2026-08-03T10:00:00+00:00"}


def test_shape_deploy_keeps_provided_attribution():
    f = tr.shape_deploy("oauth-bootstrap", account_urn="urn:dtaccount:abc",
                        client_id="dt0s02.XYZ", deployer="admin@acme.com",
                        app_version="1.0.255", now="2026-08-03T10:00:00+00:00")
    assert f["accountUrn"] == "urn:dtaccount:abc"
    assert f["clientId"] == "dt0s02.XYZ"
    assert f["deployerEmail"] == "admin@acme.com"
    assert f["appVersion"] == "1.0.255"
    assert f["via"] == "oauth-bootstrap"


def test_shape_deploy_friendly_name_set_when_provided():
    f = tr.shape_deploy("oauth-bootstrap", friendly_name="ACME Corp (prod)",
                        now="2026-08-03T10:00:00+00:00")
    assert f["friendlyName"] == "ACME Corp (prod)"
    # empty → dropped, so an HSET never blanks a name captured earlier
    f2 = tr.shape_deploy("token", now="t2")
    assert "friendlyName" not in f2


def test_deploy_vias_match_locked_design():
    assert set(tr.DEPLOY_VIAS) == {"sso-deploy", "auto", "token", "oauth-bootstrap"}


# ── audience / accountName / plan ────────────────────────────────────────────

def test_audiences_match_the_register_form():
    assert set(tr.AUDIENCES) == {"internal", "customer", "partner", "prospect"}


def test_normalize_audience_accepts_known_values_case_insensitively():
    assert tr.normalize_audience(" Customer ") == "customer"
    assert tr.normalize_audience("PARTNER") == "partner"


def test_normalize_audience_rejects_anything_else():
    # "" is the unselected dropdown; the rest are typos or hand-crafted payloads.
    for bad in ("", "   ", "internal-ish", "vendor", "Customer;DROP", None):
        assert tr.normalize_audience(bad) == ""


def test_shape_deploy_stores_audience_and_account_readings():
    f = tr.shape_deploy("oauth-bootstrap", audience="Customer",
                        account_name="ACME AG", plan="paid", now="t1")
    assert f["audience"] == "customer"
    assert f["accountName"] == "ACME AG"
    assert f["plan"] == "paid"


def test_shape_deploy_drops_unknown_audience_rather_than_storing_it():
    f = tr.shape_deploy("oauth-bootstrap", audience="vendor", now="t1")
    assert "audience" not in f


def test_blank_account_reading_never_blanks_an_earlier_one():
    # The probe returns "" whenever the client lacks account-idm-read, which is the
    # COMMON case. A re-register from a narrower client must not erase what a broader
    # one once read — same drop-empties rule that protects accountUrn/clientId.
    first = tr.merge_fields({}, tr.shape_deploy(
        "oauth-bootstrap", audience="customer", account_name="ACME AG",
        plan="paid", now="t1"))
    second = tr.merge_fields(first, tr.shape_deploy("oauth-bootstrap", now="t2"))
    assert "accountName" not in second
    assert "plan" not in second
    assert "audience" not in second


# ── merge_fields (firstSeen semantics) ───────────────────────────────────────

def test_merge_fields_stamps_first_seen_once():
    first = tr.merge_fields({}, tr.shape_deploy("token", now="2026-08-01T00:00:00+00:00"))
    assert first["firstSeen"] == "2026-08-01T00:00:00+00:00"
    # second deploy: existing entry already has firstSeen → NOT restamped
    second = tr.merge_fields(first, tr.shape_deploy("auto", now="2026-08-02T00:00:00+00:00"))
    assert "firstSeen" not in second
    assert second["lastDeploy"] == "2026-08-02T00:00:00+00:00"


def test_merge_fields_never_blanks_earlier_attribution():
    # A later token deploy carries no accountUrn/clientId — shape_deploy drops
    # the empties, so an HSET of the merged fields leaves the bootstrap values.
    later = tr.merge_fields({"firstSeen": "x", "accountUrn": "urn:dtaccount:abc"},
                            tr.shape_deploy("token", now="t2"))
    assert "accountUrn" not in later  # untouched in the hash


# ── merge_identity (runtime backstop) ────────────────────────────────────────

def test_merge_identity_fills_deployer_email_only_when_empty():
    out = tr.merge_identity({}, email="admin@acme.com", now="t1")
    assert out["deployerEmail"] == "admin@acme.com"
    assert out["identityEmail"] == "admin@acme.com"
    assert out["lastSeen"] == "t1"
    # deploy-time attribution wins: existing deployerEmail is never replaced
    out2 = tr.merge_identity({"deployerEmail": "original@acme.com", "firstSeen": "t0"},
                             email="other@acme.com", now="t2")
    assert "deployerEmail" not in out2
    assert out2["identityEmail"] == "other@acme.com"


def test_merge_identity_always_updates_identity_fields_and_urn():
    out = tr.merge_identity(
        {"firstSeen": "t0", "identityName": "Old", "accountUrn": "urn:dtaccount:old"},
        email="a@b.com", name="New Name", account_urn="urn:dtaccount:new", now="t3")
    assert out["identityName"] == "New Name"
    assert out["accountUrn"] == "urn:dtaccount:new"
    assert out["lastSeen"] == "t3"


def test_merge_identity_friendly_name_set_when_provided_never_blanked():
    # provided → set (latest registrant-supplied name wins)
    out = tr.merge_identity({"firstSeen": "t0", "friendlyName": "Old Name"},
                            email="a@b.com", friendly_name="New Name", now="t1")
    assert out["friendlyName"] == "New Name"
    # empty → absent from the merge, so the stored name survives the HSET
    out2 = tr.merge_identity({"firstSeen": "t0", "friendlyName": "Kept Name"},
                             email="a@b.com", now="t2")
    assert "friendlyName" not in out2


def test_merge_identity_stamps_first_seen_for_unseen_tenant():
    out = tr.merge_identity({}, email="a@b.com", now="t1")
    assert out["firstSeen"] == "t1"


def test_registry_key_shape():
    assert tr.registry_key("abc12345") == "tenant:registry:abc12345"
    assert tr.INDEX_KEY == "tenant:registry:index"


# ── Endpoint auth gating (TestClient; pre-Redis assertions) ──────────────────

def _client():
    import dashboard.app as a
    from fastapi.testclient import TestClient
    return a, TestClient(a.app, raise_server_exceptions=False)


BEARER = {"Authorization": "Bearer test-service-token"}


def setup_module(_module):
    """Accept a known bearer regardless of the (test-runner) environment —
    same pattern as test_live_auth.py."""
    import dashboard.app as a
    _module._saved_tokens = a.ORBITAL_TOKENS
    a.ORBITAL_TOKENS = ("test-service-token",)


def teardown_module(_module):
    import dashboard.app as a
    a.ORBITAL_TOKENS = _module._saved_tokens


def test_register_identity_anonymous_401():
    _, client = _client()
    r = client.post("/api/tenants/register-identity",
                    json={"tenant": "https://abc12345.apps.dynatrace.com",
                          "email": "a@b.com"})
    assert r.status_code == 401


def test_register_identity_wrong_bearer_401():
    _, client = _client()
    r = client.post("/api/tenants/register-identity", json={},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_register_identity_spoofed_x_auth_user_still_401():
    # X-Auth-User never grants this endpoint (service bearer only) — and nginx
    # clears the header on anonymous paths anyway.
    _, client = _client()
    r = client.post("/api/tenants/register-identity",
                    json={"tenant": "https://abc12345.apps.dynatrace.com", "email": "a@b.com"},
                    headers={"X-Auth-User": "someone"})
    assert r.status_code == 401


def test_register_identity_bearer_passes_gate_missing_tenant_400():
    # Validation runs only AFTER auth and BEFORE any Redis access.
    _, client = _client()
    r = client.post("/api/tenants/register-identity", json={}, headers=BEARER)
    assert r.status_code == 400


def test_register_identity_bearer_missing_identity_fields_400():
    _, client = _client()
    r = client.post("/api/tenants/register-identity",
                    json={"tenant": "https://abc12345.apps.dynatrace.com"}, headers=BEARER)
    assert r.status_code == 400


def test_register_identity_bearer_non_dynatrace_domain_403():
    _, client = _client()
    r = client.post("/api/tenants/register-identity",
                    json={"tenant": "https://evil.example.com", "email": "a@b.com"},
                    headers=BEARER)
    assert r.status_code == 403


def test_registry_get_anonymous_401():
    _, client = _client()
    r = client.get("/api/tenants/registry")
    assert r.status_code == 401


def test_registry_get_wrong_bearer_401():
    _, client = _client()
    r = client.get("/api/tenants/registry", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_registry_get_service_bearer_full_payload(monkeypatch):
    a, client = _client()
    async def fake_list(pool):
        return [{"tenant": "abc12345", "deployerEmail": "admin@acme.com",
                 "via": "oauth-bootstrap"}]
    monkeypatch.setattr(a.tenant_registry, "list_entries", fake_list)
    r = client.get("/api/tenants/registry", headers=BEARER)
    assert r.status_code == 200
    tenants = r.json()["tenants"]
    # auth-gated endpoint → full, unmasked values
    assert tenants[0]["deployerEmail"] == "admin@acme.com"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if (name.startswith("test_") and callable(fn)
                and fn.__code__.co_argcount == 0 and "_client" not in fn.__code__.co_names):
            fn()
            print(f"ok {name}")
    print("logic tests passed (run under pytest for the endpoint auth tests)")

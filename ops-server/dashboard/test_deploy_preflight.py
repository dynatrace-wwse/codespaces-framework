"""The deploy gate: verified capability, never inferred capability.

Every test here pins a failure that reached a learner during the APAC bootcamp
on 2026-08-19, because in each case the deploy reported a capability it had
never exercised:

  * `hpm49270`  — passed preflight, then killed a session with
                  "ActiveGate token mint failed: SSO client_credentials failed
                  (HTTP 400)". The old check asked SSO for a bearer, after the
                  install, as a warning, gated behind `mint_ready`.
  * `jxh41488`  — deploy recorded `no allowlist object (prod — outbound open)`;
                  an hour later the learner's mint died on
                  "Blocked request to 'sso.dynatrace.com' (host not in allowlist)".
  * `bth17199` / `uxn36332` — app could not reach Orbital; the deploy blamed
                  ORBITAL_TOKEN on a server the SEs do not own.
  * `bos01241`  — `document:documents:admin` stamped by SSO, refused by the API,
                  so four trainings silently stayed missing from the catalog.

Run: /home/ops/ops-venv/bin/python -m pytest dashboard/test_deploy_preflight.py -q
"""

import asyncio

import pytest

from dashboard import app_deploy as dep


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or ("{}" if payload is None else "")
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


class _Client:
    """httpx.AsyncClient stand-in driven by a caller-supplied handler."""

    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, **kw):
        return self._handler("POST", url, json)

    async def delete(self, url, headers=None, **kw):
        return self._handler("DELETE", url, None)

    async def get(self, url, headers=None, **kw):
        return self._handler("GET", url, None)


def _patch_client(monkeypatch, handler):
    monkeypatch.setattr(dep.httpx, "AsyncClient", lambda *a, **k: _Client(handler))


def _no_sleep(monkeypatch):
    """The retry ladders sleep; tests must not."""
    async def _instant(_seconds):
        return None
    monkeypatch.setattr(dep.asyncio, "sleep", _instant)


# ── ActiveGate preflight ─────────────────────────────────────────────────────

def _ag(monkeypatch, *, grantable, mint_status=201):
    """Run _preflight_activegate with SSO granting only `grantable` scopes."""
    async def _bearer(_sso, _cid, _csec, _resource, scope):
        return ("bearer", 200, "") if scope in grantable else (None, 400, "invalid_request")
    monkeypatch.setattr(dep, "_oauth_bearer", _bearer)
    _patch_client(monkeypatch, lambda method, url, body:
                  _Resp(mint_status, {"id": "ag-1"} if mint_status in (200, 201) else None,
                        text='{"error":"denied"}'))
    return asyncio.run(dep._preflight_activegate(
        "https://sso", "cid", "sec", "https://t.apps.dynatrace.com", "t"))


def test_activegate_ok_on_the_classic_scope(monkeypatch):
    ok, detail = _ag(monkeypatch, grantable={dep.AG_SCOPES[0]})
    assert ok is True
    assert dep.AG_SCOPES[0] in detail


def test_activegate_falls_back_to_the_gen3_scope(monkeypatch):
    """The hpm49270 shape: the classic scope is not in the client's catalog."""
    ok, detail = _ag(monkeypatch, grantable={dep.AG_SCOPES[1]})
    assert ok is True
    assert dep.AG_SCOPES[1] in detail


def test_activegate_failure_names_both_scopes_and_the_recreate_rule(monkeypatch):
    ok, detail = _ag(monkeypatch, grantable=set())
    assert ok is False
    for scope in dep.AG_SCOPES:
        assert scope in detail
    # Scopes cannot be edited on an existing client — saying "grant it" without
    # that sends an SE to a UI that will not let them do it.
    assert "creating a new one" in detail
    assert "Kubernetes" in detail


def test_activegate_failure_does_not_leak_the_response_body(monkeypatch):
    """Token-endpoint bodies can carry an access_token; safe_error_detail gates them."""
    async def _bearer(*a, **k):
        return ("bearer", 200, "")
    monkeypatch.setattr(dep, "_oauth_bearer", _bearer)
    _patch_client(monkeypatch, lambda m, u, b: _Resp(
        403, None, text='{"access_token":"SECRET-VALUE","error":"denied"}'))
    ok, detail = asyncio.run(dep._preflight_activegate(
        "https://sso", "cid", "sec", "https://t.apps.dynatrace.com", "t"))
    assert ok is False
    assert "SECRET-VALUE" not in detail


# ── Outbound self-test ───────────────────────────────────────────────────────

def _selftest(monkeypatch, payload, status=200):
    _patch_client(monkeypatch, lambda m, u, b: _Resp(status, payload))
    _no_sleep(monkeypatch)
    return asyncio.run(dep._selftest_outbound("bearer", "https://t.apps.dynatrace.com"))


def test_selftest_ok(monkeypatch):
    out = _selftest(monkeypatch, {"ok": True, "blocked": [], "hosts": []})
    assert out["status"] == "ok"
    assert out["blocked"] == []


def test_selftest_reports_blocked_hosts_and_the_remedy(monkeypatch):
    out = _selftest(monkeypatch, {
        "ok": False, "blocked": ["sso.dynatrace.com"],
        "remedy": "Add these hosts to Settings > Outbound connections"})
    assert out["status"] == "blocked"
    assert out["blocked"] == ["sso.dynatrace.com"]
    assert "Outbound connections" in out["detail"]


def test_an_app_without_the_function_is_UNKNOWN_not_ok(monkeypatch):
    """The distinction that keeps this honest.

    An older app has not proven anything, and reporting it as reachable is the
    exact class of bug this whole gate exists to remove. It must also not fail
    the deploy — a check that could not run is not evidence of a broken tenant.
    """
    out = _selftest(monkeypatch, None, status=404)
    assert out["status"] == "unknown"
    assert out["blocked"] == []


def test_an_unreachable_function_is_UNKNOWN_not_blocked(monkeypatch):
    out = _selftest(monkeypatch, None, status=503)
    assert out["status"] == "unknown"


def test_selftest_error_never_raises(monkeypatch):
    """This runs mid-deploy; an exception here must not fail the install."""
    def _boom(*a, **k):
        raise RuntimeError("connection reset")
    _patch_client(monkeypatch, _boom)
    _no_sleep(monkeypatch)
    out = asyncio.run(dep._selftest_outbound("bearer", "https://t.apps.dynatrace.com"))
    assert out["status"] == "unknown"
    assert "connection reset" in out["detail"]


# ── Self-test + repair ───────────────────────────────────────────────────────

def test_repair_is_not_attempted_when_nothing_is_blocked(monkeypatch):
    """We must never touch a customer's security settings without proof.

    Writing an enforced allowlist onto a tenant that was genuinely open would
    be us causing the outage we are trying to prevent.
    """
    calls = []

    async def _selftest_stub(_t, _u):
        return {"status": "ok", "blocked": [], "detail": "fine"}

    async def _ensure(*a, **k):
        calls.append(a)
        return "written"

    monkeypatch.setattr(dep, "_selftest_outbound", _selftest_stub)
    monkeypatch.setattr(dep, "_ensure_outbound_allowlist", _ensure)
    out = asyncio.run(dep._selftest_and_repair("bearer", "https://t.apps.dynatrace.com"))
    assert out["status"] == "ok"
    assert calls == [], "the allowlist must not be written when nothing is blocked"


def test_a_blocked_tenant_is_repaired_and_re_tested(monkeypatch):
    """Propagation delay after the write is undocumented, so we must ask again."""
    results = iter([
        {"status": "blocked", "blocked": ["sso.dynatrace.com"], "detail": "blocked"},
        {"status": "blocked", "blocked": ["sso.dynatrace.com"], "detail": "still"},
        {"status": "ok", "blocked": [], "detail": "fine"},
    ])

    async def _selftest_stub(_t, _u):
        return next(results)

    async def _ensure(*a, **k):
        return "added 6 host(s) to the outbound allowlist"

    monkeypatch.setattr(dep, "_selftest_outbound", _selftest_stub)
    monkeypatch.setattr(dep, "_ensure_outbound_allowlist", _ensure)
    _no_sleep(monkeypatch)
    out = asyncio.run(dep._selftest_and_repair("bearer", "https://t.apps.dynatrace.com"))
    assert out["status"] == "ok"
    assert out.get("repaired") is True


def test_a_tenant_that_cannot_be_repaired_stays_blocked(monkeypatch):
    """This is what must reach the caller as a 412 rather than a success."""
    async def _selftest_stub(_t, _u):
        return {"status": "blocked", "blocked": ["autonomous-enablements.whydevslovedynatrace.com"],
                "detail": "blocked"}

    async def _ensure(*a, **k):
        return "skipped (token lacks settings:objects:read/write)"

    monkeypatch.setattr(dep, "_selftest_outbound", _selftest_stub)
    monkeypatch.setattr(dep, "_ensure_outbound_allowlist", _ensure)
    _no_sleep(monkeypatch)
    out = asyncio.run(dep._selftest_and_repair("bearer", "https://t.apps.dynatrace.com"))
    assert out["status"] == "blocked"
    assert "settings:objects" in out["repair"]


# ── Effective permissions ────────────────────────────────────────────────────

def test_effective_permissions_parses_the_documented_shape(monkeypatch):
    """Request/response taken from the vendored SDK, not guessed."""
    seen = {}

    def _handler(method, url, body):
        seen["url"], seen["body"] = url, body
        return _Resp(200, [
            {"permission": "document:documents:admin", "granted": "false"},
            {"permission": "settings:objects:write", "granted": "true"},
        ])

    _patch_client(monkeypatch, _handler)
    out = asyncio.run(dep._effective_permissions(
        "bearer", "https://t.apps.dynatrace.com",
        ["document:documents:admin", "settings:objects:write"]))
    assert out == {"document:documents:admin": "false", "settings:objects:write": "true"}
    assert seen["url"].endswith("/platform/management/v1/effective-permissions:resolve")
    assert seen["body"] == {"permissions": [
        {"permission": "document:documents:admin"},
        {"permission": "settings:objects:write"}]}


def test_effective_permissions_returns_None_when_unavailable(monkeypatch):
    """None means "could not ask", which callers must not read as "no"."""
    _patch_client(monkeypatch, lambda m, u, b: _Resp(404))
    out = asyncio.run(dep._effective_permissions("bearer", "https://t", ["x"]))
    assert out is None


def test_effective_permissions_caps_the_request_at_the_api_limit(monkeypatch):
    """The API rejects more than 100 permissions in one call."""
    seen = {}

    def _handler(method, url, body):
        seen["n"] = len(body["permissions"])
        return _Resp(200, [])

    _patch_client(monkeypatch, _handler)
    asyncio.run(dep._effective_permissions("bearer", "https://t", [f"p:{i}" for i in range(150)]))
    assert seen["n"] == 100


def test_effective_permissions_survives_a_network_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("reset")
    _patch_client(monkeypatch, _boom)
    assert asyncio.run(dep._effective_permissions("bearer", "https://t", ["x"])) is None


def test_no_permissions_asked_means_no_call(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("should not have called the API")
    _patch_client(monkeypatch, _fail)
    assert asyncio.run(dep._effective_permissions("bearer", "https://t", [])) == {}


# ── The resolve-API trap ─────────────────────────────────────────────────────

def test_documents_admin_is_asked_with_a_bearer_that_carries_the_scopes(monkeypatch):
    """The regression that would have made this feature a permanent false alarm.

    `effective-permissions:resolve` answers for the PRESENTED token, not for
    what the client could obtain. Measured on COE 2026-08-19: asked with an
    `app-engine:apps:run`-only bearer, `document:documents:admin` came back
    "false" for a client that had just written a document successfully; asked
    with a bearer carrying the document scopes, "true".

    So this must never be wired to the deploy bearer (which holds no document
    scopes) — doing so prints ACTION REQUIRED on every deploy of every tenant.
    """
    asked = {}

    async def _bearer(_sso, _cid, _csec, _resource, scope):
        asked["scope"] = scope
        return ("doc-bearer", 200, "")

    seen = {}

    def _handler(method, url, body):
        seen["auth_used"] = True
        return _Resp(200, [{"permission": "document:documents:admin", "granted": "true"}])

    monkeypatch.setattr(dep, "_oauth_bearer", _bearer)
    _patch_client(monkeypatch, _handler)
    out = asyncio.run(dep._documents_admin_effective(
        "https://sso", "cid", "sec", "https://t.apps.dynatrace.com", "t"))
    assert out == "true"
    assert "document:documents:admin" in asked["scope"], \
        "must request the doc scopes, or the answer is always false"
    assert "app-engine:apps:run" in asked["scope"], \
        "the resolve endpoint itself needs app-engine:apps:run"


def test_an_sso_refusal_is_unknown_not_false(monkeypatch):
    """A scope-catalog gap is not an IAM gap, and must not be reported as one."""
    async def _bearer(*a, **k):
        return (None, 400, "invalid_request")
    monkeypatch.setattr(dep, "_oauth_bearer", _bearer)
    out = asyncio.run(dep._documents_admin_effective(
        "https://sso", "cid", "sec", "https://t.apps.dynatrace.com", "t"))
    assert out == "", "unknown, so the deploy stays quiet instead of blaming IAM"


def test_resolve_unavailable_is_unknown_not_false(monkeypatch):
    async def _bearer(*a, **k):
        return ("doc-bearer", 200, "")
    monkeypatch.setattr(dep, "_oauth_bearer", _bearer)
    _patch_client(monkeypatch, lambda m, u, b: _Resp(404))
    out = asyncio.run(dep._documents_admin_effective(
        "https://sso", "cid", "sec", "https://t.apps.dynatrace.com", "t"))
    assert out == ""


# ── Prod allowlist creation: only ever on proof ──────────────────────────────

def _allowlist(monkeypatch, *, items, domain="prod", proven):
    """Drive _ensure_outbound_allowlist with a given settings-object state."""
    calls = []

    def _handler(method, url, body):
        calls.append((method, body))
        if method == "GET":
            return _Resp(200, {"items": items})
        return _Resp(201, {})

    _patch_client(monkeypatch, _handler)
    monkeypatch.setattr(dep, "classify_tenant", lambda _u: ("x", domain))
    out = asyncio.run(dep._ensure_outbound_allowlist(
        "bearer", "https://t.apps.dynatrace.com", proven_blocked=proven))
    return out, calls


def test_prod_with_no_object_is_NOT_created_without_proof(monkeypatch):
    """The guess that broke the bootcamp must not become a write.

    Creating an enforced list on a tenant that is genuinely open would cause the
    outage. Without proof we report honestly and change nothing.
    """
    out, calls = _allowlist(monkeypatch, items=[], proven=False)
    assert "not verified" in out
    assert "outbound open" not in out, "must not claim open — that was the false inference"
    assert not any(m == "POST" for m, _ in calls), "nothing may be written without proof"


def test_prod_with_no_object_IS_created_once_the_app_proves_it_is_blocked(monkeypatch):
    """The uxn36332 case: no settings object at any scope, and still denying.

    Refusing to create here is what left that tenant permanently unable to reach
    Orbital. A list of exactly the hosts the app needs is strictly more permissive
    than a default-deny.
    """
    out, calls = _allowlist(monkeypatch, items=[], proven=True)
    assert "created" in out
    posted = [b for m, b in calls if m == "POST"]
    assert posted, "the object must be created"
    hosts = posted[0][0]["value"]["allowedOutboundConnections"]["hostList"]
    assert "autonomous-enablements.whydevslovedynatrace.com" in hosts


def test_an_unenforced_list_is_left_alone_even_with_proof(monkeypatch):
    """If the tenant is not enforcing, the block came from somewhere else and
    switching enforcement on would be us restricting them."""
    out, _ = _allowlist(monkeypatch, items=[
        {"objectId": "o1", "value": {"allowedOutboundConnections": {"enforced": False}}}],
        proven=True)
    assert out == "outbound not enforced (open)"


def test_repair_path_always_passes_proof(monkeypatch):
    """_selftest_and_repair only runs after a definite block, so it must say so —
    otherwise a prod tenant with no object can never be repaired."""
    seen = {}

    async def _selftest_stub(_t, _u):
        return {"status": "blocked", "blocked": ["h"], "detail": "d"}

    async def _ensure(token, url, extra_hosts=None, proven_blocked=False):
        seen["proven"] = proven_blocked
        return "created outbound allowlist with 6 host(s)"

    monkeypatch.setattr(dep, "_selftest_outbound", _selftest_stub)
    monkeypatch.setattr(dep, "_ensure_outbound_allowlist", _ensure)
    _no_sleep(monkeypatch)
    asyncio.run(dep._selftest_and_repair("bearer", "https://t.apps.dynatrace.com"))
    assert seen["proven"] is True


def test_selftest_retries_a_404_because_a_fresh_upgrade_is_not_routable_yet(monkeypatch):
    """The bug the first real 1.0.351 deploy exposed.

    Breaking the ladder on 404 made the sprint deploy report "installed app has
    no selfTest function yet" for an app that had just shipped one — the app was
    simply not routable in the second after install. Unlike the seed function,
    where 404 means "this version lacks it", here it usually means "not yet".
    """
    codes = iter([404, 404, 200])
    payloads = {200: {"ok": True, "blocked": [], "hosts": []}}

    def _handler(method, url, body):
        c = next(codes)
        return _Resp(c, payloads.get(c))

    _patch_client(monkeypatch, _handler)
    _no_sleep(monkeypatch)
    out = asyncio.run(dep._selftest_outbound("bearer", "https://t.apps.dynatrace.com"))
    assert out["status"] == "ok", "a transient 404 must not be read as 'no function'"


def test_a_persistent_404_is_still_reported_as_absent(monkeypatch):
    _patch_client(monkeypatch, lambda m, u, b: _Resp(404))
    _no_sleep(monkeypatch)
    out = asyncio.run(dep._selftest_outbound("bearer", "https://t.apps.dynatrace.com"))
    assert out["status"] == "unknown"
    assert "no selfTest function" in out["detail"]


def test_repaired_is_only_claimed_when_the_write_changed_something(monkeypatch):
    """A transient first probe must not be reported as a repair.

    The allowlist call is idempotent, so a retry that succeeds after a no-op
    write ("allowlist already complete") would otherwise be credited as
    "outbound repaired" — a fix that never happened.
    """
    results = iter([
        {"status": "blocked", "blocked": ["h"], "detail": "blocked"},
        {"status": "ok", "blocked": [], "detail": "fine"},
    ])

    async def _selftest_stub(_t, _u):
        return next(results)

    async def _ensure(*a, **k):
        return "allowlist already complete"

    monkeypatch.setattr(dep, "_selftest_outbound", _selftest_stub)
    monkeypatch.setattr(dep, "_ensure_outbound_allowlist", _ensure)
    _no_sleep(monkeypatch)
    out = asyncio.run(dep._selftest_and_repair("bearer", "https://t.apps.dynatrace.com"))
    assert out["status"] == "ok"
    assert out["repaired"] is False
    assert "unchanged" in out["detail"]


# ── Credential SHAPE, before any scope question ──────────────────────────────
#
# bnk46244, 2026-08-24: `saikkoj` registered six times. Three attempts pasted a
# PLATFORM TOKEN (dt0s16.GP6CHX54, dt0s16.YM7EA5CJ) into the OAuth client-id
# field. Orbital forwarded it to SSO, SSO answered 400 with an empty
# error_description for every scope family, and the preflight reported
# "SSO refused environment-api:api-tokens:write" — blaming the tenant's scopes
# for a credential that was never an OAuth client. The public tenant checker has
# rejected that shape since 2026-08-11, which is exactly why it could show green
# while the register refused: it cannot be handed the input that failed.

from dashboard.tenant_credentials import credential_problem, sso_failure_cause

_GOOD_ID = "dt0s02.4H4SREXX"
_GOOD_SECRET = "dt0s02.4H4SREXX." + "A" * 50
_GOOD_URN = "urn:dtaccount:11111111-2222-3333-4444-555555555555"


def test_a_well_formed_triple_has_no_shape_problem():
    assert credential_problem(_GOOD_ID, _GOOD_SECRET, _GOOD_URN) == ""


def test_a_platform_token_in_the_client_id_field_is_named_for_what_it_is():
    problem = credential_problem("dt0s16.GP6CHX54", _GOOD_SECRET, _GOOD_URN)
    assert "platform token" in problem
    # It must NOT read as a scope problem — that is the whole defect.
    assert "scope" not in problem.lower()
    assert "dt0s02" in problem


def test_a_classic_token_in_the_client_id_field_is_also_caught():
    assert "classic API token" in credential_problem("dt0c01.ABCDEFGH", _GOOD_SECRET, _GOOD_URN)


def test_a_secret_from_a_different_client_is_caught_before_sso_401s():
    problem = credential_problem("dt0s02.ABCDEFGH", _GOOD_SECRET, _GOOD_URN)
    assert "different OAuth client" in problem


def test_a_truncated_secret_is_rejected():
    assert "client secret" in credential_problem(_GOOD_ID, "dt0s02.4H4SREXX.SHORT", _GOOD_URN).lower()


def test_a_malformed_account_urn_is_rejected():
    assert "accountUrn" in credential_problem(_GOOD_ID, _GOOD_SECRET, "urn:dtaccount:nope")


# ── One SSO status, several meanings ─────────────────────────────────────────
#
# Same class as the `rejected` vs `unreachable` conflation that misdiagnosed 26
# deploys in the APAC incident: _oauth_bearer returns the same (status, body) for
# causes that need OPPOSITE fixes.

def test_an_empty_error_description_is_the_scope_catalog_gap():
    cause = sso_failure_cause(400, '{"error":"invalid_request","error_description":""}', _GOOD_ID)
    assert "not in this OAuth client's catalog" in cause
    # ...and only THIS case may advise creating a new client.
    assert "NEW client" in cause


def test_a_401_is_a_wrong_secret_not_a_missing_scope():
    cause = sso_failure_cause(401, "", _GOOD_ID)
    assert "not a scope problem" in cause


def test_an_unreachable_sso_is_not_evidence_about_scopes():
    cause = sso_failure_cause(0, "connect timeout", _GOOD_ID)
    assert "NOT evidence" in cause


def test_the_cause_blames_the_credential_when_the_client_id_is_not_a_client():
    """The bnk46244 shape, at the layer that renders the preflight detail."""
    cause = sso_failure_cause(400, '{"error":"invalid_request","error_description":""}',
                              "dt0s16.GP6CHX54")
    assert "platform token" in cause
    assert "catalog" not in cause


def test_the_cause_never_quotes_a_token_endpoint_body_verbatim():
    leaky = '{"access_token":"dt0s16.LEAKED.SECRETVALUE","error_description":"nope"}'
    assert "LEAKED" not in sso_failure_cause(400, leaky, _GOOD_ID)


# ── The preflight now renders the cause, not a scope accusation ──────────────

def test_document_preflight_reports_the_cause_and_not_the_raw_sso_body(monkeypatch):
    async def _bearer(*_a, **_k):
        return (None, 400, '{"error":"invalid_request","error_description":"",'
                           '"access_token":"dt0s16.LEAKED.X"}')
    monkeypatch.setattr(dep, "_oauth_bearer", _bearer)
    ok, detail = asyncio.run(dep._preflight_documents(
        "https://sso", _GOOD_ID, _GOOD_SECRET, "https://t.apps.dynatrace.com", "t"))
    assert ok is False
    assert "LEAKED" not in detail
    assert "catalog" in detail


def test_learner_preflight_blames_the_credential_when_it_is_a_platform_token(monkeypatch):
    async def _bearer(*_a, **_k):
        return (None, 400, '{"error":"invalid_request","error_description":""}')
    monkeypatch.setattr(dep, "_oauth_bearer", _bearer)
    _patch_client(monkeypatch, lambda m, u, b: _Resp(400, None, text="{}"))
    out = asyncio.run(dep._preflight_learner_tokens(
        "https://sso", "dt0s16.GP6CHX54", _GOOD_SECRET, "https://t.apps.dynatrace.com",
        "t", "prod", _GOOD_URN, "https://api.dynatrace.com"))
    assert out["tier"] == "none"
    assert "platform token" in out["detail"]


def test_the_register_route_refuses_a_platform_token_with_400_not_412(monkeypatch):
    """400, not 412. A 412 means "this client is under-scoped" and sends the SE to
    re-create a client; the credential being the wrong KIND is a different fix."""
    audited = {}

    async def _audit(_user, _tenant, _action, result, **extra):
        audited.update({"result": result, **extra})
    monkeypatch.setattr(dep, "_audit", _audit)

    def _boom(*_a, **_k):
        raise AssertionError("SSO must never be reached with a malformed credential")
    monkeypatch.setattr(dep, "_oauth_bearer", _boom)

    with pytest.raises(dep.HTTPException) as exc:
        asyncio.run(dep.deploy_with_oauth({
            "tenant": "https://bnk46244.apps.dynatrace.com",
            "clientId": "dt0s16.GP6CHX54",
            "clientSecret": _GOOD_SECRET,
            "accountUrn": _GOOD_URN,
        }, x_auth_user="saikkoj"))
    assert exc.value.status_code == 400
    assert "platform token" in exc.value.detail
    assert audited["result"] == "bad-credential-shape"

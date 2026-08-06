"""Seeding a tenant's orbital-config through the app's own function.

Background (measured 2026-08-05, all three tenants including the COE master
client with full account rights):

  * asking the token endpoint for `app-settings:objects:write`
      -> 400 invalid_request
  * PUT /platform/app-settings/v2/objects/<id> with the richest grantable token
      -> 403 {"missingScopes":["app-settings:objects:write"]}

So the deploy can never write that object itself. The app declares the scope,
and `app-engine:apps:run` (which the deploy token holds) is enough to invoke an
app function from outside — verified: POST .../api/fetchChangelog returned 200.
These tests pin the translation layer and the strictness of the verify oracle.

Run: /home/ops/ops-venv/bin/python -m pytest dashboard/test_orbital_seed.py -q
"""

import asyncio

import pytest

from dashboard import app_deploy as dep


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


class _Client:
    """Stands in for httpx.AsyncClient; records the one POST it receives."""

    last = {}

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _Client.last = {"url": url, "headers": headers or {}, "json": json or {}}
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def _seed(monkeypatch, resp, orbital_token="orb-token"):
    monkeypatch.setattr(dep, "_orbital_service_token", lambda: orbital_token)
    monkeypatch.setattr(dep.httpx, "AsyncClient", lambda *a, **k: _Client(resp))
    return asyncio.run(dep._seed_via_app_function("deploy-bearer", "https://t.example.com"))


def test_calls_the_app_function_with_the_deploy_bearer(monkeypatch):
    _seed(monkeypatch, _Resp(200, {"status": "seeded"}))
    sent = _Client.last
    assert sent["url"].endswith(
        f"/platform/app-engine/app-functions/v1/apps/{dep.APP_ID}/api/seedOrbitalConfig")
    assert sent["headers"]["Authorization"] == "Bearer deploy-bearer"
    # The ORBITAL token travels in the body, never as the caller's credential.
    assert sent["json"] == {"token": "orb-token"}


@pytest.mark.parametrize("status,expected", [
    ("seeded", "token seeded (via app function)"),
    ("already-configured", "already configured"),
])
def test_app_answers_are_translated_for_the_deploy_report(monkeypatch, status, expected):
    assert _seed(monkeypatch, _Resp(200, {"status": status})) == expected


def test_rejected_points_at_this_server_not_the_tenant(monkeypatch):
    # Orbital handed out a token its own verify endpoint refuses. The admin of
    # the target tenant can do nothing about that, so the message must not send
    # them looking.
    out = _seed(monkeypatch, _Resp(200, {"status": "rejected"}))
    assert "ORBITAL_TOKEN on this server" in out
    assert "ACTION REQUIRED" in dep._scope_warnings("", "", out)[0]
    assert "on this server, not the tenant" in dep._scope_warnings("", "", out)[0]


def test_missing_function_falls_back_rather_than_reporting_failure(monkeypatch):
    # An older installed version has no such function. None tells the caller to
    # try the legacy direct write instead of declaring the deploy broken.
    assert _seed(monkeypatch, _Resp(404)) is None


def test_transport_failure_is_reported_not_swallowed(monkeypatch):
    out = _seed(monkeypatch, RuntimeError("connection reset"))
    assert "error" in out and "connection reset" in out


def test_no_orbital_token_configured_skips_before_any_call(monkeypatch):
    monkeypatch.setattr(dep, "_orbital_service_token", lambda: None)
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "should not happen"

    monkeypatch.setattr(dep, "_seed_via_app_function", _boom)
    out = asyncio.run(dep._ensure_orbital_config("bearer", "https://t.example.com"))
    assert out == "skipped (ORBITAL_TOKEN not configured)"
    assert called["n"] == 0


def test_the_seed_warning_states_the_manual_step_and_why_it_is_manual():
    # An earlier version of this claimed seeding was automatic. It is not: an app
    # function invoked by an external bearer runs with the CALLER's permissions,
    # so routing the write through the app hits the same ungrantable scope.
    # Telling an admin the install handled it would leave them with a tenant that
    # 401s and no reason to look at settings.
    w = dep._scope_warnings("", "", "skipped (something)")[0]
    assert "ONE-TIME manual step" in w
    assert "Orbital Server Configuration" in w
    assert "cannot be automated" in w
    assert "CALLER's permissions" in w



# ── /api/service/verify — the oracle seedOrbitalConfig trusts ────────────────
#
# The app will only store a candidate token after this endpoint confirms it, so
# a gate that is loose in the same way _require_arena_auth is loose (anonymous
# passes during the compat window) would confirm ANY token to ANY caller and
# make the check worthless.

import dashboard.app as a  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_client = TestClient(a.app, raise_server_exceptions=False)


def _with_tokens(monkeypatch, tokens=("real-orbital-token",)):
    monkeypatch.setattr(a, "ORBITAL_TOKENS", tokens)


def test_verify_accepts_the_real_service_token(monkeypatch):
    _with_tokens(monkeypatch)
    r = _client.get("/api/service/verify",
                    headers={"Authorization": "Bearer real-orbital-token"})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_verify_rejects_a_wrong_token(monkeypatch):
    _with_tokens(monkeypatch)
    r = _client.get("/api/service/verify", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_verify_rejects_anonymous_even_in_the_arena_compat_window(monkeypatch):
    # ARENA_AUTH_ENFORCE is unset in production today. If this endpoint shared
    # that gate it would answer 200 to everyone.
    monkeypatch.delenv("ARENA_AUTH_ENFORCE", raising=False)
    _with_tokens(monkeypatch)
    assert _client.get("/api/service/verify").status_code == 401


def test_verify_ignores_a_signed_in_session(monkeypatch):
    # Being an org member says nothing about whether the token in question is
    # good, and this endpoint answers only that question.
    _with_tokens(monkeypatch)
    r = _client.get("/api/service/verify", headers={"X-Auth-User": "someone@dynatrace.com"})
    assert r.status_code == 401


def test_verify_fails_closed_when_no_token_is_configured(monkeypatch):
    _with_tokens(monkeypatch, tokens=())
    r = _client.get("/api/service/verify", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401


def test_the_app_function_is_tried_before_the_direct_write(monkeypatch):
    # Ordering is the fix. The direct write cannot succeed on any tenant, so if
    # it ran first every deploy would burn a guaranteed-403 round trip and, worse,
    # report the 403's message instead of the app's answer.
    order = []
    monkeypatch.setattr(dep, "_orbital_service_token", lambda: "orb-token")

    async def fn(_t, _u):
        order.append("app-function")
        return "token seeded (via app function)"

    class _NoDirect:
        def __init__(self, *a, **k):
            order.append("direct-write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dep, "_seed_via_app_function", fn)
    monkeypatch.setattr(dep.httpx, "AsyncClient", _NoDirect)
    out = asyncio.run(dep._ensure_orbital_config("bearer", "https://t.example.com"))
    assert out == "token seeded (via app function)"
    assert order == ["app-function"]


def test_a_transient_app_not_found_is_retried_then_reported_as_unverified(monkeypatch):
    # An app is not routable the instant its install returns: the first call after
    # an upgrade answers "App not found" and the same call seconds later succeeds.
    # Without the retry this produced an ACTION REQUIRED warning on EVERY deploy.
    calls = {"n": 0}

    class _Flaky(_Client):
        async def post(self, url, headers=None, json=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return _Resp(500, {"error": "App not found."})
            return _Resp(200, {"status": "already-configured"})

    monkeypatch.setattr(dep, "_orbital_service_token", lambda: "orb-token")
    _real = asyncio.sleep
    monkeypatch.setattr(dep.asyncio, "sleep", lambda _s: _real(0))
    monkeypatch.setattr(dep.httpx, "AsyncClient", lambda *a, **k: _Flaky(None))
    out = asyncio.run(dep._seed_via_app_function("bearer", "https://t.example.com"))
    assert calls["n"] == 3 and out == "already configured"


def test_an_unreachable_app_function_is_unverified_not_broken(monkeypatch):
    # The app ships a default bearer, so a question we could not ask is a check we
    # could not run — NOT a tenant that needs a manual paste.
    monkeypatch.setattr(dep, "_orbital_service_token", lambda: "orb-token")
    _real = asyncio.sleep
    monkeypatch.setattr(dep.asyncio, "sleep", lambda _s: _real(0))
    monkeypatch.setattr(dep.httpx, "AsyncClient", lambda *a, **k: _Client(_Resp(500, {"e": 1})))
    out = asyncio.run(dep._seed_via_app_function("bearer", "https://t.example.com"))
    assert out.startswith("unverified")
    # "unverified" must take the softer warning branch, never ACTION REQUIRED.
    assert not any("ACTION REQUIRED" in w for w in dep._scope_warnings("", "", out))

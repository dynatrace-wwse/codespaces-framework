"""Tests for the SSO-delegated deploy flow guards + PKCE (Phase 1).

Run: /home/ops/ops-venv/bin/python -m dashboard.test_app_deploy
  or pytest dashboard/test_app_deploy.py
"""

import asyncio
import base64
import hashlib
import json
import os

from fastapi import HTTPException

from dashboard import app_deploy as dep


def _expect_http(status, coro):
    try:
        asyncio.run(coro)
    except HTTPException as e:
        assert e.status_code == status, f"expected {status}, got {e.status_code}"
        return
    raise AssertionError(f"expected HTTPException {status}, none raised")


def _run(coro):
    """Await a coroutine in a sync test."""
    return asyncio.run(coro)


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = dep._pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in verifier and "=" not in challenge  # url-safe, unpadded
    # fresh each call
    assert dep._pkce()[0] != verifier


def test_start_requires_org_member():
    _expect_http(401, dep.deploy_start(tenant="https://x.apps.dynatrace.com", action="deploy", x_auth_user=None))


def test_start_rejects_bad_action():
    _expect_http(400, dep.deploy_start(tenant="https://x.apps.dynatrace.com", action="nuke", x_auth_user="alice"))


def test_start_rejects_non_dynatrace_tenant():
    _expect_http(403, dep.deploy_start(tenant="https://evil.example.com", action="deploy", x_auth_user="alice"))


def test_start_503_when_client_not_configured():
    saved = dep.DEPLOY_CLIENT_ID
    dep.DEPLOY_CLIENT_ID = ""
    try:
        _expect_http(503, dep.deploy_start(tenant="https://geu80787.apps.dynatrace.com", action="deploy", x_auth_user="alice"))
    finally:
        dep.DEPLOY_CLIENT_ID = saved


def test_require_writer():
    assert dep._require_writer("alice") == "alice"
    _expect_http(401, _raise_writer(None))


async def _raise_writer(u):
    dep._require_writer(u)


def test_url_helpers():
    assert dep._app_url("https://geu80787.apps.dynatrace.com/") == "https://geu80787.apps.dynatrace.com/ui/apps/my.dynatrace.enablements"
    assert dep._registry_url("https://t.apps.dynatrace.com") == "https://t.apps.dynatrace.com/platform/app-engine/registry/v1/apps"
    assert dep._registry_url("https://t.apps.dynatrace.com/", "my.dynatrace.enablements").endswith("/registry/v1/apps/my.dynatrace.enablements")


def test_undeploy_calls_registry_delete_with_bearer(monkeypatch=None):
    import httpx
    captured = {}

    class _Resp:
        status_code = 204
        text = ""

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def delete(self, url, headers=None):
            captured["url"] = url
            captured["auth"] = (headers or {}).get("Authorization")
            return _Resp()

    orig = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        ok, msg = asyncio.run(dep._run_undeploy("tok123", "https://t.apps.dynatrace.com"))
    finally:
        httpx.AsyncClient = orig
    assert ok is True
    assert captured["url"].endswith("/registry/v1/apps/my.dynatrace.enablements")
    assert captured["auth"] == "Bearer tok123"


def test_client_for_resolves_per_realm_with_fallback(monkeypatch=None):
    import os
    saved = dict(os.environ)
    saved_g = dep.DEPLOY_CLIENT_ID
    try:
        dep.DEPLOY_CLIENT_ID = "global-cid"
        os.environ.pop("DEPLOY_CLIENT_ID_PROD", None)
        os.environ["DEPLOY_CLIENT_ID_SPRINT"] = "sprint-cid"
        os.environ["DEPLOY_CLIENT_SECRET_SPRINT"] = "sprint-sec"
        # sprint has its own client
        assert dep._client_for("sprint") == ("sprint-cid", "sprint-sec")
        # prod falls back to the global client
        assert dep._client_for("prod")[0] == "global-cid"
    finally:
        os.environ.clear(); os.environ.update(saved)
        dep.DEPLOY_CLIENT_ID = saved_g


def test_missing_scopes_detects_insufficient_permissions():
    # user has all deploy scopes → nothing missing
    assert dep._missing_scopes("deploy", "app-engine:apps:install app-engine:apps:run storage:logs:read") == []
    # user lacks install → reported
    assert dep._missing_scopes("deploy", "app-engine:apps:run") == ["app-engine:apps:install"]
    # empty / None grant → all required missing
    assert dep._missing_scopes("deploy", "") == ["app-engine:apps:install", "app-engine:apps:run"]
    assert dep._missing_scopes("deploy", None) == ["app-engine:apps:install", "app-engine:apps:run"]
    # undeploy needs delete
    assert dep._missing_scopes("undeploy", "app-engine:apps:run") == ["app-engine:apps:delete"]
    assert dep._missing_scopes("undeploy", "app-engine:apps:delete") == []


def test_deploy_with_status_skips_when_up_to_date():
    # installed == ours → up-to-date, no deploy run
    saved_ver = dep._app_version
    saved_inst = dep._get_installed
    saved_run = dep._run_deploy
    saved_sync = dep._sync_repo
    ran = {"called": False}
    async def fake_installed(t, u): return "1.2.3"
    async def fake_run(t, u): ran["called"] = True; return 0, ""
    async def fake_sync(): return True, "master@abc"
    dep._app_version = lambda: "1.2.3"
    dep._get_installed = fake_installed
    dep._run_deploy = fake_run
    dep._sync_repo = fake_sync
    try:
        res = asyncio.run(dep._deploy_with_status("tok", "https://x.apps.dynatrace.com"))
        assert res == {"status": "up-to-date", "to": "1.2.3"}
        assert ran["called"] is False
    finally:
        dep._app_version = saved_ver; dep._get_installed = saved_inst
        dep._run_deploy = saved_run; dep._sync_repo = saved_sync


def test_deploy_with_status_upgrades_when_older():
    saved_ver = dep._app_version; saved_inst = dep._get_installed
    saved_run = dep._run_deploy; saved_sync = dep._sync_repo
    async def fake_installed(t, u): return "1.0.0"
    async def fake_run(t, u): return 0, "ok"
    async def fake_sync(): return True, "master@abc"
    dep._app_version = lambda: "1.2.0"
    dep._get_installed = fake_installed
    dep._run_deploy = fake_run
    dep._sync_repo = fake_sync
    try:
        res = asyncio.run(dep._deploy_with_status("tok", "https://x.apps.dynatrace.com"))
        assert res == {"status": "upgraded", "from": "1.0.0", "to": "1.2.0"}
    finally:
        dep._app_version = saved_ver; dep._get_installed = saved_inst
        dep._run_deploy = saved_run; dep._sync_repo = saved_sync


def test_sync_repo_skips_when_not_a_git_checkout():
    # No .git → best-effort no-op, no subprocess, deploy still proceeds on the caller side.
    saved = dep.APP_REPO_DIR
    dep.APP_REPO_DIR = "/nonexistent/app/repo"
    try:
        ok, msg = asyncio.run(dep._sync_repo())
    finally:
        dep.APP_REPO_DIR = saved
    assert ok is False and "not a git checkout" in msg


def test_deploy_with_status_syncs_repo_before_building():
    # _sync_repo must run BEFORE the build so _app_version()/dt-app see freshly pulled code.
    saved_ver = dep._app_version; saved_inst = dep._get_installed
    saved_run = dep._run_deploy; saved_sync = dep._sync_repo
    order = []
    async def fake_installed(t, u): return "1.0.0"
    async def fake_run(t, u): order.append("deploy"); return 0, "ok"
    async def fake_sync(): order.append("sync"); return True, "master@abc"
    dep._app_version = lambda: "1.2.0"
    dep._get_installed = fake_installed
    dep._run_deploy = fake_run
    dep._sync_repo = fake_sync
    try:
        res = asyncio.run(dep._deploy_with_status("tok", "https://x.apps.dynatrace.com"))
        assert res["status"] == "upgraded"
        assert order and order[0] == "sync", f"sync must precede deploy, got {order}"
        assert "deploy" in order
    finally:
        dep._app_version = saved_ver; dep._get_installed = saved_inst
        dep._run_deploy = saved_run; dep._sync_repo = saved_sync


def test_is_coe():
    saved = dep.COE_TENANT_URL
    dep.COE_TENANT_URL = "https://geu80787.apps.dynatrace.com"
    try:
        assert dep._is_coe("https://geu80787.apps.dynatrace.com")
        assert dep._is_coe("https://geu80787.apps.dynatrace.com/ui/apps")
        assert not dep._is_coe("https://other.apps.dynatrace.com")
    finally:
        dep.COE_TENANT_URL = saved


def test_coe_auto_without_creds_503():
    saved = (dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_TENANT_URL)
    dep.COE_CLIENT_ID = ""; dep.COE_CLIENT_SECRET = ""
    dep.COE_TENANT_URL = "https://geu80787.apps.dynatrace.com"
    try:
        # COE tenant, no token, no creds configured → 503
        _expect_http(503, dep.deploy_with_token({"tenant": "https://geu80787.apps.dynatrace.com", "token": ""}, x_auth_user="a"))
    finally:
        dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_TENANT_URL = saved


def test_is_sro():
    saved = dep.SRO_TENANT_URL
    dep.SRO_TENANT_URL = "https://sro97894.apps.dynatrace.com"
    try:
        assert dep._is_sro("https://sro97894.apps.dynatrace.com")
        assert dep._is_sro("https://sro97894.apps.dynatrace.com/ui/apps")
        assert not dep._is_sro("https://other.apps.dynatrace.com")
    finally:
        dep.SRO_TENANT_URL = saved


def test_sro_auto_without_creds_503():
    saved = (dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET, dep.SRO_PLATFORM_TOKEN, dep.SRO_TENANT_URL)
    dep.SRO_CLIENT_ID = ""; dep.SRO_CLIENT_SECRET = ""; dep.SRO_PLATFORM_TOKEN = ""
    dep.SRO_TENANT_URL = "https://sro97894.apps.dynatrace.com"
    try:
        # SRO tenant, no token, no creds/platform-token configured → 503
        _expect_http(503, dep.deploy_with_token({"tenant": "https://sro97894.apps.dynatrace.com", "token": ""}, x_auth_user="a"))
    finally:
        dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET, dep.SRO_PLATFORM_TOKEN, dep.SRO_TENANT_URL = saved


def test_non_coe_without_token_400():
    # any tenant that is neither COE nor SRO, without a token → 400 (token required)
    _expect_http(400, dep.deploy_with_token({"tenant": "https://other.apps.dynatrace.com", "token": ""}, x_auth_user="a"))


def test_token_deploy_guards():
    # bad action → 400
    _expect_http(400, dep.deploy_with_token({"tenant": "https://x.apps.dynatrace.com", "token": "t", "action": "nuke"}, x_auth_user="a"))
    # non-Dynatrace tenant → 403
    _expect_http(403, dep.deploy_with_token({"tenant": "https://evil.example.com", "token": "t"}, x_auth_user="a"))
    # Dynatrace tenant but no token → 400
    _expect_http(400, dep.deploy_with_token({"tenant": "https://x.apps.dynatrace.com", "token": ""}, x_auth_user="a"))


def test_token_deploy_allows_anonymous():
    """Regression: a signed-out user holding a valid platform token MUST be able to deploy.
    The token carries the target tenant's own authority, so no GitHub identity is required —
    deploy_with_token must NOT raise 401 when x_auth_user is None, and must audit "anonymous".
    (Mirrors nginx `location = /api/deploy/token` opportunistic-auth + the frontend buttons
    having no `data-action` guest-gate.) Guards against re-introducing the org-member gate."""
    saved = (dep._deploy_with_status, dep._ensure_outbound_allowlist, dep._ensure_remote_grail,
             dep._register_in_content_service, dep._audit)
    audited = {}
    async def fake_deploy(t, u): return {"status": "up-to-date", "to": "1.2.3"}
    async def fake_allow(t, u): return ""
    async def fake_grail(t, u): return ""
    async def fake_register(u, t): return {"profile": None}
    async def fake_audit(user, tenant, action, result, **extra): audited.update(user=user, result=result)
    dep._deploy_with_status = fake_deploy
    dep._ensure_outbound_allowlist = fake_allow
    dep._ensure_remote_grail = fake_grail
    dep._register_in_content_service = fake_register
    dep._audit = fake_audit
    try:
        res = asyncio.run(dep.deploy_with_token(
            {"tenant": "https://x.apps.dynatrace.com", "token": "valid-tok"}, x_auth_user=None))
    finally:
        (dep._deploy_with_status, dep._ensure_outbound_allowlist, dep._ensure_remote_grail,
         dep._register_in_content_service, dep._audit) = saved
    assert res["ok"] is True and res["status"] == "up-to-date"
    assert audited.get("user") == "anonymous", f"signed-out deploy must audit anonymous, got {audited}"


class _FakeRedis:
    """Just enough Redis for the background-deploy job record."""

    def __init__(self):
        self.store = {}

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)


def _with_fake_pool(fn):
    """Run an async test body with dep._pool() served by a fresh _FakeRedis."""
    fake = _FakeRedis()
    saved = dep._pool
    dep._pool = lambda: fake
    try:
        return asyncio.run(fn(fake))
    finally:
        dep._pool = saved


def test_token_start_validates_synchronously():
    """Obviously-wrong calls must fail on the spot, not become a job the caller has to poll."""
    def check(fake):
        _expect_http(400, dep.deploy_with_token_start(
            {"tenant": "https://x.apps.dynatrace.com", "token": "t", "action": "nuke"}, x_auth_user="a"))
        _expect_http(403, dep.deploy_with_token_start(
            {"tenant": "https://evil.example.com", "token": "t"}, x_auth_user="a"))
        assert fake.store == {}, "a rejected start must not leave a job record behind"
    fake = _FakeRedis()
    saved = dep._pool
    dep._pool = lambda: fake
    try:
        check(fake)
    finally:
        dep._pool = saved


def test_token_start_runs_the_deploy_in_the_background():
    """token-start returns immediately, then the same result /api/deploy/token would have
    returned shows up on token-status."""
    saved = (dep._deploy_with_status, dep._ensure_outbound_allowlist, dep._ensure_remote_grail,
             dep._register_in_content_service, dep._audit)
    async def fake_deploy(t, u): return {"status": "upgraded", "from": "1.0.1", "to": "1.0.2"}
    async def fake_allow(t, u): return ""
    async def fake_grail(t, u): return ""
    async def fake_register(u, t): return {"profile": None}
    async def fake_audit(user, tenant, action, result, **extra): return None
    dep._deploy_with_status = fake_deploy
    dep._ensure_outbound_allowlist = fake_allow
    dep._ensure_remote_grail = fake_grail
    dep._register_in_content_service = fake_register
    dep._audit = fake_audit

    async def body(fake):
        started = await dep.deploy_with_token_start(
            {"tenant": "https://x.apps.dynatrace.com", "token": "valid-tok"}, x_auth_user="a")
        assert started["done"] is False and started["deployId"]
        # Not finished the instant we return — the caller is expected to poll.
        assert json.loads(fake.store[dep._deploy_job_key(started["deployId"])])["done"] is False
        for _ in range(50):
            status = await dep.deploy_with_token_status(started["deployId"])
            if status["done"]:
                return status
            await asyncio.sleep(0.01)
        raise AssertionError("background deploy never finished")

    try:
        status = _with_fake_pool(body)
    finally:
        (dep._deploy_with_status, dep._ensure_outbound_allowlist, dep._ensure_remote_grail,
         dep._register_in_content_service, dep._audit) = saved
    assert status["ok"] is True and status["status"] == "upgraded" and status["version"] == "1.0.2"


def test_token_start_reports_a_failed_deploy_instead_of_hanging():
    """A deploy that raises must land as done+ok:false with the HTTP status, so the app can
    show the real reason rather than polling forever."""
    async def body(fake):
        # 'other' tenant with no token → the flow raises 400 inside the task.
        started = await dep.deploy_with_token_start(
            {"tenant": "https://other.apps.dynatrace.com", "token": ""}, x_auth_user="a")
        for _ in range(50):
            status = await dep.deploy_with_token_status(started["deployId"])
            if status["done"]:
                return status
            await asyncio.sleep(0.01)
        raise AssertionError("failed deploy never resolved")
    status = _with_fake_pool(body)
    assert status["ok"] is False and status["status"] == 400
    assert "platform token" in status["error"]


def test_token_status_404s_on_an_unknown_id():
    def body(fake):
        _expect_http(404, dep.deploy_with_token_status("nope"))
    fake = _FakeRedis()
    saved = dep._pool
    dep._pool = lambda: fake
    try:
        body(fake)
    finally:
        dep._pool = saved


def test_sro_auto_deploys_with_platform_token():
    """SRO no-token deploy: with SRO_PLATFORM_TOKEN set and no OAuth client, deploy_with_token
    must proceed (not 400/503) using the stored token and audit via='sro-auto'."""
    saved = (dep._deploy_with_status, dep._ensure_outbound_allowlist, dep._ensure_remote_grail,
             dep._register_in_content_service, dep._audit,
             dep.SRO_PLATFORM_TOKEN, dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET, dep.SRO_TENANT_URL)
    audited = {}
    async def fake_deploy(t, u): return {"status": "installed", "to": "9.9.9"}
    async def fake_allow(t, u): return ""
    async def fake_grail(t, u): return ""
    async def fake_register(u, t): return {"profile": None}
    async def fake_audit(user, tenant, action, result, **extra): audited.update(result=result, via=extra.get("via"))
    dep._deploy_with_status = fake_deploy
    dep._ensure_outbound_allowlist = fake_allow
    dep._ensure_remote_grail = fake_grail
    dep._register_in_content_service = fake_register
    dep._audit = fake_audit
    dep.SRO_CLIENT_ID = ""; dep.SRO_CLIENT_SECRET = ""          # no OAuth client → no mint
    dep.SRO_PLATFORM_TOKEN = "dt0s16.STORED-SRO-TOKEN"          # stored platform token used
    dep.SRO_TENANT_URL = "https://sro97894.apps.dynatrace.com"
    try:
        res = asyncio.run(dep.deploy_with_token(
            {"tenant": "https://sro97894.apps.dynatrace.com", "token": ""}, x_auth_user="a"))
    finally:
        (dep._deploy_with_status, dep._ensure_outbound_allowlist, dep._ensure_remote_grail,
         dep._register_in_content_service, dep._audit,
         dep.SRO_PLATFORM_TOKEN, dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET, dep.SRO_TENANT_URL) = saved
    assert res["ok"] is True and res["status"] == "installed"
    assert audited.get("via") == "sro-auto", f"SRO auto-deploy must audit via=sro-auto, got {audited}"


def test_register_buttons_have_no_guest_gate():
    """Root-cause regression: the Register-Tenant deploy/undeploy buttons must NOT carry the
    `data-action` attribute. CSS `body.role-guest [data-action]` sets pointer-events:none, which
    made the buttons silently unclickable for guests/anonymous users ("nothing happens"). Token
    deploy is authorized by the platform token, not GitHub org membership, so these buttons must
    stay clickable for everyone. If this fails, signed-out token deploys are broken in the UI."""
    import os
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(here, "templates", "index.html"), encoding="utf-8").read()
    # Renamed to reg-oa-* when the OAuth-client bootstrap deploy replaced the
    # token-paste form (59e93ad) — the guest-gate rule is unchanged.
    for bid in ("reg-oa-deploy", "reg-oa-undeploy"):
        m = re.search(r"<button[^>]*\bid=\"" + bid + r"\"[^>]*>", html)
        assert m, f"button #{bid} not found in index.html"
        assert "data-action" not in m.group(0), \
            f"#{bid} must not have data-action — it re-enables the guest gate that blocks token deploys"


def test_hash_restore_runs_after_init_not_at_parse_time():
    """Race regression: restoring the active tab from the URL hash must happen inside the init
    IIFE (after every top-level declaration), NOT in a bare IIFE at parse time. The early form
    ran activateTab() -> loadRegister() -> wireRegister(), which touched `let regWired` while it
    was still in the temporal dead zone; the throw aborted the rest of init so loadAuthState()
    never ran — header stuck on "checking…" and the sign-in button never appeared on a fresh
    deep-link load (e.g. /#register)."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    js = open(os.path.join(here, "static", "app.js"), encoding="utf-8").read()
    # The old parse-time form must be gone.
    assert "// Restore tab from URL hash on load" not in js, \
        "parse-time hash-restore IIFE reintroduced — it aborts init via a TDZ on regWired"
    # The restore must be deferred (setTimeout) so it runs after the whole script — and
    # therefore after every `let`/`const` (regWired, csState, …) — has initialized.
    import re
    m = re.search(r"setTimeout\(\(\)\s*=>\s*\{[^}]*location\.hash\.replace\('#', ''\)",
                  js, re.DOTALL)
    assert m, "hash restore must be deferred via setTimeout so tab handlers don't hit a TDZ"


def test_standalone_deploy_page_removed():
    """The legacy /deploy standalone page is gone; /#register is the only deploy UI."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    app_py = open(os.path.join(here, "app.py"), encoding="utf-8").read()
    assert "_DEPLOY_PAGE" not in app_py, "_DEPLOY_PAGE constant should be removed"
    assert '@app.get("/deploy"' not in app_py, "/deploy route should be removed"


def test_deploy_missing_repo_returns_127():
    saved = dep.APP_REPO_DIR
    dep.APP_REPO_DIR = "/nonexistent/app/repo"
    try:
        rc, out = asyncio.run(dep._run_deploy("tok", "https://t.apps.dynatrace.com"))
    finally:
        dep.APP_REPO_DIR = saved
    assert rc == 127 and "dt-app not found" in out


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} deploy tests passed")


# ── _ensure_remote_grail (auto-enable cross-tenant forwarding) ──────────────────

def _grail_client(captured, existing_items):
    import httpx

    class _Resp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._payload = payload or {}
            self.text = ""
        def json(self): return self._payload

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None):
            captured["get_params"] = params
            return _Resp(200, {"items": existing_items})
        async def post(self, url, headers=None, json=None):
            captured["post"] = json
            return _Resp(200)
        async def put(self, url, headers=None, json=None):
            captured["put"] = json
            captured["put_url"] = url
            return _Resp(200)

    orig = httpx.AsyncClient
    httpx.AsyncClient = _Client
    return orig


def test_ensure_remote_grail_skips_coe_tenant(monkeypatch=None):
    msg = asyncio.run(dep._ensure_remote_grail("tok", "https://wwse.apps.dynatrace.com"))
    assert "central tenant" in msg
    msg2 = asyncio.run(dep._ensure_remote_grail("tok", "https://geu80787.apps.dynatrace.com"))
    assert "central tenant" in msg2


def test_ensure_remote_grail_skips_without_token():
    orig = dep._coe_remote_grail_token
    dep._coe_remote_grail_token = lambda: None
    try:
        msg = asyncio.run(dep._ensure_remote_grail("tok", "https://sro97894.apps.dynatrace.com"))
    finally:
        dep._coe_remote_grail_token = orig
    assert "not configured" in msg


def test_ensure_remote_grail_creates_setting_with_coe_token():
    import httpx
    captured = {}
    orig_tok = dep._coe_remote_grail_token
    dep._coe_remote_grail_token = lambda: "COE-SECRET"
    orig_client = _grail_client(captured, existing_items=[])  # no existing object → create
    try:
        msg = asyncio.run(dep._ensure_remote_grail("deploytok", "https://sro97894.apps.dynatrace.com"))
    finally:
        httpx.AsyncClient = orig_client
        dep._coe_remote_grail_token = orig_tok
    assert msg == "enabled → wwse"
    body = captured["post"][0]
    assert body["schemaId"] == "app:my.dynatrace.enablements:remote-grail"
    assert body["value"]["enabled"] is True
    assert body["value"]["tenantUrl"] == "https://wwse.apps.dynatrace.com"
    assert body["value"]["apiToken"] == "COE-SECRET"


def test_ensure_remote_grail_updates_existing_setting():
    import httpx
    captured = {}
    orig_tok = dep._coe_remote_grail_token
    dep._coe_remote_grail_token = lambda: "COE-SECRET"
    orig_client = _grail_client(captured, existing_items=[{"objectId": "obj-1"}])
    try:
        msg = asyncio.run(dep._ensure_remote_grail("deploytok", "https://sro97894.apps.dynatrace.com"))
    finally:
        httpx.AsyncClient = orig_client
        dep._coe_remote_grail_token = orig_tok
    assert msg == "updated → wwse"
    assert captured["put_url"].endswith("/obj-1")
    assert captured["put"]["value"]["apiToken"] == "COE-SECRET"


def test_scope_warnings_flags_missing_settings_scope():
    w = dep._scope_warnings("skipped (token lacks settings:objects:read/write)",
                            "skipped (token lacks settings:objects:read/write)")
    assert len(w) == 2
    assert any("remote-grail NOT configured" in x for x in w)
    # clean results → no warnings
    assert dep._scope_warnings("added 1 host(s)", "enabled → wwse") == []
    assert dep._scope_warnings("", "skipped (central tenant — stores locally)") == []


# ── _ensure_orbital_config (seed the app's Orbital service token) ───────────────
#
# NOTE: since E6b the PRIMARY route is `_seed_via_app_function` — the app writes
# its own settings, because `app-settings:objects:write` cannot be granted to an
# OAuth client (measured: 400 on the scope request, 403 on the write, on all
# three tenants including the COE master). The direct writes below are the
# FALLBACK for a tenant still running a version without that function, so these
# tests pin it there by saying the function is absent.

def _no_app_function():
    """Stand in for a tenant whose installed app predates seedOrbitalConfig."""
    saved = dep._seed_via_app_function

    async def absent(_token, _tenant):
        return None

    dep._seed_via_app_function = absent
    return saved

def test_ensure_orbital_config_skips_without_orbital_token():
    orig = dep._orbital_service_token
    dep._orbital_service_token = lambda: None
    try:
        msg = asyncio.run(dep._ensure_orbital_config("tok", "https://sro97894.apps.dynatrace.com"))
    finally:
        dep._orbital_service_token = orig
    assert "ORBITAL_TOKEN not configured" in msg


def test_ensure_orbital_config_creates_when_absent():
    import httpx
    captured = {}
    orig_fn = _no_app_function()
    orig_tok = dep._orbital_service_token
    dep._orbital_service_token = lambda: "ORB-SECRET"
    orig_client = _grail_client(captured, existing_items=[])
    try:
        msg = asyncio.run(dep._ensure_orbital_config("deploytok", "https://sro97894.apps.dynatrace.com"))
    finally:
        httpx.AsyncClient = orig_client
        dep._orbital_service_token = orig_tok
        dep._seed_via_app_function = orig_fn
    assert msg == "token seeded"
    # App-settings v2 takes ONE object (not a list) and kebab-case query params.
    assert captured["post"]["schemaId"] == "orbital-config"
    assert captured["post"]["value"]["token"] == "ORB-SECRET"
    assert captured["get_params"]["schema-id"] == "orbital-config"


def test_ensure_orbital_config_fills_empty_existing_object():
    import httpx
    captured = {}
    orig_fn = _no_app_function()
    orig_tok = dep._orbital_service_token
    dep._orbital_service_token = lambda: "ORB-SECRET"
    orig_client = _grail_client(captured, existing_items=[{"objectId": "obj-9", "value": {"token": ""}}])
    try:
        msg = asyncio.run(dep._ensure_orbital_config("deploytok", "https://sro97894.apps.dynatrace.com"))
    finally:
        httpx.AsyncClient = orig_client
        dep._orbital_service_token = orig_tok
        dep._seed_via_app_function = orig_fn
    assert msg == "token seeded"
    assert captured["put_url"].endswith("/obj-9")
    assert captured["put"]["value"]["token"] == "ORB-SECRET"


def test_ensure_orbital_config_never_clobbers_configured_token():
    """The API masks secrets on read, so a non-empty value can't be compared —
    the only safe rule is to leave it alone."""
    import httpx
    captured = {}
    orig_fn = _no_app_function()
    orig_tok = dep._orbital_service_token
    dep._orbital_service_token = lambda: "ORB-SECRET"
    orig_client = _grail_client(
        captured, existing_items=[{"objectId": "obj-9", "value": {"token": "***bb81c3df***"}}])
    try:
        msg = asyncio.run(dep._ensure_orbital_config("deploytok", "https://sro97894.apps.dynatrace.com"))
    finally:
        httpx.AsyncClient = orig_client
        dep._orbital_service_token = orig_tok
        dep._seed_via_app_function = orig_fn
    assert msg == "already configured"
    assert "put" not in captured and "post" not in captured


def test_scope_warnings_says_nothing_about_orbital_config():
    """Every orbital-config outcome except "seed refused" is silent — we do not
    seed any more, the app carries its own bearer. See _scope_warnings."""
    for status in ("skipped (token lacks app-settings:objects:write)",
                   "seed failed (HTTP 500)", "token seeded", "already configured"):
        assert dep._scope_warnings("added 1 host(s)", "enabled → wwse", status) == [], status


# --- deploy ref pinning -------------------------------------------------------
# "Update now" is public and tokenless: an unpinned Orbital ships whatever last
# landed on master to every tenant that clicks it. APP_DEPLOY_REF decouples
# "merged" from "publicly deployable".

def _with_ref(value, fn):
    saved = dep.APP_DEPLOY_REF
    dep.APP_DEPLOY_REF = value
    try:
        return fn()
    finally:
        dep.APP_DEPLOY_REF = saved


def test_deploy_ref_unpinned_follows_branch_tip():
    assert _with_ref("", dep.deploy_ref) == f"origin/{dep.APP_DEPLOY_BRANCH}"


def test_deploy_ref_pinned_returns_the_exact_ref():
    assert _with_ref("1.0.271", dep.deploy_ref) == "1.0.271"


def test_fetch_pulls_tags_only_when_pinned():
    # A plain branch fetch does not bring tags down, so a tag pin is unresolvable
    # without --tags — and the deploy would silently fall back to "local".
    calls = []

    async def fake_git(*args):
        calls.append(args)
        return 0, ""

    _with_ref("", lambda: asyncio.run(dep._fetch_deploy_ref(fake_git)))
    assert "--tags" not in calls[-1]

    _with_ref("1.0.271", lambda: asyncio.run(dep._fetch_deploy_ref(fake_git)))
    assert "--tags" in calls[-1]
    assert calls[-1][-1] == dep.APP_DEPLOY_BRANCH


def test_latest_version_reports_the_pin_to_admins():
    # The app's "Check for updates" shows this; an admin must be able to tell a
    # released version from the moving tip of the branch.
    saved = dep._latest_repo_version

    async def fake_latest():
        return "1.0.271", dep.deploy_ref()

    dep._latest_repo_version = fake_latest
    try:
        pinned = _with_ref("1.0.271", lambda: asyncio.run(dep.latest_version()))
        loose = _with_ref("", lambda: asyncio.run(dep.latest_version()))
    finally:
        dep._latest_repo_version = saved
    assert pinned["pinned"] is True and pinned["ref"] == "1.0.271"
    assert loose["pinned"] is False and loose["ref"] == f"origin/{dep.APP_DEPLOY_BRANCH}"


# ── Auto-deploy tenants: OAuth first, for all three ──────────────────────────
#
# The bug these pin: SRO's account client was configured as SRO_OAUTH_CLIENT_ID /
# SRO_ACCOUNT_URN but read as SRO_CLIENT_ID / SRO_RESOURCE. Nothing raised — the
# mint just returned None on every call and SRO silently deployed with the stored
# platform token instead. The OAuth route is the one we hand tenant admins, so it
# is the one that must be exercised, and a silent fallback hid that it never was.

def test_env_first_prefers_the_canonical_name():
    os.environ["ZZ_CANON"] = "canonical"
    os.environ["ZZ_ALIAS"] = "alias"
    try:
        assert dep._env_first("ZZ_CANON", "ZZ_ALIAS") == "canonical"
    finally:
        del os.environ["ZZ_CANON"], os.environ["ZZ_ALIAS"]


def test_env_first_falls_back_to_the_alias():
    os.environ["ZZ_ALIAS"] = "alias"
    try:
        assert dep._env_first("ZZ_CANON", "ZZ_ALIAS") == "alias"
    finally:
        del os.environ["ZZ_ALIAS"]


def test_env_first_ignores_empty_and_whitespace():
    os.environ["ZZ_CANON"] = "   "
    os.environ["ZZ_ALIAS"] = "alias"
    try:
        assert dep._env_first("ZZ_CANON", "ZZ_ALIAS") == "alias"
        assert dep._env_first("ZZ_NOPE", default="fallback") == "fallback"
    finally:
        del os.environ["ZZ_CANON"], os.environ["ZZ_ALIAS"]


def test_deploy_scopes_ask_for_settings_then_degrade():
    """Richest first, then the minimum that still installs.

    The grant is all-or-nothing: a scope the client lacks 400s the whole request.
    Asking only for apps:install/run always succeeds but silently skips the
    outbound allowlist, remote-grail forwarding and the stored Orbital bearer —
    a tenant that installs and then never forwards a training event.
    """
    sets = dep._deploy_scopes("deploy")
    assert len(sets) >= 2
    for s in sets:
        # Every rung must still install; that is the floor.
        assert "app-engine:apps:install" in s and "app-engine:apps:run" in s
    assert "settings:objects:write" in sets[0]       # ask for the full configuration
    assert "app-settings:objects:write" in sets[0]
    assert sets[-1].split() == ["app-engine:apps:install", "app-engine:apps:run"]
    assert dep._deploy_scopes("undeploy") == ["app-engine:apps:delete"]


def test_is_sprint():
    saved = dep.SPRINT_TENANT_URL
    dep.SPRINT_TENANT_URL = "https://ydi9582h.sprint.apps.dynatracelabs.com"
    try:
        assert dep._is_sprint("https://ydi9582h.sprint.apps.dynatracelabs.com")
        assert dep._is_sprint("https://ydi9582h.sprint.apps.dynatracelabs.com/ui/apps")
        # The sprint tenant is NOT reachable at the production apps domain.
        assert not dep._is_sprint("https://ydi9582h.apps.dynatrace.com")
    finally:
        dep.SPRINT_TENANT_URL = saved


def test_auto_deploy_token_prefers_oauth_over_the_stored_sro_token():
    saved = (dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET, dep.SRO_PLATFORM_TOKEN,
             dep.SRO_TENANT_URL, dep._mint_account_token)
    dep.SRO_TENANT_URL = "https://sro97894.apps.dynatrace.com"
    dep.SRO_CLIENT_ID = "cid"; dep.SRO_CLIENT_SECRET = "sec"
    dep.SRO_PLATFORM_TOKEN = "dt0s16.STORED"

    async def fake_mint(label, cid, csec, resource, action, sso_url=""):
        return "MINTED"
    dep._mint_account_token = fake_mint
    try:
        token, auto = _run(dep.auto_deploy_token("https://sro97894.apps.dynatrace.com", "deploy"))
        assert auto == "SRO"
        assert token == "MINTED"          # not the stored platform token
    finally:
        (dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET, dep.SRO_PLATFORM_TOKEN,
         dep.SRO_TENANT_URL, dep._mint_account_token) = saved


def test_auto_deploy_token_falls_back_to_the_stored_sro_token():
    saved = (dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET, dep.SRO_PLATFORM_TOKEN, dep.SRO_TENANT_URL)
    dep.SRO_TENANT_URL = "https://sro97894.apps.dynatrace.com"
    dep.SRO_CLIENT_ID = ""; dep.SRO_CLIENT_SECRET = ""     # no client → no mint
    dep.SRO_PLATFORM_TOKEN = "dt0s16.STORED"
    try:
        token, auto = _run(dep.auto_deploy_token("https://sro97894.apps.dynatrace.com", "deploy"))
        assert (token, auto) == ("dt0s16.STORED", "SRO")
    finally:
        (dep.SRO_CLIENT_ID, dep.SRO_CLIENT_SECRET,
         dep.SRO_PLATFORM_TOKEN, dep.SRO_TENANT_URL) = saved


def test_auto_deploy_token_ignores_an_unknown_tenant():
    assert _run(dep.auto_deploy_token("https://other.apps.dynatrace.com", "deploy")) == ("", "")


def test_sprint_auto_without_creds_503():
    saved = (dep.SPRINT_CLIENT_ID, dep.SPRINT_CLIENT_SECRET, dep.SPRINT_TENANT_URL)
    dep.SPRINT_CLIENT_ID = ""; dep.SPRINT_CLIENT_SECRET = ""
    dep.SPRINT_TENANT_URL = "https://ydi9582h.sprint.apps.dynatracelabs.com"
    try:
        _expect_http(503, dep.deploy_with_token(
            {"tenant": "https://ydi9582h.sprint.apps.dynatracelabs.com", "token": ""},
            x_auth_user="a"))
    finally:
        (dep.SPRINT_CLIENT_ID, dep.SPRINT_CLIENT_SECRET, dep.SPRINT_TENANT_URL) = saved


def test_is_coe_matches_both_of_its_names():
    """COE answers to geu80787 AND the wwse vanity alias.

    The shadowing bug: CENTRAL_TENANT_URL was declared as COE_TENANT_URL 400
    lines below the deploy-target constant of the same name, so _is_coe compared
    against wwse and never recognised the canonical geu80787 host the app sends.
    COE auto-deploy returned "a platform token is required for this tenant".
    """
    saved = dep.COE_TENANT_URL
    dep.COE_TENANT_URL = "https://geu80787.apps.dynatrace.com"
    try:
        assert dep._is_coe("https://geu80787.apps.dynatrace.com")
        assert dep._is_coe("https://wwse.apps.dynatrace.com")
        assert dep._is_coe("https://wwse.apps.dynatrace.com/ui/apps/my.dynatrace.enablements")
        assert not dep._is_coe("https://sro97894.apps.dynatrace.com")
        assert not dep._is_coe("")
    finally:
        dep.COE_TENANT_URL = saved


def test_central_forwarding_target_is_separate_from_the_deploy_target():
    # Two different jobs, two different names. Collapsing them is what broke COE.
    assert dep.CENTRAL_TENANT_HOST in dep.OUTBOUND_HOSTS
    assert dep.CENTRAL_TENANT_URL == "https://wwse.apps.dynatrace.com"
    assert "geu80787" in dep.COE_TENANT_URL


# ── Telling an operator WHICH permission is missing ──────────────────────────
#
# The delegated SSO flow checks scopes before deploying, because the token
# response says what was granted (test above: 403 naming the missing scopes). A
# pasted platform token carries no such claim, so an under-scoped one only fails
# at the registry — and reached the operator as "exit 1" plus 1500 characters of
# build log, with nothing in it saying "add app-engine:apps:install".

def test_permission_hint_names_the_scopes_a_deploy_needs():
    hint = dep._permission_hint("deploy", "HTTP 403 Forbidden from registry")
    assert "app-engine:apps:install" in hint and "app-engine:apps:run" in hint
    assert "TARGET tenant" in hint


def test_permission_hint_names_the_scope_an_undeploy_needs():
    assert "app-engine:apps:delete" in dep._permission_hint("undeploy", "403 forbidden")


def test_permission_hint_recognises_the_shapes_a_refusal_arrives_in():
    for out in ("HTTP 401", "Unauthorized", "insufficient permissions",
                "Access denied", "not permitted", "status 403"):
        assert dep._permission_hint("deploy", out), f"missed: {out}"


def test_permission_hint_stays_silent_on_unrelated_failures():
    # A build break or a network blip must not be reported as a permissions problem.
    assert dep._permission_hint("deploy", "TypeError: cannot read property of undefined") == ""
    assert dep._permission_hint("deploy", "ECONNREFUSED 127.0.0.1:443") == ""
    assert dep._permission_hint("deploy", "") == ""


# ── Preflight: prove the credential can finish BEFORE installing ─────────────
#
# Installing is only part of a deploy. A credential holding just apps:install
# produces a tenant where the app appears successfully and then 401s on every
# environment action, because the Orbital bearer was never seeded. That is the
# expensive kind of broken: it looks fine. So probe first, and refuse rather
# than leave a half-configured install behind.

class _FakeResp:
    def __init__(self, code): self.status_code = code
    def json(self): return {"items": []}


def _fake_httpx(codes):
    """AsyncClient stub returning the queued status codes in call order."""
    seq = list(codes)

    class C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _FakeResp(seq.pop(0))
        async def post(self, *a, **k): return _FakeResp(seq.pop(0))
    return lambda *a, **k: C()


def test_probe_reports_a_fully_capable_credential():
    saved = dep.httpx.AsyncClient
    dep.httpx.AsyncClient = _fake_httpx([200, 200, 200, 200])
    try:
        caps = _run(dep.probe_capabilities("tok", "https://t.apps.dynatrace.com"))
        assert dep.missing_capabilities(caps) == []
    finally:
        dep.httpx.AsyncClient = saved


def test_probe_names_exactly_what_a_403_denies():
    # registry ok, settings read/write denied, app-settings denied.
    saved = dep.httpx.AsyncClient
    dep.httpx.AsyncClient = _fake_httpx([200, 403, 403, 403])
    try:
        caps = _run(dep.probe_capabilities("tok", "https://t.apps.dynatrace.com"))
        assert dep.missing_capabilities(caps) == [
            "settings_read", "settings_write", "app_settings"]
    finally:
        dep.httpx.AsyncClient = saved


def test_probe_treats_non_403_failures_as_capable():
    # A blip or an unfamiliar status must never silently block a deploy — the
    # deploy itself stays the authority. Only an explicit 403 is a "no".
    saved = dep.httpx.AsyncClient
    dep.httpx.AsyncClient = _fake_httpx([500, 404, 400, 502])
    try:
        assert dep.missing_capabilities(
            _run(dep.probe_capabilities("tok", "https://t.apps.dynatrace.com"))) == []
    finally:
        dep.httpx.AsyncClient = saved


def test_describe_missing_names_the_scope_and_the_consequence():
    msg = dep.describe_missing(["app_settings"])
    assert "app-settings:objects:write" in msg
    assert "401" in msg                      # says what actually breaks
    msg2 = dep.describe_missing(["registry"])
    assert "app-engine:apps:install" in msg2


def test_choose_prefers_a_complete_credential_over_the_first_one():
    """SRO holds two credentials and the OAuth one is the LESS capable today.

    Order alone would pick OAuth and silently produce a half-configured tenant.
    Evidence picks the platform token, which measurably carries the settings
    scopes the OAuth client lacks.
    """
    saved = (dep._auto_candidates, dep.probe_capabilities)

    async def cands(tenant_url, action):
        return [("OAUTH", "oauth"), ("PLATFORM", "platform-token")], "SRO"

    async def probe(token, tenant_url):
        full = dict.fromkeys(dep.CAPABILITY_COST, True)
        if token == "OAUTH":
            return {**full, "settings_read": False, "settings_write": False,
                    "app_settings": False}
        return full

    dep._auto_candidates, dep.probe_capabilities = cands, probe
    try:
        pick = _run(dep.choose_deploy_credential("https://sro97894.apps.dynatrace.com", "deploy"))
        assert pick["source"] == "platform-token"
        assert pick["missing"] == []
    finally:
        dep._auto_candidates, dep.probe_capabilities = saved


def test_choose_returns_the_least_incomplete_when_none_are_complete():
    saved = (dep._auto_candidates, dep.probe_capabilities)

    async def cands(tenant_url, action):
        return [("A", "oauth"), ("B", "platform-token")], "SRO"

    async def probe(token, tenant_url):
        full = dict.fromkeys(dep.CAPABILITY_COST, True)
        if token == "A":
            return {**full, "settings_read": False, "settings_write": False}
        return {**full, "settings_write": False}

    dep._auto_candidates, dep.probe_capabilities = cands, probe
    try:
        pick = _run(dep.choose_deploy_credential("https://sro97894.apps.dynatrace.com", "deploy"))
        assert pick["source"] == "platform-token"      # one gap beats two
        assert pick["missing"] == ["settings_write"]
    finally:
        dep._auto_candidates, dep.probe_capabilities = saved


def test_undeploy_skips_the_probe():
    # Undeploy only touches the registry; demanding settings scopes for it would
    # block a legitimate removal for no reason.
    saved = (dep._auto_candidates, dep.probe_capabilities)

    async def cands(tenant_url, action):
        return [("T", "oauth")], "COE"

    async def probe(token, tenant_url):
        raise AssertionError("undeploy must not probe")

    dep._auto_candidates, dep.probe_capabilities = cands, probe
    try:
        pick = _run(dep.choose_deploy_credential("https://geu80787.apps.dynatrace.com", "undeploy"))
        assert pick["token"] == "T" and pick["missing"] == []
    finally:
        dep._auto_candidates, dep.probe_capabilities = saved


def test_deploy_is_refused_before_installing_when_scopes_are_missing():
    saved = (dep._auto_candidates, dep.probe_capabilities, dep._deploy_with_status)

    async def cands(tenant_url, action):
        return [("T", "oauth")], "COE"

    async def probe(token, tenant_url):
        return {**dict.fromkeys(dep.CAPABILITY_COST, True), "settings_write": False}

    async def never(*a, **k):
        raise AssertionError("must not install when the credential is incomplete")

    dep._auto_candidates, dep.probe_capabilities, dep._deploy_with_status = cands, probe, never
    try:
        _expect_http(412, dep.deploy_with_token(
            {"tenant": "https://geu80787.apps.dynatrace.com", "token": ""}, x_auth_user="a"))
    finally:
        dep._auto_candidates, dep.probe_capabilities, dep._deploy_with_status = saved


def test_allow_partial_is_an_explicit_opt_in():
    # The escape hatch exists, but you have to ask for it by name.
    saved = (dep._auto_candidates, dep.probe_capabilities)

    async def cands(tenant_url, action):
        return [("T", "oauth")], "COE"

    async def probe(token, tenant_url):
        return {**dict.fromkeys(dep.CAPABILITY_COST, True), "settings_write": False}

    dep._auto_candidates, dep.probe_capabilities = cands, probe
    try:
        # Without the flag: refused. With it: gets past the gate (and fails later
        # on the un-stubbed deploy, which is a different error, not a 412).
        _expect_http(412, dep.deploy_with_token(
            {"tenant": "https://geu80787.apps.dynatrace.com", "token": ""}, x_auth_user="a"))
        try:
            _run(dep.deploy_with_token(
                {"tenant": "https://geu80787.apps.dynatrace.com", "token": "",
                 "allowPartial": True}, x_auth_user="a"))
        except HTTPException as e:
            assert e.status_code != 412, "allowPartial must clear the scope gate"
        except Exception:
            pass  # any non-HTTP failure downstream is fine — the gate was passed
    finally:
        dep._auto_candidates, dep.probe_capabilities = saved


# ── Capability from the grant, not from a read probe ─────────────────────────
#
# The COE client holds app-settings:objects:read but NOT :write. A GET-based
# probe answers "may I read this?" and passes — while the deploy's actual write
# still fails. Reading the granted scope removes that class of false pass.

def test_capabilities_from_scope_is_exact():
    caps = dep.capabilities_from_scope(
        "app-engine:apps:install app-engine:apps:run "
        "settings:objects:read settings:objects:write app-settings:objects:write")
    assert dep.missing_capabilities(caps) == []


def test_read_access_is_not_write_access():
    # Exactly the COE client's shape.
    caps = dep.capabilities_from_scope(
        "app-engine:apps:install app-engine:apps:run app-settings:objects:read "
        "settings:objects:read settings:objects:write")
    assert dep.missing_capabilities(caps) == ["app_settings"]
    # ...but it must not BLOCK: see test_ungrantable_scope_does_not_block_a_deploy.
    assert dep.blocking_missing(caps) == []


def test_install_scopes_are_both_required_for_registry():
    assert not dep.capabilities_from_scope("app-engine:apps:install")["registry"]
    assert dep.capabilities_from_scope(
        "app-engine:apps:install app-engine:apps:run")["registry"]


def test_empty_grant_can_do_nothing():
    assert dep.missing_capabilities(dep.capabilities_from_scope("")) == \
        ["registry", "settings_read", "settings_write", "app_settings"]


def test_scope_ladder_descends_one_capability_at_a_time():
    rungs = dep._deploy_scopes("deploy")
    # Richest first — capabilities_from_scope reads whichever rung succeeded, so a
    # mis-ordered ladder would understate what the token can do.
    assert len(rungs) >= 2
    counts = [len(dep.missing_capabilities(dep.capabilities_from_scope(r))) for r in rungs]
    assert counts == sorted(counts), f"ladder not richest-first: {counts}"
    assert dep.missing_capabilities(dep.capabilities_from_scope(rungs[0])) == []


# ── An ungrantable scope must not block a deploy ─────────────────────────────
#
# app-settings permissions are declared and held by an APP; they are not in the
# OAuth client scope catalog, so no admin can grant app-settings:objects:write
# however carefully they follow the instructions. Refusing a deploy over it makes
# deployment impossible rather than safe. The audit on this server records that
# write skipped 7 times and succeeded 0 across every deploy ever run.

def test_blocking_set_covers_only_grantable_capabilities():
    assert "app_settings" not in dep.BLOCKING_CAPABILITIES
    for k in dep.BLOCKING_CAPABILITIES:
        assert k in dep.CAPABILITY_COST


def test_ungrantable_scope_does_not_block_a_deploy():
    caps = {**dict.fromkeys(dep.CAPABILITY_COST, True), "app_settings": False}
    assert dep.missing_capabilities(caps) == ["app_settings"]   # still reported
    assert dep.blocking_missing(caps) == []                     # but never blocks


def test_grantable_gaps_still_block():
    for cap in ("registry", "settings_read", "settings_write"):
        caps = {**dict.fromkeys(dep.CAPABILITY_COST, True), cap: False}
        assert dep.blocking_missing(caps) == [cap], cap


def test_a_client_with_the_documented_scopes_deploys():
    # Exactly what the register page now asks for.
    caps = dep.capabilities_from_scope(
        "app-engine:apps:install app-engine:apps:run app-engine:apps:delete "
        "settings:objects:read settings:objects:write")
    assert dep.blocking_missing(caps) == []


def test_an_unseeded_orbital_token_is_not_a_warning_at_all():
    """We do not seed any more, so not having seeded is not news.

    The app ships its own bearer (api/_orbital-baked-token.ts), so a tenant that
    was never seeded provisions labs and runs workshops exactly like a seeded
    one. The seed call additionally CANNOT succeed on a tenant we do not own —
    an app function invoked by an external bearer runs with the caller's
    permissions, and app-settings:objects:write is ungrantable. Every one of
    these statuses is therefore silent; `orbital_config` still carries the raw
    string for diagnosis.
    """
    for status in ("skipped (token lacks app-settings:objects:write)",
                   "skipped (ORBITAL_TOKEN not configured)",
                   'seed via app function: error Missing scopes: '
                   '["app-settings:objects:write"]',
                   "seed failed (HTTP 500)",
                   "unverified (credential cannot read app settings)",
                   "unverified (app function unreachable: HTTP 404)"):
        assert dep._scope_warnings("", "", status) == [], status


def test_only_a_stale_server_token_is_action_required():
    """The one branch the baked bearer does NOT cover.

    "seed refused" means Orbital rejected the token Orbital itself sent, i.e.
    ORBITAL_TOKEN here is stale. The shipped bearer is normally that same value,
    so it is stale too and unseeded tenants really do 401 — the only case worth
    shouting about.
    """
    w = dep._scope_warnings("", "", "seed refused: Orbital did not accept the token")
    assert len(w) == 1
    assert "ACTION REQUIRED" in w[0]
    assert "ORBITAL_TOKEN in /home/ops/.env" in w[0]
    assert "_orbital-baked-token.ts" in w[0]


def test_the_other_two_post_install_warnings_are_untouched():
    """Silencing the orbital-config family must not silence its neighbours.

    remote-grail and the outbound allowlist really are skipped when the deploy
    token lacks settings:objects:write, and really do fail silently afterwards.
    """
    w = dep._scope_warnings("skipped (token lacks settings:objects:write)",
                            "skipped (token lacks settings:objects:write)",
                            "skipped (token lacks app-settings:objects:write)")
    assert len(w) == 2
    assert any("remote-grail NOT configured" in x for x in w)
    assert any("outbound allowlist NOT updated" in x for x in w)


def test_every_rung_that_can_carry_app_settings_read_does():
    """The read scope must be requested, not merely granted.

    Granting app-settings:objects:read to the client does nothing if the minted
    token never asks for it: the install still cannot read the app's own settings
    and still reports "unverified". The two richest rungs — and the install-only
    tail's neighbour — carry it so the common case (a client with read but not
    write) still produces a token that can check.
    """
    rungs = dep._deploy_scopes("deploy")
    assert "app-settings:objects:read" in rungs[0]
    assert "app-settings:objects:read" in rungs[1]
    # The realistic best today: settings r/w + app-settings read, no write.
    assert any("app-settings:objects:read" in r and "app-settings:objects:write" not in r
               and "settings:objects:write" in r for r in rungs)
    # Still degrades all the way to install-only.
    assert rungs[-1].split() == ["app-engine:apps:install", "app-engine:apps:run"]


# ---------------------------------------------------------------------------
# Realm-aware outbound allowlist. A sprint/dev tenant mints through its OWN SSO
# host, and mintCredentials verifies the client against SSO *before* storing it —
# so an allowlist missing that host makes the store fail, which means the host is
# never added, which means the store never succeeds. Measured on sprint 2026-08-06:
#   "Blocked request to 'sso-sprint.dynatracelabs.com' (host not in allowlist)"
# ---------------------------------------------------------------------------

def test_prod_tenant_gets_only_the_prod_hosts():
    hosts = dep._outbound_hosts_for("https://geu80787.apps.dynatrace.com")
    assert "sso.dynatrace.com" in hosts
    assert not any("sprint" in h for h in hosts)


def test_sprint_tenant_also_gets_its_own_realm_hosts():
    hosts = dep._outbound_hosts_for("https://ydi9582h.sprint.apps.dynatracelabs.com")
    assert "sso-sprint.dynatracelabs.com" in hosts
    assert "api-hardening.internal.dynatracelabs.com" in hosts
    # the prod baseline is still there — realm hosts are additive, never a swap
    assert "sso.dynatrace.com" in hosts
    assert "autonomous-enablements.whydevslovedynatrace.com" in hosts


def test_realm_hosts_do_not_mutate_the_shared_baseline():
    # _outbound_hosts_for must not append into OUTBOUND_HOSTS itself, or one
    # sprint deploy would leak sprint hosts into every later prod deploy.
    before = list(dep.OUTBOUND_HOSTS)
    dep._outbound_hosts_for("https://ydi9582h.sprint.apps.dynatracelabs.com")
    assert dep.OUTBOUND_HOSTS == before


# --- build once, upload many -------------------------------------------------

class _CountingProc:
    """Records how many of its kind overlap, so a test can assert serialisation."""

    def __init__(self, kind, seen, hold=0.05):
        self.kind, self.seen, self.hold = kind, seen, hold
        self.returncode = 0

    async def communicate(self):
        s = self.seen[self.kind]
        s["cur"] += 1
        s["max"] = max(s["max"], s["cur"])
        s["total"] += 1
        await asyncio.sleep(self.hold)
        s["cur"] -= 1
        return (b"ok", b"")

    async def wait(self):
        return 0

    def kill(self):
        pass


def _patch_deploy_subprocesses(seen):
    """Fake dt-app so build/upload can be counted separately. Returns a restore fn."""
    saved = {
        "exec": asyncio.create_subprocess_exec,
        "stamp": dep._stamp_ui_version,
        "exists": dep.Path.exists,
        "sandbox": dep._build_sandbox,
        "rmtree": dep.shutil.rmtree,
        "head": dep._head_sha,
        "version": dep._app_version,
    }

    async def fake_exec(*a, **kw):
        argv = [str(x) for x in a]
        if "build" in argv:
            return _CountingProc("build", seen)
        if "rev-parse" in argv:
            return _CountingProc("git", seen, hold=0)
        return _CountingProc("upload", seen)

    asyncio.create_subprocess_exec = fake_exec
    async def _no_stamp(env): return "v-test"
    dep._stamp_ui_version = _no_stamp
    dep.Path.exists = lambda self: True
    dep._build_sandbox = lambda dest: seen["sandboxes"].append(str(dest))
    dep.shutil.rmtree = lambda *a, **kw: None
    async def _head(): return "abc1234"
    dep._head_sha = _head
    dep._app_version = lambda: "1.0.0"

    def restore():
        asyncio.create_subprocess_exec = saved["exec"]
        dep._stamp_ui_version = saved["stamp"]
        dep.Path.exists = saved["exists"]
        dep._build_sandbox = saved["sandbox"]
        dep.shutil.rmtree = saved["rmtree"]
        dep._head_sha = saved["head"]
        dep._app_version = saved["version"]
        dep._BUILD_STAMP = None
    return restore


def _fresh_seen():
    return {k: {"cur": 0, "max": 0, "total": 0} for k in ("build", "upload", "git")} | {"sandboxes": []}


def test_the_build_is_serialised_and_happens_once_for_many_tenants():
    """The build mutates one shared checkout, so it must never overlap itself —
    and six tenants on the same commit must produce ONE build, not six."""
    seen = _fresh_seen()
    restore = _patch_deploy_subprocesses(seen)
    dep._BUILD_STAMP = None
    try:
        async def go():
            await asyncio.gather(*[
                dep._run_deploy("tok", f"https://t{i}.example.com") for i in range(6)
            ])
        asyncio.run(go())
    finally:
        restore()

    assert seen["build"]["max"] == 1, (
        f"{seen['build']['max']} builds ran in the shared checkout at once — "
        "the tree lock is not holding")
    assert seen["build"]["total"] == 1, (
        f"{seen['build']['total']} builds for one commit — the build stamp is not being reused")


def test_uploads_run_in_parallel():
    """Uploads work from private sandboxes, so they must NOT serialise —
    that is the whole point of build-once/upload-many."""
    seen = _fresh_seen()
    restore = _patch_deploy_subprocesses(seen)
    dep._BUILD_STAMP = None
    try:
        async def go():
            await asyncio.gather(*[
                dep._run_deploy("tok", f"https://t{i}.example.com") for i in range(6)
            ])
        asyncio.run(go())
    finally:
        restore()

    assert seen["upload"]["total"] == 6
    assert seen["upload"]["max"] > 1, (
        "uploads ran one at a time — parallel upload is not working")
    assert seen["upload"]["max"] <= dep.DEPLOY_UPLOAD_CONCURRENCY


def test_every_upload_gets_its_own_sandbox():
    """dt-app derives its token cache as <root>/.dt-app/.tokens.json with no env
    override, so two uploads sharing a root could hand tenant A the bearer
    written by tenant B's deploy. Distinct roots are what makes that impossible."""
    seen = _fresh_seen()
    restore = _patch_deploy_subprocesses(seen)
    dep._BUILD_STAMP = None
    try:
        async def go():
            await asyncio.gather(*[
                dep._run_deploy("tok", f"https://t{i}.example.com") for i in range(6)
            ])
        asyncio.run(go())
    finally:
        restore()

    boxes = seen["sandboxes"]
    assert len(boxes) == 6
    assert len(set(boxes)) == 6, f"sandboxes were reused across tenants: {boxes}"
    assert dep.APP_REPO_DIR not in boxes, "an upload ran in the shared checkout"


def test_sandbox_never_carries_the_token_cache():
    """.dt-app is skipped wholesale; the build metadata under it is re-linked
    explicitly. A credential file must never be one of the things copied."""
    assert ".dt-app" in dep._SANDBOX_SKIP
    assert ".env" in dep._SANDBOX_SKIP
    assert "node_modules" in dep._SANDBOX_SKIP


def test_a_failed_build_leaves_no_stamp():
    """A stamp after a failed build would let the next tenant upload whatever
    stale dist/ survived."""
    saved_exec = asyncio.create_subprocess_exec
    saved_exists = dep.Path.exists
    saved_stamp = dep._stamp_ui_version
    saved_head = dep._head_sha

    class Failing:
        returncode = 1
        async def communicate(self): return (b"build blew up", b"")
        async def wait(self): return 1
        def kill(self): pass

    async def fake_exec(*a, **kw):
        return Failing()

    asyncio.create_subprocess_exec = fake_exec
    dep.Path.exists = lambda self: True
    async def _no_stamp(env): return "v"
    dep._stamp_ui_version = _no_stamp
    async def _head(): return "deadbee"
    dep._head_sha = _head
    dep._BUILD_STAMP = ("stale", "0.0.1")
    try:
        ok, msg = asyncio.run(dep._ensure_build())
    finally:
        asyncio.create_subprocess_exec = saved_exec
        dep.Path.exists = saved_exists
        dep._stamp_ui_version = saved_stamp
        dep._head_sha = saved_head

    assert ok is False
    assert dep._BUILD_STAMP is None, "a failed build left a stamp behind"
    assert "blew up" in msg


def test_run_deploy_does_not_upload_when_the_build_failed():
    """A build failure must stop the deploy, not ship the previous bundle."""
    saved_build = dep._ensure_build
    saved_upload = dep._upload
    uploaded = []

    async def failing_build(): return False, "build failed"
    async def spy_upload(token, tenant): uploaded.append(tenant); return 0, "ok"

    dep._ensure_build = failing_build
    dep._upload = spy_upload
    try:
        rc, out = asyncio.run(dep._run_deploy("tok", "https://t.example.com"))
    finally:
        dep._ensure_build = saved_build
        dep._upload = saved_upload

    assert rc != 0
    assert uploaded == [], "uploaded despite a failed build"


# ── Account probes (verified against the COE account, 2026-08-17) ────────────
#
# The endpoint/scope pairs below are NOT interchangeable, and the first shipped
# version of this probe used a third scope that reaches neither of them:
#   account-env-read → GET /env/v2/accounts/{uuid}/environments  → display name
#   account-uac-read → GET /sub/v2/accounts/{uuid}/subscriptions → plan
#   account-idm-read → only /iam/v1/accounts/{uuid}/users; the bare account GET 404s.

def test_plan_reads_the_real_subscription_shape():
    payload = {"data": [
        {"uuid": "a", "type": "FREE", "subType": "PROSPECT", "status": "ACTIVE"},
        {"uuid": "b", "type": "FREE", "subType": "TRIAL", "status": "EXPIRED"},
    ]}
    assert dep._plan_from_subscriptions(payload) == "free"


def test_plan_trial_only_when_an_ACTIVE_row_says_trial():
    active_trial = {"data": [{"type": "FREE", "subType": "TRIAL", "status": "ACTIVE"}]}
    assert dep._plan_from_subscriptions(active_trial) == "trial"
    # the same row, expired → we know nothing about today's entitlement
    expired = {"data": [{"type": "FREE", "subType": "TRIAL", "status": "EXPIRED"}]}
    assert dep._plan_from_subscriptions(expired) == ""


def test_plan_paid_when_any_active_row_is_not_free():
    payload = {"data": [
        {"type": "FREE", "subType": "PROSPECT", "status": "ACTIVE"},
        {"type": "SUBSCRIPTION", "subType": "", "status": "ACTIVE"},
    ]}
    assert dep._plan_from_subscriptions(payload) == "paid"


def test_plan_blank_for_shapes_we_did_not_verify():
    # A wrong commercial label is worse than an empty cell — every one of these
    # degrades to "" rather than guessing.
    for bad in ({}, {"data": []}, {"subscriptions": [{"type": "PAID"}]}, [], None,
                {"data": [{"status": "ACTIVE"}]}, "nope"):
        assert dep._plan_from_subscriptions(bad) == "", bad


def test_env_name_matches_the_registered_environment_only():
    """The account list is keyed by env id. A single-entry list is NOT proof the
    entry belongs to the tenant being registered, so no rows[0] fallback."""
    import asyncio

    async def fake_bearer(*a, **k):
        return "tok", 200, ""

    class FakeResp:
        status_code = 200
        @staticmethod
        def json():
            return {"data": [{"id": "geu80787", "name": "WWSE COE", "active": True}]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return FakeResp()

    saved_bearer, saved_httpx = dep._oauth_bearer, dep.httpx.AsyncClient
    dep._oauth_bearer = fake_bearer
    dep.httpx.AsyncClient = FakeClient
    try:
        hit = asyncio.run(dep._probe_env_name(
            "sso", "cid", "sec", "urn:dtaccount:u", "https://api", "u", "geu80787"))
        miss = asyncio.run(dep._probe_env_name(
            "sso", "cid", "sec", "urn:dtaccount:u", "https://api", "u", "abc12345"))
    finally:
        dep._oauth_bearer, dep.httpx.AsyncClient = saved_bearer, saved_httpx

    assert hit == ("WWSE COE", "")
    assert miss[0] == "", "matched a name that belongs to a different environment"
    assert "abc12345" in miss[1]


def test_env_name_blank_without_an_env_id_and_never_mints_a_token():
    import asyncio
    called = []

    async def spy_bearer(*a, **k):
        called.append(a); return "tok", 200, ""

    saved = dep._oauth_bearer
    dep._oauth_bearer = spy_bearer
    try:
        out = asyncio.run(dep._probe_env_name(
            "sso", "cid", "sec", "urn:dtaccount:u", "https://api", "u", ""))
    finally:
        dep._oauth_bearer = saved
    assert out[0] == ""
    assert called == [], "minted an account bearer with nothing to look up"


# ── content sync credential: the ladder nothing used to execute ───────────────
# `CONTENT_SYNC_SCOPES` / `content_sync_token()` had no test of any kind. Every
# case in test_content_sync.py injects a ready-made credential
# (`kw.setdefault("credential", CRED)`), so the mint was never once exercised in
# CI — the same blind spot that let the self-update deploy scopes rot. These
# tests drive the real mint against a fake SSO that enforces the one rule that
# matters: a client is granted a scope set only if it holds every scope in it.

def _sso_holding(catalog):
    """A fake SSO for a client whose catalog is exactly `catalog`.

    Mirrors the real all-or-nothing grant: ask for one scope the client lacks and
    the WHOLE request is refused with 400 invalid_request and an empty
    error_description — indistinguishable from a wrong id or secret.
    Returns (client_class, attempts) where attempts records each scope asked for.
    """
    import httpx  # noqa: F401  (imported for symmetry with the other stubs)
    held, attempts = set(catalog), []

    class _Resp:
        def __init__(self, status, payload=None):
            self.status_code, self._payload = status, payload or {}
            self.text = json.dumps(self._payload)

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, data=None, headers=None):
            asked = set((data or {}).get("scope", "").split())
            attempts.append(sorted(asked))
            if asked - held:
                return _Resp(400, {"error": "invalid_request", "error_description": ""})
            return _Resp(200, {"access_token": "tok-" + str(len(attempts)),
                               "scope": " ".join(sorted(asked))})

    return _Client, attempts


def _with_sso(client_cls, coro):
    import httpx
    orig = httpx.AsyncClient
    httpx.AsyncClient = client_cls
    try:
        return asyncio.run(coro)
    finally:
        httpx.AsyncClient = orig


# The scopes a tenant admin is actually told to grant, straight from the module the
# Register gate and the checker page both read. Importing it (rather than repeating
# a literal) is the point: if the registerable set ever changes, these tests move
# with it instead of silently pinning a stale copy.
from dashboard.tenant_credentials import REGISTER_SCOPES  # noqa: E402

_REGISTERABLE = {s for entry in REGISTER_SCOPES for s in entry}


def test_content_sync_token_mints_for_a_client_that_holds_every_rung_scope():
    """The happy path, which nothing exercised before.

    A client holding everything the richest rung asks for gets rung 1 and one
    attempt — no needless degrading.
    """
    cls, attempts = _sso_holding(_REGISTERABLE | {"state:app-states:read"})
    saved = (dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE)
    dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE = "cid", "csec", "urn:dtaccount:x"
    try:
        token, label = _with_sso(cls, dep.content_sync_token(dep.COE_TENANT_URL))
    finally:
        (dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE) = saved
    assert token and label == "COE"
    assert len(attempts) == 1, f"should take the richest rung, took {len(attempts)} attempts"
    assert "document:documents:write" in attempts[0], "the rung that can write documents"


def test_content_sync_ladder_degrades_instead_of_giving_up():
    """A client without the document scopes still gets the mint path.

    The rungs shed document access first, deliberately: the mint path (documents
    owned by the service principal) is worth more than the caller-context
    fallback, so it is the last thing dropped.
    """
    cls, attempts = _sso_holding({"app-engine:apps:run", "state:app-states:read",
                                  "app-settings:objects:read"})
    saved = (dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE)
    dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE = "cid", "csec", "urn:dtaccount:x"
    try:
        token, _ = _with_sso(cls, dep.content_sync_token(dep.COE_TENANT_URL))
    finally:
        (dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE) = saved
    assert token, "should have fallen through to a rung it can hold"
    assert len(attempts) > 1, "the richest rung must have been refused first"


def test_content_sync_scopes_divergence_from_the_registerable_set_is_the_known_one():
    """Guard: what content sync ASKS FOR vs what a tenant can actually GRANT.

    A tenant admin is told to create a client with exactly REGISTER_SCOPES (the
    15). Because the grant is all-or-nothing, any scope in CONTENT_SYNC_SCOPES
    that is NOT registerable fails the entire mint for every correctly-built
    client — the tenant is never even contacted.

    Today there is exactly one such scope, `state:app-states:read`, and it is a
    leftover: CONTENT_SYNC_SCOPES was written 2026-08-05 (a562790) when app state
    was the only place the mint client lived, and `loadMintClient()` moved to
    reading the app-settings `mint-client` object first on 2026-08-10 (8db0f45,
    app #73), keeping app state only as a fallback for its own comment's "legacy
    app-state copy".

    This assertion is deliberately an equality, not a subset check. It stays green
    while the known divergence stands, and turns red BOTH ways: if someone adds
    another unregisterable scope, and if someone removes this one — because
    removing it is a behaviour change (content sync starts writing documents on
    every tenant, every 6h) that should be a decision, not a drive-by edit.
    """
    asked = {s for rung in dep.CONTENT_SYNC_SCOPES for s in rung.split()}
    assert asked - _REGISTERABLE == {"state:app-states:read"}, (
        "CONTENT_SYNC_SCOPES asks for scopes outside REGISTER_SCOPES: "
        f"{sorted(asked - _REGISTERABLE)}. Every one of these fails the whole "
        "grant for a correctly-registered tenant."
    )


def test_a_correctly_registered_client_cannot_mint_content_sync_today():
    """The production symptom, pinned.

    A client holding exactly the 15 registerable scopes — which is what COE, SRO
    and sprint now carry, and what every tenant admin is instructed to create —
    is refused on EVERY rung, because all three ask for `state:app-states:read`.
    `content_sync_token()` returns "" and `sync_tenant` reports `no-credential`.

    Observed live 2026-08-26 11:00 on all three tenants. When the divergence above
    is resolved this test must be inverted, not deleted: it is the difference
    between "the sync is off" and "the sync is on and writing".
    """
    cls, attempts = _sso_holding(_REGISTERABLE)
    saved = (dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE)
    dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE = "cid", "csec", "urn:dtaccount:x"
    try:
        token, label = _with_sso(cls, dep.content_sync_token(dep.COE_TENANT_URL))
    finally:
        (dep.COE_CLIENT_ID, dep.COE_CLIENT_SECRET, dep.COE_RESOURCE) = saved
    assert token == "" and label == "COE"
    assert len(attempts) == len(dep.CONTENT_SYNC_SCOPES), "every rung must have been tried"


def test_every_content_sync_rung_can_still_invoke_the_app_function():
    """`app-engine:apps:run` is the floor, exactly as install/run is for deploys.

    Without it the bearer cannot call the import function at all, so no rung may
    shed it however far the ladder degrades.
    """
    for rung in dep.CONTENT_SYNC_SCOPES:
        assert "app-engine:apps:run" in rung.split(), f"rung lost apps:run: {rung}"


def test_content_sync_rungs_are_strictly_descending_and_free_of_duplicates():
    """Each rung must be a strict subset of the one above, and internally unique.

    `_mint_account_token` returns the first rung that is granted and records its
    scope as an exact statement of what the bearer can do. That reasoning only
    holds if the rungs descend — an out-of-order rung would hand back a weaker
    token than the client could have had. A repeated scope inside one rung is
    harmless to SSO but means the list has been hand-edited into drift.
    """
    rungs = [rung.split() for rung in dep.CONTENT_SYNC_SCOPES]
    for rung in rungs:
        assert len(rung) == len(set(rung)), f"duplicate scope within a rung: {rung}"
    for richer, poorer in zip(rungs, rungs[1:]):
        assert set(poorer) < set(richer), (
            f"rungs must strictly descend; {poorer} is not a proper subset of {richer}")

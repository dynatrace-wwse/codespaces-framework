"""Tests for the SSO-delegated deploy flow guards + PKCE (Phase 1).

Run: /home/ops/ops-venv/bin/python -m dashboard.test_app_deploy
  or pytest dashboard/test_app_deploy.py
"""

import asyncio
import base64
import hashlib

from fastapi import HTTPException

from dashboard import app_deploy as dep


def _expect_http(status, coro):
    try:
        asyncio.run(coro)
    except HTTPException as e:
        assert e.status_code == status, f"expected {status}, got {e.status_code}"
        return
    raise AssertionError(f"expected HTTPException {status}, none raised")


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


def test_non_coe_without_token_400():
    # any non-COE Dynatrace tenant without a token → 400 (token required)
    _expect_http(400, dep.deploy_with_token({"tenant": "https://other.apps.dynatrace.com", "token": ""}, x_auth_user="a"))


def test_token_deploy_guards():
    # no auth → 401
    _expect_http(401, dep.deploy_with_token({"tenant": "https://x.apps.dynatrace.com", "token": "t"}, x_auth_user=None))
    # bad action → 400
    _expect_http(400, dep.deploy_with_token({"tenant": "https://x.apps.dynatrace.com", "token": "t", "action": "nuke"}, x_auth_user="a"))
    # non-Dynatrace tenant → 403
    _expect_http(403, dep.deploy_with_token({"tenant": "https://evil.example.com", "token": "t"}, x_auth_user="a"))
    # Dynatrace tenant but no token → 400
    _expect_http(400, dep.deploy_with_token({"tenant": "https://x.apps.dynatrace.com", "token": ""}, x_auth_user="a"))


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

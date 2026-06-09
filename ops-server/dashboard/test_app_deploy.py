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

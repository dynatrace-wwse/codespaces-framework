"""Tests for DTTokenProvisioner — minting per-training DT API tokens.

No network: httpx.AsyncClient is swapped for a fake (mirrors
dashboard/test_app_deploy.py).

Run: /home/ops/ops-venv/bin/python -m provisioning.test_dt_token_provisioner
  or pytest provisioning/test_dt_token_provisioner.py
"""
import asyncio

import httpx

from .dt_token_provisioner import DTTokenProvisioner
from .token_specs import TokenSpec


SPECS = [
    TokenSpec(name_suffix="operator", env_var="DT_OPERATOR_TOKEN",
              scopes=["entities.read", "settings.write"]),
    TokenSpec(name_suffix="ingest", env_var="DT_INGEST_TOKEN",
              scopes=["metrics.ingest"]),
]


class _Resp:
    def __init__(self, status, json_body=None, text=""):
        self.status_code = status
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("POST", "http://x"), response=self)


def _install_fake(handler):
    """Swap httpx.AsyncClient with a fake dispatching to handler(method,url,**kw).
    Returns (restore_fn, captured_calls list)."""
    calls = []

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None, data=None):
            calls.append(("POST", url, {"headers": headers, "json": json, "data": data}))
            return handler("POST", url, headers=headers, json=json, data=data)
        async def delete(self, url, headers=None):
            calls.append(("DELETE", url, {"headers": headers}))
            return handler("DELETE", url, headers=headers)

    orig = httpx.AsyncClient
    httpx.AsyncClient = _Client
    return (lambda: setattr(httpx, "AsyncClient", orig)), calls


def _seq_token_handler():
    """Handler that mints sequential ids/values for apiTokens POST, 200 on delete."""
    n = {"i": 0}

    def h(method, url, **kw):
        if method == "POST" and url.endswith("/api/v2/apiTokens"):
            n["i"] += 1
            return _Resp(201, {"id": f"id-{n['i']}", "token": f"dt0c01.TOKEN{n['i']}"})
        if method == "DELETE":
            return _Resp(204)
        return _Resp(404, text="unexpected")
    return h


# ── tests ────────────────────────────────────────────────────────────────────

def test_create_requires_credentials():
    try:
        DTTokenProvisioner("https://t.apps.dynatrace.com")
    except ValueError:
        return
    raise AssertionError("expected ValueError without creds")


def test_create_tokens_api_token_mode():
    restore, calls = _install_fake(_seq_token_handler())
    try:
        p = DTTokenProvisioner("https://geu80787.apps.dynatrace.com/", api_token="dt0c01.ADMIN")
        result = asyncio.run(p.create_tokens("dynatrace-wwse/bug-busters", "alice@dynatrace.com", SPECS))
    finally:
        restore()

    # env maps each spec.env_var -> minted token value; DT_ENVIRONMENT exposed
    assert result.env["DT_OPERATOR_TOKEN"] == "dt0c01.TOKEN1"
    assert result.env["DT_INGEST_TOKEN"] == "dt0c01.TOKEN2"
    assert result.env["DT_ENVIRONMENT"] == "https://geu80787.apps.dynatrace.com"
    assert result.token_ids == ["id-1", "id-2"]

    # two create POSTs, each carrying the right name prefix, scopes, expiry, auth
    posts = [c for c in calls if c[0] == "POST" and c[1].endswith("/api/v2/apiTokens")]
    assert len(posts) == 2
    body0 = posts[0][2]["json"]
    assert body0["name"] == "enbl-bug-busters-alice-operator"
    assert body0["scopes"] == ["entities.read", "settings.write"]
    assert body0["expirationDate"].endswith("Z")
    assert posts[0][2]["headers"]["Authorization"] == "Api-Token dt0c01.ADMIN"


def test_create_tokens_oauth_mode_refreshes_bearer():
    def h(method, url, **kw):
        if method == "POST" and url.endswith("/sso/oauth2/token"):
            return _Resp(200, {"access_token": "BEARER123", "expires_in": 3600})
        if method == "POST" and url.endswith("/api/v2/apiTokens"):
            return _Resp(201, {"id": "id-x", "token": "dt0c01.X"})
        return _Resp(404)
    restore, calls = _install_fake(h)
    try:
        p = DTTokenProvisioner("https://geu80787.apps.dynatrace.com",
                               oauth_client_id="cid", oauth_client_secret="sec")
        result = asyncio.run(p.create_tokens("org/repo", "bob@dynatrace.com", SPECS[:1]))
    finally:
        restore()
    # SSO called first, then apiTokens with the bearer
    assert calls[0][1].endswith("/sso/oauth2/token")
    create = [c for c in calls if c[1].endswith("/api/v2/apiTokens")][0]
    assert create[2]["headers"]["Authorization"] == "Bearer BEARER123"
    assert result.env["DT_OPERATOR_TOKEN"] == "dt0c01.X"


def test_create_tokens_revokes_on_partial_failure():
    """Second spec fails — already-created tokens must be revoked, then raise."""
    state = {"i": 0}

    def h(method, url, **kw):
        if method == "POST" and url.endswith("/api/v2/apiTokens"):
            state["i"] += 1
            if state["i"] == 1:
                return _Resp(201, {"id": "id-1", "token": "dt0c01.OK"})
            return _Resp(403, text="forbidden: token policy")
        if method == "DELETE":
            return _Resp(204)
        return _Resp(404)
    restore, calls = _install_fake(h)
    try:
        p = DTTokenProvisioner("https://geu80787.apps.dynatrace.com", api_token="dt0c01.ADMIN")
        raised = False
        try:
            asyncio.run(p.create_tokens("org/repo", "carol@dynatrace.com", SPECS))
        except RuntimeError:
            raised = True
    finally:
        restore()
    assert raised, "expected RuntimeError on partial failure"
    # the one successful token was revoked
    deletes = [c for c in calls if c[0] == "DELETE"]
    assert any(c[1].endswith("/api/v2/apiTokens/id-1") for c in deletes)


def test_revoke_tokens_deletes_each_and_tolerates_404():
    def h(method, url, **kw):
        return _Resp(404 if url.endswith("gone") else 204)
    restore, calls = _install_fake(h)
    try:
        p = DTTokenProvisioner("https://geu80787.apps.dynatrace.com", api_token="dt0c01.ADMIN")
        asyncio.run(p.revoke_tokens(["id-1", "gone"]))
    finally:
        restore()
    deletes = [c for c in calls if c[0] == "DELETE"]
    assert len(deletes) == 2


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


# ── PlatformTokenProvisioner (gen3 / Account Management) ────────────────────────

def _pt():
    from .dt_token_provisioner import PlatformTokenProvisioner
    return PlatformTokenProvisioner(
        tenant_url="https://ydi9582h.sprint.apps.dynatracelabs.com", env_id="ydi9582h",
        account_uuid="ceae4b9d", sso_token_url="https://sso-sprint.dynatracelabs.com/sso/oauth2/token",
        account_api_host="https://api-hardening.internal.dynatracelabs.com",
        oauth_client_id="dt0s02.X", oauth_client_secret="dt0s02.X.SEC")


def test_platform_token_requires_all_config():
    from .dt_token_provisioner import PlatformTokenProvisioner
    try:
        PlatformTokenProvisioner("https://t", "", "", "", "", "", "")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def _pt_handler():
    n = {"i": 0}
    def h(method, url, **kw):
        if method == "POST" and url.endswith("/sso/oauth2/token"):
            return _Resp(200, {"access_token": "BEARER", "expires_in": 3600})
        if method == "POST" and url.endswith("/platform-tokens"):
            n["i"] += 1
            return _Resp(200, {"name": "x", "tokenId": f"dt0s16.ID{n['i']}", "token": f"dt0s16.ID{n['i']}.VAL{n['i']}"})
        if method == "DELETE":
            return _Resp(200)
        return _Resp(404, text="unexpected")
    return h


def test_platform_token_create_uses_account_api_and_env_resource():
    restore, calls = _install_fake(_pt_handler())
    try:
        res = asyncio.run(_pt().create_tokens("dynatrace-wwse/enablement-kubernetes-101", "devlove@dynatracelabs.com", SPECS))
    finally:
        restore()
    # tokens minted into env vars
    assert res.env["DT_OPERATOR_TOKEN"].startswith("dt0s16.")
    assert res.env["DT_INGEST_TOKEN"].startswith("dt0s16.")
    assert len(res.token_ids) == 2
    # create POSTs hit the account platform-tokens endpoint with the env URN as resource
    creates = [c for c in calls if c[0] == "POST" and c[1].endswith("/platform-tokens")]
    assert creates and all("/iam/v1/accounts/ceae4b9d/platform-tokens" in c[1] for c in creates)
    body = creates[0][2]["json"]
    assert body["resource"] == ["urn:dtenvironment:ydi9582h"]
    assert "expirationDate" in body and isinstance(body["scope"], list)
    # bearer fetched with platform-token scope + account resource
    tok = [c for c in calls if c[1].endswith("/sso/oauth2/token")][0][2]["data"]
    assert "platform-token:tokens:write" in tok["scope"]
    assert tok["resource"] == "urn:dtaccount:ceae4b9d"


def test_platform_token_revoke_deletes_each():
    restore, calls = _install_fake(_pt_handler())
    try:
        asyncio.run(_pt().revoke_tokens(["dt0s16.A", "dt0s16.B", ""]))
    finally:
        restore()
    dels = [c for c in calls if c[0] == "DELETE"]
    assert len(dels) == 2  # empty id skipped
    assert all("/platform-tokens/dt0s16." in c[1] for c in dels)


def test_platform_create_activegate_token():
    import httpx
    captured = {}
    def h(method, url, **kw):
        if method == "POST" and url.endswith("/sso/oauth2/token"):
            captured["sso"] = kw.get("data")
            return _Resp(200, {"access_token": "AGBEARER", "expires_in": 3600})
        if method == "POST" and url.endswith("/activeGateTokens"):
            captured["create"] = kw.get("json")
            return _Resp(200, {"id": "dt0g02.ABC", "token": "dt0g02.ABC.SECRET"})
        return _Resp(404)
    restore, calls = _install_fake(h)
    try:
        out = asyncio.run(_pt().create_activegate_token("enbl-k8s-ag"))
    finally:
        restore()
    assert out["id"] == "dt0g02.ABC" and out["token"].startswith("dt0g02.")
    # AG-token endpoint hit with ENVIRONMENT type; bearer scoped to the ENVIRONMENT resource
    assert captured["create"]["activeGateType"] == "ENVIRONMENT"
    assert captured["sso"]["scope"] == "environment-api:activegate-tokens:write"
    assert captured["sso"]["resource"] == "urn:dtenvironment:ydi9582h"
    assert any(c[1].endswith("/platform/classic/environment-api/v2/activeGateTokens") for c in calls)

"""The bootstrap deploy hands the pasted OAuth client to the TENANT, and keeps nothing.

Why this file exists, and why it contradicts a comment you may still find nearby:

    Writing the app's own settings was believed impossible from a deploy credential,
    because `app-settings:objects:write` is not offered in the OAuth client scope
    catalog (still true — 400 invalid_request even for the COE master client). The
    conclusion drawn from that — "a new tenant needs a human to paste the client into
    the app" — was wrong. App settings and classic settings are the SAME objects:
    measured 2026-08-10 on ydi9582h, the app's `remote-grail` object comes back with
    an identical objectId through

        GET /platform/classic/environment-api/v2/settings/objects?schemaIds=app:my.dynatrace.enablements:remote-grail
        GET /platform/app-settings/v2/objects?schema-id=remote-grail

    and the classic door opens with `settings:objects:write`, which every account
    OAuth client can hold. `_ensure_remote_grail` had been using that door on every
    deploy the whole time.

So: `_store_mint_client` writes through the classic API, and after it a brand-new
tenant in a brand-new account mints its own per-learner tokens and updates itself.

Run: /home/ops/ops-venv/bin/python -m pytest dashboard/test_mint_client_store.py -q
"""

import asyncio
import json

import pytest

from dashboard import app_deploy as dep


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """httpx.AsyncClient stand-in. `calls` records every request in order."""

    def __init__(self, get_resp, post_resp=None, put_resp=None):
        self._get, self._post, self._put = get_resp, post_resp, put_resp
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, params, None))
        return self._get

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, None, json))
        return self._post

    async def put(self, url, headers=None, json=None):
        self.calls.append(("PUT", url, None, json))
        return self._put


CLIENT = dict(client_id="dt0s02.ABCDEFGH", client_secret="dt0s02.ABCDEFGH.SECRETVALUE",
              account_urn="urn:dtaccount:11111111-2222-3333-4444-555555555555",
              sso_url="https://sso-sprint.dynatracelabs.com/sso/oauth2/token",
              api_host="https://api-hardening.internal.dynatracelabs.com")


async def _no_wait(*_a, **_k):
    """Retry backoff, minus the wall-clock. Patching asyncio.sleep with something that
    itself calls asyncio.sleep recurses forever — do not be clever here."""
    return None


def _store(monkeypatch, client):
    monkeypatch.setattr(dep.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(dep.asyncio, "sleep", _no_wait)
    return asyncio.run(dep._store_mint_client("deploy-bearer", "https://new.example.com", **CLIENT))


def test_creates_the_object_when_the_tenant_has_none(monkeypatch):
    c = _Client(_Resp(200, {"items": []}), post_resp=_Resp(200, []))
    out = _store(monkeypatch, c)
    assert out.startswith("stored")
    method, url, _, body = c.calls[-1]
    assert method == "POST"
    assert url.endswith("/platform/classic/environment-api/v2/settings/objects")
    assert body[0]["schemaId"] == dep.MINT_CLIENT_SCHEMA
    assert body[0]["schemaVersion"] == dep.MINT_CLIENT_SCHEMA_VERSION
    assert body[0]["scope"] == "environment"
    # Every field the app needs to authenticate, or loadMintClient() returns null.
    assert body[0]["value"] == {
        "clientId": CLIENT["client_id"], "clientSecret": CLIENT["client_secret"],
        "accountUrn": CLIENT["account_urn"], "ssoUrl": CLIENT["sso_url"],
        "apiHost": CLIENT["api_host"],
    }


def test_re_registering_overwrites_the_existing_client(monkeypatch):
    # The admin just pasted this client. It is the freshest statement of intent, so a
    # re-register must replace what is there rather than leave a stale secret behind.
    c = _Client(_Resp(200, {"items": [{"objectId": "OBJ-1"}]}), put_resp=_Resp(200))
    out = _store(monkeypatch, c)
    assert out.startswith("updated")
    method, url, _, body = c.calls[-1]
    assert method == "PUT" and url.endswith("/OBJ-1")
    assert body["value"]["clientSecret"] == CLIENT["client_secret"]


def test_a_client_without_settings_scopes_is_reported_not_crashed(monkeypatch):
    out = _store(monkeypatch, _Client(_Resp(403)))
    assert "settings:objects" in out
    assert out.startswith("skipped")


def test_a_failed_write_says_so_instead_of_claiming_success(monkeypatch):
    c = _Client(_Resp(200, {"items": []}), post_resp=_Resp(400, text="schema unknown"))
    out = _store(monkeypatch, c)
    assert out.startswith("create failed")
    assert "400" in out


def test_the_schema_is_retried_while_the_fresh_install_settles(monkeypatch):
    # An app's schemas are not queryable the instant its install returns. Concluding
    # "this tenant cannot hold a client" on the first 404 would leave every brand-new
    # tenant unconfigured — the exact failure this whole change exists to remove.
    seq = [_Resp(404), _Resp(404), _Resp(200, {"items": []})]

    class _Retry(_Client):
        async def get(self, url, headers=None, params=None):
            self.calls.append(("GET", url, params, None))
            return seq.pop(0)

    c = _Retry(None, post_resp=_Resp(201, []))
    out = _store(monkeypatch, c)
    assert out.startswith("stored")
    assert sum(1 for m, *_ in c.calls if m == "GET") == 3


def test_the_secret_never_reaches_redis_or_the_audit(monkeypatch):
    # Orbital's promise: the client is in memory for one deploy and nowhere else.
    # The store call is the only place it is used, and it goes to the TENANT.
    c = _Client(_Resp(200, {"items": []}), post_resp=_Resp(200, []))
    _store(monkeypatch, c)
    targets = {url for _, url, _, _ in c.calls}
    assert all(u.startswith("https://new.example.com/") for u in targets)
    # And nothing carries the secret except the settings write body.
    carrying = [b for _, _, _, b in c.calls if b and CLIENT["client_secret"] in json.dumps(b)]
    assert len(carrying) == 1


@pytest.mark.parametrize("status", ["stored (app mints + self-updates on its own)",
                                    "updated (app mints + self-updates on its own)"])
def test_success_statuses_are_the_ones_the_route_checks_for(status):
    # The route decides whether to raise ACTION REQUIRED by prefix. Keep them in sync.
    assert status.startswith(("stored", "updated"))


# ── the realm the client authenticates against must be reachable ──────────────

def test_the_pasted_clients_own_realm_is_added_to_the_outbound_allowlist(monkeypatch):
    # A tenant in a realm REALM_OUTBOUND_HOSTS has never met would otherwise store the
    # client happily and then fail every mint at the allowlist — the client's own
    # ssoUrl/apiHost are the authoritative answer, so they travel with it.
    seen = {}

    class _AL:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            return _Resp(200, {"items": [{"objectId": "AL-1", "value": {
                "allowedOutboundConnections": {"enforced": True, "hostList": []}}}]})

        async def put(self, url, headers=None, json=None):
            seen["hosts"] = json["value"]["allowedOutboundConnections"]["hostList"]
            return _Resp(200)

    monkeypatch.setattr(dep.httpx, "AsyncClient", lambda *a, **k: _AL())
    out = asyncio.run(dep._ensure_outbound_allowlist(
        "bearer", "https://t.sprint.apps.dynatracelabs.com",
        extra_hosts=["sso-unknown-realm.example.com", "api-unknown-realm.example.com"]))
    assert out.startswith("added")
    assert "sso-unknown-realm.example.com" in seen["hosts"]
    assert "api-unknown-realm.example.com" in seen["hosts"]
    # and the baseline hosts are still there
    assert "autonomous-enablements.whydevslovedynatrace.com" in seen["hosts"]


def test_a_realm_already_on_the_list_is_not_duplicated(monkeypatch):
    class _AL:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            return _Resp(200, {"items": [{"objectId": "AL-1", "value": {
                "allowedOutboundConnections": {
                    "enforced": True,
                    "hostList": dep._outbound_hosts_for("https://t.sprint.apps.dynatracelabs.com"),
                }}}]})

    monkeypatch.setattr(dep.httpx, "AsyncClient", lambda *a, **k: _AL())
    out = asyncio.run(dep._ensure_outbound_allowlist(
        "bearer", "https://t.sprint.apps.dynatracelabs.com",
        extra_hosts=["sso-sprint.dynatracelabs.com"]))
    assert out == "allowlist already complete"

#!/usr/bin/env python3
"""Mint (and revoke) one short-lived gen3 platform token for a framework test run.

This is the *harness* path, not the learner path — see the header of
`mint_platform_token.sh` for why it exists and when it must not be used.

The shape of the two calls is copied from the enablement app's own minting, and
every line of it was paid for once already:

  1. The client-credentials grant goes to the **SSO host**, which is a different
     origin from the tenant. POSTing it to the tenant answers 301 and the failure
     reads like a bad secret.
  2. Minting a `dt0s16` is the **Account Management API**, not the environment API,
     and it needs its own grant (`platform-token:tokens:write|manage`) with the
     **account** URN as `resource`.
  3. In the mint body, `scope` is a **LIST**. A space-delimited string — which is
     what the grant above takes — returns 400 "Problem with json mapping."
  4. `resource` in the mint body is the **environment** URN, not the account one.

Stdlib only: this runs inside the training container, which has no `requests`.

stdout is data (token id, then token value) and nothing else, so the caller can
capture it without a token ever reaching a terminal. Diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# The grant that lets a client mint platform tokens. Distinct from the
# environment-API grant the classic token path uses.
PLATFORM_TOKEN_GRANT_SCOPES = "platform-token:tokens:write platform-token:tokens:manage"

DEFAULT_SSO = "https://sso.dynatrace.com"
DEFAULT_ACCOUNT_API = "https://api.dynatrace.com"

# Non-production Dynatrace realms (the internal `*.dynatracelabs.com` families) do
# NOT share either host with production, and neither host is derivable from the
# tenant URL. Rather than carry an internal address table in a public repository,
# this asks the tenant where its SSO is, and requires DT_SSO_URL / DT_ACCOUNT_API_HOST
# to be given explicitly for such a tenant when the probe cannot answer. Silently
# using the production hosts for a labs tenant mints against the wrong realm and
# fails with a 400 that reads like a bad secret.
NON_PRODUCTION_DOMAIN = ".dynatracelabs.com"

TOKEN_PATH = "/sso/oauth2/token"
SSO_PROBE_PATH = "/platform/oauth2/authorization/dynatrace-sso"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def host_of(url: str) -> str:
    u = urlparse(url if "://" in url else f"https://{url}")
    return (u.hostname or "").lower()


def environment_id(url: str) -> str:
    """The environment id (`abc12345`) from any of the tenant's URL shapes."""
    return host_of(url).split(".")[0]


def sso_origin(tenant_url: str) -> str:
    """Ask the tenant where its SSO is.

    ``HEAD /platform/oauth2/authorization/dynatrace-sso`` redirects to the tenant's
    own SSO origin, and the redirect IS the answer — it is the only source that
    stays correct when a new realm appears. Production falls back to the production
    SSO when the probe cannot run; a non-production tenant refuses instead, because
    guessing there means minting against the wrong realm.
    """
    override = os.environ.get("DT_SSO_URL", "").strip()
    if override:
        return override.rstrip("/")
    host = host_of(tenant_url)
    if host.endswith(".dynatrace.com") or host.endswith(NON_PRODUCTION_DOMAIN):
        req = urllib.request.Request(f"https://{host}{SSO_PROBE_PATH}", method="HEAD")
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            resp = opener.open(req, timeout=8)
            loc = resp.headers.get("location") or ""
            if loc:
                p = urlparse(loc)
                if p.scheme and p.netloc:
                    return f"{p.scheme}://{p.netloc}"
        except Exception as exc:
            log(f"   SSO discovery probe failed ({type(exc).__name__})")
    if host.endswith(NON_PRODUCTION_DOMAIN):
        # Refuse rather than guess: the production SSO would answer, and answer wrong.
        log("❌ this is a non-production tenant and its SSO could not be discovered.")
        log("   Set DT_SSO_URL to that realm's SSO origin and retry.")
        sys.exit(3)
    return DEFAULT_SSO


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The redirect IS the answer here — following it throws it away."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None

    def http_error_302(self, req, fp, code, msg, headers):  # noqa: D102
        return fp

    http_error_301 = http_error_303 = http_error_307 = http_error_302


def http(url: str, *, method: str = "GET", data: bytes | None = None,
         headers: dict[str, str] | None = None, timeout: int = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def require_env(names: list[str]) -> dict[str, str]:
    vals = {n: os.environ.get(n, "").strip() for n in names}
    missing = [n for n, v in vals.items() if not v]
    if missing:
        log(f"❌ missing required variable(s): {', '.join(missing)}")
        sys.exit(2)
    return vals


def account_bearer(tenant_url: str, client_id: str, client_secret: str,
                   resource: str) -> str:
    token_url = f"{sso_origin(tenant_url)}{TOKEN_PATH}"
    log(f"   SSO: {token_url}")
    body = "&".join([
        "grant_type=client_credentials",
        f"client_id={urllib.parse.quote(client_id)}",
        f"client_secret={urllib.parse.quote(client_secret)}",
        f"scope={urllib.parse.quote(PLATFORM_TOKEN_GRANT_SCOPES)}",
        f"resource={urllib.parse.quote(resource)}",
    ]).encode()
    status, raw = http(token_url, method="POST", data=body,
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status >= 400:
        # SSO hard-400s a scope the client does not hold and leaves
        # error_description EMPTY, so the body is all there is. Name the scopes
        # asked for — otherwise this is indistinguishable from a bad secret.
        log(f"❌ OAuth grant HTTP {status} (scopes={PLATFORM_TOKEN_GRANT_SCOPES!r})")
        log(f"   body: {raw[:400].decode('utf-8', 'replace')}")
        log("   An empty error_description here means the CLIENT does not hold the")
        log("   platform-token scopes — that is a finding about the client, not a")
        log("   reason to widen it.")
        sys.exit(3)
    return json.loads(raw)["access_token"]


def tokens_url(tenant_url: str, resource: str) -> str:
    api_host = os.environ.get("DT_ACCOUNT_API_HOST", "").strip()
    if not api_host:
        if host_of(tenant_url).endswith(NON_PRODUCTION_DOMAIN):
            log("❌ this is a non-production tenant, whose Account Management API is not")
            log("   the production one and cannot be derived from the tenant URL.")
            log("   Set DT_ACCOUNT_API_HOST for that realm and retry.")
            sys.exit(3)
        api_host = DEFAULT_ACCOUNT_API
    account_id = resource.split(":")[-1]
    return f"{api_host.rstrip('/')}/iam/v1/accounts/{account_id}/platform-tokens"


def cmd_mint(scopes: list[str]) -> int:
    if not scopes:
        log("❌ no scopes given")
        return 2
    env = require_env(["DT_ENVIRONMENT", "DT_OAUTH_CLIENT_ID",
                       "DT_OAUTH_CLIENT_SECRET", "DT_OAUTH_RESOURCE"])
    tenant = env["DT_ENVIRONMENT"]
    ttl_hours = int(os.environ.get("DT_PLATFORM_TOKEN_TTL_HOURS", "1"))
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
               ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    name = os.environ.get("DT_PLATFORM_TOKEN_NAME", "").strip() or \
        f"enbl-fwtest-{secrets.token_hex(4)}"

    log(f"   minting '{name}' — {len(scopes)} scope(s), expires {expires}")
    bearer = account_bearer(tenant, env["DT_OAUTH_CLIENT_ID"],
                            env["DT_OAUTH_CLIENT_SECRET"], env["DT_OAUTH_RESOURCE"])
    url = tokens_url(tenant, env["DT_OAUTH_RESOURCE"])
    payload = {
        "name": name,
        "scope": list(scopes),                                   # a LIST. Not a string.
        "resource": [f"urn:dtenvironment:{environment_id(tenant)}"],
        "tags": ["enablement", "framework-test"],
        "expirationDate": expires,
    }
    status, raw = http(url, method="POST", data=json.dumps(payload).encode(),
                       headers={"Authorization": f"Bearer {bearer}",
                                "Content-Type": "application/json"})
    if status >= 400:
        log(f"❌ platform-token mint HTTP {status}")
        log(f"   body: {raw[:600].decode('utf-8', 'replace')}")
        log("   If this names a scope: that is a FINDING about what this tenant grants")
        log("   this client. Report it — do not drop the scope to get a green run.")
        return 4
    data = json.loads(raw)
    token = data.get("token")
    if not token:
        log("❌ mint returned 200 with no token value")
        return 4
    # Data on stdout, in the order the caller reads it: id, then value.
    print(data.get("tokenId") or data.get("id") or "")
    print(token)
    return 0


def cmd_revoke(token_id: str) -> int:
    env = require_env(["DT_ENVIRONMENT", "DT_OAUTH_CLIENT_ID",
                       "DT_OAUTH_CLIENT_SECRET", "DT_OAUTH_RESOURCE"])
    bearer = account_bearer(env["DT_ENVIRONMENT"], env["DT_OAUTH_CLIENT_ID"],
                            env["DT_OAUTH_CLIENT_SECRET"], env["DT_OAUTH_RESOURCE"])
    url = f"{tokens_url(env['DT_ENVIRONMENT'], env['DT_OAUTH_RESOURCE'])}/{token_id}"
    status, raw = http(url, method="DELETE",
                       headers={"Authorization": f"Bearer {bearer}"})
    # 404 counts as revoked — the token is not there, which is the state asked for —
    # but SAY which of the two happened. "Revoked" printed for a token that was
    # never found is a check that cannot fail: a wrong id would read as success.
    if status in (200, 204):
        log(f"   revoked (HTTP {status})")
        return 0
    if status == 404:
        log("   already gone (HTTP 404) — nothing to revoke")
        return 0
    log(f"❌ revoke HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
    return 5


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        log(__doc__ or "")
        log("usage: mint_platform_token.py mint <scope> [<scope>...] | revoke <token-id>")
        return 2
    if argv[1] == "mint":
        return cmd_mint(argv[2:])
    if argv[1] == "revoke":
        if len(argv) != 3:
            log("usage: mint_platform_token.py revoke <token-id>")
            return 2
        return cmd_revoke(argv[2])
    log(f"unknown command: {argv[1]}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

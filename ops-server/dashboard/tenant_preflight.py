"""What a tenant + its OAuth client can ACTUALLY do — measured, never inferred.

This module is the single implementation of the deploy gate. It has two front doors:

  * `POST /api/deploy/oauth`     — Register Tenant. Refuses with 412 and installs nothing.
  * `POST /api/deploy/preflight` — the public tenant checker. Same checks, no install.

They must never disagree. They used to: the checker was an independent 267-line bash
re-implementation, the scope list was hardcoded in six places, and nothing tested one
against the other. On 2026-08-24 that produced exactly the failure it was built to
prevent — an SE saw an all-green checker and a 412 from the register in the same minute
(`bnk46244`, see dashboard/tenant_credentials.py for the credential-shape half).

Rules this module keeps, each bought with an incident:

  * A granted scope is not proof. Every capability is EXERCISED — a real token minted and
    deleted, a real document created and deleted — because SSO stamps scope names without
    checking entitlement (`scu37051`: 12 scopes ACTIVE, every call "Permission denied.").
  * `skip` is not `pass`. A check that could not run is UNKNOWN, and neither door may
    report a tenant ready on the strength of a check that never happened.
  * `effective-permissions:resolve` answers for the PRESENTED token, so the bearer must
    already carry the permissions being asked about, or every answer is "false".
  * Never quote a token endpoint's raw body — it can carry an access_token.

Nothing here imports FastAPI, Redis, or app_deploy: the routes decide what a report means,
this module only establishes the facts.
"""

import asyncio  # noqa: F401  (kept for parity with the probes' retry ladders)
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from dashboard.tenant_credentials import (REGISTER_SCOPES, missing_from_catalog,
                                          sso_failure_cause)
from shared.log_safety import safe_error_detail, scrub_for_log

import logging

log = logging.getLogger(__name__)

APP_ID = "my.dynatrace.enablements"
OUTBOUND_SCHEMA = "builtin:dt-javascript-runtime.allowed-outbound-connections"


def _registry_url(tenant_url: str, app_id: str | None = None) -> str:
    base = f"{tenant_url.rstrip('/')}/platform/app-engine/registry/v1/apps"
    return f"{base}/{app_id}" if app_id else base


MINT_SCOPE = "platform-token:tokens:write platform-token:tokens:manage"


# Environment-scoped, and NOT covered by MINT_SCOPE: DynaKube's per-session ActiveGate
# token. Mirrors AG_SCOPE in the app's api/mintCredentials.function.ts.
AG_SCOPE = "environment-api:activegate-tokens:write"


# ...and its gen3 twin. The classic scope above is NOT in every account client's
# catalog, and SSO says so with HTTP 400 at the TOKEN endpoint — before any API
# call, so no IAM binding can rescue it. Measured 2026-08-19 on hpm49270, where
# it killed a learner's session mid-workshop. `fleet-management:*` mints the same
# dt0g02 against the same endpoint and exists exactly where the classic one does
# not, so the preflight tries both before calling a tenant unfit.
# Order and spelling must match AG_SCOPES in the app's api/_platform-mint.ts.
AG_SCOPES = (AG_SCOPE, "fleet-management:activegate.tokens:write")


# Realm SSO token endpoints + Account Management API hosts per domain class
# (classify_tenant → prod/sprint/dev). Overridable per request for unusual realms.
SSO_TOKEN_URL_BY_DOMAIN = {
    "prod": "https://sso.dynatrace.com/sso/oauth2/token",
    "sprint": "https://sso-sprint.dynatracelabs.com/sso/oauth2/token",
    "dev": "https://sso-dev.dynatracelabs.com/sso/oauth2/token",
}


ACCOUNT_API_BY_DOMAIN = {
    "prod": "https://api.dynatrace.com",
    "sprint": "https://api-hardening.internal.dynatracelabs.com",
    "dev": "https://api-hardening.internal.dynatracelabs.com",
}


LIVE_HOST_BY_DOMAIN = {
    # The host that authenticates raw token values — sprint has NO `.live.`.
    "prod": "https://{tid}.live.dynatrace.com",
    "sprint": "https://{tid}.sprint.dynatracelabs.com",
    "dev": "https://{tid}.dev.dynatracelabs.com",
}


CLASSIC_MINT_SCOPE = "environment-api:api-tokens:write"


DOC_SCOPE = ("document:documents:read document:documents:write "
             "document:documents:delete")


# What the app's PLATFORM_SPECS translate to (api/_platform-mint.ts toPlatformScopes) —
# the preflight mints the same shape the first learner will get.
PLATFORM_LEARNER_SCOPES = [
    "fleet-management:activegate.connection-info:read",
    "fleet-management:activegate.tokens:create",
    "fleet-management:activegate.tokens:write",
    "fleet-management:container-images:read",
    "fleet-management:oneagent.connection-info:read",
    "fleet-management:oneagents:download",
    "settings:objects:read",
    "settings:objects:write",
    "storage:entities:read",
    "storage:events:write",
    "storage:logs:write",
    "storage:metrics:write",
]


EFFECTIVE_PERMISSIONS_PATH = "/platform/management/v1/effective-permissions:resolve"


def _preflight_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def _oauth_bearer(sso_url: str, cid: str, csec: str, resource: str,
                        scope: str) -> tuple[str | None, int, str]:
    """client_credentials grant. Returns (access_token|None, http_status, error_snippet).
    The secret goes into the form only — never logged."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(sso_url, data={
                "grant_type": "client_credentials", "client_id": cid,
                "client_secret": csec, "resource": resource, "scope": scope,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code == 200:
            return r.json().get("access_token"), 200, ""
        return None, r.status_code, r.text[:200]
    except Exception as exc:
        return None, 0, str(exc)


async def _client_catalog(sso_url: str, cid: str, csec: str) -> tuple[set | None, int, str]:
    """Every scope this OAuth client actually holds, in ONE request.

    A client_credentials grant sent WITHOUT a `scope` parameter returns 200 and lists the
    client's entire granted catalog in the response `scope` field (measured against
    sso.dynatrace.com, 2026-08-24). That makes it the only way to separate the three causes
    SSO hides behind an identical `400 invalid_request` + empty `error_description`:

        bare grant 400  → the client id or secret is wrong, or no such client
        bare grant 200  → the client is real, so a 400 on a SCOPED grant is a catalog gap

    It also turns "which of the 15 scopes is missing" into a set difference instead of 15
    round-trips, which is what the checker page does today.

    Returns (scopes|None, status, error-snippet). None means the grant itself failed.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(sso_url, data={
                "grant_type": "client_credentials", "client_id": cid, "client_secret": csec,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code == 200:
            return set((r.json().get("scope") or "").split()), 200, ""
        return None, r.status_code, r.text[:200]
    except Exception as exc:
        return None, 0, str(exc)


async def _effective_permissions(token: str, tenant_url: str,
                                 permissions: list[str]) -> dict[str, str] | None:
    """Which of `permissions` this bearer can ACTUALLY exercise here.

    Returns {permission: "true"|"false"|"condition"}, or None when the platform
    does not offer the endpoint (older environments) so callers can fall back
    to a live probe rather than treat "could not ask" as "no".

    Why this is not the same as reading the token's `scope` claim: SSO stamps
    scope names WITHOUT an entitlement check, and effective permission is
    `scopes ∩ the owner's IAM policy`. That gap is not academic — it is
    precisely what silently broke content on bos01241, where a token carrying
    `document:documents:admin` was refused by every call that used it:

        403 {"error":{"code":403,"message":"Document not accessible: 8ff8e6fd-…"}}

    The import then failed on exactly the repos whose document already existed
    under another owner, and reported `errors=3` to a browser console nobody
    was reading. Four trainings were missing from that tenant's catalog for the
    whole delivery, and it read to the learner as a permissions bug in the app.

    Request/response shape is taken from the vendored SDK
    (@dynatrace-sdk/client-platform-management-service), not guessed:
        POST  {"permissions": [{"permission": "..."}]}
        200   [{"permission": "...", "granted": "true"|"false"|"condition"}]

    ⚠️ THE TRAP, measured on COE 2026-08-19 — `token` MUST already carry the
    permissions you are asking about. The API resolves for the PRESENTED TOKEN
    (its scopes ∩ the owner's IAM), not for what the client could obtain. Ask
    with a bearer that lacks the scope and every answer is `"false"`:

        bearer scoped app-engine:apps:run only
          → document:documents:admin  "false"   ← says nothing about IAM
        bearer scoped ...+document:documents:admin
          → document:documents:admin  "true"

    On COE both answers came back for a client that demonstrably CAN write
    documents. Wiring this to the deploy bearer would therefore have printed
    "ACTION REQUIRED — document:documents:admin is not effective" on every
    single deploy — the same cry-wolf failure this whole change exists to
    remove. Mint a bearer for the scopes first; see `_documents_admin_effective`.
    """
    if not permissions:
        return {}
    url = f"{tenant_url.rstrip('/')}{EFFECTIVE_PERMISSIONS_PATH}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/json"},
                             # The API caps a request at 100 permissions.
                             json={"permissions": [{"permission": p}
                                                   for p in permissions[:100]]})
        if r.status_code != 200:
            log.info("effective-permissions unavailable on %s (HTTP %s)",
                     scrub_for_log(tenant_url), r.status_code)
            return None
        rows = r.json() or []
        return {str(row.get("permission", "")): str(row.get("granted", "")) for row in rows}
    except Exception as exc:
        log.warning("effective-permissions on %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
        return None


async def _documents_admin_effective(sso_url: str, cid: str, csec: str, tenant: str,
                                     tenant_id: str) -> str:
    """Is `document:documents:admin` actually exercisable here?

    Returns "true" | "false" | "condition" | "" (unknown).

    Asks with a bearer that CARRIES the document scopes, because the resolve API
    answers for the presented token — see the warning in `_effective_permissions`.
    An SSO refusal is reported as unknown rather than false: that is a scope
    catalog gap, which the preflight already reports on its own, and calling it
    an IAM problem would send the operator to the wrong fix.
    """
    # DOC_SCOPE is read/write/delete — it does NOT include admin, which is the
    # one being asked about. Asking with it would reproduce the very trap this
    # function exists to avoid, so admin is named explicitly.
    bearer, _st, _err = await _oauth_bearer(
        sso_url, cid, csec, f"urn:dtenvironment:{tenant_id}",
        f"app-engine:apps:run {DOC_SCOPE} document:documents:admin")
    if bearer is None:
        return ""
    eff = await _effective_permissions(bearer, tenant, ["document:documents:admin"])
    if eff is None:
        return ""
    return eff.get("document:documents:admin", "")


async def _preflight_learner_tokens(sso_url: str, cid: str, csec: str, tenant: str,
                                    tenant_id: str, domain: str, account_urn: str,
                                    api_host: str, client_exists: bool | None = None) -> dict:
    """Which learner-token tier this tenant+client can actually deliver.

    Classic first: mint a real dt0c01 through the client and call the live domain with
    it (self-contained scopes — no owner-IAM intersection). Where classic creation is
    retired (HTTP 400, rolled out per ENVIRONMENT), mint a real platform token and probe
    it the same way — the only check that exposes the `scopes ∩ owner IAM policy` trap,
    because the mint API stamps scope names without any entitlement check (measured on
    scu37051: 12 scopes ACTIVE, every call "Permission denied.").

    Returns {"tier": "classic"|"platform"|"none", "detail": str}.
    """
    live = LIVE_HOST_BY_DOMAIN.get(domain, LIVE_HOST_BY_DOMAIN["prod"]).format(tid=tenant_id)
    proxy = f"{tenant.rstrip('/')}/platform/classic/environment-api/v2/apiTokens"
    detail: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            bearer, st, _err = await _oauth_bearer(sso_url, cid, csec,
                                                   f"urn:dtenvironment:{tenant_id}",
                                                   CLASSIC_MINT_SCOPE)  # _err feeds sso_failure_cause
            if bearer is None:
                detail.append(f"classic path unavailable ({CLASSIC_MINT_SCOPE}): "
                              f"{sso_failure_cause(st, _err, cid, client_exists)}")
            else:
                hdr = {"Authorization": f"Bearer {bearer}"}
                r = await c.post(proxy, headers=hdr, json={
                    "name": "enbl-preflight", "scopes": ["InstallerDownload"],
                    "expirationDate": _preflight_expiry()})
                if r.status_code == 201:
                    d = r.json()
                    probe = await c.get(
                        f"{live}/api/v1/deployment/installer/agent/connectioninfo",
                        headers={"Authorization": f"Api-Token {d.get('token', '')}"})
                    if d.get("id"):
                        await c.delete(f"{proxy}/{d['id']}", headers=hdr)
                    if probe.status_code == 200:
                        return {"tier": "classic",
                                "detail": "classic dt0c01 minted and proven live"}
                    detail.append(f"classic token minted but refused live "
                                  f"(HTTP {probe.status_code})")
                elif r.status_code == 400:
                    detail.append("classic API-token creation is retired on this "
                                  "environment (HTTP 400)")
                else:
                    detail.append(f"classic mint refused (HTTP {r.status_code}): "
                                  f"{r.text[:160]}")

            pt_bearer, st2, _err2 = await _oauth_bearer(sso_url, cid, csec, account_urn,
                                                        MINT_SCOPE)
            if pt_bearer is None:
                detail.append(f"platform path unavailable ({MINT_SCOPE}): "
                              f"{sso_failure_cause(st2, _err2, cid, client_exists)}")
                return {"tier": "none", "detail": "; ".join(detail)}
            acct = account_urn.split(":")[-1]
            base = f"{api_host.rstrip('/')}/iam/v1/accounts/{acct}/platform-tokens"
            hdr = {"Authorization": f"Bearer {pt_bearer}"}
            r = await c.post(base, headers=hdr, json={
                "name": "enbl-preflight", "scope": PLATFORM_LEARNER_SCOPES,
                "resource": [f"urn:dtenvironment:{tenant_id}"],
                "tags": ["enablement", "preflight"],
                "expirationDate": _preflight_expiry()})
            if r.status_code not in (200, 201):
                detail.append(f"platform mint refused (HTTP {r.status_code}): {r.text[:160]}")
                return {"tier": "none", "detail": "; ".join(detail)}
            d = r.json()
            probe = await c.get(f"{live}/api/v1/deployment/installer/agent/connectioninfo",
                                headers={"Authorization": f"Api-Token {d.get('token', '')}"})
            tok_id = d.get("tokenId") or d.get("id")
            if tok_id:
                await c.delete(f"{base}/{tok_id}", headers=hdr)
            if probe.status_code == 200:
                return {"tier": "platform", "detail": "; ".join(detail)}
            detail.append(
                f"platform token minted but the live environment refused it "
                f"(HTTP {probe.status_code}). A platform token's effective permissions are "
                f"its scopes ∩ the IAM policy of its OWNER — the person who created this "
                f"OAuth client — and the mint API does not check that. Recreate the client "
                f"as a user with admin rights on this environment.")
            return {"tier": "none", "detail": "; ".join(detail)}
    except httpx.HTTPError as e:
        detail.append(f"preflight error: {e}")
        return {"tier": "none", "detail": "; ".join(detail)}


async def _preflight_documents(sso_url: str, cid: str, csec: str, tenant: str,
                               tenant_id: str,
                               client_exists: bool | None = None) -> tuple[bool, str]:
    """The path the content importer uses: create a document AS THE APP's service
    identity (env-scoped client-credentials bearer) and delete it again. This is what
    failed on Asad's tenant while every scope readback said fine."""
    bearer, st, err = await _oauth_bearer(sso_url, cid, csec,
                                          f"urn:dtenvironment:{tenant_id}", DOC_SCOPE)
    if bearer is None:
        return False, f"the document scopes ({DOC_SCOPE}): {sso_failure_cause(st, err, cid, client_exists)}"
    base = f"{tenant.rstrip('/')}/platform/document/v1/documents"
    hdr = {"Authorization": f"Bearer {bearer}"}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(base, headers=hdr,
                             data={"name": "enbl-preflight", "type": "enablement-preflight"},
                             files={"content": ("content", b"{}", "application/json")})
            if r.status_code not in (200, 201):
                return False, (f"document create refused (HTTP {r.status_code}): "
                               f"{safe_error_detail(r.text)}")
            d = r.json()
            doc_id, ver = d.get("id"), d.get("version", 1)
            if doc_id:
                await c.delete(f"{base}/{doc_id}", headers=hdr,
                               params={"optimistic-locking-version": str(ver)})
            return True, "document created and deleted as the service identity"
    except httpx.HTTPError as e:
        return False, f"document probe error: {e}"


async def _preflight_activegate(sso_url: str, cid: str, csec: str, tenant: str,
                                tenant_id: str,
                                client_exists: bool | None = None) -> tuple[bool, str]:
    """Can this tenant mint the ActiveGate token (dt0g02) a DynaKube needs?

    Mints a REAL token and deletes it. Asking SSO for a bearer is not enough —
    that is what the deploy used to do, and it only ever ran AFTER the install,
    as a warning, gated behind `mint_ready` so a tenant failing both produced no
    warning at all. hpm49270 passed every check the deploy made and then failed
    the learner four hours later, inside the operator install, with:

        ActiveGate token mint failed: SSO client_credentials failed (HTTP 400)

    Tries both scope families (see AG_SCOPES). Every Kubernetes training needs
    this, so a tenant that can mint neither is refused rather than installed.
    """
    proxy = f"{tenant.rstrip('/')}/platform/classic/environment-api/v2/activeGateTokens"
    refusals: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            for scope in AG_SCOPES:
                bearer, st, err = await _oauth_bearer(
                    sso_url, cid, csec, f"urn:dtenvironment:{tenant_id}", scope)
                if bearer is None:
                    refusals.append(f"{scope}: {sso_failure_cause(st, err, cid, client_exists)}")
                    continue
                hdr = {"Authorization": f"Bearer {bearer}"}
                r = await c.post(proxy, headers=hdr, json={
                    "name": "enbl-preflight-ag", "activeGateType": "ENVIRONMENT",
                    "expirationDate": _preflight_expiry()})
                if r.status_code in (200, 201):
                    tok_id = (r.json() or {}).get("id")
                    if tok_id:
                        await c.delete(f"{proxy}/{tok_id}", headers=hdr)
                    return True, f"ActiveGate token minted via {scope}"
                # A bearer that WAS issued and is then refused by the API is an
                # IAM binding problem, not a catalog problem. Record it and try
                # the other family anyway — they are granted independently.
                refusals.append(f"{scope}: mint HTTP {r.status_code} "
                                f"{safe_error_detail(r.text)}")
    except httpx.HTTPError as e:
        return False, f"ActiveGate probe error: {e}"
    return False, ("cannot mint an ActiveGate token, so every Kubernetes training will "
                   "fail when DynaKube starts. Grant the OAuth client one of "
                   f"{' or '.join(AG_SCOPES)} on this environment — scopes cannot be "
                   "edited on an existing client, so this means creating a new one. "
                   f"Tried: {'; '.join(refusals)}")


# ─── checks the checker page used to re-implement, badly ─────────────────────────────
#
# Each of these existed on ONE side only, which is how a green checker and a 412 register
# could describe the same client. They live here now so both doors ask the same question.


async def _preflight_settings(sso_url: str, cid: str, csec: str, tenant: str,
                              tenant_id: str,
                              client_exists: bool | None = None) -> tuple[bool, str]:
    """Can this client write environment settings?

    Probed against the OUTBOUND ALLOWLIST schema — the object the deploy actually writes —
    with `validateOnly=true`, which validates and returns without creating anything.

    The checker page used to probe `builtin:management-zones` and treat a 404 as SKIP, so
    on any tenant without that schema it skipped the check entirely and still printed
    READY. Both sprint tenants are in exactly that state. Settings-write is not optional:
    without it the mint client is never stored, so the tenant can neither mint learner
    tokens nor self-update.
    """
    scope = "settings:objects:read settings:objects:write"
    bearer, st, err = await _oauth_bearer(sso_url, cid, csec,
                                          f"urn:dtenvironment:{tenant_id}", scope)
    if bearer is None:
        return False, f"({scope}): {sso_failure_cause(st, err, cid, client_exists)}"
    url = f"{tenant.rstrip('/')}/platform/classic/environment-api/v2/settings/objects"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{url}?validateOnly=true",
                             headers={"Authorization": f"Bearer {bearer}",
                                      "Content-Type": "application/json"},
                             json=[{"schemaId": OUTBOUND_SCHEMA, "scope": "environment",
                                    "value": {"allowedOutboundConnections": {
                                        "enforced": True, "hostList": []}}}])
    except httpx.HTTPError as e:
        return False, f"settings probe error: {e}"
    if r.status_code == 403:
        return False, ("settings write refused (HTTP 403) — the scope is granted but not "
                       "effective for the client's service user; bind an IAM policy "
                       "carrying it AT ENVIRONMENT LEVEL")
    if r.status_code >= 400 and r.status_code != 404:
        return False, (f"settings write refused (HTTP {r.status_code}): "
                       f"{safe_error_detail(r.text)}")
    return True, "environment settings are writable (validateOnly)"


async def _preflight_registry(sso_url: str, cid: str, csec: str, tenant: str,
                              tenant_id: str,
                              client_exists: bool | None = None) -> tuple[bool, str]:
    """Can this client reach the app registry — i.e. can it install at all?

    The checker page never probed this. It asked SSO to issue a bearer for
    `app-engine:apps:install` and called that a pass, which is precisely the
    granted-but-not-effective trap the rest of the page exists to catch. Orbital's token
    path has always probed it (a 403 on the registry GET is the only "no"), so an install
    scope that SSO stamps and IAM refuses was green on one door and 412 on the other.
    """
    scope = "app-engine:apps:install app-engine:apps:run"
    bearer, st, err = await _oauth_bearer(sso_url, cid, csec,
                                          f"urn:dtenvironment:{tenant_id}", scope)
    if bearer is None:
        return False, f"({scope}): {sso_failure_cause(st, err, cid, client_exists)}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_registry_url(tenant, APP_ID),
                            headers={"Authorization": f"Bearer {bearer}"})
    except httpx.HTTPError as e:
        return False, f"registry probe error: {e}"
    if r.status_code == 403:
        return False, ("the app registry refused this client (HTTP 403) — the install "
                       "scopes are granted but not effective, so the deploy would fail "
                       "after the credential looked fine")
    # 404 is the healthy answer on a tenant that does not have the app yet.
    return True, f"app registry reachable (HTTP {r.status_code})"


async def _preflight_deploy_bearer(sso_url: str, cid: str, csec: str,
                                   account_urn: str) -> tuple[bool, str]:
    """The bearer the install itself runs on — minted on the ACCOUNT urn, not the
    environment urn, with environment-level scopes.

    That combination is what `/api/deploy/oauth` uses and what the checker never tried: it
    only ever asked for env scopes with `resource=urn:dtenvironment:`. A client that
    answers there and refuses here fails AFTER the checker says ready, as a 502 rather
    than a 412, which reads like an Orbital fault rather than a client one.

    Reports which rung of the ladder succeeded, because the lower rungs silently drop
    capabilities: MIN carries no settings scopes, so remote-grail and the outbound
    allowlist are skipped on an install that otherwise looks clean.
    """
    rungs = [("full", "app-engine:apps:install app-engine:apps:run settings:objects:read "
                      "settings:objects:write app-settings:objects:read"),
             ("no app-settings:objects:read", "app-engine:apps:install app-engine:apps:run "
                                              "settings:objects:read settings:objects:write"),
             ("install-only", "app-engine:apps:install app-engine:apps:run")]
    last = ""
    for label, scope in rungs:
        bearer, st, err = await _oauth_bearer(sso_url, cid, csec, account_urn, scope)
        if bearer is not None:
            if label == "full":
                return True, "deploy bearer issued with the full install scope set"
            return True, (f"deploy bearer issued only at the '{label}' rung — the install "
                          f"will work but steps needing the dropped scopes are skipped")
        last = sso_failure_cause(st, err, cid, client_exists=True)
    return False, (f"SSO would not issue an install bearer on the ACCOUNT urn at any scope "
                   f"level: {last}. The client may hold these scopes on the environment "
                   f"and still refuse them here.")


# ─── the report both doors read ──────────────────────────────────────────────────────


@dataclass
class Check:
    """One capability, and whether it was established.

    `status` is deliberately three-valued. A check that could not RUN is `skip`, never
    `pass`: the checker page used to fold an unreachable probe into a green verdict, which
    is how a tenant could be declared ready on the strength of a check that never happened.
    """
    key: str
    title: str
    status: str          # "pass" | "fail" | "skip"
    detail: str
    blocking: bool = False
    scopes: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "status": self.status,
                "detail": self.detail, "blocking": self.blocking, "scopes": self.scopes}


@dataclass
class PreflightReport:
    client_exists: bool
    catalog: list
    missing_scopes: list
    checks: list
    learner_tier: str = "none"
    credential_detail: str = ""

    @property
    def blocking_failures(self) -> list:
        return [f"{c.title} — {c.detail}" for c in self.checks
                if c.blocking and c.status == "fail"]

    @property
    def warnings(self) -> list:
        return [f"{c.title} — {c.detail}" for c in self.checks
                if not c.blocking and c.status == "fail"]

    @property
    def unproven(self) -> list:
        """Checks that did not run. Not failures — but not evidence of health either."""
        return [f"{c.title} — {c.detail}" for c in self.checks if c.status == "skip"]

    @property
    def ready(self) -> bool:
        """The single verdict. Both doors read THIS — that is the whole point of the
        module: the checker cannot be generous where the register is strict."""
        return self.client_exists and not self.blocking_failures

    def as_dict(self) -> dict:
        return {"ready": self.ready, "clientExists": self.client_exists,
                "learnerTokenTier": self.learner_tier,
                "catalog": sorted(self.catalog), "missingScopes": self.missing_scopes,
                "blocking": self.blocking_failures, "warnings": self.warnings,
                "unproven": self.unproven,
                "checks": [c.as_dict() for c in self.checks]}


async def preflight_all(sso_url: str, cid: str, csec: str, tenant: str, tenant_id: str,
                        domain: str, account_urn: str, api_host: str,
                        catalog=None) -> PreflightReport:
    """Everything a tenant + client must be able to do, exercised once.

    Called by `/api/deploy/oauth` (which refuses on `blocking_failures`) and by
    `/api/deploy/preflight` (which just renders the report). Same calls, same order, same
    verdict — which is the property the two hand-written implementations never had.

    Every probe cleans up after itself: tokens minted here are deleted here.
    """
    # `catalog` may be passed in by a caller that already read it (the register route
    # gates on it before doing anything else) — one bare grant is enough for both.
    cat_st, cat_err = 200, ""
    if catalog is None:
        catalog, cat_st, cat_err = await _client_catalog(sso_url, cid, csec)
    exists = catalog is not None
    checks: list = []

    if not exists:
        # Nothing below can mean anything: every scoped grant will fail for a reason that
        # has nothing to do with this tenant's scopes.
        return PreflightReport(
            client_exists=False, catalog=[], missing_scopes=[], checks=[
                Check("credential", "OAuth client", "fail",
                      sso_failure_cause(cat_st, cat_err, cid, client_exists=False),
                      blocking=True)],
            credential_detail=sso_failure_cause(cat_st, cat_err, cid, client_exists=False))

    missing = missing_from_catalog(catalog)
    checks.append(Check(
        "catalog", "Scope catalog", "pass" if not missing else "fail",
        "the client holds every scope Register Tenant needs" if not missing
        else ("missing: " + ", ".join(missing) + ". Scopes cannot be added to an existing "
              "client, so this needs a NEW client"),
        blocking=False, scopes=str(len(REGISTER_SCOPES)) + " required"))

    learner = await _preflight_learner_tokens(sso_url, cid, csec, tenant, tenant_id, domain,
                                              account_urn, api_host, client_exists=True)
    checks.append(Check(
        "learner_token", "Learner token", "fail" if learner["tier"] == "none" else "pass",
        learner["detail"] or f"minted and proven on the {learner['tier']} tier",
        blocking=True, scopes=f"{CLASSIC_MINT_SCOPE} | {MINT_SCOPE}"))

    docs_ok, docs_detail = await _preflight_documents(sso_url, cid, csec, tenant, tenant_id,
                                                      client_exists=True)
    checks.append(Check("documents", "Training content", "pass" if docs_ok else "fail",
                        docs_detail, blocking=True, scopes=DOC_SCOPE))

    ag_ok, ag_detail = await _preflight_activegate(sso_url, cid, csec, tenant, tenant_id,
                                                   client_exists=True)
    checks.append(Check("activegate", "ActiveGate token", "pass" if ag_ok else "fail",
                        ag_detail, blocking=True, scopes=" or ".join(AG_SCOPES)))

    reg_ok, reg_detail = await _preflight_registry(sso_url, cid, csec, tenant, tenant_id,
                                                   client_exists=True)
    checks.append(Check("registry", "App install", "pass" if reg_ok else "fail", reg_detail,
                        blocking=False, scopes="app-engine:apps:install, app-engine:apps:run"))

    set_ok, set_detail = await _preflight_settings(sso_url, cid, csec, tenant, tenant_id,
                                                   client_exists=True)
    checks.append(Check("settings", "Environment settings", "pass" if set_ok else "fail",
                        set_detail, blocking=False,
                        scopes="settings:objects:read, settings:objects:write"))

    bearer_ok, bearer_detail = await _preflight_deploy_bearer(sso_url, cid, csec, account_urn)
    checks.append(Check("deploy_bearer", "Install bearer (account urn)",
                        "pass" if bearer_ok else "fail", bearer_detail, blocking=False,
                        scopes="account-scoped install grant"))

    admin = await _documents_admin_effective(sso_url, cid, csec, tenant, tenant_id)
    checks.append(Check(
        "documents_admin", "Content ownership",
        {"true": "pass", "false": "fail"}.get(admin, "skip"),
        {"true": "the app can adopt documents another user owns",
         "false": ("document:documents:admin is granted but NOT effective — bind an IAM "
                   "policy carrying it to the client's service user AT ENVIRONMENT LEVEL, "
                   "or imports will fork copies and 'delete all trainings' will fail"),
         }.get(admin, "could not be resolved on this tenant — not proven either way"),
        blocking=False, scopes="document:documents:admin"))

    return PreflightReport(client_exists=True, catalog=sorted(catalog),
                           missing_scopes=missing, checks=checks,
                           learner_tier=learner["tier"])

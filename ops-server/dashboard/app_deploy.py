"""SSO-delegated app deploy (Phase 1: OAuth flow + audit).

Lets an org member deploy/undeploy the Enablement App into a given Dynatrace tenant using
**their own** Dynatrace SSO (Authorization Code + PKCE, public client, no secret) — no
per-tenant OAuth client. The delegated token is obtained live, held in memory, used once,
and discarded. We audit user + tenant + action, never the token.

Flow: domain validation → SSO discovery → PKCE → signed state (Redis) → authorize redirect →
callback + token exchange → **deploy/undeploy** → register tenant for content → audit.

Deploy shells `dt-app deploy` with the delegated token as DT_APP_PLATFORM_TOKEN (dt-app
builds/signs/uploads the archive). Undeploy calls the registry DELETE directly. On success we
show the app URL + log "deployed"; on error we show + log it. The token lives only in memory
for the one call and is never logged or persisted.

Needs the registered Orbital public OAuth client (set DEPLOY_CLIENT_ID); until then
/api/deploy/start returns a clear 503.

Spec: dynatrace-app-enablements/docs/orbital-sso-deploy.md
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webhook.config import REDIS_URL
from dashboard.content_service import classify_tenant, register_tenant

log = logging.getLogger("ops-dashboard.deploy")

# Registered Orbital OAuth client (auth-code grant + redirect URI). Set in /home/ops/.env.
# A self-created Dynatrace client is confidential → also set DEPLOY_CLIENT_SECRET (held only
# on Orbital, server-side; never shared with tenants/users). PKCE is still used.
DEPLOY_CLIENT_ID = os.environ.get("DEPLOY_CLIENT_ID", "")
DEPLOY_CLIENT_SECRET = os.environ.get("DEPLOY_CLIENT_SECRET", "")
DEPLOY_REDIRECT_URI = os.environ.get(
    "DEPLOY_REDIRECT_URI",
    "https://autonomous-enablements.whydevslovedynatrace.com/auth/dt-callback",
)
DEPLOY_SCOPES = os.environ.get(
    "DEPLOY_SCOPES",
    "app-engine:apps:install app-engine:apps:run app-engine:apps:delete app-settings:objects:write",
)
APP_ID = "my.dynatrace.enablements"
DEFAULT_SSO = "https://sso.dynatrace.com"
FLOW_TTL = 600  # seconds a started flow stays valid
AUDIT_KEY = "audit:deploy"
# IAM permissions the signed-in user must actually hold (reflected in the token's granted
# scope) for each action. If missing, the deploy would 403 at the registry — we check up
# front and report it clearly instead.
REQUIRED_SCOPES = {
    "deploy": {"app-engine:apps:install", "app-engine:apps:run"},
    "undeploy": {"app-engine:apps:delete"},
}
# Local checkout of the app repo (has node_modules/dt-app) used to build + deploy.
APP_REPO_DIR = os.environ.get("APP_REPO_DIR", "/home/ops/enablement-framework/dynatrace-app-enablements")
DEPLOY_TIMEOUT = int(os.environ.get("DEPLOY_TIMEOUT", "600"))
# Branch the deploy checkout is fast-forwarded to before every build, so a `git push`
# is enough to ship — no manual rsync to the ops checkout. See _sync_repo().
APP_DEPLOY_BRANCH = os.environ.get("APP_DEPLOY_BRANCH", "master")
REPO_SYNC_TIMEOUT = int(os.environ.get("REPO_SYNC_TIMEOUT", "90"))

# COE tenant — the one tenant in the COE account. Orbital holds its client credentials, so a
# deploy to COE needs NO pasted token (auto). Every other tenant requires a token.
COE_TENANT_URL = os.environ.get("COE_TENANT_URL", "https://geu80787.apps.dynatrace.com")
COE_CLIENT_ID = os.environ.get("COE_CLIENT_ID", "")
COE_CLIENT_SECRET = os.environ.get("COE_CLIENT_SECRET", "")
COE_RESOURCE = os.environ.get("COE_RESOURCE", "")  # urn:dtaccount:...

router = APIRouter(tags=["deploy"])
_redis: redis.Redis | None = None


def _pool() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def _require_writer(x_auth_user: str | None) -> str:
    if not x_auth_user:
        raise HTTPException(401, "Sign in (org member) to deploy.")
    return x_auth_user


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def discover_sso(tenant_url: str) -> str:
    """Discover the tenant's SSO origin (HEAD /platform/oauth2/authorization/dynatrace-sso →
    Location origin). Falls back to the default SSO."""
    try:
        u = urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}")
        probe = f"{u.scheme}://{u.netloc}/platform/oauth2/authorization/dynatrace-sso"
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as c:
            r = await c.head(probe)
            loc = r.headers.get("location")
            if 300 <= r.status_code < 400 and loc:
                p = urlparse(loc)
                return f"{p.scheme}://{p.netloc}"
    except Exception as exc:
        log.warning("SSO discovery failed for %s: %s", tenant_url, exc)
    return DEFAULT_SSO


async def _audit(user: str, tenant: str, action: str, result: str, **extra) -> None:
    rec = {"user": user, "tenant": tenant, "action": action, "result": result,
           "ts": datetime.now(timezone.utc).isoformat(), **extra}
    try:
        p = _pool()
        await p.lpush(AUDIT_KEY, json.dumps(rec))
        await p.ltrim(AUDIT_KEY, 0, 499)
    except Exception as exc:  # never let auditing break the flow
        log.warning("audit write failed: %s", exc)
    # token is never part of `rec`
    log.info("DEPLOY-AUDIT %s", {k: v for k, v in rec.items()})


def _client_for(domain: str) -> tuple[str, str]:
    """The OAuth client for a domain class (prod/sprint/dev). Each is a separate SSO realm,
    so each can have its own client: DEPLOY_CLIENT_ID_PROD / _SPRINT / _DEV (+ _SECRET_*).
    Falls back to the global DEPLOY_CLIENT_ID/SECRET when no per-realm client is set."""
    cid = os.environ.get(f"DEPLOY_CLIENT_ID_{domain.upper()}") or DEPLOY_CLIENT_ID
    sec = os.environ.get(f"DEPLOY_CLIENT_SECRET_{domain.upper()}") or DEPLOY_CLIENT_SECRET
    return cid, sec


def _missing_scopes(action: str, granted: str | None) -> list[str]:
    """Required IAM scopes for the action minus what the user's token actually granted."""
    return sorted(REQUIRED_SCOPES.get(action, set()) - set((granted or "").split()))


def _app_url(tenant_url: str) -> str:
    return f"{tenant_url.rstrip('/')}/ui/apps/{APP_ID}"


def _registry_url(tenant_url: str, app_id: str | None = None) -> str:
    base = f"{tenant_url.rstrip('/')}/platform/app-engine/registry/v1/apps"
    return f"{base}/{app_id}" if app_id else base


def _app_version() -> str:
    try:
        cfg = json.loads((Path(APP_REPO_DIR) / "app.config.json").read_text())
        return cfg.get("app", {}).get("version") or cfg.get("version") or "?"
    except Exception:
        return "?"


async def _sync_repo() -> tuple[bool, str]:
    """Fast-forward the deploy checkout to origin/<APP_DEPLOY_BRANCH> before building.

    This makes `git push` the only step needed to ship the app — no manual rsync into the
    ops checkout. Best-effort: on any failure we log and let the deploy proceed with whatever
    is currently checked out (returns (False, reason)).

    `git reset --hard` only rewrites tracked files, so the checkout's untracked/ignored
    node_modules, .env and .dt-app are preserved. Dependency changes (package-lock.json) still
    need a manual `npm ci` in the checkout — surfaced via the returned message.
    """
    if not (Path(APP_REPO_DIR) / ".git").is_dir():
        return False, "not a git checkout"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "HOME": os.environ.get("HOME", "/home/ops")}

    async def _git(*args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", APP_REPO_DIR, *args, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=REPO_SYNC_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "timed out"
        return proc.returncode or 0, out.decode(errors="replace").strip()

    rc, msg = await _git("fetch", "--quiet", "origin", APP_DEPLOY_BRANCH)
    if rc != 0:
        return False, f"fetch failed: {msg[-300:]}"
    # Note whether dependencies changed so the operator knows to `npm ci` if a build fails.
    _, lock_diff = await _git("diff", "--name-only", f"HEAD..origin/{APP_DEPLOY_BRANCH}", "--", "package-lock.json")
    rc, msg = await _git("reset", "--hard", f"origin/{APP_DEPLOY_BRANCH}")
    if rc != 0:
        return False, f"reset failed: {msg[-300:]}"
    _, head = await _git("rev-parse", "--short", "HEAD")
    suffix = " (package-lock changed — run `npm ci` if build fails)" if lock_diff else ""
    return True, f"{APP_DEPLOY_BRANCH}@{head}{suffix}"


async def _run_deploy(token: str, tenant_url: str) -> tuple[int, str]:
    """Shell `dt-app deploy` with the delegated token as DT_APP_PLATFORM_TOKEN (dt-app builds,
    signs and POSTs the archive to the registry — correct by construction). Token is passed via
    the child env only, never logged."""
    binary = Path(APP_REPO_DIR) / "node_modules" / ".bin" / "dt-app"
    if not binary.exists():
        return 127, f"dt-app not found in {APP_REPO_DIR} (is the app repo checked out with node_modules?)"
    env = {**os.environ, "DT_APP_PLATFORM_TOKEN": token, "DT_APP_ENVIRONMENT_URL": tenant_url,
           "DT_APP_DEACTIVATE_SPINNER": "1", "CI": "1",
           # node lives in /usr/local/bin (symlink); ensure it's on PATH for the systemd service
           "PATH": "/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", ""),
           "HOME": os.environ.get("HOME", "/home/ops")}
    proc = await asyncio.create_subprocess_exec(
        str(binary), "deploy", "--non-interactive", cwd=APP_REPO_DIR, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=DEPLOY_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "deploy timed out"
    return proc.returncode or 0, out.decode(errors="replace")[-1500:]


async def _get_installed(token: str, tenant_url: str) -> str | None:
    """Return the installed app version on the tenant, or None if not installed."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(_registry_url(tenant_url, APP_ID), headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            j = r.json()
            return j.get("version") or j.get("appVersion")
        return None  # 404 → not installed
    except Exception as exc:
        log.warning("installed-version check failed for %s: %s", tenant_url, exc)
        return None


async def _deploy_with_status(token: str, tenant_url: str) -> dict:
    """Idempotent deploy: check what's installed; skip if already current, else install/upgrade.
    Returns {status: up-to-date|installed|upgraded|error, from, to, output}."""
    # Pull the latest pushed code into the deploy checkout first, so `_app_version()` and the
    # build below reflect origin/<branch>. Best-effort — a sync failure never blocks deploy.
    synced, sync_msg = await _sync_repo()
    if synced:
        log.info("deploy repo synced: %s", sync_msg)
    else:
        log.warning("deploy repo sync skipped/failed (deploying current checkout): %s", sync_msg)
    installed = await _get_installed(token, tenant_url)
    ours = _app_version()
    if installed and installed == ours:
        return {"status": "up-to-date", "to": ours}
    rc, out = await _run_deploy(token, tenant_url)
    if rc != 0:
        return {"status": "error", "rc": rc, "output": out, "from": installed}
    return {"status": "upgraded" if installed else "installed", "from": installed, "to": _app_version()}


async def _run_undeploy(token: str, tenant_url: str) -> tuple[bool, str]:
    """Uninstall via the registry API directly (no packaging needed)."""
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.delete(_registry_url(tenant_url, APP_ID), headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (200, 202, 204):
            return True, "uninstalled"
        if r.status_code == 404:
            return True, "app was not installed"
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)


def _is_coe(tenant_url: str) -> bool:
    h1 = (urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}").hostname or "").lower()
    h2 = (urlparse(COE_TENANT_URL).hostname or "").lower()
    return bool(h2) and h1 == h2


async def _mint_coe_token(action: str) -> str | None:
    """Mint a bearer for the COE tenant from Orbital's COE client credentials (server-side).
    Scope by action so we never request a scope the client lacks."""
    if not (COE_CLIENT_ID and COE_CLIENT_SECRET):
        return None
    scope = "app-engine:apps:delete" if action == "undeploy" else "app-engine:apps:install app-engine:apps:run"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://sso.dynatrace.com/sso/oauth2/token", data={
                "grant_type": "client_credentials", "client_id": COE_CLIENT_ID,
                "client_secret": COE_CLIENT_SECRET, "resource": COE_RESOURCE, "scope": scope,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code == 200:
            return r.json().get("access_token")
        log.warning("COE token mint HTTP %s", r.status_code)
    except Exception as exc:
        log.warning("COE token mint failed: %s", exc)
    return None


OUTBOUND_SCHEMA = "builtin:dt-javascript-runtime.allowed-outbound-connections"
# Hosts the app's functions must reach for content delivery + manual GitHub imports.
OUTBOUND_HOSTS = [
    "autonomous-enablements.whydevslovedynatrace.com",
    "raw.githubusercontent.com",
    "api.github.com",
]


async def _ensure_outbound_allowlist(token: str, tenant_url: str) -> str:
    """If the tenant enforces a JS-runtime outbound allowlist (sprint/dev do, prod usually
    doesn't), add the content-delivery hosts so the app's functions can reach Orbital + GitHub.
    Only ever adds hosts to an existing enforced list — never creates or tightens a restriction.
    Best-effort; needs settings:objects:read+write on the token."""
    base = tenant_url.rstrip("/") + "/platform/classic/environment-api/v2/settings/objects"
    h = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(base, headers=h, params={
                "schemaIds": OUTBOUND_SCHEMA, "scopes": "environment", "fields": "objectId,value"})
            if r.status_code == 403:
                return "skipped (token lacks settings:objects:read/write)"
            if r.status_code != 200:
                return f"skipped (settings read HTTP {r.status_code})"
            items = r.json().get("items", [])
            if not items:
                # No settings object. Sprint/dev default to DENY-ALL (enforced, empty list)
                # so the app's functions are blocked until we CREATE the object with our
                # hosts. Prod with no object means outbound is open → never create one
                # there (that would tighten prod).
                _, domain = classify_tenant(tenant_url)
                if domain not in ("sprint", "dev"):
                    return "no allowlist object (prod — outbound open)"
                cr = await c.post(base, headers={**h, "Content-Type": "application/json"}, json=[{
                    "schemaId": OUTBOUND_SCHEMA, "scope": "environment",
                    "value": {"allowedOutboundConnections": {"enforced": True, "hostList": list(OUTBOUND_HOSTS)}},
                }])
                if cr.status_code in (200, 201):
                    return f"created outbound allowlist with {len(OUTBOUND_HOSTS)} host(s)"
                return f"allowlist create failed (HTTP {cr.status_code}: {cr.text[:120]})"
            obj = items[0]
            aoc = (obj.get("value") or {}).get("allowedOutboundConnections", {})
            if not aoc.get("enforced"):
                return "outbound not enforced (open)"
            hosts = list(aoc.get("hostList", []))
            missing = [x for x in OUTBOUND_HOSTS if x not in hosts]
            if not missing:
                return "allowlist already complete"
            hosts.extend(missing)
            pr = await c.put(f"{base}/{obj['objectId']}", headers={**h, "Content-Type": "application/json"},
                             json={"value": {"allowedOutboundConnections": {"enforced": True, "hostList": hosts}}})
            if pr.status_code in (200, 201, 204):
                return f"added {len(missing)} host(s) to the outbound allowlist"
            return f"allowlist update failed (HTTP {pr.status_code})"
    except Exception as exc:
        log.warning("outbound allowlist for %s: %s", tenant_url, exc)
        return f"allowlist error: {exc}"


async def _register_in_content_service(user: str, tenant_url: str) -> dict | None:
    """Best-effort: add the tenant to the delivery table so its content can be managed."""
    try:
        return await register_tenant({"tenant": tenant_url}, x_auth_user=user)
    except Exception as exc:
        log.warning("register-tenant failed for %s: %s", tenant_url, exc)
        return None


@router.get("/api/deploy/start")
async def deploy_start(tenant: str, action: str = "deploy", x_auth_user: str | None = Header(default=None)):
    """Begin the SSO flow for a tenant. Validates the Dynatrace domain, then 302s to the
    Dynatrace authorize endpoint (PKCE). nginx gates this to org members (X-Auth-User)."""
    user = _require_writer(x_auth_user)
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    tenant_id, domain = classify_tenant(tenant)  # 403 if not a Dynatrace domain
    client_id, _ = _client_for(domain)
    if not client_id:
        raise HTTPException(503, f"Deploy not configured for the {domain} realm: register an OAuth "
                                 f"client there and set DEPLOY_CLIENT_ID_{domain.upper()} (or DEPLOY_CLIENT_ID).")

    sso = await discover_sso(tenant)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    await _pool().setex(
        f"deploy:flow:{state}", FLOW_TTL,
        json.dumps({"tenant": tenant, "tenant_id": tenant_id, "domain": domain, "client_id": client_id,
                    "verifier": verifier, "user": user, "action": action, "sso": sso}),
    )
    authorize = f"{sso}/oauth2/authorize?" + urlencode({
        "client_id": client_id,
        "redirect_uri": DEPLOY_REDIRECT_URI,
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": DEPLOY_SCOPES,
        "state": state,
    })
    await _audit(user, tenant_id, action, "auth-started", domain=domain)
    return RedirectResponse(authorize, status_code=302)


@router.get("/auth/dt-callback", response_class=HTMLResponse)
async def deploy_callback(request: Request):
    """Dynatrace SSO redirect target. Validates state, exchanges the code for the user's
    delegated token, audits, and reports. (Phase 2 will run the registry install/uninstall
    here.) Public route — auth is carried by the OAuth state, not a GitHub session."""
    params = request.query_params
    err = params.get("error")
    state = params.get("state") or ""
    code = params.get("code") or ""

    raw = await _pool().get(f"deploy:flow:{state}") if state else None
    if not raw:
        return HTMLResponse(_page("Invalid or expired deploy session.", ok=False), status_code=400)
    flow = json.loads(raw)
    await _pool().delete(f"deploy:flow:{state}")  # one-time use
    user, tenant_id, action = flow["user"], flow["tenant_id"], flow["action"]

    if err:
        await _audit(user, tenant_id, action, "auth-error", error=err)
        return HTMLResponse(_page(f"Sign-in failed: {err}", ok=False), status_code=400)

    # Exchange the code (public client + PKCE, no secret) for the delegated token.
    try:
        # Use the same per-realm client the flow started with; re-resolve its secret from env.
        client_id = flow.get("client_id") or DEPLOY_CLIENT_ID
        _, client_secret = _client_for(flow.get("domain", ""))
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": DEPLOY_REDIRECT_URI,
            "code_verifier": flow["verifier"],
        }
        if client_secret:  # confidential client (self-created) — secret stays server-side
            form["client_secret"] = client_secret
        async with httpx.AsyncClient(timeout=15) as c:
            tok = await c.post(f"{flow['sso']}/sso/oauth2/token", data=form,
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tok.status_code != 200:
            await _audit(user, tenant_id, action, "token-error", status=tok.status_code)
            return HTMLResponse(_page(f"Token exchange failed (HTTP {tok.status_code}).", ok=False), status_code=502)
        payload_t = tok.json()
        token = payload_t.get("access_token", "")  # held in memory only, never logged/stored
        granted = payload_t.get("scope", "")
    except Exception as exc:
        await _audit(user, tenant_id, action, "token-error", message=str(exc))
        return HTMLResponse(_page(f"Token exchange error: {exc}", ok=False), status_code=502)

    # Validate the signed-in user actually holds the IAM permissions for this action.
    # SSO only grants scopes the user is entitled to, so a missing scope ⇒ no permission.
    missing = _missing_scopes(action, granted)
    if missing:
        del token
        await _audit(user, tenant_id, action, "insufficient-permissions", missing=missing)
        return HTMLResponse(_page(
            f"<b>{user}</b> lacks permission to {action} apps on <b>{tenant_id}</b>.<br><br>"
            f"Missing IAM permission(s): <b>{', '.join(missing)}</b>.<br>"
            f"Ask a tenant administrator to grant them, then try again.", ok=False), status_code=403)

    tenant_url = flow["tenant"]
    app_url = _app_url(tenant_url)

    if action == "undeploy":
        ok, msg = await _run_undeploy(token, tenant_url)
        del token  # discard the credential
        await _audit(user, tenant_id, "undeploy", "undeployed" if ok else "undeploy-error", detail=msg)
        return HTMLResponse(_page(
            f"App <b>{APP_ID}</b> undeployed from <b>{tenant_id}</b>." if ok
            else f"Undeploy failed for <b>{tenant_id}</b>: {msg}", ok=ok),
            status_code=200 if ok else 502)

    # deploy — idempotent: skip if up-to-date, else install/upgrade
    res = await _deploy_with_status(token, tenant_url)
    del token  # discard the credential before doing anything else
    if res["status"] == "error":
        await _audit(user, tenant_id, "deploy", "deploy-error", rc=res.get("rc"))
        return HTMLResponse(_page(
            f"Deploy to <b>{tenant_id}</b> failed (exit {res.get('rc')}).<br><br>"
            f"<pre style='white-space:pre-wrap;color:#f0c674'>{res.get('output','')}</pre>", ok=False), status_code=502)

    reg = await _register_in_content_service(user, tenant_url)
    profile = (reg or {}).get("profile")
    await _audit(user, tenant_id, "deploy", res["status"],
                 **{k: res[k] for k in ("from", "to") if res.get(k)}, url=app_url, profile=profile)
    if res["status"] == "up-to-date":
        head = f"App already up-to-date on <b>{tenant_id}</b> (v{res.get('to')}) — nothing to do."
    elif res["status"] == "upgraded":
        head = f"App upgraded on <b>{tenant_id}</b>: v{res.get('from')} → v{res.get('to')}."
    else:
        head = f"App installed on <b>{tenant_id}</b> (v{res.get('to')})."
    return HTMLResponse(_page(
        f"{head}<br><br>Open: <a href='{app_url}'>{app_url}</a><br>"
        + (f"Content profile: <b>{profile}</b> — open the app and Refresh to load it."
           if profile else "Tenant registered for content delivery."), ok=True))


@router.post("/api/deploy/token")
async def deploy_with_token(body: dict, x_auth_user: str | None = Header(default=None)):
    """Override path for ANY tenant (customer / prospect / free trial / cross-account): the
    caller supplies a platform token created IN the target tenant (scopes apps:install/run/
    delete). That credential carries the target account's authority, so no SSO/account binding
    is needed. The token is used once and discarded — never logged or persisted. Writer-gated."""
    user = _require_writer(x_auth_user)
    action = body.get("action", "deploy")
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    tenant = (body.get("tenant") or "").strip()
    token = (body.get("token") or "").strip()
    tenant_id, domain = classify_tenant(tenant)  # 403 if not a Dynatrace domain
    coe_auto = False
    if not token:
        # COE is the one tenant Orbital can deploy on its own (it holds COE's credentials).
        if _is_coe(tenant):
            token = await _mint_coe_token(action) or ""
            coe_auto = True
            if not token:
                raise HTTPException(503, "COE auto-deploy not configured (set COE_CLIENT_ID/SECRET/RESOURCE).")
        else:
            raise HTTPException(400, "A valid platform token is required for this tenant. "
                                     "Auto-deploy (no token) is only available for the COE tenant.")

    via = "coe-auto" if coe_auto else "token"
    if action == "undeploy":
        ok, msg = await _run_undeploy(token, tenant)
        del token
        await _audit(user, tenant_id, "undeploy", "undeployed" if ok else "undeploy-error", via=via, detail=msg)
        if not ok:
            raise HTTPException(502, f"Undeploy failed: {msg}")
        return {"ok": True, "tenant": tenant_id, "action": "undeploy"}

    res = await _deploy_with_status(token, tenant)
    allowlist = ""
    if res["status"] != "error":
        allowlist = await _ensure_outbound_allowlist(token, tenant)  # use token before discarding
    del token
    if res["status"] == "error":
        await _audit(user, tenant_id, "deploy", "deploy-error", via=via, rc=res.get("rc"))
        raise HTTPException(502, f"Deploy failed (exit {res.get('rc')}): {res.get('output','')}")
    reg = await _register_in_content_service(user, tenant)
    profile = (reg or {}).get("profile")
    url = _app_url(tenant)
    await _audit(user, tenant_id, "deploy", res["status"], via=via,
                 **{k: res[k] for k in ("from", "to") if res.get(k)}, url=url, profile=profile, allowlist=allowlist)
    return {"ok": True, "tenant": tenant_id, "status": res["status"], "from": res.get("from"),
            "version": res.get("to"), "url": url, "profile": profile, "allowlist": allowlist}


@router.get("/api/deploy/audit")
async def deploy_audit(limit: int = 50, x_auth_user: str | None = Header(default=None)):
    _require_writer(x_auth_user)
    rows = await _pool().lrange(AUDIT_KEY, 0, max(0, min(limit, 500) - 1))
    return {"audit": [json.loads(r) for r in rows]}


def _page(msg: str, ok: bool) -> str:
    color = "#2da44e" if ok else "#f85149"
    return (f"<!doctype html><html><head><meta charset=utf-8><title>Deploy</title></head>"
            f"<body style='font-family:system-ui;background:#0d1117;color:#e6edf3;padding:40px'>"
            f"<h2 style='color:{color}'>{'✓' if ok else '✗'} App deploy</h2><p>{msg}</p>"
            f"<p><a style='color:#9d9dff' href='/deploy'>← back</a></p></body></html>")

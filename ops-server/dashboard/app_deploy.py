"""SSO-delegated app deploy (Phase 1: OAuth flow + audit).

Lets an org member deploy/undeploy the Enablement App into a given Dynatrace tenant using
**their own** Dynatrace SSO (Authorization Code + PKCE, public client, no secret) — no
per-tenant OAuth client. The delegated token is obtained live, held in memory, used once,
and discarded. We audit user + tenant + action, never the token.

Phase 1 implements: domain validation, SSO discovery, PKCE, signed state (in Redis), the
authorize redirect, the callback + token exchange, and the audit log. The actual
registry install/uninstall (POST/DELETE …/app-engine/registry/v1/apps) is Phase 2 — the
callback currently obtains the token, audits, and reports "authenticated, ready to deploy".

Spec: dynatrace-app-enablements/docs/orbital-sso-deploy.md
"""

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webhook.config import REDIS_URL
from dashboard.content_service import classify_tenant

log = logging.getLogger("ops-dashboard.deploy")

# Registered Orbital public OAuth client (dt0s12-type, PKCE, no secret). Set in /home/ops/.env.
DEPLOY_CLIENT_ID = os.environ.get("DEPLOY_CLIENT_ID", "")
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


@router.get("/api/deploy/start")
async def deploy_start(tenant: str, action: str = "deploy", x_auth_user: str | None = Header(default=None)):
    """Begin the SSO flow for a tenant. Validates the Dynatrace domain, then 302s to the
    Dynatrace authorize endpoint (PKCE). nginx gates this to org members (X-Auth-User)."""
    user = _require_writer(x_auth_user)
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    tenant_id, domain = classify_tenant(tenant)  # 403 if not a Dynatrace domain
    if not DEPLOY_CLIENT_ID:
        raise HTTPException(503, "Deploy not configured: register the Orbital OAuth client and set DEPLOY_CLIENT_ID.")

    sso = await discover_sso(tenant)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    await _pool().setex(
        f"deploy:flow:{state}", FLOW_TTL,
        json.dumps({"tenant": tenant, "tenant_id": tenant_id, "domain": domain,
                    "verifier": verifier, "user": user, "action": action, "sso": sso}),
    )
    authorize = f"{sso}/oauth2/authorize?" + urlencode({
        "client_id": DEPLOY_CLIENT_ID,
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
        async with httpx.AsyncClient(timeout=15) as c:
            tok = await c.post(f"{flow['sso']}/sso/oauth2/token", data={
                "grant_type": "authorization_code",
                "client_id": DEPLOY_CLIENT_ID,
                "code": code,
                "redirect_uri": DEPLOY_REDIRECT_URI,
                "code_verifier": flow["verifier"],
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if tok.status_code != 200:
            await _audit(user, tenant_id, action, "token-error", status=tok.status_code)
            return HTMLResponse(_page(f"Token exchange failed (HTTP {tok.status_code}).", ok=False), status_code=502)
        token = tok.json().get("access_token", "")  # held in memory only, never logged/stored
    except Exception as exc:
        await _audit(user, tenant_id, action, "token-error", message=str(exc))
        return HTMLResponse(_page(f"Token exchange error: {exc}", ok=False), status_code=502)

    # ── Phase 2 goes here: POST/DELETE {tenant}/platform/app-engine/registry/v1/apps with
    #    `Authorization: Bearer {token}`, then register-tenant + show the app URL. ──
    del token  # discard the credential (Phase 1 stops after authentication)
    await _audit(user, tenant_id, action, "authenticated")
    app_url = f"{flow['tenant'].rstrip('/')}/ui/apps/{APP_ID}"
    return HTMLResponse(_page(
        f"Authenticated as <b>{user}</b> for <b>{tenant_id}</b> ({action}). "
        f"Ready to {action} — registry call lands in Phase 2.<br><br>"
        f"App URL (after deploy): <a href='{app_url}'>{app_url}</a>", ok=True))


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

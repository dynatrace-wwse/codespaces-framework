"""Per-user GitHub OAuth — connect a learner's own GitHub account to Orbital.

A learner == one GitHub identity. The Dynatrace app drives this flow so a user can
authorize Orbital to act as them on GitHub (scope ``codespace``), letting Orbital
create a Codespace **in the user's own account** (owned + billed to the user) and
inject the Dynatrace tokens as the user's Codespaces secrets. Orbital never owns the
Codespace; it only holds a short-lived user access token to drive the `gh` CLs/REST.

Flow:
  GET  /auth/github/start?dtUser=&return=  → 302 to GitHub authorize (signed `state`)
  GET  /auth/github/callback?code=&state=  → exchange code → store ENCRYPTED user token
                                             in Redis `gh:token:{dtUser}` (TTL); postMessage close
  GET  /auth/github/status?dtUser=         → {connected, login?}
  POST /auth/github/disconnect?dtUser=     → delete token (best-effort revoke grant)

Token storage: the user access token is encrypted at rest with Fernet
(``cryptography``) using the key in ``GH_OAUTH_ENC_KEY`` and given a TTL
(``GH_TOKEN_TTL``, default 8h). ``get_user_token`` / ``set_user_token`` are importable
by ``codespace_service.py``.

NOTE: this module needs the ``cryptography`` package (``pip install cryptography``).
It is imported lazily so a missing install never crashes app.py at import — the
Fernet helpers raise a clear runtime error instead. All env vars are read lazily
inside handlers/helpers, so the router is always safe to import.

Env:
  GITHUB_OAUTH_CLIENT_ID      — the OAuth App client id (per-user, scope=codespace)
  GITHUB_OAUTH_CLIENT_SECRET  — the OAuth App client secret
  ORBITAL_PUBLIC_URL          — public base url (default autonomous-enablements…)
  GH_OAUTH_ENC_KEY            — Fernet key (urlsafe base64, 32 bytes) for token encryption
  GH_OAUTH_STATE_SECRET       — HMAC secret for signing the OAuth `state` (falls back
                                to GH_OAUTH_ENC_KEY if unset)
  GH_TOKEN_TTL                — seconds the stored token lives (default 28800 = 8h)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urlencode

import httpx
import redis.asyncio as redis
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from webhook.config import REDIS_URL

log = logging.getLogger("ops-dashboard.github-oauth")

GH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
GH_API = "https://api.github.com"
DEFAULT_ORBITAL_URL = "https://autonomous-enablements.whydevslovedynatrace.com"
DEFAULT_TOKEN_TTL = 28800  # 8h
STATE_TTL = 600  # signed state is only valid for 10 minutes

router = APIRouter(tags=["github-oauth"])
_redis: redis.Redis | None = None


def _pool() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


# ── env (lazy — read inside handlers, never at import) ───────────────────────

def _client_id() -> str:
    return os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")


def _public_url() -> str:
    return os.environ.get("ORBITAL_PUBLIC_URL", DEFAULT_ORBITAL_URL).rstrip("/")


def _token_ttl() -> int:
    try:
        return int(os.environ.get("GH_TOKEN_TTL", "") or DEFAULT_TOKEN_TTL)
    except ValueError:
        return DEFAULT_TOKEN_TTL


def _state_secret() -> bytes:
    sec = os.environ.get("GH_OAUTH_STATE_SECRET") or os.environ.get("GH_OAUTH_ENC_KEY") or ""
    if not sec:
        raise HTTPException(503, "GitHub OAuth not configured (set GH_OAUTH_STATE_SECRET or GH_OAUTH_ENC_KEY).")
    return sec.encode()


# ── encryption (lazy import of cryptography so app.py import never crashes) ──

def _fernet():
    """Return a Fernet instance from GH_OAUTH_ENC_KEY, or raise a clear 503.

    Imported lazily: if `cryptography` is not installed, callers get a runtime
    HTTPException rather than an ImportError at module import (which would break
    every other router mounted in app.py)."""
    key = os.environ.get("GH_OAUTH_ENC_KEY", "")
    if not key:
        raise HTTPException(503, "GitHub OAuth token encryption not configured (set GH_OAUTH_ENC_KEY).")
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on install
        raise HTTPException(
            503, "cryptography package not installed on Orbital (run `pip install cryptography`)."
        ) from exc
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:
        raise HTTPException(503, f"GH_OAUTH_ENC_KEY is not a valid Fernet key: {exc}") from exc


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


# ── signed state (HMAC + base64; no extra deps) ──────────────────────────────

def _sign_state(dt_user: str, return_url: str) -> str:
    payload = {"u": dt_user, "r": return_url, "t": int(time.time())}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    sig = hmac.new(_state_secret(), body, hashlib.sha256).digest()
    sig_b = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return f"{body.decode()}.{sig_b.decode()}"


def _verify_state(state: str) -> dict:
    try:
        body_s, sig_s = state.split(".", 1)
    except ValueError:
        raise HTTPException(400, "Malformed OAuth state.")
    body = body_s.encode()
    want = hmac.new(_state_secret(), body, hashlib.sha256).digest()
    got = base64.urlsafe_b64decode(sig_s + "=" * (-len(sig_s) % 4))
    if not hmac.compare_digest(want, got):
        raise HTTPException(400, "Invalid OAuth state signature.")
    payload = json.loads(base64.urlsafe_b64decode(body_s + "=" * (-len(body_s) % 4)))
    if int(time.time()) - int(payload.get("t", 0)) > STATE_TTL:
        raise HTTPException(400, "OAuth state expired — start the connection again.")
    return payload


# ── token store helpers (importable by codespace_service.py) ─────────────────

async def set_user_token(r: redis.Redis, dt_user: str, token: str, ttl: int | None = None) -> None:
    """Encrypt + store the user's GitHub access token at gh:token:{dtUser} with a TTL."""
    await r.setex(f"gh:token:{dt_user}", ttl or _token_ttl(), _encrypt(token))


async def get_user_token(r: redis.Redis, dt_user: str) -> str | None:
    """Return the decrypted GitHub access token for a learner, or None if absent/undecryptable."""
    if not dt_user:
        return None
    enc = await r.get(f"gh:token:{dt_user}")
    if not enc:
        return None
    try:
        return _decrypt(enc)
    except Exception as exc:  # corrupt / key-rotated — treat as not connected
        log.warning("could not decrypt gh:token for %s: %s", dt_user, exc)
        return None


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/auth/github/start")
async def github_start(dtUser: str, request: Request):
    """Begin the per-user GitHub OAuth flow. 302s to GitHub's authorize endpoint with a
    signed `state` carrying the dtUser + post-connect return URL. `scope=codespace` is the
    minimum to create Codespaces + set the user's Codespaces secrets."""
    if not dtUser:
        raise HTTPException(400, "dtUser is required.")
    client_id = _client_id()
    if not client_id:
        raise HTTPException(503, "GitHub OAuth not configured (set GITHUB_OAUTH_CLIENT_ID).")
    return_url = request.query_params.get("return", "")
    state = _sign_state(dtUser, return_url)
    authorize = f"{GH_AUTHORIZE_URL}?" + urlencode({
        "client_id": client_id,
        "scope": "codespace",
        "redirect_uri": f"{_public_url()}/auth/github/callback",
        "state": state,
    })
    return RedirectResponse(authorize, status_code=302)


@router.get("/auth/github/callback", response_class=HTMLResponse)
async def github_callback(request: Request):
    """GitHub redirect target. Verifies the signed state, exchanges the code for the user's
    access token, stores it ENCRYPTED in Redis (TTL), and closes the popup via postMessage."""
    params = request.query_params
    err = params.get("error")
    code = params.get("code") or ""
    state = params.get("state") or ""
    if err:
        return HTMLResponse(_close_page(f"GitHub sign-in failed: {err}", ok=False), status_code=400)
    if not code or not state:
        return HTMLResponse(_close_page("Missing code or state.", ok=False), status_code=400)

    flow = _verify_state(state)  # raises 400 on tamper/expiry
    dt_user = flow["u"]

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            tok = await c.post(GH_TOKEN_URL, data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "code": code,
                "redirect_uri": f"{_public_url()}/auth/github/callback",
            }, headers={"Accept": "application/json"})
    except Exception as exc:
        log.warning("gh token exchange error for %s: %s", dt_user, exc)
        return HTMLResponse(_close_page(f"Token exchange error: {exc}", ok=False), status_code=502)

    if tok.status_code != 200:
        return HTMLResponse(_close_page(f"Token exchange failed (HTTP {tok.status_code}).", ok=False), status_code=502)
    data = tok.json()
    access_token = data.get("access_token", "")
    if not access_token:
        # GitHub returns 200 with {error: ...} on bad/expired code
        return HTMLResponse(_close_page(f"GitHub did not return a token: {data.get('error_description') or data.get('error') or 'unknown'}", ok=False), status_code=502)

    try:
        await set_user_token(_pool(), dt_user, access_token)
    except HTTPException as exc:
        return HTMLResponse(_close_page(f"Could not store token: {exc.detail}", ok=False), status_code=exc.status_code)
    log.info("GitHub connected for dtUser=%s", dt_user)
    return HTMLResponse(_close_page("GitHub connected. You can close this window.", ok=True))


@router.get("/auth/github/status")
async def github_status(dtUser: str):
    """Whether a learner has a live GitHub connection. If a token is present, confirm it by
    calling GET /user and returning the GitHub login."""
    if not dtUser:
        raise HTTPException(400, "dtUser is required.")
    token = await get_user_token(_pool(), dtUser)
    if not token:
        return {"connected": False}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{GH_API}/user", headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            })
        if r.status_code == 200:
            return {"connected": True, "login": r.json().get("login")}
        # Token present but rejected → treat as not connected (likely expired/revoked).
        log.info("gh /user returned %s for %s — token stale", r.status_code, dtUser)
        return {"connected": False}
    except Exception as exc:
        log.warning("gh /user check failed for %s: %s", dtUser, exc)
        # Token exists; network blip — report connected without a login.
        return {"connected": True}


@router.post("/auth/github/disconnect")
async def github_disconnect(dtUser: str):
    """Forget a learner's GitHub connection (delete the stored token). Best-effort: also try
    to revoke the OAuth grant on GitHub so the access token is invalidated server-side."""
    if not dtUser:
        raise HTTPException(400, "dtUser is required.")
    token = await get_user_token(_pool(), dtUser)
    await _pool().delete(f"gh:token:{dtUser}")
    revoked = False
    if token and _client_id() and _client_secret():
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                # Delete the app authorization (revokes the user's grant + token).
                r = await c.request(
                    "DELETE",
                    f"{GH_API}/applications/{_client_id()}/grant",
                    auth=(_client_id(), _client_secret()),
                    headers={"Accept": "application/vnd.github+json"},
                    json={"access_token": token},
                )
            revoked = r.status_code in (204, 404)
        except Exception as exc:
            log.warning("gh grant revoke failed for %s: %s", dtUser, exc)
    return {"disconnected": True, "revoked": revoked}


def _close_page(msg: str, ok: bool) -> str:
    """Tiny HTML page that notifies the opener (the DT app) and closes the popup."""
    color = "#2da44e" if ok else "#f85149"
    icon = "✓" if ok else "✗"
    ok_js = "true" if ok else "false"
    msg_js = json.dumps(msg)
    payload = "{type:'github-connected',ok:" + ok_js + ",message:" + msg_js + "}"
    script = (
        "<script>"
        "try{if(window.opener){window.opener.postMessage(" + payload + ",'*');}}catch(e){}"
        "setTimeout(function(){window.close();},800);"
        "</script>"
    )
    return (
        "<!doctype html><html><head><meta charset=utf-8><title>GitHub</title></head>"
        "<body style='font-family:system-ui;background:#0d1117;color:#e6edf3;padding:40px'>"
        f"<h2 style='color:{color}'>{icon} GitHub</h2>"
        f"<p>{msg}</p>"
        + script +
        "</body></html>"
    )

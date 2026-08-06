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
from dashboard.github_oauth import _decrypt, _encrypt  # Fernet (GH_OAUTH_ENC_KEY) — COE token + stashed deploy token
from dashboard import tenant_registry  # durable WHO-deployed-WHERE attribution (EPIC-002 §9)

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
    # apps:* to install/run/delete; app-settings + CLASSIC settings:objects:* so the
    # post-install steps can write the outbound-allowlist and remote-grail settings via
    # the classic settings API. Without classic settings:objects:write those steps 403
    # and silently skip (cross-tenant forwarding wouldn't get configured).
    "app-engine:apps:install app-engine:apps:run app-engine:apps:delete "
    "app-settings:objects:write settings:objects:read settings:objects:write",
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
# Pin public deploys to an exact ref (tag or commit) instead of the branch tip.
# Empty = follow origin/<APP_DEPLOY_BRANCH>, the historical behaviour.
#
# "Update now" in the app is public and tokenless, so an unpinned Orbital ships
# whatever last landed on master to every tenant that clicks it — including a
# build that has not been through QA. Setting this to a released tag decouples
# "merged" from "publicly deployable"; move it when a version is signed off.
APP_DEPLOY_REF = os.environ.get("APP_DEPLOY_REF", "").strip()
REPO_SYNC_TIMEOUT = int(os.environ.get("REPO_SYNC_TIMEOUT", "90"))


def deploy_ref() -> str:
    """The git ref both the version check and the build resolve to."""
    return APP_DEPLOY_REF or f"origin/{APP_DEPLOY_BRANCH}"


async def _fetch_deploy_ref(git) -> tuple[int, str]:
    """Fetch whatever `deploy_ref()` names. A plain branch fetch does not bring
    tags down, so a pinned ref additionally needs --tags to be resolvable."""
    if APP_DEPLOY_REF:
        return await git("fetch", "--quiet", "--tags", "origin", APP_DEPLOY_BRANCH)
    return await git("fetch", "--quiet", "origin", APP_DEPLOY_BRANCH)

# Auto-deploy tenants — tenants whose client credentials Orbital holds, so a deploy to them
# needs NO pasted token (auto). Every other tenant requires a token. Each is one env group:
# <PREFIX>_TENANT_URL / _CLIENT_ID / _CLIENT_SECRET / _RESOURCE (urn:dtaccount:...).
#   COE — the COE account tenant (geu80787, vanity alias wwse).
#   SRO — the SRO (QA) tenant; same wiring as COE.
def _env_first(*names: str, default: str = "") -> str:
    """First non-empty value among several env names.

    The SRO deploy client was configured as SRO_OAUTH_CLIENT_ID/SECRET and
    SRO_ACCOUNT_URN but read here as SRO_CLIENT_ID/SECRET/RESOURCE. Nothing
    errored: the mint simply returned None every time and every SRO deploy fell
    through to the platform-token branch, so the OAuth path we ask tenant admins
    to use was the one path we never exercised ourselves. Accepting both
    spellings costs nothing and removes a failure that is invisible until
    someone goes looking.
    """
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


COE_TENANT_URL = _env_first("COE_TENANT_URL", default="https://geu80787.apps.dynatrace.com")
COE_CLIENT_ID = _env_first("COE_CLIENT_ID", "COE_OAUTH_CLIENT_ID")
COE_CLIENT_SECRET = _env_first("COE_CLIENT_SECRET", "COE_OAUTH_CLIENT_SECRET")
COE_RESOURCE = _env_first("COE_RESOURCE", "COE_ACCOUNT_URN")  # urn:dtaccount:...
SRO_TENANT_URL = _env_first("SRO_TENANT_URL", default="https://sro97894.apps.dynatrace.com")
SRO_CLIENT_ID = _env_first("SRO_CLIENT_ID", "SRO_OAUTH_CLIENT_ID")
SRO_CLIENT_SECRET = _env_first("SRO_CLIENT_SECRET", "SRO_OAUTH_CLIENT_SECRET")
SRO_RESOURCE = _env_first("SRO_RESOURCE", "SRO_ACCOUNT_URN")  # urn:dtaccount:...
# Legacy fallback for SRO only: a long-lived platform token (dt0s16…) created in the
# tenant with apps:install/run/delete, used directly as the bearer. It predates the
# SRO OAuth client and stays as a safety net — minting takes precedence. Prefer the
# OAuth client: it is the path tenant admins are given, so it is the path that must work.
SRO_PLATFORM_TOKEN = os.environ.get("SRO_PLATFORM_TOKEN", "")

# Sprint (ydi9582h) lives on the labs domain with its own SSO host, so unlike COE/SRO
# it cannot use the default sso.dynatrace.com. Its account client is the same one used
# for token minting there — it already carries apps:install/run.
SPRINT_TENANT_URL = _env_first(
    "SPRINT_TENANT_URL", default="https://ydi9582h.sprint.apps.dynatracelabs.com")
SPRINT_CLIENT_ID = _env_first("SPRINT_CLIENT_ID", "MINT_CLIENT_ID_SPRINT")
SPRINT_CLIENT_SECRET = _env_first("SPRINT_CLIENT_SECRET", "MINT_CLIENT_SECRET_SPRINT")
SPRINT_RESOURCE = _env_first("SPRINT_RESOURCE", "MINT_RESOURCE_SPRINT")
SPRINT_SSO_URL = _env_first(
    "SPRINT_SSO_URL", "MINT_SSO_SPRINT",
    default="https://sso-sprint.dynatracelabs.com/sso/oauth2/token")

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


# What a deploy credential must be able to do, and what breaks when it cannot.
# Order matters: this is the order they are reported in.
CAPABILITY_COST = {
    "registry": "install or upgrade the app at all",
    "settings_read": "read the JS-runtime outbound allowlist",
    "settings_write": "enable cross-tenant telemetry forwarding and fix the outbound allowlist",
    "app_settings": "seed the Orbital bearer — without it the app installs and then "
                    "401s on every environment action",
}
CAPABILITY_SCOPE = {
    "registry": "app-engine:apps:install, app-engine:apps:run",
    "settings_read": "settings:objects:read",
    "settings_write": "settings:objects:write",
    "app_settings": "app-settings:objects:write",
}
# Capabilities an account OAuth client can actually be granted, and therefore the
# only ones it is fair to refuse a deploy over.
#
# app-settings:objects:write is deliberately NOT here. App-settings permissions are
# declared by an app and held by the app; they are not offered in the OAuth client
# scope catalog, so no admin can grant it however carefully they follow the
# instructions. The audit bears that out — across every deploy this server has ever
# run, that write was skipped 7 times and succeeded 0. Blocking on an ungrantable
# scope would make deployment impossible rather than safe.
#
# The consequence it guards against is still real, so it is reported as a warning
# with the one manual step that fixes it (see _scope_warnings).
BLOCKING_CAPABILITIES = ("registry", "settings_read", "settings_write")


async def probe_capabilities(token: str, tenant_url: str) -> dict[str, bool]:
    """What this credential can actually do on this tenant, measured not assumed.

    Platform tokens carry no introspectable scope claim — there is no working
    introspection endpoint on the tenant — and even an OAuth `scope` response only
    describes what was granted, not what the tenant will honour. So each capability
    is probed with the cheapest harmless call that exercises the same permission the
    deploy will need:

      registry       GET the app in the registry
      settings_read  GET the outbound-allowlist objects
      settings_write POST them back with validateOnly=true — validates and returns,
                     creating nothing
      app_settings   GET the app-settings object (same permission as the write)

    A 403 is the only "no". Any other failure is treated as "yes" so a transient
    blip or an unfamiliar status can never silently block a deploy: the deploy
    itself remains the authority, this is a pre-flight.
    """
    base = tenant_url.rstrip("/")
    settings = f"{base}/platform/classic/environment-api/v2/settings/objects"
    h = {"Authorization": f"Bearer {token}"}
    caps = dict.fromkeys(CAPABILITY_COST, True)
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_registry_url(tenant_url, APP_ID), headers=h)
            caps["registry"] = r.status_code != 403

            r = await c.get(settings, headers=h, params={
                "schemaIds": OUTBOUND_SCHEMA, "scopes": "environment", "fields": "objectId"})
            caps["settings_read"] = r.status_code != 403

            r = await c.post(f"{settings}?validateOnly=true",
                             headers={**h, "Content-Type": "application/json"}, json=[{
                                 "schemaId": OUTBOUND_SCHEMA, "scope": "environment",
                                 "value": {"allowedOutboundConnections": {
                                     "enforced": True, "hostList": list(OUTBOUND_HOSTS)}},
                             }])
            caps["settings_write"] = r.status_code != 403

            r = await c.get(f"{base}/platform/app-settings/v2/objects", headers={
                **h, "Dt-App-Context": APP_ID, "Dt-App-Version": _app_version(),
            }, params={"schema-id": ORBITAL_SCHEMA})
            caps["app_settings"] = r.status_code != 403
    except Exception as exc:
        log.warning("capability probe failed for %s: %s", tenant_url, exc)
    return caps


def missing_capabilities(caps: dict[str, bool]) -> list[str]:
    """Capability keys the credential lacks, in report order."""
    return [k for k in CAPABILITY_COST if not caps.get(k, True)]


def blocking_missing(caps: dict[str, bool]) -> list[str]:
    """Missing capabilities that should stop a deploy — the grantable ones only."""
    return [k for k in missing_capabilities(caps) if k in BLOCKING_CAPABILITIES]


def describe_missing(missing: list[str]) -> str:
    """One actionable line per missing capability: the scope, and what it costs."""
    return " ".join(
        f"Missing {CAPABILITY_SCOPE[k]} — cannot {CAPABILITY_COST[k]}." for k in missing)


def _permission_hint(action: str, output: str) -> str:
    """Name the missing permission when a deploy fails because the token lacks it.

    The delegated SSO flow can check scopes up front, because the token response
    tells us what was granted. A pasted platform token carries no such claim, so
    the first sign of an under-scoped token is the registry refusing the install
    — which reaches the operator as "exit 1" plus 1500 characters of build log.
    That is technically the truth and practically unreadable; nothing in it says
    "add app-engine:apps:install".
    """
    low = (output or "").lower()
    if not any(s in low for s in ("403", "401", "forbidden", "unauthorized",
                                  "insufficient", "not permitted", "access denied")):
        return ""
    needed = ", ".join(sorted(REQUIRED_SCOPES.get(action, set())))
    return (f"The token was refused. A token used to {action} this app must carry: {needed}. "
            f"Create the token in the TARGET tenant with those scopes and retry. ")


# What an unseeded Orbital token actually costs, today.
#
# Stated precisely rather than dramatically. Workshops break immediately: the
# /api/live/* endpoints require the service bearer with no exception. Hands-on
# labs keep working for now only because /api/arena/* is still inside its
# compatibility window (_require_arena_auth allows anonymous callers while
# ARENA_AUTH_ENFORCE != "1") — when that flips, they stop too. Saying "every
# environment action fails" is wrong today and would be right later; saying which
# breaks now and which breaks next is right in both.
_ORBITAL_TOKEN_CONSEQUENCE = (
    "Without it, workshops and live sessions fail immediately (those endpoints always "
    "require the token), and hands-on labs keep working only while Orbital's arena "
    "compatibility window is open — they fail too once it closes.")


def _scope_warnings(allowlist: str, remote_grail: str, orbital_config: str = "") -> list[str]:
    """Surface post-install steps that were SKIPPED because the deploy token lacked
    settings:objects:write. The deploy itself still succeeds (those steps are best-effort),
    but the operator must know cross-tenant forwarding wasn't configured — otherwise it
    fails silently. Returned in the deploy response + audit so it's visible, not buried."""
    warnings: list[str] = []
    if "token lacks settings" in (remote_grail or ""):
        warnings.append(
            "remote-grail NOT configured: the deploy token is missing settings:objects:write, "
            "so cross-tenant forwarding to wwse was not enabled. Re-deploy with a token that has "
            "settings:objects:read+write, or set the remote-grail setting by hand.")
    if "token lacks settings" in (allowlist or ""):
        warnings.append(
            "outbound allowlist NOT updated: the deploy token is missing settings:objects:write.")
    # An unseeded orbital-config is the loudest failure of the three: the app installs
    # fine and then 401s on every provision, with nothing in the UI explaining why.
    if (orbital_config or "").startswith("unverified"):
        # Weaker claim, weaker warning: this is "check it", not "it is broken".
        # Add app-settings:objects:read to the deploy client and this becomes a
        # definite answer instead of a shrug.
        warnings.append(
            "Could not verify the app's Orbital token — the deploy credential cannot read app "
            "settings. Add app-settings:objects:read to the client and this check becomes "
            f"definite. If this tenant is new, set the token once in the app → "
            f"Admin → Orbital Server Configuration; {_ORBITAL_TOKEN_CONSEQUENCE} "
            "An already-configured tenant needs nothing.")
    elif (orbital_config or "").startswith("seed refused"):
        # The app answered, and said no. That is a server-side problem, not the
        # admin's: Orbital handed out a token its own /api/service/verify rejects.
        warnings.append(
            f"ACTION REQUIRED — Orbital token not seeded ({orbital_config}). This one is on "
            f"this server, not the tenant: ORBITAL_TOKEN in /home/ops/.env is not a value "
            f"Orbital itself accepts. Fix it there and re-deploy. "
            f"{_ORBITAL_TOKEN_CONSEQUENCE}")
    elif (orbital_config or "").startswith("skipped") or "failed" in (orbital_config or "") \
            or "error" in (orbital_config or ""):
        warnings.append(
            f"ACTION REQUIRED — Orbital token not seeded ({orbital_config}). "
            f"{_ORBITAL_TOKEN_CONSEQUENCE} This is a ONE-TIME manual step on a new tenant: "
            f"open the app → Settings → Orbital Server Configuration and paste the Orbital "
            f"token. A tenant that already has one needs nothing. It cannot be automated "
            f"FROM HERE — app-settings:objects:write is not offered in the OAuth client "
            f"scope catalog (400 invalid_request even for a client with full account "
            f"rights), and routing the write through an app function does not help: an app "
            f"function invoked by an external bearer runs with the CALLER's permissions, "
            f"not the app's. A signed-in browser session DOES carry the app's own scopes, "
            f"so a headless login can do it (that is how COE/SRO/sprint were seeded on "
            f"2026-08-02) — but that needs an interactive SSO login per tenant, which we "
            f"only have for tenants we own.")
    return warnings


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


async def _latest_repo_version() -> tuple[str, str]:
    """Version Orbital WOULD deploy = app.config.json on origin/<branch>, without mutating the
    working tree. `git fetch` then read the file from the remote ref via `git show`. Falls back
    to the checked-out version (`_app_version()`) on any git error. Returns (version, source)."""
    if not (Path(APP_REPO_DIR) / ".git").is_dir():
        return _app_version(), "local"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "HOME": os.environ.get("HOME", "/home/ops")}

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

    ref = deploy_ref()
    rc, _ = await _fetch_deploy_ref(_git)
    if rc != 0:
        return _app_version(), "local"
    rc, out = await _git("show", f"{ref}:app.config.json")
    if rc != 0:
        return _app_version(), "local"
    try:
        cfg = json.loads(out)
        return (cfg.get("app", {}).get("version") or cfg.get("version") or "?"), ref
    except Exception:
        return _app_version(), "local"


async def _sync_repo() -> tuple[bool, str]:
    """Move the deploy checkout to `deploy_ref()` before building.

    That is origin/<APP_DEPLOY_BRANCH> by default, or the exact ref in APP_DEPLOY_REF
    when public deploys are pinned to a released version.

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

    ref = deploy_ref()
    rc, msg = await _fetch_deploy_ref(_git)
    if rc != 0:
        return False, f"fetch failed: {msg[-300:]}"
    # Note whether dependencies changed so the operator knows to `npm ci` if a build fails.
    _, lock_diff = await _git("diff", "--name-only", f"HEAD..{ref}", "--", "package-lock.json")
    rc, msg = await _git("reset", "--hard", ref)
    if rc != 0:
        return False, f"reset failed: {msg[-300:]}"
    _, head = await _git("rev-parse", "--short", "HEAD")
    suffix = " (package-lock changed — run `npm ci` if build fails)" if lock_diff else ""
    return True, f"{ref}@{head}{suffix}"


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


def _tenant_host(tenant_url: str) -> str:
    return (urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}").hostname or "").lower()


def _is_coe(tenant_url: str) -> bool:
    """COE, addressed by either of its two names.

    COE answers to both geu80787.apps.dynatrace.com and the vanity alias
    wwse.apps.dynatrace.com, and which one arrives depends on what
    getEnvironmentUrl() returns in the app. Matching a single configured host
    meant one of the two silently failed to be recognised as COE.
    """
    h1 = _tenant_host(tenant_url)
    if not h1:
        return False
    if h1.split(".")[0] in COE_TENANT_IDS:
        return True
    return h1 == _tenant_host(COE_TENANT_URL)


def _deploy_scopes(action: str) -> list[str]:
    """Scope sets to try for an action, richest first.

    Installing the app is only half a deploy. The post-install steps — the
    JS-runtime outbound allowlist, remote-grail forwarding to the central tenant,
    and the stored Orbital bearer — need settings scopes, and a token without
    them skips all three silently. A tenant deployed that way installs fine and
    then never forwards a single training event.

    But the grant is all-or-nothing: asking for a scope the client lacks returns
    400 invalid_request with an empty error_description, which fails the deploy
    outright rather than degrading. So ask for everything, and fall back to the
    minimum that still installs. A fully-provisioned client gets a fully
    configured tenant; a minimal one still deploys, and _finish_deploy already
    reports which steps it had to skip.
    """
    if action == "undeploy":
        return ["app-engine:apps:delete"]
    install = "app-engine:apps:install app-engine:apps:run"
    settings = "settings:objects:read settings:objects:write"
    # A ladder, not a pair. Descending one capability at a time means the token we
    # end up holding carries the most the client allows, and — because the grant is
    # all-or-nothing — its granted scope is then an exact statement of what it can
    # do. That is what capabilities_from_scope reads, so the rungs must stay
    # ordered richest-first.
    # app-settings READ rides along on every rung that can carry it. It grants no
    # power the deploy needs, but without it in the request the minted token cannot
    # read the app's own settings — so the install cannot tell whether this tenant
    # already has its Orbital token and reports "unverified" even when the client
    # was granted the scope. That was the whole point of granting it.
    aps_r, aps_w = "app-settings:objects:read", "app-settings:objects:write"
    # Strictly descending: each rung is a subset of the one above, so the first that
    # succeeds is the most the client allows. (A client holding app-settings WRITE
    # but not READ would drop to rung 3 and lose the write — not a real shape, and
    # the write is ungrantable today regardless.)
    return [f"{install} {settings} {aps_r} {aps_w}",
            f"{install} {settings} {aps_r}",
            f"{install} {settings}",
            f"{install} {aps_r}",
            install]


# Scope actually granted by the last successful mint, per tenant label. The token
# itself is never kept — only what it was allowed to do, so the credential chooser
# can reason about it without re-minting.
_LAST_GRANT: dict[str, str] = {}


def capabilities_from_scope(granted: str) -> dict[str, bool]:
    """What a bearer with this granted scope can do. Exact, not probed.

    Preferred over probe_capabilities wherever a scope claim exists, because a
    probe can only ask "may I read this?" — and read is not write. The COE client
    holds app-settings:objects:read but NOT :write, so a GET-based probe passes
    while the deploy's actual write still fails. Reading the grant avoids that
    whole class of false pass.
    """
    g = set((granted or "").split())
    return {
        "registry": {"app-engine:apps:install", "app-engine:apps:run"} <= g,
        "settings_read": "settings:objects:read" in g,
        "settings_write": "settings:objects:write" in g,
        "app_settings": "app-settings:objects:write" in g,
    }


async def _mint_account_token(label: str, client_id: str, client_secret: str,
                              resource: str, action: str,
                              sso_url: str = f"{DEFAULT_SSO}/sso/oauth2/token",
                              scope_sets: list[str] | None = None) -> str | None:
    """Mint a deploy bearer from a tenant's account OAuth client (server-side).

    One implementation for every tenant Orbital deploys to itself. sso_url is a
    parameter because the labs tenants (sprint) authenticate against their own
    SSO host, not sso.dynatrace.com. `scope_sets` overrides the deploy ladder for
    callers that need different powers — content sync, notably.
    """
    if not (client_id and client_secret):
        return None
    scope_sets = scope_sets if scope_sets is not None else _deploy_scopes(action)
    for i, scope in enumerate(scope_sets):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(sso_url, data={
                    "grant_type": "client_credentials", "client_id": client_id,
                    "client_secret": client_secret, "resource": resource,
                    "scope": scope,
                }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            if r.status_code == 200:
                j = r.json()
                _LAST_GRANT[label] = j.get("scope", "") or scope
                if i:
                    log.info("%s deploy token: granted %s (rung %d/%d) — richer scope sets "
                             "were refused", label, _LAST_GRANT[label], i + 1, len(scope_sets))
                return j.get("access_token")
            # Body, not just status: a scope the client lacks and a wrong resource
            # are both 400, and only the body tells them apart.
            log.warning("%s token mint HTTP %s (scope set %d/%d): %s",
                        label, r.status_code, i + 1, len(scope_sets), r.text[:200])
        except Exception as exc:
            log.warning("%s token mint failed: %s", label, exc)
            return None
    return None


async def _mint_coe_token(action: str) -> str | None:
    """Mint a bearer for the COE tenant from Orbital's COE client credentials."""
    return await _mint_account_token("COE", COE_CLIENT_ID, COE_CLIENT_SECRET,
                                     COE_RESOURCE, action)


# What a content-sync caller needs, and WHY each one — this list was arrived at
# empirically, and the reasoning is easy to lose.
#
# The decisive fact: an app function invoked by an external OAuth bearer runs
# with the CALLER's permissions, not the app's. Measured — `POST .../api/boot`
# with a deploy bearer returns `labs: 0, canWrite: false` on a tenant holding 42
# lab documents, because the caller has no document scope. So every permission
# the import needs has to be on the token Orbital presents.
#
#   state:app-states:read  — `loadMintClient` reads the mint OAuth client out of
#       app state. Without it `isServiceIdentityAvailable()` answers false, the
#       import silently falls back to caller-context document writes, and every
#       source fails `[storeLabDocument] Forbidden`. This is the load-bearing
#       one: WITH it the app mints its own token and documents are written by
#       the service principal, which is what keeps ownership uniform.
#   document:* — the fallback path, for a tenant with no mint client configured.
#       Documents then belong to this client rather than the service principal,
#       which is worse than the mint path but far better than no content.
#   settings/app-settings reads — the import reads content-service config.
CONTENT_SYNC_SCOPES = [
    "app-engine:apps:run state:app-states:read app-settings:objects:read "
    "settings:objects:read document:documents:read document:documents:write "
    "document:documents:delete",
    # Degrade in the same spirit as the deploy ladder: keep the mint path even if
    # the client cannot hold document scopes, since the mint path is the good one.
    "app-engine:apps:run state:app-states:read app-settings:objects:read",
    "app-engine:apps:run state:app-states:read",
]


async def content_sync_token(tenant_url: str) -> tuple[str, str]:
    """(token, label) — a bearer able to drive an import on this tenant."""
    if _is_coe(tenant_url):
        return (await _mint_account_token("COE-content", COE_CLIENT_ID, COE_CLIENT_SECRET,
                                          COE_RESOURCE, "deploy",
                                          scope_sets=CONTENT_SYNC_SCOPES) or ""), "COE"
    if _is_sro(tenant_url):
        return (await _mint_account_token("SRO-content", SRO_CLIENT_ID, SRO_CLIENT_SECRET,
                                          SRO_RESOURCE, "deploy",
                                          scope_sets=CONTENT_SYNC_SCOPES) or ""), "SRO"
    if _is_sprint(tenant_url):
        return (await _mint_account_token("SPRINT-content", SPRINT_CLIENT_ID, SPRINT_CLIENT_SECRET,
                                          SPRINT_RESOURCE, "deploy", sso_url=SPRINT_SSO_URL,
                                          scope_sets=CONTENT_SYNC_SCOPES) or ""), "SPRINT"
    return "", ""


def _is_sprint(tenant_url: str) -> bool:
    h1 = (urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}").hostname or "").lower()
    h2 = (urlparse(SPRINT_TENANT_URL).hostname or "").lower()
    return bool(h2) and h1 == h2


async def _mint_sprint_token(action: str) -> str | None:
    """Mint a bearer for the sprint tenant. Its own SSO host, its own account."""
    return await _mint_account_token("SPRINT", SPRINT_CLIENT_ID, SPRINT_CLIENT_SECRET,
                                     SPRINT_RESOURCE, action, sso_url=SPRINT_SSO_URL)


async def _auto_candidates(tenant_url: str, action: str) -> tuple[list[tuple[str, str]], str]:
    """([(token, source)], label) — every credential Orbital holds for this tenant.

    OAuth first: it is the route we hand tenant admins, so it is the one that must
    be exercised. SRO's stored platform token stays behind it as a fallback, but it
    is not merely a fallback for *failure* — it is measurably more capable than the
    OAuth client today (it carries the settings scopes the client lacks), so the
    caller picks between them on evidence rather than on order alone.
    """
    if _is_coe(tenant_url):
        return [(await _mint_coe_token(action) or "", "oauth")], "COE"
    if _is_sro(tenant_url):
        return [(await _mint_sro_token(action) or "", "oauth"),
                (SRO_PLATFORM_TOKEN, "platform-token")], "SRO"
    if _is_sprint(tenant_url):
        return [(await _mint_sprint_token(action) or "", "oauth")], "SPRINT"
    return [], ""


async def auto_deploy_token(tenant_url: str, action: str) -> tuple[str, str]:
    """(token, label) — first credential Orbital holds for this tenant. No probing."""
    cands, label = await _auto_candidates(tenant_url, action)
    return next((t for t, _ in cands if t), ""), label


async def choose_deploy_credential(tenant_url: str, action: str) -> dict:
    """Pick the credential that can actually complete the deploy, and prove it first.

    Installing the app is only part of a deploy: the outbound allowlist, cross-tenant
    forwarding and the seeded Orbital bearer all need scopes beyond apps:install. A
    credential holding only the install scopes produces a tenant where the app appears
    successfully and then fails every environment action with an opaque 401 — the
    expensive kind of broken, because it looks fine.

    So every candidate is probed against the real tenant BEFORE anything is installed,
    and the first complete one wins. Undeploy skips this: it only needs the registry.

    Returns {token, source, label, caps, missing} — `missing` empty means complete.
    """
    cands, label = await _auto_candidates(tenant_url, action)
    cands = [(t, s) for t, s in cands if t]
    if not cands:
        return {"token": "", "source": "", "label": label, "caps": {}, "missing": []}
    if action == "undeploy":
        t, s = cands[0]
        return {"token": t, "source": s, "label": label, "caps": {}, "missing": []}

    best = None
    for token, source in cands:
        # An OAuth mint tells us exactly what it granted; a pasted platform token
        # carries no such claim and has to be measured against the tenant.
        granted = _LAST_GRANT.get(label, "") if source == "oauth" else ""
        caps = (capabilities_from_scope(granted) if granted
                else await probe_capabilities(token, tenant_url))
        # Rank on what an admin can actually fix; report everything.
        missing = blocking_missing(caps)
        if not missing:
            if best is not None:
                log.info("%s deploy: using the %s credential — %s lacks %s",
                         label, source, best["source"], ", ".join(best["missing"]))
            return {"token": token, "source": source, "label": label,
                    "caps": caps, "missing": []}
        log.warning("%s deploy: %s credential lacks %s", label, source, ", ".join(missing))
        if best is None or len(missing) < len(best["missing"]):
            best = {"token": token, "source": source, "label": label,
                    "caps": caps, "missing": missing}
    return best


def _is_sro(tenant_url: str) -> bool:
    h1 = (urlparse(tenant_url if "://" in tenant_url else f"https://{tenant_url}").hostname or "").lower()
    h2 = (urlparse(SRO_TENANT_URL).hostname or "").lower()
    return bool(h2) and h1 == h2


async def _mint_sro_token(action: str) -> str | None:
    """Mint a bearer for the SRO (QA) tenant from Orbital's SRO client credentials."""
    return await _mint_account_token("SRO", SRO_CLIENT_ID, SRO_CLIENT_SECRET,
                                     SRO_RESOURCE, action)


OUTBOUND_SCHEMA = "builtin:dt-javascript-runtime.allowed-outbound-connections"
# The central tenant that non-COE installs forward training telemetry TO.
# wwse.apps.dynatrace.com is a vanity alias for geu80787.apps.dynatrace.com.
#
# Deliberately NOT named COE_TENANT_URL. It used to be, and being declared here —
# 400 lines below the identically-named deploy-target constant — it silently
# overwrote it, so `_is_coe` compared incoming tenants against the vanity alias
# and never matched the canonical geu80787 host the app actually sends. COE
# auto-deploy was dead for exactly as long as that shadowing existed.
CENTRAL_TENANT_URL = "https://wwse.apps.dynatrace.com"
CENTRAL_TENANT_HOST = "wwse.apps.dynatrace.com"
# Tenants that ARE the central tenant — never forward to themselves (store locally).
COE_TENANT_IDS = {"wwse", "geu80787"}
REMOTE_GRAIL_SCHEMA = "app:my.dynatrace.enablements:remote-grail"
REMOTE_GRAIL_SCHEMA_VERSION = "1.1"
# App-settings (NOT classic settings) schema holding the Orbital service bearer —
# settings/schemas/orbital-config.schema.json in the app repo. Unprefixed here because
# the app-settings API resolves it within the Dt-App-Context app.
ORBITAL_SCHEMA = "orbital-config"
# Hosts the app's functions must reach for content delivery + manual GitHub imports
# + forwarding training bizevents to the central tenant + (gen3 self-mint) the account SSO
# and Account Management API so the app can mint per-user platform tokens with its own
# stored OAuth client. sso/api.dynatrace.com cover prod; sprint/dev realms differ — their
# hosts are added from the stored client's ssoUrl/apiHost when the app is configured there.
OUTBOUND_HOSTS = [
    "autonomous-enablements.whydevslovedynatrace.com",
    "raw.githubusercontent.com",
    "api.github.com",
    CENTRAL_TENANT_HOST,
    "sso.dynatrace.com",
    "api.dynatrace.com",
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


def _coe_remote_grail_token() -> str | None:
    """Decrypt the COE remote-grail token stored encrypted at rest.

    The plaintext token is NEVER stored or logged. It is held only as a Fernet
    ciphertext in env `REMOTE_GRAIL_COE_TOKEN_ENC` (encrypted with GH_OAUTH_ENC_KEY)
    and decrypted in-memory here, then written into the target tenant's remote-grail
    setting (where the platform stores it as a `secret`-typed property). The token is
    scoped read+ingest only (storage:events:write, storage:bizevents:read,
    storage:buckets:read). Returns None when not configured."""
    enc = os.environ.get("REMOTE_GRAIL_COE_TOKEN_ENC", "")
    if not enc:
        return None
    try:
        return _decrypt(enc)
    except Exception as exc:
        log.warning("remote-grail: could not decrypt COE token: %s", exc)
        return None


async def _ensure_remote_grail(token: str, tenant_url: str) -> str:
    """For a NON-COE tenant, set the app's `remote-grail` setting so its training
    bizevents forward to (and are read back from) the central COE tenant. The COE
    token is injected from encrypted storage — never logged, never returned.

    Skips the central tenant itself (it stores locally). Best-effort; needs
    settings:objects:read+write on the deploy token. Idempotent: updates the existing
    object in place. See docs/remote-grail-setup-and-automation.md."""
    tenant_id, _ = classify_tenant(tenant_url)
    if tenant_id in COE_TENANT_IDS:
        return "skipped (central tenant — stores locally)"
    coe_token = _coe_remote_grail_token()
    if not coe_token:
        return "skipped (REMOTE_GRAIL_COE_TOKEN_ENC not configured)"
    base = tenant_url.rstrip("/") + "/platform/classic/environment-api/v2/settings/objects"
    h = {"Authorization": f"Bearer {token}"}
    value = {"enabled": True, "tenantUrl": CENTRAL_TENANT_URL, "apiToken": coe_token}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(base, headers=h, params={
                "schemaIds": REMOTE_GRAIL_SCHEMA, "scopes": "environment", "fields": "objectId"})
            if r.status_code == 403:
                return "skipped (token lacks settings:objects:read/write)"
            if r.status_code != 200:
                return f"skipped (settings read HTTP {r.status_code})"
            items = r.json().get("items", [])
            if items:
                oid = items[0]["objectId"]
                pr = await c.put(f"{base}/{oid}", headers={**h, "Content-Type": "application/json"},
                                 json={"value": value})
                ok = pr.status_code in (200, 201, 204)
                return "updated → wwse" if ok else f"update failed (HTTP {pr.status_code})"
            cr = await c.post(base, headers={**h, "Content-Type": "application/json"}, json=[{
                "schemaId": REMOTE_GRAIL_SCHEMA, "schemaVersion": REMOTE_GRAIL_SCHEMA_VERSION,
                "scope": "environment", "value": value,
            }])
            ok = cr.status_code in (200, 201)
            return "enabled → wwse" if ok else f"create failed (HTTP {cr.status_code}: {cr.text[:120]})"
    except Exception as exc:
        log.warning("remote-grail for %s: %s", tenant_url, exc)
        return f"remote-grail error: {exc}"


def _orbital_service_token() -> str | None:
    """The bearer the app's functions must present to Orbital.

    Unlike the remote-grail token this is not a tenant credential — it is Orbital's
    OWN service token (the same value `/api/arena/*` is gated with), so it lives in
    plain `ORBITAL_TOKEN` rather than encrypted at rest. It is written into a
    `secret`-typed app-settings property and is never logged or returned."""
    return (os.environ.get("ORBITAL_TOKEN") or "").strip() or None


async def _seed_via_app_function(token: str, tenant_url: str) -> str | None:
    """Ask the app about its orbital-config, and report what it said.

    THIS CANNOT SEED A FRESH TENANT, and the reason is worth keeping written down
    because it looks like it should.

    Writing app-settings needs `app-settings:objects:write`, which is not offered
    in the account OAuth client scope catalog — measured on all three tenants,
    including the COE master client with full account rights: requesting it
    answers `400 invalid_request`, and a direct PUT with the richest grantable
    token answers `403 {"missingScopes":["app-settings:objects:write"]}`.

    The app declares that scope itself, so routing the write through an app
    function is the obvious idea. It does not work: an app function invoked by an
    external bearer runs with the CALLER's permissions, not the app's. Measured —
    `POST .../api/boot` with a deploy bearer returns `labs: 0, canWrite: false` on
    a tenant holding 42 lab documents. The function would hit the same 403.

    The corollary, since it is easy to read the above as "impossible": a SIGNED-IN
    session is exactly the caller whose permissions include the app's, so the same
    POST issued from inside a logged-in browser succeeds. That is how COE, SRO and
    sprint actually got their token — a headless browser drove the app on
    2026-08-02 (23:03/23:06/23:26 UTC, per the UUIDv1 in each object's version) and
    POSTed `orbital-config` from the app's own page context. It is automatable for
    any tenant we can log into; it is not automatable from a deploy credential, and
    a customer tenant gives us neither.

    What it IS good for: an honest, definite answer about whether this tenant is
    already configured, which the read-only probe below can only guess at when the
    credential cannot read app settings. `already-configured` from here is a fact.

    Returns None when the function is absent (older app version), so the caller
    falls back to the legacy direct write and its message.
    """
    fn = (f"{tenant_url.rstrip('/')}/platform/app-engine/app-functions/v1/apps/"
          f"{APP_ID}/api/seedOrbitalConfig")
    try:
        # An app is not routable the instant its install returns — the first call
        # after an upgrade answers "App not found", and the same call seconds later
        # succeeds. Retrying here is the difference between a definite answer and
        # an ACTION REQUIRED warning on every single deploy.
        r = None
        for attempt, pause in enumerate((0, 5, 10)):
            if pause:
                await asyncio.sleep(pause)
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(fn, headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"},
                                 json={"token": _orbital_service_token()})
            if r.status_code == 200 or r.status_code == 404:
                break
        if r.status_code == 404:
            return None  # function not in the installed version yet
        if r.status_code != 200:
            # Could not ask. That is not "not seeded" — the app ships a default
            # bearer, so an unanswered question is a check we could not run, not a
            # tenant that is broken. Saying otherwise cries wolf on every deploy.
            return f"unverified (app function unreachable: HTTP {r.status_code})"
        body = r.json() if r.content else {}
        status = (body or {}).get("status", "")
        return {
            "seeded": "token seeded (via app function)",
            "already-configured": "already configured",
            "rejected": "seed refused: Orbital did not accept the token Orbital sent "
                        "— check ORBITAL_TOKEN on this server",
            "missing-token": "skipped (ORBITAL_TOKEN not configured)",
        }.get(status, f"seed via app function: {status or 'unknown'} "
                      f"{(body or {}).get('detail', '')}".strip())
    except Exception as exc:
        log.warning("seedOrbitalConfig on %s: %s", tenant_url, exc)
        return f"unverified (app function error: {exc})"


async def _ensure_orbital_config(token: str, tenant_url: str) -> str:
    """Seed the app-settings object the app's functions read their Orbital bearer from.

    Without it `getOrbitalToken()` (api/orbital.function.ts, api/codespace.function.ts)
    finds nothing, calls Orbital unauthenticated, and every provision/terminate/exec on
    that tenant fails with an opaque 401 that the admin has no way to diagnose from the
    UI. Seeding it at deploy time is the difference between "the app works after
    install" and "the app installs and then silently does nothing".

    Idempotent and non-destructive: an object that already carries a token is left
    alone — the tenant may legitimately hold a different valid one, and the API masks
    secrets on read so we could not compare anyway. Only absent/empty is filled.

    App-settings v2 is NOT the classic settings API: it needs the `Dt-App-Context` /
    `Dt-App-Version` headers (which the platform injects for in-app callers but an
    external caller must supply) and kebab-case query params. Best-effort — needs
    app-settings:objects:write on the deploy token."""
    orbital_token = _orbital_service_token()
    if not orbital_token:
        return "skipped (ORBITAL_TOKEN not configured)"

    # Preferred route: let the app write its own settings. Only falls through to
    # the direct write below when the installed version predates the function.
    via_fn = await _seed_via_app_function(token, tenant_url)
    if via_fn is not None:
        return via_fn

    base = tenant_url.rstrip("/") + "/platform/app-settings/v2/objects"
    h = {
        "Authorization": f"Bearer {token}",
        "Dt-App-Context": APP_ID,
        "Dt-App-Version": _app_version(),
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(base, headers=h, params={
                "schema-id": ORBITAL_SCHEMA, "add-fields": "value"})
            if r.status_code == 403:
                # Cannot even READ, so we do not know whether a token is already
                # there. Reporting this as "not seeded" cried wolf on every
                # already-configured tenant; say what is actually true instead.
                return "unverified (credential cannot read app settings)"
            if r.status_code != 200:
                return f"skipped (app-settings read HTTP {r.status_code})"
            items = r.json().get("items", [])
            if items:
                obj = items[0]
                # Read-back is masked ("***…***"), so any non-empty value means the
                # tenant already has one configured — don't clobber it.
                if (obj.get("value") or {}).get("token"):
                    return "already configured"
                pr = await c.put(f"{base}/{obj['objectId']}",
                                 headers={**h, "Content-Type": "application/json"},
                                 json={"value": {"token": orbital_token}})
                ok = pr.status_code in (200, 201, 204)
                return "token seeded" if ok else f"seed failed (HTTP {pr.status_code})"
            cr = await c.post(base, headers={**h, "Content-Type": "application/json"},
                              json={"schemaId": ORBITAL_SCHEMA,
                                    "value": {"token": orbital_token}})
            ok = cr.status_code in (200, 201)
            return "token seeded" if ok else f"seed failed (HTTP {cr.status_code}: {cr.text[:120]})"
    except Exception as exc:
        log.warning("orbital-config for %s: %s", tenant_url, exc)
        return f"orbital-config error: {exc}"


async def _register_in_content_service(user: str, tenant_url: str) -> dict | None:
    """Best-effort: add the tenant to the delivery table so its content can be managed."""
    try:
        return await register_tenant({"tenant": tenant_url}, x_auth_user=user)
    except Exception as exc:
        log.warning("register-tenant failed for %s: %s", tenant_url, exc)
        return None


async def _begin_sso_flow(tenant: str, action: str, user: str) -> RedirectResponse:
    """Build the Dynatrace SSO authorize redirect (Auth-Code + PKCE) for a tenant deploy and
    stash the flow in Redis. Used by the dashboard operator path (/api/deploy/start, writer-gated).
    NOTE: the in-app "Update now" path does NOT use SSO — Dynatrace OAuth clients are account-
    scoped, so they can't deploy a foreign tenant; the app uses a pasted platform token instead
    (see /api/deploy/stash + /api/deploy/app-start)."""
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


@router.get("/api/deploy/start")
async def deploy_start(tenant: str, action: str = "deploy", x_auth_user: str | None = Header(default=None)):
    """Begin the SSO flow for a tenant (dashboard operator path). nginx gates this to org
    members (X-Auth-User)."""
    user = _require_writer(x_auth_user)
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    return await _begin_sso_flow(tenant, action, user)


async def _finish_deploy(user: str, tenant_id: str, tenant_url: str, action: str, token: str,
                         via: str = "sso-deploy", deployer: str = "") -> HTMLResponse:
    """Run the install/upgrade (or uninstall) with a valid bearer and return the result page.
    Shared by the SSO callback and the in-app COE/SRO auto-deploy path. The caller owns the
    token's lifetime and must `del` it after this returns (it is never logged here).
    `via`/`deployer` feed the tenant-attribution registry (deployer = GitHub username on the
    SSO path; empty on app-triggered paths — the runtime backstop fills identity later)."""
    app_url = _app_url(tenant_url)
    if action == "undeploy":
        ok, msg = await _run_undeploy(token, tenant_url)
        await _audit(user, tenant_id, "undeploy", "undeployed" if ok else "undeploy-error", detail=msg)
        return HTMLResponse(_page(
            f"App <b>{APP_ID}</b> undeployed from <b>{tenant_id}</b>." if ok
            else f"Undeploy failed for <b>{tenant_id}</b>: {msg}", ok=ok),
            status_code=200 if ok else 502)

    # deploy — idempotent: skip if up-to-date, else install/upgrade
    res = await _deploy_with_status(token, tenant_url)
    remote_grail = ""
    orbital_cfg = ""
    if res["status"] != "error":
        await _ensure_outbound_allowlist(token, tenant_url)
        remote_grail = await _ensure_remote_grail(token, tenant_url)  # auto-enable cross-tenant forwarding
        orbital_cfg = await _ensure_orbital_config(token, tenant_url)  # so app functions can reach Orbital
    if res["status"] == "error":
        hint = _permission_hint(action, res.get("output", ""))
        await _audit(user, tenant_id, "deploy", "deploy-error", rc=res.get("rc"),
                     permission_hint=bool(hint))
        return HTMLResponse(_page(
            f"Deploy to <b>{tenant_id}</b> failed (exit {res.get('rc')}).<br><br>"
            + (f"<b>{hint}</b><br><br>" if hint else "")
            + f"<pre style='white-space:pre-wrap;color:#f0c674'>{res.get('output','')}</pre>", ok=False), status_code=502)

    reg = await _register_in_content_service(user, tenant_url)
    profile = (reg or {}).get("profile")
    await tenant_registry.record_deploy(_pool(), tenant_id, via, deployer=deployer,
                                        app_version=res.get("to") or "")
    await _audit(user, tenant_id, "deploy", res["status"],
                 **{k: res[k] for k in ("from", "to") if res.get(k)}, url=app_url, profile=profile,
                 remote_grail=remote_grail, orbital_config=orbital_cfg)
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


# Required scopes for a deploy platform token created in the target tenant. Surfaced to the
# admin so the token they paste actually works (install/run/delete + settings for the
# post-install allowlist + cross-tenant forwarding steps).
DEPLOY_TOKEN_SCOPES = ("app-engine:apps:install", "app-engine:apps:run", "app-engine:apps:delete",
                       "settings:objects:read", "settings:objects:write")


@router.post("/api/deploy/stash")
async def deploy_stash(body: dict):
    """Step 1 of the in-app "Update now" for a self-managed tenant: the app POSTs the platform
    token the admin pasted; we encrypt it (Fernet, GH_OAUTH_ENC_KEY) and hold it under a random
    nonce for 5 minutes, returning the nonce. The popup then calls /api/deploy/app-start?nonce=…
    so the token is NEVER put in a URL / access log. PUBLIC, opaque blob keyed by the nonce."""
    tenant = (body.get("tenant") or "").strip()
    token = (body.get("token") or "").strip()
    classify_tenant(tenant)  # 403 if not a Dynatrace domain
    if not token:
        raise HTTPException(400, "token is required.")
    nonce = secrets.token_urlsafe(24)
    await _pool().setex(f"deploy:pending:{nonce}", 300, _encrypt(json.dumps({"tenant": tenant, "token": token})))
    del token
    return {"nonce": nonce}


@router.get("/api/deploy/app-start", response_class=HTMLResponse)
async def deploy_app_start(tenant: str, action: str = "deploy", nonce: str = ""):
    """In-app trigger for the Admin "Update now" button (opened in a popup). PUBLIC, no GitHub
    gate, no OAuth client (Dynatrace OAuth clients are account-scoped → can't deploy a foreign
    tenant). Credential paths, in order:
      1. COE/SRO — Orbital holds these tenants' creds → deploy directly, no token needed.
      2. nonce — the admin pasted a platform token in the app; we deploy with the stashed,
         encrypted token (created in the target tenant, so it carries that account's authority),
         then discard it.
      3. neither — show how to update: the required token scopes + the Orbital register link."""
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    tenant_id, _ = classify_tenant(tenant)  # 403 if not a Dynatrace domain

    token, auto = await auto_deploy_token(tenant, action)
    if auto:
        if not token:
            return HTMLResponse(_page(f"{auto} auto-deploy not configured.", ok=False), status_code=503)
        try:
            return await _finish_deploy(f"app:{tenant_id}", tenant_id, tenant, action, token, via="auto")
        finally:
            del token

    if nonce:
        raw = await _pool().get(f"deploy:pending:{nonce}")
        if not raw:
            return HTMLResponse(_page("Update session expired — go back to the app and try again.", ok=False), status_code=400)
        await _pool().delete(f"deploy:pending:{nonce}")  # one-time use
        try:
            stashed = json.loads(_decrypt(raw))
        except Exception:
            return HTMLResponse(_page("Could not read the update session.", ok=False), status_code=400)
        # Bind the stashed token to the tenant it was pasted for.
        if stashed.get("tenant") != tenant:
            return HTMLResponse(_page("Tenant mismatch for this update session.", ok=False), status_code=400)
        token = stashed.get("token", "")
        try:
            return await _finish_deploy(f"app:{tenant_id}", tenant_id, tenant, action, token, via="token")
        finally:
            del token

    scopes = ", ".join(DEPLOY_TOKEN_SCOPES)
    return HTMLResponse(_page(
        f"To update the app on <b>{tenant_id}</b>, paste a Dynatrace <b>platform token</b> created "
        f"in this tenant into the app’s <b>Update now</b> field, then click it again.<br><br>"
        f"The token needs scopes: <b>{scopes}</b>.<br><br>"
        f"Alternatively, an enablement admin can deploy it for you from "
        f"<a href='/#register'>Autonomous Enablement (Orbital)</a>.", ok=False), status_code=200)


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
    try:
        return await _finish_deploy(user, tenant_id, tenant_url, action, token,
                                    via="sso-deploy", deployer=user)
    finally:
        del token  # discard the delegated credential on every path


@router.get("/api/deploy/latest-version")
async def latest_version():
    """The app version Orbital would deploy (app.config.json at `deploy_ref()`). Tokenless — just a
    version string, no tenant credential involved. The app's Admin "Check for updates" compares
    this against its own baked-in installed version (no platform token needed for the check).

    `pinned` tells an admin whether they are being offered a released version or
    the moving tip of the branch."""
    version, source = await _latest_repo_version()
    return {"version": version, "branch": APP_DEPLOY_BRANCH, "source": source,
            "ref": deploy_ref(), "pinned": bool(APP_DEPLOY_REF)}


@router.post("/api/deploy/token")
async def deploy_with_token(body: dict, x_auth_user: str | None = Header(default=None)):
    """Override path for ANY tenant (customer / prospect / free trial / cross-account): the
    caller supplies a platform token created IN the target tenant (scopes apps:install/run/
    delete). That credential carries the target account's authority, so no SSO/account binding
    is needed. The token is used once and discarded — never logged or persisted.

    NOT org-member-gated: the platform token IS the authority to deploy on the target tenant,
    so a signed-out user holding a valid token may deploy/register (nginx routes this one path
    with opportunistic auth — see ops-server.conf `location = /api/deploy/token`). We audit the
    GitHub identity when nginx forwards one (signed-in org member), else "anonymous"."""
    user = x_auth_user or "anonymous"
    action = body.get("action", "deploy")
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    tenant = (body.get("tenant") or "").strip()
    token = (body.get("token") or "").strip()
    tenant_id, domain = classify_tenant(tenant)  # 403 if not a Dynatrace domain
    # Proceed with a credential that cannot finish the job. Off by default: a
    # half-configured install is worse than no install, because it looks like a
    # success and fails later somewhere unrelated.
    allow_partial = bool(body.get("allowPartial"))
    auto = ""  # which auto-deploy tenant matched (COE/SRO/SPRINT), or "" for token deploys
    source = "pasted-token"
    if not token:
        # The tenants Orbital deploys on its own, because it holds their account clients.
        pick = await choose_deploy_credential(tenant, action)
        auto, token, source = pick["label"], pick["token"], pick["source"]
        if not auto:
            raise HTTPException(400, "A valid platform token is required for this tenant. "
                                     "Auto-deploy (no token) is only available for the COE, "
                                     "SRO and sprint tenants.")
        if not token:
            raise HTTPException(503, f"{auto} auto-deploy not configured "
                                     f"(set {auto}_CLIENT_ID/SECRET/RESOURCE).")
        if pick["missing"] and not allow_partial:
            await _audit(user, tenant_id, action, "insufficient-scopes",
                         via=f"{auto.lower()}-auto", source=source, missing=pick["missing"])
            raise HTTPException(412, f"{auto} deploy refused before installing anything: the "
                                     f"{source} credential cannot complete it. "
                                     f"{describe_missing(pick['missing'])} "
                                     f"Grant the scopes to the account OAuth client and retry, "
                                     f"or send allowPartial:true to install anyway.")
    elif action == "deploy":
        # A pasted platform token carries no readable scope claim, so probe it too —
        # same guarantee for a customer admin as for our own tenants.
        caps = await probe_capabilities(token, tenant)
        missing = blocking_missing(caps)
        if missing and not allow_partial:
            await _audit(user, tenant_id, action, "insufficient-scopes",
                         via="token", source=source, missing=missing)
            raise HTTPException(412, "Deploy refused before installing anything: this token "
                                     f"cannot complete it. {describe_missing(missing)} "
                                     "Create the token in the target tenant with those scopes "
                                     "and retry, or send allowPartial:true to install anyway.")

    via = f"{auto.lower()}-auto" if auto else "token"
    if action == "undeploy":
        ok, msg = await _run_undeploy(token, tenant)
        del token
        await _audit(user, tenant_id, "undeploy", "undeployed" if ok else "undeploy-error", via=via, detail=msg)
        if not ok:
            raise HTTPException(502, f"Undeploy failed: {msg}")
        return {"ok": True, "tenant": tenant_id, "action": "undeploy"}

    res = await _deploy_with_status(token, tenant)
    allowlist = ""
    remote_grail = ""
    orbital_cfg = ""
    if res["status"] != "error":
        allowlist = await _ensure_outbound_allowlist(token, tenant)  # use token before discarding
        remote_grail = await _ensure_remote_grail(token, tenant)     # auto-enable cross-tenant forwarding
        orbital_cfg = await _ensure_orbital_config(token, tenant)    # so app functions can reach Orbital
    del token
    if res["status"] == "error":
        hint = _permission_hint(action, res.get("output", ""))
        await _audit(user, tenant_id, "deploy", "deploy-error", via=via, rc=res.get("rc"),
                     permission_hint=bool(hint))
        raise HTTPException(502, f"Deploy failed (exit {res.get('rc')}). "
                                 f"{hint}{res.get('output','')}")
    reg = await _register_in_content_service(user, tenant)
    profile = (reg or {}).get("profile")
    url = _app_url(tenant)
    warnings = _scope_warnings(allowlist, remote_grail, orbital_cfg)
    await tenant_registry.record_deploy(
        _pool(), tenant_id, "auto" if auto else "token",
        deployer=x_auth_user or "", app_version=res.get("to") or "")
    await _audit(user, tenant_id, "deploy", res["status"], via=via,
                 **{k: res[k] for k in ("from", "to") if res.get(k)}, url=url, profile=profile,
                 allowlist=allowlist, remote_grail=remote_grail, orbital_config=orbital_cfg,
                 warnings=warnings)
    return {"ok": True, "tenant": tenant_id, "status": res["status"], "from": res.get("from"),
            "version": res.get("to"), "url": url, "profile": profile, "credential": source,
            "allowlist": allowlist, "remote_grail": remote_grail,
            "orbital_config": orbital_cfg, "warnings": warnings}


# ── Account-OAuth-client BOOTSTRAP deploy (transient — Orbital stores NOTHING) ───
#
# First-install chicken-and-egg: the app can't hold its own OAuth client until it's
# installed. So the tenant admin pastes the account OAuth client (id + secret + account
# URN) ONCE to land the app. Orbital uses it for the single deploy call and DISCARDS it —
# it is never written to Redis, disk, or logs. After this, the admin configures the client
# INSIDE the app (app-state, tenant-local); from then on the app self-mints user tokens and
# self-updates by handing Orbital only short-lived, install-scoped bearers. Orbital holds no
# tenant credential at rest — an Orbital compromise exposes nothing.
#
# Design + threat model: ops-server/docs/tenant-credentials.md.

MINT_SCOPE = "platform-token:tokens:write platform-token:tokens:manage"
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
# Environment permissions the client needs for a full deploy. settings:objects:* are
# best-effort (post-install remote-grail + outbound allowlist) — if the client lacks
# them SSO 400s the full request and we retry with the minimal set.
OAUTH_DEPLOY_SCOPES_FULL = ("app-engine:apps:install app-engine:apps:run "
                            "settings:objects:read settings:objects:write")
OAUTH_DEPLOY_SCOPES_MIN = "app-engine:apps:install app-engine:apps:run"
OAUTH_UNDEPLOY_SCOPES = "app-engine:apps:delete"


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


@router.post("/api/deploy/oauth")
async def deploy_with_oauth(body: dict, x_auth_user: str | None = Header(default=None)):
    """BOOTSTRAP deploy/undeploy with an account-level OAuth client — transient, Orbital
    stores NOTHING. The tenant admin pastes the client once to land the app; we mint a
    bearer, deploy, probe whether the client CAN mint platform tokens (so the UI can tell
    the admin to finish setup inside the app), and discard every credential. The client is
    NEVER written to Redis/disk/logs. After this the admin configures the client inside the
    app (app-state, tenant-local); the app then self-mints user tokens and self-updates,
    handing Orbital only short-lived install bearers.

    Like /api/deploy/token, NOT org-member-gated: the OAuth client IS the account's
    authority."""
    user = x_auth_user or "anonymous"
    action = body.get("action", "deploy")
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    tenant = (body.get("tenant") or "").strip()
    cid = (body.get("clientId") or "").strip()
    csec = (body.get("clientSecret") or "").strip()
    account_urn = (body.get("accountUrn") or "").strip()
    # Optional attribution: the Register Tenant form asks the admin for their email (so the
    # tenant-attribution registry can answer "who owns this install" later) and a friendly
    # tenant name (the account name is NOT retrievable via API, so the registrant supplies
    # it). Never required.
    deployer_email = (body.get("deployerEmail") or "").strip()
    friendly_name = (body.get("friendlyName") or "").strip()
    tenant_id, domain = classify_tenant(tenant)  # 403 if not a Dynatrace domain
    if not (cid and csec):
        raise HTTPException(400, "clientId and clientSecret are required.")
    if not account_urn.startswith("urn:dtaccount:"):
        raise HTTPException(400, "accountUrn must look like urn:dtaccount:<uuid>.")
    sso_url = (body.get("ssoUrl") or "").strip() or SSO_TOKEN_URL_BY_DOMAIN.get(
        domain, SSO_TOKEN_URL_BY_DOMAIN["prod"])

    # 1. Deploy bearer — full scope set first, minimal on SSO 400 (client lacks settings:*).
    scope_warnings: list[str] = []
    if action == "undeploy":
        token, st, err = await _oauth_bearer(sso_url, cid, csec, account_urn, OAUTH_UNDEPLOY_SCOPES)
    else:
        token, st, err = await _oauth_bearer(sso_url, cid, csec, account_urn, OAUTH_DEPLOY_SCOPES_FULL)
        if token is None and st == 400:
            token, st, err = await _oauth_bearer(sso_url, cid, csec, account_urn, OAUTH_DEPLOY_SCOPES_MIN)
            if token:
                scope_warnings.append(
                    "client lacks settings:objects:read/write — remote-grail + outbound "
                    "allowlist will be skipped; grant those environment permissions for a full install.")
    if token is None:
        await _audit(user, tenant_id, action, "oauth-error", via="oauth-bootstrap",
                     client_id=cid, status=st, detail=err)
        raise HTTPException(502, f"OAuth token request failed (HTTP {st}): {err}. "
                                 f"Check client id/secret, the account URN, and that the client has "
                                 f"app-engine:apps:install + app-engine:apps:run on this environment.")

    if action == "undeploy":
        ok, msg = await _run_undeploy(token, tenant)
        del token
        await _audit(user, tenant_id, "undeploy", "undeployed" if ok else "undeploy-error",
                     via="oauth-bootstrap", client_id=cid, detail=msg)
        if not ok:
            raise HTTPException(502, f"Undeploy failed: {msg}")
        return {"ok": True, "tenant": tenant_id, "action": "undeploy"}

    res = await _deploy_with_status(token, tenant)
    allowlist = ""
    remote_grail = ""
    orbital_cfg = ""
    if res["status"] != "error":
        allowlist = await _ensure_outbound_allowlist(token, tenant)
        remote_grail = await _ensure_remote_grail(token, tenant)
        orbital_cfg = await _ensure_orbital_config(token, tenant)
    del token
    if res["status"] == "error":
        await _audit(user, tenant_id, "deploy", "deploy-error", via="oauth-bootstrap",
                     client_id=cid, rc=res.get("rc"))
        raise HTTPException(502, f"Deploy failed (exit {res.get('rc')}): {res.get('output','')}")

    # 2. Mint probe — can this client mint platform tokens? (Advisory only: tells the admin
    #    the client is ready to paste INTO the app. We store nothing either way.)
    mint_bearer, mint_st, _ = await _oauth_bearer(sso_url, cid, csec, account_urn, MINT_SCOPE)
    mint_ready = mint_bearer is not None
    if mint_bearer:
        del mint_bearer
    del csec  # discard the secret — never persisted
    if not mint_ready:
        scope_warnings.append(
            f"token minting NOT available (SSO HTTP {mint_st}): the client lacks the account "
            f"permissions platform-token:tokens:write + platform-token:tokens:manage. Grant them "
            f"before configuring the client inside the app, or hands-on labs can't mint per-user tokens.")

    reg = await _register_in_content_service(user, tenant)
    profile = (reg or {}).get("profile")
    url = _app_url(tenant)
    warnings = _scope_warnings(allowlist, remote_grail, orbital_cfg) + scope_warnings
    # The bootstrap path is the ONE moment Orbital ever sees the account URN + client id —
    # record them (attribution only, never the secret) before they are discarded.
    await tenant_registry.record_deploy(
        _pool(), tenant_id, "oauth-bootstrap", account_urn=account_urn, client_id=cid,
        deployer=deployer_email or (x_auth_user or ""), friendly_name=friendly_name,
        app_version=res.get("to") or "")
    await _audit(user, tenant_id, "deploy", res["status"], via="oauth-bootstrap", client_id=cid,
                 **{k: res[k] for k in ("from", "to") if res.get(k)}, url=url, profile=profile,
                 allowlist=allowlist, remote_grail=remote_grail, orbital_config=orbital_cfg,
                 mint_ready=mint_ready, warnings=warnings)
    return {"ok": True, "tenant": tenant_id, "status": res["status"], "from": res.get("from"),
            "version": res.get("to"), "url": url, "profile": profile, "allowlist": allowlist,
            "remote_grail": remote_grail, "orbital_config": orbital_cfg,
            "mintReady": mint_ready, "warnings": warnings}


@router.get("/api/deploy/audit")
async def deploy_audit(limit: int = 50, x_auth_user: str | None = Header(default=None)):
    _require_writer(x_auth_user)
    rows = await _pool().lrange(AUDIT_KEY, 0, max(0, min(limit, 500) - 1))
    return {"audit": [json.loads(r) for r in rows]}


@router.get("/api/deploy/mint-clients")
async def mint_clients(x_auth_user: str | None = Header(default=None)):
    """Read-only: the account OAuth clients configured for gen3 platform-token minting,
    per domain. Returns the client_id + account URN only — NEVER the secret — so an admin
    can see which client is in use and rotate it in myaccount if needed."""
    _require_writer(x_auth_user)
    out = []
    for dom in ("SPRINT", "DEV", "PROD"):
        cid = os.environ.get(f"MINT_CLIENT_ID_{dom}")
        if cid:
            out.append({"domain": dom.lower(), "clientId": cid,
                        "account": os.environ.get(f"MINT_RESOURCE_{dom}", ""),
                        "sso": os.environ.get(f"MINT_SSO_{dom}", ""), "scope": "domain (env, legacy)"})
    # No per-tenant clients are stored on Orbital by design — self-managed tenants hold
    # their own OAuth client inside the app (app-state), and mint locally. This lists only
    # the legacy per-domain env clients used for tenants we own (COE/SRO/sprint).
    return {"mintClients": out}


def _page(msg: str, ok: bool) -> str:
    color = "#2da44e" if ok else "#f85149"
    return (f"<!doctype html><html><head><meta charset=utf-8><title>Deploy</title></head>"
            f"<body style='font-family:system-ui;background:#0d1117;color:#e6edf3;padding:40px'>"
            f"<h2 style='color:{color}'>{'✓' if ok else '✗'} App deploy</h2><p>{msg}</p>"
            f"<p><a style='color:#9d9dff' href='/#register'>← back</a></p></body></html>")

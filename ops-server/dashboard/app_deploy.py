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
import shutil
from datetime import datetime, timedelta, timezone
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
from provisioning.sso import DEFAULT_SSO, discover_sso as _discover_sso
from shared.log_safety import safe_error_detail, scrub_for_log

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
    """Discover the tenant's SSO origin.

    Re-exported from ``provisioning.sso`` so the deploy path and the token
    provisioner cannot drift apart again — they did, and the provisioner's copy
    was formatting the *tenant* URL into a token endpoint.
    """
    return await _discover_sso(tenant_url)


async def _audit(user: str, tenant: str, action: str, result: str, **extra) -> None:
    rec = {"user": user, "tenant": tenant, "action": action, "result": result,
           "ts": datetime.now(timezone.utc).isoformat(), **extra}
    try:
        p = _pool()
        await p.lpush(AUDIT_KEY, json.dumps(rec))
        await p.ltrim(AUDIT_KEY, 0, 499)
    except Exception as exc:  # never let auditing break the flow
        log.warning("audit write failed: %s", scrub_for_log(exc))
    # token is never part of `rec`
    log.info("DEPLOY-AUDIT %s", scrub_for_log(json.dumps(rec), limit=1000))


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
        log.warning("capability probe failed for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
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


# WE DO NOT SEED THE ORBITAL TOKEN ANY MORE, so its absence is not a warning.
#
# The app ships its own bearer (dynatrace-app-enablements
# api/_orbital-baked-token.ts, since 0c030fa / 2026-08-06). getOrbitalToken()
# resolves the tenant's `orbital-config` object → ORBITAL_TOKEN env (dt-app dev
# only) → the bearer compiled into the bundle, and EVERY Orbital call the app
# makes goes through that one resolver — /api/live/* as much as /api/arena/*. An
# unseeded tenant provisions, mints and runs workshops exactly like a seeded one.
#
# This file used to emit "ACTION REQUIRED — Orbital token not seeded … workshops
# and live sessions fail immediately", and did so on a fresh sprint tenant that
# had already provisioned a training and minted tokens. The seed call fails by
# design on any tenant we do not own: an app function invoked by an EXTERNAL
# bearer runs with the CALLER's permissions, and app-settings:objects:write is
# not grantable to an OAuth client at all. Warning about a step that cannot
# succeed and does not need to teaches operators to skim the warnings list,
# which is how the one that matters gets missed.
#
# So: skipped / failed / error / unverified produce NOTHING. The raw status is
# still returned as the `orbital_config` field and written to the audit record,
# so a deploy can still be diagnosed. Only `seed refused` warns — see below.


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
    if (orbital_config or "").startswith("app cannot reach Orbital"):
        # The one that was misdiagnosed for a whole bootcamp. Nothing on this
        # server is wrong; the tenant's JS runtime is refusing the egress, and
        # the fix is on the tenant. Said plainly so nobody goes looking at
        # /home/ops/.env again.
        warnings.append(
            "ACTION REQUIRED — this tenant's app cannot reach Orbital "
            "(autonomous-enablements.whydevslovedynatrace.com). Nothing is wrong with "
            "ORBITAL_TOKEN on the server: add the host to Settings > Outbound "
            "connections on this environment (schema "
            "builtin:dt-javascript-runtime.allowed-outbound-connections) and redeploy. "
            "Until then the app cannot provision environments or run workshops.")
    elif (orbital_config or "").startswith("seed refused"):
        # The only genuinely actionable branch. The app asked Orbital about the
        # token Orbital itself sent, and Orbital said no — which means the value
        # in ORBITAL_TOKEN is stale. The bearer baked into the shipped app is
        # normally that same value, so a mismatch here is the one failure mode
        # the default bearer does NOT cover: unseeded tenants 401 everywhere.
        warnings.append(
            f"ACTION REQUIRED — Orbital token not seeded ({orbital_config}). This one is on "
            f"this server, not the tenant: ORBITAL_TOKEN in /home/ops/.env is not a value "
            f"Orbital itself accepts. Fix it there and re-deploy. This is also the one case "
            f"the app's baked bearer does not cover — if the server token was rotated, the "
            f"shipped one is stale too and every unseeded tenant will 401 on Orbital until "
            f"the app is rebuilt with the new value (api/_orbital-baked-token.ts).")
    # No branch for skipped/failed/error. A seed that did not happen is not an
    # event: the app runs on its baked bearer, and the write fails by design on
    # any tenant we do not own (external bearer → caller's permissions → the
    # ungrantable app-settings:objects:write). A warning nobody can act on is
    # noise that teaches operators to skim the list, which is how the one
    # warning that matters gets missed. The raw status is still returned as the
    # `orbital_config` field and written to the audit record, so nothing is lost.
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

    Runs under _TREE_LOCK. It mutates the same working tree the build reads, so
    without the lock a second tenant's sync could reset the tree mid-build — a
    race that existed at concurrency 1 and would be routine at 8.
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
    tree_lock, _ = _deploy_locks()
    async with tree_lock:
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


async def _stamp_ui_version(env: dict) -> str:
    """Regenerate ui/app/enablement/config/version.ts from app.config.json before building.

    The app repo wires `scripts/update-version.cjs` as the npm `prebuild`/`predeploy` hook, but we
    invoke `node_modules/.bin/dt-app deploy` directly, so npm lifecycle hooks never fire. Without
    this the shipped bundle carries whatever APP_VERSION was last committed by hand while
    app.config.json — the version the registry and `/api/deploy/latest-version` report — moves
    ahead. Tenants then sit permanently on "update available": the deploy really succeeds, but
    Admin keeps displaying the stale baked-in version, so the check never clears (COE showed
    1.0.306 while running 1.0.310).

    Best-effort: on any failure the deploy proceeds — a stale version string is better than a
    blocked deploy, and `git reset --hard` in `_sync_repo()` restores the file next time.
    """
    script = Path(APP_REPO_DIR) / "scripts" / "update-version.cjs"
    if not script.exists():
        return "update-version.cjs not found"
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", str(script), cwd=APP_REPO_DIR, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, OSError) as exc:
        return f"version stamp failed: {exc}"
    return out.decode(errors="replace").strip()


# Every deploy runs `dt-app` in ONE shared checkout (APP_REPO_DIR): _sync_repo()
# does a `git reset --hard` in it, _stamp_ui_version() rewrites a file in it, and
# the build writes into its dist/. Two deploys at once therefore do not merely
# queue -- they interleave inside the same working tree, and the failure mode is
# a tenant receiving a bundle built from someone else's state. There was no
# guard of any kind here, so a bootcamp morning where many tenants self-update
# at once was a silent-corruption risk, not a slow-but-correct one.
#
# Two primitives, because the tree is mutated for seconds and uploaded from for
# minutes:
#
#   _TREE_LOCK   serialises everything that MUTATES the checkout -- the git
#                sync, the version stamp, the build, and the instant a sandbox
#                is hardlinked out of it.
#   _UPLOAD_SEM  bounds concurrent uploads. An upload holds NO tree lock at all,
#                because it runs out of its own private sandbox.
#
# MEASURED 2026-08-16: two builds of one commit with different
# DT_APP_ENVIRONMENT_URL values produced 33/33 byte-identical files under dist/.
# The tenant is a deploy-target concern, not a build input -- that is what makes
# "build once, upload N times" correct rather than merely faster. The only tenant
# hostname in the bundle is COE, hardcoded in source as the analytics home, and
# it is identical in both builds. RE-RUN that check after any dt-app upgrade;
# the procedure is phase 0a of docs/PROVISIONING-AND-LANES.md.
DEPLOY_UPLOAD_CONCURRENCY = int(os.environ.get("DEPLOY_UPLOAD_CONCURRENCY", "8"))

# Created on first use inside the running loop, not at import: an asyncio
# primitive binds to the loop that first awaits it, and a module-level one then
# raises "bound to a different event loop" in any process that runs more than
# one loop. The service has a single loop; its tests do not.
_TREE_LOCK: asyncio.Lock | None = None
_UPLOAD_SEM: asyncio.Semaphore | None = None
_PRIMITIVE_LOOP = None


def _deploy_locks() -> tuple[asyncio.Lock, asyncio.Semaphore]:
    """(tree lock, upload semaphore) for the currently running loop."""
    global _TREE_LOCK, _UPLOAD_SEM, _PRIMITIVE_LOOP
    loop = asyncio.get_running_loop()
    if _PRIMITIVE_LOOP is not loop or _TREE_LOCK is None or _UPLOAD_SEM is None:
        _TREE_LOCK = asyncio.Lock()
        _UPLOAD_SEM = asyncio.Semaphore(DEPLOY_UPLOAD_CONCURRENCY)
        _PRIMITIVE_LOOP = loop
    return _TREE_LOCK, _UPLOAD_SEM
# (head_sha, app_version) of the build currently in dist/. A second tenant on the
# same commit waits for the lock, finds this fresh, and uploads without building.
_BUILD_STAMP: tuple[str, str] | None = None

# Sandboxes are hardlink snapshots, so they MUST sit on the same filesystem as
# the checkout -- os.link across devices raises EXDEV. A sibling directory
# guarantees that; the copy fallback below exists only so a misconfigured root
# degrades to slow rather than broken.
DEPLOY_SANDBOX_ROOT = os.environ.get(
    "DEPLOY_SANDBOX_ROOT", str(Path(APP_REPO_DIR).parent / ".deploy-sandboxes"))

# Never hardlinked into a sandbox. `.dt-app` is the important one: dt-app derives
# its token cache path as <root>/.dt-app/.tokens.json with no env override, so
# giving every upload its own root is what makes it STRUCTURALLY impossible for
# two tenants to share a credential file. (dt-app 1.9.0 also short-circuits on
# DT_APP_PLATFORM_TOKEN and never consults that cache on our route -- but that is
# a third-party code path that an upgrade could change, and this does not depend
# on it.) `.env` is excluded because an upload has no business reading it.
_SANDBOX_SKIP = {
    "node_modules", ".git", ".dt-app", ".env", ".venv",
    ".pytest_cache", "graphify-out", ".claude", "coverage", ".nyc_output",
}


def _child_env() -> dict:
    """Env every dt-app child needs. Carries no credential — callers add one."""
    return {**os.environ,
            "DT_APP_DEACTIVATE_SPINNER": "1", "CI": "1",
            # node lives in /usr/local/bin (symlink); ensure it's on PATH for the systemd service
            "PATH": "/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", "/home/ops")}


def _dt_app_binary() -> Path:
    return Path(APP_REPO_DIR) / "node_modules" / ".bin" / "dt-app"


async def _head_sha() -> str:
    """Short HEAD of the deploy checkout, or "" when it is not a git tree."""
    if not (Path(APP_REPO_DIR) / ".git").is_dir():
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", APP_REPO_DIR, "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (asyncio.TimeoutError, OSError):
        return ""
    return out.decode(errors="replace").strip() if proc.returncode == 0 else ""


def _link_tree(src: Path, dst: Path) -> None:
    """Hardlink-copy ``src`` to ``dst``, skipping the entries in _SANDBOX_SKIP.

    Hardlinks rather than copies because a sandbox is created per upload and the
    tree is ~17 MB: linking is near-instant and costs no disk. It is safe here
    because an upload only ever READS the linked files -- dt-app is invoked with
    --skip-build, so nothing rewrites dist/ in place.
    """
    def _ignore(directory, names):
        # Only prune at the top level: a nested directory legitimately named
        # "coverage" inside ui/ is source, not a cache.
        return set(names) & _SANDBOX_SKIP if Path(directory) == src else set()

    def _link_or_copy(a, b):
        try:
            os.link(a, b)
        except OSError:
            # EXDEV (sandbox root on another filesystem) or a link-count limit.
            # Slower, still correct.
            shutil.copy2(a, b)

    shutil.copytree(src, dst, copy_function=_link_or_copy, ignore=_ignore,
                    symlinks=True, dirs_exist_ok=True)


def _build_sandbox(dest: Path) -> None:
    """A private, self-contained project root for one upload.

    Everything dt-app reads is present; the one thing it must not share is. The
    build metadata under .dt-app/build IS copied (it is a build artefact and the
    deploy refuses to run without it) while .dt-app/.tokens.json is NOT (it is a
    credential).
    """
    repo = Path(APP_REPO_DIR)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _link_tree(repo, dest)

    (dest / ".dt-app").mkdir(parents=True, exist_ok=True)
    build_meta = repo / ".dt-app" / "build"
    if build_meta.is_dir():
        _link_tree(build_meta, dest / ".dt-app" / "build")
    schema = repo / ".dt-app" / "app.config.schema.json"
    if schema.is_file():
        shutil.copy2(schema, dest / ".dt-app" / "app.config.schema.json")

    # node_modules is symlinked, never copied: it is ~1 GB and read-only here.
    link = dest / "node_modules"
    if not link.exists():
        link.symlink_to(repo / "node_modules")


async def _ensure_build() -> tuple[bool, str]:
    """Build ``dist/`` once for the commit currently checked out.

    Holds _TREE_LOCK for the whole build, so a second tenant arriving mid-build
    waits and then finds the stamp fresh instead of rebuilding. This is the
    "build once" half of build-once/upload-many.
    """
    global _BUILD_STAMP
    binary = _dt_app_binary()
    if not binary.exists():
        return False, f"dt-app not found in {APP_REPO_DIR} (is the app repo checked out with node_modules?)"

    env = _child_env()
    tree_lock, _ = _deploy_locks()
    waited_from = asyncio.get_event_loop().time()
    async with tree_lock:
        waited = asyncio.get_event_loop().time() - waited_from
        if waited > 1.0:
            log.info("build tree was busy for %.1fs before this deploy", waited)

        want = (await _head_sha(), _app_version())
        if _BUILD_STAMP == want and (Path(APP_REPO_DIR) / "dist").is_dir():
            return True, f"reusing build {want[1]}@{want[0] or 'nogit'}"

        await _stamp_ui_version(env)
        proc = await asyncio.create_subprocess_exec(
            str(binary), "build", "--non-interactive", "--no-color",
            cwd=APP_REPO_DIR, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=DEPLOY_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            # Reap the killed child inside the lock; releasing while it still
            # holds file handles would hand the next deploy a half-written dist/.
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                log.error("build child did not die after kill")
            _BUILD_STAMP = None
            return False, "build timed out"

        if proc.returncode:
            # A failed build must never leave a stamp behind, or the next tenant
            # would upload whatever stale dist/ survived.
            _BUILD_STAMP = None
            return False, out.decode(errors="replace")[-1500:]

        # Re-read the version: _stamp_ui_version regenerates it from app.config.json.
        _BUILD_STAMP = (want[0], _app_version())
        return True, f"built {_BUILD_STAMP[1]}@{_BUILD_STAMP[0] or 'nogit'}"


async def _upload(token: str, tenant_url: str) -> tuple[int, str]:
    """Ship the already-built bundle to one tenant, from a private sandbox.

    The token is passed through the child env only, never logged and never
    written to disk. The sandbox is what keeps two concurrent uploads from
    sharing dt-app's token cache; it is removed on every exit path.
    """
    binary = _dt_app_binary()
    if not binary.exists():
        return 127, f"dt-app not found in {APP_REPO_DIR}"

    env = {**_child_env(), "DT_APP_PLATFORM_TOKEN": token,
           "DT_APP_ENVIRONMENT_URL": tenant_url}
    sandbox = Path(DEPLOY_SANDBOX_ROOT) / f"deploy-{secrets.token_hex(6)}"

    tree_lock, upload_sem = _deploy_locks()
    async with upload_sem:
        try:
            # Snapshot under the tree lock so a concurrent build can never be
            # observed half-written; the link itself takes milliseconds.
            async with tree_lock:
                await asyncio.to_thread(_build_sandbox, sandbox)

            # No lock from here on -- this sandbox is nobody else's.
            proc = await asyncio.create_subprocess_exec(
                str(binary), "deploy", "--skip-build", "--non-interactive", "--no-color",
                "--environment-url", tenant_url,
                cwd=str(sandbox), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=DEPLOY_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    log.error("deploy child for %s did not die after kill",
                              scrub_for_log(tenant_url))
                return 124, "deploy timed out"
            return proc.returncode or 0, out.decode(errors="replace")[-1500:]
        finally:
            await asyncio.to_thread(shutil.rmtree, sandbox, True)


async def _run_deploy(token: str, tenant_url: str) -> tuple[int, str]:
    """Build once if needed, then upload to this tenant.

    Kept as one function so both routes and `_deploy_with_status` are unchanged.
    """
    if not _dt_app_binary().exists():
        return 127, f"dt-app not found in {APP_REPO_DIR} (is the app repo checked out with node_modules?)"
    ok, msg = await _ensure_build()
    if not ok:
        return 1, msg
    return await _upload(token, tenant_url)


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
        log.warning("installed-version check failed for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
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
# The tenant's own copy of its account OAuth client, so the app can mint per-learner
# platform tokens and update itself without Orbital holding anything.
# settings/schemas/mint-client.schema.json in the app repo.
MINT_CLIENT_SCHEMA = "app:my.dynatrace.enablements:mint-client"
MINT_CLIENT_SCHEMA_VERSION = "1.0.0"
# Per-tenant instructor allowlist — settings/schemas/instructors.schema.json in the app
# repo. Seeded here with the account admin who registers the tenant, so a self-service
# tenant recognises its own admin as an instructor without a code change (the baked
# instructors.json can never list the 70 SEs installing on their own tenants).
INSTRUCTORS_SCHEMA = "app:my.dynatrace.enablements:instructors"
INSTRUCTORS_SCHEMA_VERSION = "1.0.0"
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

# Non-prod realms mint through their OWN SSO and Account-Management hosts, and the
# prod pair above does not cover them. Leaving them out is a silent chicken-and-egg:
# mintCredentials verifies the client against SSO *before* storing it, the verify is
# blocked by the allowlist, so the client is never stored — and the old comment said
# these hosts get added "when the app is configured there", which can therefore never
# happen. Measured on sprint 2026-08-06:
#   "client cannot mint platform tokens — … Blocked request to
#    'sso-sprint.dynatracelabs.com' (host not in allowlist)"
# dev is deliberately absent: its realm hosts are unverified, and guessing one would
# put a wrong entry in a security allowlist. Add it once a dev tenant is exercised.
REALM_OUTBOUND_HOSTS: dict[str, list[str]] = {
    "sprint": ["sso-sprint.dynatracelabs.com",
               "api-hardening.internal.dynatracelabs.com"],
}


def _outbound_hosts_for(tenant_url: str) -> list[str]:
    """Hosts this tenant's app functions must reach, prod baseline + its own realm."""
    _, domain = classify_tenant(tenant_url)
    return OUTBOUND_HOSTS + REALM_OUTBOUND_HOSTS.get(domain, [])


async def _ensure_outbound_allowlist(token: str, tenant_url: str,
                                     extra_hosts: list[str] | None = None,
                                     proven_blocked: bool = False) -> str:
    """If the tenant enforces a JS-runtime outbound allowlist (sprint/dev do, prod usually
    doesn't), add the content-delivery hosts so the app's functions can reach Orbital + GitHub.
    Only ever adds hosts to an existing enforced list — never creates or tightens a restriction.
    Best-effort; needs settings:objects:read+write on the token."""
    base = tenant_url.rstrip("/") + "/platform/classic/environment-api/v2/settings/objects"
    h = {"Authorization": f"Bearer {token}"}
    # `extra_hosts` is where the realm the app will ACTUALLY authenticate against comes
    # from: the ssoUrl/apiHost of the client being installed. REALM_OUTBOUND_HOSTS only
    # knows the realms we happen to have met, so a tenant in an unlisted one would store
    # a client and then fail every mint at the allowlist — the same chicken-and-egg the
    # sprint entry was added to fix, one layer up.
    wanted = _outbound_hosts_for(tenant_url)
    for extra in extra_hosts or []:
        if extra and extra not in wanted:
            wanted = wanted + [extra]
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
                # `proven_blocked` means the app itself just told us, from inside
                # this tenant's runtime, that it cannot reach a required host.
                # Measured on uxn36332 (2026-08-19): NO settings object at ANY
                # scope, and the app still answered
                #   Blocked request to 'autonomous-enablements…' (host not in allowlist)
                # So "prod + no object = outbound open" is false, and refusing to
                # create here left the tenant permanently broken. Creating a list
                # of exactly the hosts the app needs is strictly MORE permissive
                # than a default-deny, so it repairs rather than tightens — but
                # only ever on proof, never on the guess that got us here.
                if domain not in ("sprint", "dev") and not proven_blocked:
                    return "no allowlist object (prod — outbound not verified)"
                cr = await c.post(base, headers={**h, "Content-Type": "application/json"}, json=[{
                    "schemaId": OUTBOUND_SCHEMA, "scope": "environment",
                    "value": {"allowedOutboundConnections": {"enforced": True, "hostList": list(wanted)}},
                }])
                if cr.status_code in (200, 201):
                    return f"created outbound allowlist with {len(wanted)} host(s)"
                return f"allowlist create failed (HTTP {cr.status_code}: {cr.text[:120]})"
            obj = items[0]
            aoc = (obj.get("value") or {}).get("allowedOutboundConnections", {})
            if not aoc.get("enforced"):
                return "outbound not enforced (open)"
            hosts = list(aoc.get("hostList", []))
            missing = [x for x in wanted if x not in hosts]
            if not missing:
                return "allowlist already complete"
            hosts.extend(missing)
            pr = await c.put(f"{base}/{obj['objectId']}", headers={**h, "Content-Type": "application/json"},
                             json={"value": {"allowedOutboundConnections": {"enforced": True, "hostList": hosts}}})
            if pr.status_code in (200, 201, 204):
                return f"added {len(missing)} host(s) to the outbound allowlist"
            return f"allowlist update failed (HTTP {pr.status_code})"
    except Exception as exc:
        log.warning("outbound allowlist for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
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
        log.warning("remote-grail for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
        return f"remote-grail error: {exc}"


async def _store_mint_client(token: str, tenant_url: str, client_id: str, client_secret: str,
                             account_urn: str, sso_url: str, api_host: str) -> str:
    """Write the pasted account OAuth client into the TENANT'S OWN `mint-client` settings
    object, so from here on the app mints its own per-learner tokens and its own install
    bearer. Orbital keeps nothing: the secret is in memory for this one call and is deleted
    by the caller immediately after.

    This is the step whose absence made every new tenant half-broken. It looks impossible if
    you go through the app-settings API — `app-settings:objects:write` is genuinely not in
    the OAuth client scope catalog. But app settings and classic settings are the SAME
    objects (measured on ydi9582h: the app's `remote-grail` object has an identical objectId
    through both APIs), and the classic door opens with `settings:objects:write`, which every
    account client can hold. `_ensure_remote_grail` has been walking through that door on
    every deploy the whole time.

    Idempotent, and re-registering a tenant deliberately overwrites: the admin just supplied
    this client, so it is the freshest statement of intent.

    Best-effort — returns a human-readable status; never raises, and never logs the secret.
    """
    base = tenant_url.rstrip("/") + "/platform/classic/environment-api/v2/settings/objects"
    h = {"Authorization": f"Bearer {token}"}
    value = {"clientId": client_id, "clientSecret": client_secret,
             "accountUrn": account_urn, "ssoUrl": sso_url, "apiHost": api_host}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            # A freshly installed app's schemas are not queryable the instant the install
            # returns — same race the orbital-config seeding hits. Retry before concluding
            # the tenant cannot hold a client.
            r = None
            for pause in (0, 5, 10):
                if pause:
                    await asyncio.sleep(pause)
                r = await c.get(base, headers=h, params={
                    "schemaIds": MINT_CLIENT_SCHEMA, "scopes": "environment",
                    "fields": "objectId"})
                if r.status_code == 200:
                    break
            if r is None or r.status_code == 403:
                return "skipped (token lacks settings:objects:read/write)"
            if r.status_code != 200:
                return f"skipped (settings read HTTP {r.status_code})"
            items = r.json().get("items", [])
            if items:
                pr = await c.put(f"{base}/{items[0]['objectId']}",
                                 headers={**h, "Content-Type": "application/json"},
                                 json={"value": value})
                if pr.status_code in (200, 201, 204):
                    return "updated (app mints + self-updates on its own)"
                return f"update failed (HTTP {pr.status_code}: {pr.text[:120]})"
            cr = await c.post(base, headers={**h, "Content-Type": "application/json"}, json=[{
                "schemaId": MINT_CLIENT_SCHEMA, "schemaVersion": MINT_CLIENT_SCHEMA_VERSION,
                "scope": "environment", "value": value,
            }])
            if cr.status_code in (200, 201):
                return "stored (app mints + self-updates on its own)"
            return f"create failed (HTTP {cr.status_code}: {cr.text[:120]})"
    except Exception as exc:
        log.warning("mint-client store for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
        return f"mint-client error: {exc}"


def _email_from_bearer(token: str) -> str | None:
    """The `email` claim of a JWT bearer, lower-cased. The account OAuth client's
    client-credentials token carries the CREATOR's email (measured on scu37051:
    asad.ali@dynatrace.com), which is exactly the Dynatrace login that will sign into the
    app — so it is the right identity to seed as this tenant's instructor. Best-effort."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        email = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
        return email if "@" in email else None
    except Exception:
        return None


async def _store_instructors(token: str, tenant_url: str, emails: list[str]) -> str:
    """Merge `emails` into the tenant's own `instructors` settings object (union with any
    already there — never clobber an admin-curated list). Same classic-settings door as
    `_store_mint_client`: `settings:objects:write`, which every account client can hold.
    Best-effort; returns a human-readable status and never raises."""
    wanted = sorted({e.strip().lower() for e in emails if e and "@" in e})
    if not wanted:
        return "skipped (no instructor email to seed)"
    base = tenant_url.rstrip("/") + "/platform/classic/environment-api/v2/settings/objects"
    h = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = None
            for pause in (0, 5, 10):
                if pause:
                    await asyncio.sleep(pause)
                r = await c.get(base, headers=h, params={
                    "schemaIds": INSTRUCTORS_SCHEMA, "scopes": "environment",
                    "fields": "objectId,value"})
                if r.status_code == 200:
                    break
            if r is None or r.status_code == 403:
                return "skipped (token lacks settings:objects:read/write)"
            if r.status_code != 200:
                return f"skipped (settings read HTTP {r.status_code})"
            items = r.json().get("items", [])
            existing = []
            if items:
                existing = list((items[0].get("value") or {}).get("emails") or [])
            merged = sorted({*(str(e).strip().lower() for e in existing), *wanted})
            value = {"emails": merged}
            if items:
                if {str(e).strip().lower() for e in existing} >= set(wanted):
                    return f"unchanged ({len(existing)} instructor(s) already set)"
                pr = await c.put(f"{base}/{items[0]['objectId']}",
                                 headers={**h, "Content-Type": "application/json"},
                                 json={"value": value})
                return ("updated (%d instructor(s))" % len(merged)) if pr.status_code in (200, 201, 204) \
                    else f"update failed (HTTP {pr.status_code}: {pr.text[:120]})"
            cr = await c.post(base, headers={**h, "Content-Type": "application/json"}, json=[{
                "schemaId": INSTRUCTORS_SCHEMA, "schemaVersion": INSTRUCTORS_SCHEMA_VERSION,
                "scope": "environment", "value": value,
            }])
            return ("stored (%d instructor(s))" % len(merged)) if cr.status_code in (200, 201) \
                else f"create failed (HTTP {cr.status_code}: {cr.text[:120]})"
    except Exception as exc:
        log.warning("instructors store for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
        return f"instructors error: {exc}"


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
            # NOT "rejected". The app could not reach Orbital at all, which says
            # nothing about the token. Before the app distinguished these, every
            # blocked tenant was reported as a bad server token: 26 times on
            # 2026-08-19 alone, while that token was demonstrably valid (probed:
            # HTTP 200). Two SEs spent a delivery debugging the wrong system.
            "unreachable": "app cannot reach Orbital from this tenant — outbound blocked",
            "missing-token": "skipped (ORBITAL_TOKEN not configured)",
        }.get(status) or await _explain_unseeded(token, tenant_url, status, (body or {}).get("detail", ""))
    except Exception as exc:
        log.warning("seedOrbitalConfig on %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
        return f"unverified (app function error: {exc})"


# The app declares app-settings:objects:write and the write still 403s, because an app
# function invoked by an external bearer runs with the CALLER's permissions — and that
# scope is not in the account-client catalog at all (SSO 400s the request; measured on
# SRO, COE and sprint). So this specific failure is a property of the platform, not of
# our configuration, and printing the raw permission error made every deploy of an
# unseeded tenant read as broken. Say what is true instead, and check it.
_UNGRANTABLE_WRITE = "app-settings:objects:write"


async def _read_orbital_config(token: str, tenant_url: str) -> bool | None:
    """Is orbital-config seeded on this tenant? None when we cannot tell.

    Needs app-settings:objects:read, which IS grantable to an account client (unlike its
    write counterpart) and is requested on the first rung of the scope ladder."""
    base = tenant_url.rstrip("/") + "/platform/app-settings/v2/objects"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(base, headers={
                "Authorization": f"Bearer {token}",
                "Dt-App-Context": APP_ID,
                "Dt-App-Version": _app_version(),
            }, params={"schema-id": ORBITAL_SCHEMA, "add-fields": "value"})
        if r.status_code != 200:
            return None
        items = r.json().get("items", [])
        return bool(items and (items[0].get("value") or {}).get("token"))
    except Exception as exc:
        log.warning("orbital-config read for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
        return None


async def _explain_unseeded(token: str, tenant_url: str, status: str, detail: str) -> str:
    """Report the tenant's actual orbital-config state instead of a platform error string."""
    if _UNGRANTABLE_WRITE in (detail or ""):
        seeded = await _read_orbital_config(token, tenant_url)
        if seeded:
            return "already configured"
        if seeded is False:
            return ("not seeded — the app runs on its baked bearer "
                    f"({_UNGRANTABLE_WRITE} is not grantable to an account OAuth client)")
        return ("unverified — cannot read app settings "
                f"({_UNGRANTABLE_WRITE} is not grantable to an account OAuth client)")
    return f"seed via app function: {status or 'unknown'} {detail}".strip()


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
        log.warning("orbital-config for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
        return f"orbital-config error: {exc}"


async def _register_in_content_service(user: str, tenant_url: str) -> dict | None:
    """Best-effort: add the tenant to the delivery table so its content can be managed."""
    try:
        return await register_tenant({"tenant": tenant_url}, x_auth_user=user)
    except Exception as exc:
        log.warning("register-tenant failed for %s: %s",
                    scrub_for_log(tenant_url), scrub_for_log(exc))
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


async def _deploy_token_flow(user: str, body: dict) -> dict:
    """The whole token/auto deploy, factored out of the route so it can also be run in the
    background by /api/deploy/token-start. Raises HTTPException exactly as the route does."""
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
    selftest: dict = {"status": "unknown", "blocked": [], "detail": "deploy failed"}
    if res["status"] != "error":
        allowlist = await _ensure_outbound_allowlist(token, tenant)  # use token before discarding
        # Same gate as the bootstrap path: prove the app can reach what it needs
        # from inside the tenant's runtime, and repair the allowlist on proof of
        # a block. This is the route "Update now" and the auto-deploys use, so
        # without it the MAIN deploy path would keep shipping unverified installs.
        selftest = await _selftest_and_repair(token, tenant)
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
    if selftest.get("status") == "blocked":
        blocked = ", ".join(selftest.get("blocked") or [])
        if not allow_partial:
            await _audit(user, tenant_id, "deploy", "outbound-blocked", via=via, detail=blocked)
            raise HTTPException(412,
                f"App {res.get('to') or 'version'} installed, but this tenant's app runtime "
                f"cannot reach: {blocked}. Until that is fixed the app CANNOT provision "
                f"environments, mint learner tokens or run workshops. "
                + (selftest.get("detail") or "")
                + " Send allowPartial:true to accept the install anyway.")
        warnings.append("outbound blocked, overridden by allowPartial: " + blocked)
    elif selftest.get("status") == "unknown":
        warnings.append(f"outbound NOT verified ({selftest.get('detail')}) — the install is "
                        "unproven until this check passes.")
    await tenant_registry.record_deploy(
        _pool(), tenant_id, "auto" if auto else "token",
        deployer="" if user == "anonymous" else user, app_version=res.get("to") or "")
    await _audit(user, tenant_id, "deploy", res["status"], via=via,
                 **{k: res[k] for k in ("from", "to") if res.get(k)}, url=url, profile=profile,
                 allowlist=allowlist, remote_grail=remote_grail, orbital_config=orbital_cfg,
                 selftest=selftest, warnings=warnings)
    return {"ok": True, "tenant": tenant_id, "status": res["status"], "from": res.get("from"),
            "version": res.get("to"), "url": url, "profile": profile, "credential": source,
            "allowlist": allowlist, "remote_grail": remote_grail, "selfTest": selftest,
            "orbital_config": orbital_cfg, "warnings": warnings}


@router.post("/api/deploy/token")
async def deploy_with_token(body: dict, x_auth_user: str | None = Header(default=None)):
    """Override path for ANY tenant (customer / prospect / free trial / cross-account): the
    caller supplies a platform token created IN the target tenant (scopes apps:install/run/
    delete). That credential carries the target account's authority, so no SSO/account binding
    is needed. The token is used once and discarded — never logged or persisted.

    An empty token means "use the account OAuth client Orbital holds for this tenant"
    (COE / SRO / sprint) — the tokenless auto-deploy path.

    NOT org-member-gated: the platform token IS the authority to deploy on the target tenant,
    so a signed-out user holding a valid token may deploy/register (nginx routes this one path
    with opportunistic auth — see ops-server.conf `location = /api/deploy/token`). We audit the
    GitHub identity when nginx forwards one (signed-in org member), else "anonymous"."""
    return await _deploy_token_flow(x_auth_user or "anonymous", body)


# ── Background variant of the above, for the app's in-app "Update now" ───────────
# A deploy is a 1–2 minute build; a Dynatrace app function is capped at ~120 s, and the
# deploy itself swaps the app out from under any in-flight function. So the app starts the
# deploy here, gets an id back immediately, and polls token-status until done — the same
# start/poll shape as /api/arena/sessions/{id}/exec-start.
DEPLOY_JOB_TTL = 3600


def _deploy_job_key(deploy_id: str) -> str:
    return f"deploy:job:{deploy_id}"


@router.post("/api/deploy/token-start")
async def deploy_with_token_start(body: dict, x_auth_user: str | None = Header(default=None)):
    """Start a token/auto deploy in the background. Returns {deployId} straight away.

    Argument validation that is cheap and certain (bad action, non-Dynatrace tenant) still
    fails synchronously, so an obviously wrong call never becomes a job the caller has to
    poll. Everything after that — credential choice, scope preflight, build, install — runs
    in the task and surfaces through token-status."""
    action = body.get("action", "deploy")
    if action not in ("deploy", "undeploy"):
        raise HTTPException(400, "action must be deploy or undeploy.")
    classify_tenant((body.get("tenant") or "").strip())  # 403 if not a Dynatrace domain

    user = x_auth_user or "anonymous"
    deploy_id = secrets.token_urlsafe(12)
    key = _deploy_job_key(deploy_id)
    await _pool().setex(key, DEPLOY_JOB_TTL, json.dumps({"done": False, "action": action}))

    async def _run() -> None:
        try:
            result = await _deploy_token_flow(user, body)
            payload = {"done": True, "ok": True, **result}
        except HTTPException as exc:
            payload = {"done": True, "ok": False, "status": exc.status_code,
                       "error": str(exc.detail)}
        except Exception as exc:  # never leave the caller polling a job that will never finish
            log.exception("background deploy %s failed", deploy_id)
            payload = {"done": True, "ok": False, "status": 500, "error": str(exc)}
        try:
            await _pool().setex(key, DEPLOY_JOB_TTL, json.dumps(payload))
        except Exception:
            log.exception("could not record result of background deploy %s", deploy_id)

    asyncio.ensure_future(_run())
    return {"deployId": deploy_id, "done": False}


@router.get("/api/deploy/token-status/{deploy_id}")
async def deploy_with_token_status(deploy_id: str):
    """Poll a deploy started via token-start. {done:false} while running; when done,
    {done:true, ok:true, ...} carries the same body /api/deploy/token returns, and
    {done:true, ok:false, status, error} carries the HTTP error it would have raised."""
    raw = await _pool().get(_deploy_job_key(deploy_id))
    if not raw:
        raise HTTPException(404, "Unknown or expired deploy id.")
    return json.loads(raw)


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
# Environment permissions the client needs for a full deploy. settings:objects:* are
# best-effort (post-install remote-grail + outbound allowlist) — if the client lacks
# them SSO 400s the full request and we retry with the minimal set.
# Three rungs, tried in order. `app-settings:objects:read` is what lets a deploy say
# whether the tenant's orbital-config is seeded instead of guessing; it is grantable on
# every account client measured (SRO/COE/sprint, 2026-08-17). Its WRITE counterpart is
# not grantable to ANY account client — SSO answers 400 invalid_request — which is why
# seeding cannot happen from here at all; see _ensure_orbital_config.
OAUTH_DEPLOY_SCOPES_VERIFY = ("app-engine:apps:install app-engine:apps:run "
                              "settings:objects:read settings:objects:write "
                              "app-settings:objects:read")
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


# ─── Account name / plan — opportunistic, NEVER a required scope ─────────────────────
#
# docs/EPIC-002-ws-d-tenant-identity.md established that neither the account's display
# name nor its commercial plan is reachable for a foreign tenant: they need account-scoped
# reads on the customer's OWN account, which the 15-scope register list deliberately does
# not ask for. Scopes cannot be added to an existing OAuth client, so requiring one more
# would force every already-registered tenant to create a new client and register again.
#
# So we ask, and accept "no". Some clients are created with broad account rights and will
# answer; most will not. A blank name is the EXPECTED outcome, not a failure — the UI
# falls back to the friendlyName the registrant typed. This must never raise, never block
# a deploy, and never lengthen it by more than its own timeouts.
# Two INDEPENDENT account scopes, verified against the COE account 2026-08-17.
# They are not interchangeable and a client may hold either, both or neither:
#   account-env-read → GET /env/v2/accounts/{uuid}/environments  (the display name)
#   account-uac-read → GET /sub/v2/accounts/{uuid}/subscriptions (the commercial plan)
# account-idm-read reaches NEITHER — it only opens /iam/v1/accounts/{uuid}/users,
# and the bare GET /iam/v1/accounts/{uuid} does not exist at all (404). Mint the two
# separately so a client holding one still yields half the answer.
ACCOUNT_ENV_SCOPE = "account-env-read"
ACCOUNT_SUB_SCOPE = "account-uac-read"


async def _probe_account_name(sso_url: str, cid: str, csec: str, account_urn: str,
                              api_host: str, env_id: str = "") -> tuple[str, str, str]:
    """(environment_name, plan, reason) — best effort, two independent probes.

    `reason` explains a blank for the deploy response; it is diagnostic text for a
    human, never an error condition. A client that holds neither account scope (the
    common case) makes this return ("", "", …) and the UI falls back to the
    registrant-supplied friendlyName."""
    uuid = account_urn.rsplit(":", 1)[-1].strip()
    if not uuid:
        return "", "", "no account uuid in the URN"
    name, name_reason = await _probe_env_name(sso_url, cid, csec, account_urn,
                                              api_host, uuid, env_id)
    plan, plan_reason = await _probe_plan(sso_url, cid, csec, account_urn,
                                          api_host, uuid)
    return name, plan, "; ".join(x for x in (name_reason, plan_reason) if x)


async def _probe_env_name(sso_url, cid, csec, account_urn, api_host, uuid,
                          env_id) -> tuple[str, str]:
    """The environment's display name from the account's environment list.

    Named per ENVIRONMENT, not per account, and that is the right granularity:
    a tenant is registered as one environment, and an account holding several
    would otherwise all show the same label. Requires account-env-read."""
    if not env_id:
        return "", "no environment id to match"
    # SSO 400s a scope the client does not hold, and does it with an EMPTY
    # error_description — so the status code is the whole signal. Do not log the body.
    token, st, _ = await _oauth_bearer(sso_url, cid, csec, account_urn, ACCOUNT_ENV_SCOPE)
    if token is None:
        return "", f"client lacks {ACCOUNT_ENV_SCOPE} (SSO HTTP {st})"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{api_host}/env/v2/accounts/{uuid}/environments",
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return "", f"environment list HTTP {r.status_code}"
        rows = (r.json() or {}).get("data") or []
        for row in rows:
            if isinstance(row, dict) and str(row.get("id") or "").strip() == env_id:
                return str(row.get("name") or "").strip(), ""
        # Do NOT fall back to rows[0] when there is exactly one environment: the
        # account may simply be a different one from the tenant being registered,
        # and a confidently wrong name is worse than none.
        return "", f"{env_id} not in the account's {len(rows)} environment(s)"
    except Exception as exc:
        return "", f"environment list failed: {exc}"
    finally:
        del token


async def _probe_plan(sso_url, cid, csec, account_urn, api_host, uuid) -> tuple[str, str]:
    """Commercial plan from the account's subscriptions. Requires account-uac-read —
    a DIFFERENT scope from the environment list, so one can answer without the other."""
    token, st, _ = await _oauth_bearer(sso_url, cid, csec, account_urn, ACCOUNT_SUB_SCOPE)
    if token is None:
        return "", f"client lacks {ACCOUNT_SUB_SCOPE} (SSO HTTP {st})"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{api_host}/sub/v2/accounts/{uuid}/subscriptions",
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return "", f"subscriptions HTTP {r.status_code}"
        return _plan_from_subscriptions(r.json()), ""
    except Exception as exc:
        return "", f"subscriptions probe failed: {exc}"
    finally:
        del token


def _plan_from_subscriptions(payload) -> str:
    """`paid` | `trial` | `free` | "" from the subscription list.

    Shape verified against the COE account 2026-08-17:
      {"data": [{"uuid", "name", "type": "FREE", "subType": "PROSPECT|TRIAL",
                 "status": "ACTIVE|EXPIRED", "startTime", "endTime"}]}

    Only ACTIVE rows count — an account whose every subscription has EXPIRED says
    nothing about what it is entitled to today, so that answers "" rather than a
    stale label. An unrecognised shape also answers "": a wrong guess about a
    customer's commercial status is worse than an empty cell."""
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return ""
    live = [s for s in items
            if isinstance(s, dict) and str(s.get("status") or "").upper() == "ACTIVE"]
    if not live:
        return ""
    types = {str(s.get("type") or "").upper() for s in live}
    types.discard("")
    if not types:
        return ""
    if types - {"FREE"}:
        return "paid"
    subs = {str(s.get("subType") or "").upper() for s in live}
    return "trial" if "TRIAL" in subs else "free"


# ─── Registration preflight (2026-08-11 — HANDOFF_TOKEN_AND_DOCUMENT_IDENTITY §8.9) ───
#
# A granted scope is not proof. A 200 from the mint API is not proof. The only evidence a
# tenant can hand a learner a WORKING token is minting one and using it where the Operator
# will — so that is what registration does now, BEFORE anything is installed. Everything
# the preflight creates is deleted before it returns.

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


def _preflight_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def _preflight_learner_tokens(sso_url: str, cid: str, csec: str, tenant: str,
                                    tenant_id: str, domain: str, account_urn: str,
                                    api_host: str) -> dict:
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
                                                   CLASSIC_MINT_SCOPE)
            if bearer is None:
                detail.append(f"classic path unavailable: SSO refused {CLASSIC_MINT_SCOPE} "
                              f"(HTTP {st})")
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
                detail.append(f"platform path unavailable: SSO refused the account mint "
                              f"permissions (HTTP {st2})")
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


# ── Verified capability, not inferred capability ─────────────────────────────
#
# Everything below exists because the deploy used to REPORT capabilities it had
# never exercised, and the APAC bootcamp (2026-08-19) cashed in every one of
# those guesses at once. The rule these follow:
#
#   Never report a capability that was inferred. Either resolve it against the
#   effective-permissions API, or exercise it for real. Anything that cannot be
#   established is reported as UNKNOWN — never as fine.

EFFECTIVE_PERMISSIONS_PATH = "/platform/management/v1/effective-permissions:resolve"


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


async def _preflight_activegate(sso_url: str, cid: str, csec: str, tenant: str,
                                tenant_id: str) -> tuple[bool, str]:
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
                    refusals.append(f"{scope}: SSO HTTP {st}")
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


async def _selftest_outbound(token: str, tenant_url: str) -> dict:
    """Ask the APP, from inside the tenant's own JS runtime, what it can reach.

    This replaces an inference that was simply wrong. `_ensure_outbound_allowlist`
    reads the allowlist settings object and concludes that a prod tenant with no
    object has open egress. On 2026-08-19 twelve tenants got that verdict; eight
    of them never provisioned anything, and on jxh41488 a learner's mint died on

        Blocked request to 'sso.dynatrace.com' (host not in allowlist)

    an hour after the deploy recorded `no allowlist object (prod — outbound open)`.

    There is no API that returns the effective allowlist and no way to ask
    whether a host would be permitted, so the only instrument that answers is a
    real outbound call from inside the runtime.

    Returns {"status": ..., "blocked": [...], "detail": ...}. `status` is
    "ok" | "blocked" | "unknown" — and "unknown" is deliberately not "ok": an
    app too old to carry the function, or one we could not reach, has not
    proven anything. Only a definite "blocked" is treated as a failure, so a
    tenant is never refused over a check that did not run.
    """
    fn = (f"{tenant_url.rstrip('/')}/platform/app-engine/app-functions/v1/apps/"
          f"{APP_ID}/api/selfTest")
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        # An app is not routable the instant its install returns — the first call
        # after an upgrade answers "App not found" and the same call seconds later
        # succeeds. Same ladder as _seed_via_app_function, for the same reason.
        r = None
        for pause in (0, 5, 10):
            if pause:
                await asyncio.sleep(pause)
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(fn, headers=hdr, json={})
            if r.status_code in (200, 404):
                break
        if r.status_code == 404:
            return {"status": "unknown", "blocked": [],
                    "detail": "installed app has no selfTest function yet (pre-1.0.351)"}
        if r.status_code != 200:
            return {"status": "unknown", "blocked": [],
                    "detail": f"selfTest unreachable (HTTP {r.status_code})"}
        body = r.json() if r.content else {}
        blocked = list(body.get("blocked") or [])
        if blocked:
            return {"status": "blocked", "blocked": blocked,
                    "detail": body.get("remedy") or "outbound hosts blocked"}
        if body.get("ok"):
            return {"status": "ok", "blocked": [], "detail": "all required hosts reachable"}
        # Reachability could not be proven for every host, but nothing was
        # definitively blocked — a transient network error, most likely.
        unreachable = [h.get("host") for h in (body.get("hosts") or [])
                       if h.get("outcome") != "ok"]
        return {"status": "unknown", "blocked": [],
                "detail": f"could not prove reachability for: {', '.join(filter(None, unreachable))}"}
    except Exception as exc:
        log.warning("selfTest on %s: %s", scrub_for_log(tenant_url), scrub_for_log(exc))
        return {"status": "unknown", "blocked": [], "detail": f"selfTest error: {exc}"}


async def _selftest_and_repair(token: str, tenant_url: str,
                               extra_hosts: list[str] | None = None) -> dict:
    """Self-test; if hosts are blocked, write the allowlist and test again.

    The repair only ever runs on PROOF of a block, which is what makes it safe
    to do on a customer's prod tenant: we are restoring egress the app already
    needs, never tightening a tenant that was open. `_ensure_outbound_allowlist`
    itself only ever ADDS hosts to an existing enforced list.

    The second self-test is not ceremony. The propagation delay between writing
    that settings object and the runtime honouring it is undocumented, so the
    only way to know the repair worked is to ask again.
    """
    result = await _selftest_outbound(token, tenant_url)
    if result["status"] != "blocked":
        return result
    log.warning("outbound blocked on %s: %s — repairing allowlist",
                scrub_for_log(tenant_url), scrub_for_log(", ".join(result["blocked"])))
    repair = await _ensure_outbound_allowlist(token, tenant_url, extra_hosts=extra_hosts,
                                             proven_blocked=True)
    for pause in (2, 5, 10):
        await asyncio.sleep(pause)
        again = await _selftest_outbound(token, tenant_url)
        if again["status"] == "ok":
            again["detail"] = f"outbound repaired ({repair}); all required hosts reachable"
            again["repaired"] = True
            return again
        result = again
        if again["status"] != "blocked":
            break
    result["repair"] = repair
    return result


async def _preflight_documents(sso_url: str, cid: str, csec: str, tenant: str,
                               tenant_id: str) -> tuple[bool, str]:
    """The path the content importer uses: create a document AS THE APP's service
    identity (env-scoped client-credentials bearer) and delete it again. This is what
    failed on Asad's tenant while every scope readback said fine."""
    bearer, st, err = await _oauth_bearer(sso_url, cid, csec,
                                          f"urn:dtenvironment:{tenant_id}", DOC_SCOPE)
    if bearer is None:
        return False, f"SSO refused the document scopes (HTTP {st}): {err}"
    base = f"{tenant.rstrip('/')}/platform/document/v1/documents"
    hdr = {"Authorization": f"Bearer {bearer}"}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(base, headers=hdr,
                             data={"name": "enbl-preflight", "type": "enablement-preflight"},
                             files={"content": ("content", b"{}", "application/json")})
            if r.status_code not in (200, 201):
                return False, f"document create refused (HTTP {r.status_code}): {r.text[:160]}"
            d = r.json()
            doc_id, ver = d.get("id"), d.get("version", 1)
            if doc_id:
                await c.delete(f"{base}/{doc_id}", headers=hdr,
                               params={"optimistic-locking-version": str(ver)})
            return True, "document created and deleted as the service identity"
    except httpx.HTTPError as e:
        return False, f"document probe error: {e}"


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
    # Optional attribution. The form no longer asks for the registrant's email — the
    # client-credentials JWT carries the client CREATOR's address, so asking was both
    # redundant and a free-text field nobody could verify (_email_from_bearer below).
    # What it does ask for is the one thing no API can answer: who this tenant is FOR.
    # `friendlyName` stays as the fallback label when the account name cannot be read.
    audience_raw = (body.get("audience") or "").strip()
    audience = tenant_registry.normalize_audience(audience_raw)
    if audience_raw and not audience:
        raise HTTPException(400, "audience must be one of: "
                                 + ", ".join(tenant_registry.AUDIENCES) + ".")
    friendly_name = (body.get("friendlyName") or "").strip()
    tenant_id, domain = classify_tenant(tenant)  # 403 if not a Dynatrace domain
    if not (cid and csec):
        raise HTTPException(400, "clientId and clientSecret are required.")
    if not account_urn.startswith("urn:dtaccount:"):
        raise HTTPException(400, "accountUrn must look like urn:dtaccount:<uuid>.")
    sso_url = (body.get("ssoUrl") or "").strip() or SSO_TOKEN_URL_BY_DOMAIN.get(
        domain, SSO_TOKEN_URL_BY_DOMAIN["prod"])

    scope_warnings: list[str] = []
    api_host = ACCOUNT_API_BY_DOMAIN.get(domain, ACCOUNT_API_BY_DOMAIN["prod"])
    allow_partial = bool(body.get("allowPartial"))

    # 0. PREFLIGHT (deploy only) — refuse and install NOTHING when this tenant+client
    #    cannot hand a learner a working token or cannot own its content documents.
    #    HTTP 412 names what failed; allowPartial:true is the explicit human override.
    preflight: dict = {}
    if action == "deploy":
        learner = await _preflight_learner_tokens(
            sso_url, cid, csec, tenant, tenant_id, domain, account_urn, api_host)
        docs_ok, docs_detail = await _preflight_documents(sso_url, cid, csec, tenant, tenant_id)
        # Every Kubernetes training needs a dt0g02. This used to be a post-install
        # warning; hpm49270 proved a warning is not enough (see _preflight_activegate).
        ag_ok, ag_detail = await _preflight_activegate(sso_url, cid, csec, tenant, tenant_id)
        preflight = {"learnerTokenTier": learner["tier"], "learnerDetail": learner["detail"],
                     "documentsReady": docs_ok, "documentsDetail": docs_detail,
                     "activeGateReady": ag_ok, "activeGateDetail": ag_detail}
        failures = []
        if learner["tier"] == "none":
            failures.append(f"no working learner-token path — {learner['detail']}")
        if not docs_ok:
            failures.append(f"content documents cannot be written as the app — {docs_detail}")
        if not ag_ok:
            failures.append(ag_detail)
        if failures:
            if not allow_partial:
                await _audit(user, tenant_id, "deploy", "preflight-refused",
                             via="oauth-bootstrap", client_id=cid, detail=" | ".join(failures))
                raise HTTPException(412,
                    "Preflight refused — nothing was installed. " + " | ".join(failures)
                    + " Scopes cannot be added to an existing OAuth client: create a new one "
                      "with the full 15-scope list (verify it first at "
                      "https://autonomous-enablements-check.whydevslovedynatrace.com), then "
                      "register again — or send allowPartial:true to install anyway.")
            scope_warnings.append("preflight failures overridden by allowPartial: "
                                  + " | ".join(failures))

    # 1. Deploy bearer — full scope set first, minimal on SSO 400 (client lacks settings:*).
    if action == "undeploy":
        token, st, err = await _oauth_bearer(sso_url, cid, csec, account_urn, OAUTH_UNDEPLOY_SCOPES)
    else:
        token, st, err = await _oauth_bearer(sso_url, cid, csec, account_urn, OAUTH_DEPLOY_SCOPES_VERIFY)
        if token is None and st == 400:
            # Client cannot read app settings — everything still installs, the deploy
            # just cannot report whether orbital-config is seeded.
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

    # Registrant identity, taken HERE and not at the instructor-seeding call below, for
    # two reasons: that call sits inside `if res["status"] != "error"`, and `del token`
    # runs before the registry write at the end of this function. Deriving it once, the
    # moment a bearer exists, is what makes the email available on every path.
    client_email = _email_from_bearer(token) or ""

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
    selftest: dict = {"status": "unknown", "blocked": [], "detail": "deploy failed"}
    docs_admin = ""
    mint_client = "skipped (deploy failed)"
    instructors = "skipped (deploy failed)"
    mint_ready = False
    mint_st = 0
    # The app authenticates this client against the SSO and account-API hosts on every
    # mint and every self-update, and live-probes minted tokens against the LIVE host.
    # If the tenant enforces an outbound allowlist and they are not on it, storing the
    # client succeeds and everything that uses it fails.
    live_host = urlparse(LIVE_HOST_BY_DOMAIN.get(
        domain, LIVE_HOST_BY_DOMAIN["prod"]).format(tid=tenant_id)).hostname
    realm_hosts = [h for h in (urlparse(sso_url).hostname, urlparse(api_host).hostname,
                               live_host) if h]
    account_name = ""
    plan = ""
    account_detail = ""
    if res["status"] != "error":
        allowlist = await _ensure_outbound_allowlist(token, tenant, extra_hosts=realm_hosts)
        # PROVE the app can reach what it needs, from inside the tenant's own
        # runtime. The allowlist write above is a best guess based on a settings
        # object; this is the only thing that knows whether it worked. Repairs on
        # proof of a block, then re-tests. See _selftest_and_repair.
        selftest = await _selftest_and_repair(token, tenant, extra_hosts=realm_hosts)
        # `document:documents:admin` is stamped by SSO without an entitlement
        # check, and a tenant where it is not EFFECTIVE cannot update a lab
        # document that already exists under another owner — which is how four
        # trainings went missing on bos01241 while every readback said "held".
        # NOT asked with `token`: the resolve API answers for the presented
        # bearer, and the deploy bearer holds no document scopes, so that would
        # report "not effective" on every tenant. See _documents_admin_effective.
        docs_admin = await _documents_admin_effective(
            sso_url, cid, csec, tenant, tenant_id)
        remote_grail = await _ensure_remote_grail(token, tenant)
        orbital_cfg = await _ensure_orbital_config(token, tenant)

        # 1b. Account display name + commercial plan, IF this client happens to carry
        #     account-scoped reads. Blank is the normal answer — see _probe_account_name.
        account_name, plan, account_detail = await _probe_account_name(
            sso_url, cid, csec, account_urn, api_host, tenant_id)

        # 2. Can this client mint platform tokens? Storing one that cannot would install a
        #    credential that fails at the first hands-on launch instead of here.
        mint_bearer, mint_st, _ = await _oauth_bearer(sso_url, cid, csec, account_urn, MINT_SCOPE)
        mint_ready = mint_bearer is not None
        if mint_bearer:
            del mint_bearer

        # 2b. The ActiveGate check moved to the PREFLIGHT (_preflight_activegate),
        #     where it mints a real dt0g02 across both scope families and refuses
        #     the deploy when neither works. It used to live here: an SSO-bearer
        #     probe, post-install, warning-only, and gated behind `mint_ready` so
        #     a tenant failing both produced no warning at all. hpm49270 passed
        #     it and killed a learner's session four hours later.

        # 3. Hand the client to the TENANT — the step that makes it self-sufficient. From
        #    here the app mints its own per-learner tokens and its own install bearer for
        #    "Update now", so this tenant never needs Orbital to hold a credential for it.
        #    Stored whenever the preflight proved a learner-token tier: with classic-first
        #    the client is what mints CLASSIC tokens too, so "cannot mint platform tokens"
        #    is no longer a reason to withhold it.
        if preflight.get("learnerTokenTier") in ("classic", "platform") or mint_ready:
            mint_client = await _store_mint_client(
                token, tenant, cid, csec, account_urn, sso_url, api_host)
        else:
            mint_client = "skipped (client cannot mint learner tokens)"

        # 4. Seed the account admin as an instructor ON THIS TENANT. The baked
        #    instructors.json can never list the SEs installing on their own tenants, so
        #    without this every one of them (measured: asad.ali@dynatrace.com on scu37051)
        #    is refused "Only instructors can import content" on their own tenant. The
        #    email is the client creator's (JWT `email` claim = the Dynatrace login that
        #    signs into the app), plus the Register-Tenant form's deployer email if given.
        seed_emails = [e for e in (client_email,) if e]
        instructors = await _store_instructors(token, tenant, seed_emails)
    del token
    del csec  # discard the secret — never persisted
    if res["status"] == "error":
        await _audit(user, tenant_id, "deploy", "deploy-error", via="oauth-bootstrap",
                     client_id=cid, rc=res.get("rc"))
        raise HTTPException(502, f"Deploy failed (exit {res.get('rc')}): {res.get('output','')}")

    # An app that cannot reach Orbital is not a working install, and shipping it as
    # one is exactly what happened to Edrick Leong (bth17199) and Cruz Lim
    # (uxn36332): the deploy reported success, the app could not call out, and the
    # only signal they got blamed a server they do not own. Refuse, and name the
    # hosts. The app stays installed — uninstalling would destroy app-state on a
    # tenant that may have been working on an older version — but the caller is
    # told plainly that it is not functional.
    if selftest.get("status") == "blocked" and not allow_partial:
        blocked = ", ".join(selftest.get("blocked") or [])
        await _audit(user, tenant_id, "deploy", "outbound-blocked", via="oauth-bootstrap",
                     client_id=cid, detail=blocked)
        raise HTTPException(412,
            f"App {res.get('to') or 'version'} installed, but this tenant's app runtime "
            f"cannot reach: {blocked}. Until that is fixed the app CANNOT provision "
            f"environments, mint learner tokens or run workshops. "
            + (selftest.get("detail") or "")
            + " Orbital tried to add the hosts automatically and could not "
              f"({selftest.get('repair', 'no settings access')}). Send allowPartial:true "
              "to accept the install anyway.")
    if selftest.get("status") == "blocked":
        scope_warnings.append("outbound blocked, overridden by allowPartial: "
                              + ", ".join(selftest.get("blocked") or []))
    elif selftest.get("status") == "unknown":
        scope_warnings.append(
            f"outbound NOT verified ({selftest.get('detail')}). This is the check that "
            "catches a tenant whose app cannot reach Orbital; treat the install as "
            "unproven until it passes.")
    if docs_admin == "false":
        scope_warnings.append(
            "ACTION REQUIRED — document:documents:admin is granted by SSO but NOT "
            "effective on this environment. The app cannot update a lab document that "
            "already exists under another owner, so those trainings will silently stay "
            "missing from the catalog (measured on bos01241: 4 trainings). Bind an IAM "
            "policy carrying document:documents:admin to the OAuth client's service user "
            "at environment level.")

    if not mint_ready and preflight.get("learnerTokenTier") == "classic":
        scope_warnings.append(
            f"platform-token FALLBACK not available (SSO HTTP {mint_st}): the client lacks "
            f"the account permissions platform-token:tokens:write + platform-token:tokens:"
            f"manage. Classic minting through the client works today; if this environment "
            f"later retires classic API-token creation, launches will refuse. Create a new "
            f"client with the full 15-scope list to be future-proof.")
    elif not mint_ready:
        scope_warnings.append(
            f"ACTION REQUIRED — token minting NOT available (SSO HTTP {mint_st}): the client "
            f"lacks the account permissions platform-token:tokens:write + "
            f"platform-token:tokens:manage. On an environment that still allows classic API "
            f"tokens the app will mint those through the stored client and labs will work; on "
            f"an environment where classic creation has been retired (it is rolled out per "
            f"environment) every hands-on launch will refuse. Grant the two permissions and "
            f"register the tenant again.")
    if (mint_ready or preflight.get("learnerTokenTier") in ("classic", "platform")) \
            and not mint_client.startswith(("stored", "updated")):
        scope_warnings.append(
            f"ACTION REQUIRED — the OAuth client could NOT be stored on this tenant "
            f"({mint_client}). Until it is, this tenant cannot mint per-learner platform "
            f"tokens and cannot update itself from inside the app. Grant the client "
            f"settings:objects:read + settings:objects:write on this environment and register "
            f"the tenant again, or paste the client by hand in the app under "
            f"Settings → Training Token Minting.")

    reg = await _register_in_content_service(user, tenant)
    profile = (reg or {}).get("profile")
    url = _app_url(tenant)
    warnings = _scope_warnings(allowlist, remote_grail, orbital_cfg) + scope_warnings
    # The bootstrap path is the ONE moment Orbital ever sees the account URN + client id —
    # record them (attribution only, never the secret) before they are discarded.
    await tenant_registry.record_deploy(
        _pool(), tenant_id, "oauth-bootstrap", account_urn=account_urn, client_id=cid,
        deployer=client_email or (x_auth_user or ""), friendly_name=friendly_name,
        app_version=res.get("to") or "", audience=audience,
        account_name=account_name, plan=plan)
    await _audit(user, tenant_id, "deploy", res["status"], via="oauth-bootstrap", client_id=cid,
                 **{k: res[k] for k in ("from", "to") if res.get(k)}, url=url, profile=profile,
                 allowlist=allowlist, remote_grail=remote_grail, orbital_config=orbital_cfg,
                 mint_ready=mint_ready, mint_client=mint_client, instructors=instructors,
                 preflight=preflight, selftest=selftest, documents_admin=docs_admin,
                 warnings=warnings)
    return {"ok": True, "tenant": tenant_id, "status": res["status"], "from": res.get("from"),
            "version": res.get("to"), "url": url, "profile": profile, "allowlist": allowlist,
            "selfTest": selftest, "documentsAdminEffective": docs_admin,
            "remote_grail": remote_grail, "orbital_config": orbital_cfg,
            "mintReady": mint_ready, "mintClient": mint_client, "instructors": instructors,
            "registrant": client_email, "audience": audience,
            "accountName": account_name, "plan": plan, "accountDetail": account_detail,
            "preflight": preflight, "warnings": warnings}


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
            f"<p><a style='color:#9d9dff' href='/#register'>← back</a></p></body></html>")

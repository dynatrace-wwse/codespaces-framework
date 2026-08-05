"""Server-side content reconciliation (EPIC-006 E6b / epic W5).

WHY THIS MOVED OFF THE LEARNER
------------------------------
Content used to converge because every user's first page load ran a profile
sync in their browser. That was two problems wearing one coat:

  * a thundering herd — push one content commit before a 300-seat bootcamp and
    all 300 first page loads start importing at the same moment, and
  * a privileged write on a learner session, which stopped being tolerable the
    moment content became app-owned rather than caller-owned.

The instructor gate in `import-lab.function.ts` fixed both, and left a gap:
content then only converged when an instructor happened to open the app. This
module closes it — Orbital drives the reconciliation itself, on a schedule, with
no user involved.

HOW IT AUTHENTICATES
--------------------
There is no user, so neither identity path in the import gate can speak for it.
It proves itself with the Orbital service token instead: the token travels in
the request body and the app asks Orbital (`GET /api/service/verify`) whether it
is real. Only Orbital knows a valid one, so this cannot be forged tenant-side.

The transport is the same app-function invocation the deploy uses for
`seedOrbitalConfig` — a deploy credential holding `app-engine:apps:run` may call
an app function from outside.

SCOPE — AND ITS LIMIT, STATED PLAINLY
-------------------------------------
This only covers tenants Orbital holds a credential for, which today means the
same set it auto-deploys to (COE, SRO, sprint). A tenant that installed the app
by pasting its own token leaves Orbital no way to call back into it, so those
keep converging the old way: an instructor opens the app, or someone presses
Admin → Refresh. That is a real limitation of the credential model, not an
oversight, and `sync_all` reports those tenants as `no-credential` rather than
quietly counting them as done.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("ops.content_sync")

APP_ID = "my.dynatrace.enablements"

# Six hours: content changes are pushes to a handful of repos, not a stream, and
# the sync is a no-op when nothing moved (unchanged shas are skipped below). A
# tighter loop would buy minutes of freshness for a constant trickle of calls.
DEFAULT_INTERVAL_S = 6 * 3600

# Long: an import walks a whole repo, and a cold pack build is not fast. Well
# under the loop interval, so a slow tenant can never overlap its own next run.
IMPORT_TIMEOUT_S = 300

# Don't reconcile the instant the service starts. A restart during an incident
# would otherwise add a full import sweep across every tenant to whatever else is
# going wrong, and a deploy restarts this process.
STARTUP_DELAY_S = 120

# How many identical failures before we call it a credential problem and stop.
SYSTEMATIC_FAILURE_THRESHOLD = 3


def _signature(error: str) -> str:
    """Collapse an error to its shape, so per-repo noise (names, errorRefs) does
    not make three instances of one problem look like three problems."""
    import re
    return re.sub(r'"[^"]*"|[0-9a-f]{8}-[0-9a-f-]{27}|^[^:]+: ', "", error).strip()


def orbital_token() -> str:
    """The service token the app will verify us by. Empty disables the sync."""
    return (os.environ.get("ORBITAL_TOKEN") or "").split(",")[0].strip()


def auto_tenants() -> list[str]:
    """Tenants Orbital can call back into, i.e. the ones it holds credentials for."""
    from dashboard import app_deploy as dep
    return [t for t in (dep.COE_TENANT_URL, dep.SRO_TENANT_URL, dep.SPRINT_TENANT_URL) if t]


def import_payload(source: dict, token: str) -> dict:
    """The import request for one profile source.

    Mirrors what the app's own `syncProfile` sends, so a scheduled import and a
    hand-triggered one produce identical documents:
      * `useContentService` — fetch through Orbital's proxy, so the tenant needs
        no GitHub token of its own.
      * `isPublic` — profile content is env-wide, not private to an importer.
      * `resetProgress: false` — reconciliation must never wipe learner progress.
    """
    return {
        "repoUrl": f"https://github.com/{source.get('repo', '')}",
        "useContentService": True,
        "contentSha": source.get("version") or "",
        "category": source.get("category") or "",
        "isPublic": True,
        "resetProgress": False,
        "orbitalToken": token,
    }


async def invoke_import(tenant_url: str, bearer: str, payload: dict) -> dict:
    """POST one import to a tenant's app. Returns the function's own JSON."""
    url = (f"{tenant_url.rstrip('/')}/platform/app-engine/app-functions/v1/apps/"
           f"{APP_ID}/api/import-lab")
    async with httpx.AsyncClient(timeout=IMPORT_TIMEOUT_S) as c:
        r = await c.post(url, headers={"Authorization": f"Bearer {bearer}",
                                       "Content-Type": "application/json"},
                         json=payload)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}", "details": r.text[:200]}
    try:
        return r.json() or {}
    except Exception:
        return {"error": "non-JSON response"}


async def sync_tenant(tenant_url: str, *, invoke=invoke_import,
                      manifest=None, credential=None) -> dict:
    """Reconcile one tenant's content. Never raises — a failure is a report."""
    token = orbital_token()
    if not token:
        return {"tenant": tenant_url, "status": "disabled",
                "detail": "ORBITAL_TOKEN not configured"}

    try:
        if manifest is None:
            from dashboard import content_service
            manifest = await content_service.get_manifest(tenant=tenant_url)
        sources = (manifest or {}).get("sources") or []
    except Exception as exc:
        return {"tenant": tenant_url, "status": "error", "detail": f"manifest: {exc}"}

    if not sources:
        return {"tenant": tenant_url, "status": "ok", "imported": 0, "failed": 0,
                "detail": "profile has no sources"}

    try:
        if credential is None:
            from dashboard import app_deploy as dep
            # NOT the deploy credential. An app function invoked from outside runs
            # with the CALLER's permissions, so an import driven from here needs
            # document + app-state scopes that a deploy bearer has no reason to
            # hold. See CONTENT_SYNC_SCOPES for what each one buys.
            token, label = await dep.content_sync_token(tenant_url)
            credential = {"token": token, "source": f"{label}-content"}
    except Exception as exc:
        return {"tenant": tenant_url, "status": "error", "detail": f"credential: {exc}"}

    bearer = (credential or {}).get("token") or ""
    if not bearer:
        # Not a failure — a tenant Orbital simply cannot reach. Named so, so it
        # is never mistaken for a successful no-op.
        return {"tenant": tenant_url, "status": "no-credential", "sources": len(sources)}

    imported, failed, errors = 0, 0, []
    for i, src in enumerate(sources):
        repo = src.get("repo", "?")
        try:
            res = await invoke(tenant_url, bearer, import_payload(src, token))
        except Exception as exc:
            res = {"error": str(exc)}
        if res.get("error"):
            failed += 1
            errors.append(f"{repo}: {res['error']}")
            log.warning("content sync %s: %s failed — %s", tenant_url, repo, res["error"])
            # A permission problem is not 22 problems. When the first few sources
            # fail the same way, the cause is the credential, not the content, and
            # grinding through the rest only buys an identical error per repo and
            # a log nobody reads. Stop and say so.
            if i + 1 == SYSTEMATIC_FAILURE_THRESHOLD and imported == 0 \
                    and len({_signature(e) for e in errors}) == 1:
                return {"tenant": tenant_url, "status": "blocked",
                        "sources": len(sources), "imported": 0, "failed": failed,
                        "detail": f"first {failed} sources failed identically — "
                                  f"treating as a credential problem, not content",
                        "errors": errors}
        else:
            imported += 1
    return {"tenant": tenant_url, "status": "ok" if not failed else "partial",
            "sources": len(sources), "imported": imported, "failed": failed,
            "errors": errors[:10]}


async def sync_all(tenants=None, **kw) -> list[dict]:
    """Reconcile every reachable tenant, sequentially.

    Sequential on purpose: each tenant's import pulls whole repos through
    Orbital's own content proxy, so running them at once would have this server
    compete with itself for the exact resource the imports depend on.
    """
    out = []
    for t in (tenants if tenants is not None else auto_tenants()):
        out.append(await sync_tenant(t, **kw))
    return out


async def sync_loop(interval_s: int = DEFAULT_INTERVAL_S) -> None:
    """Background reconciliation. Started at app startup; never lets an error
    kill the loop, because a sync that stops silently is worse than one that
    fails loudly every six hours."""
    if not orbital_token():
        log.info("content sync disabled (no ORBITAL_TOKEN)")
        return
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        started = time.monotonic()
        try:
            results = await sync_all()
            for r in results:
                log.info("content sync %s: %s", r.get("tenant"), r)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("content sync pass failed: %s", exc)
        await asyncio.sleep(max(60, interval_s - (time.monotonic() - started)))

"""Codespace launch service — run a training lab in the LEARNER'S own GitHub Codespace.

Pairs with ``github_oauth.py``. Once a learner has connected their GitHub account
(``gh:token:{dtUser}`` in Redis), the Dynatrace app calls these routes to:
  - list machine types available for a repo,
  - provision a Codespace **as that user** (so it's owned + billed to them), after
    injecting the Dynatrace tokens (DT_ENVIRONMENT / DT_OPERATOR_TOKEN / DT_INGEST_TOKEN)
    as the user's repo-scoped Codespaces secrets,
  - poll session status, terminate, and optionally make a forwarded port public.

Every GitHub call is made via the ``gh`` CLI with ``GH_TOKEN`` set to the learner's
stored token (``get_user_token``). If no token is stored, we fall back to ambient
``gh`` auth so the routes still work for local testing.

A ``job:running:{name}`` Redis hash mirrors the Arena daemon-job shape (provider,
dtUser, repo, status, created) so the existing dashboard plumbing can see it.

All env/config is read lazily, so importing this router never fails app.py.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import redis.asyncio as redis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webhook.config import REDIS_URL
from dashboard.github_oauth import get_user_token

log = logging.getLogger("ops-dashboard.codespace")

CODESPACE_JOB_TTL = int(os.environ.get("CODESPACE_JOB_TTL", "14400"))  # 4h, lazy-overridable
GH_TIMEOUT = int(os.environ.get("CODESPACE_GH_TIMEOUT", "120"))

router = APIRouter(tags=["codespace"])
_redis: redis.Redis | None = None


def _pool() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


# GitHub Codespace state → our normalized lifecycle status.
_STATE_MAP = {
    "Created": "provisioning",
    "Queued": "provisioning",
    "Provisioning": "provisioning",
    "Awaiting": "provisioning",
    "Starting": "provisioning",
    "Rebuilding": "provisioning",
    "Available": "ready",
    "Failed": "failed",
    "Shutdown": "terminated",
    "ShuttingDown": "terminated",
    "Archived": "expired",
    "Deleted": "terminated",
    "Unknown": "provisioning",
}


def _redact(text: str, *secrets: str) -> str:
    """Strip any token values out of subprocess output before it reaches a client/log."""
    out = text or ""
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


def _tenant_meta(dt_environment: str) -> tuple[str, str]:
    """Derive (tenant_id, stage) from a DT environment URL so the Orbital Running
    tab can show which tenant + whether it's production or development.
      https://geu80787.apps.dynatrace.com              -> ("geu80787", "production")
      https://abc.sprint.apps.dynatracelabs.com        -> ("abc", "sprint")
      https://abc.dev.apps.dynatracelabs.com           -> ("abc", "development")
    """
    host = (dt_environment or "").split("://", 1)[-1].split("/", 1)[0]
    tenant_id = host.split(".", 1)[0] if host else ""
    if "sprint.apps.dynatracelabs.com" in host:
        stage = "sprint"
    elif "dynatracelabs.com" in host:           # dev/hardening labs tenants
        stage = "development"
    elif "apps.dynatrace.com" in host:
        stage = "production"
    else:
        stage = "unknown"
    return tenant_id, stage


async def delete_codespace(dtUser: str, name: str) -> None:
    """Delete a learner's Codespace and clear its running record. Shared by the
    /api/codespace terminate route and the dashboard's generic terminate path."""
    await _gh(dtUser, "api", "-X", "DELETE", f"user/codespaces/{name}")
    await _pool().delete(f"job:running:{name}")
    log.info("Codespace deleted name=%s dtUser=%s", name, dtUser)


async def _gh(dtUser: str, *args: str, input: str | None = None) -> str:
    """Run the `gh` CLI as the learner. Sets GH_TOKEN to their stored token; if none,
    falls back to ambient `gh` auth (local testing). Returns stdout; raises HTTPException
    on non-zero exit, with any token values redacted from the message."""
    token = await get_user_token(_pool(), dtUser) if dtUser else None
    env = {**os.environ}
    if token:
        env["GH_TOKEN"] = token
        # Ensure the user token wins over any ambient GITHUB_TOKEN in the service env.
        env.pop("GITHUB_TOKEN", None)
    proc = await asyncio.create_subprocess_exec(
        "gh", *args, env=env,
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(
            proc.communicate(input.encode() if input is not None else None),
            timeout=GH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, f"gh {args[0] if args else ''} timed out")
    out = out_b.decode(errors="replace")
    if proc.returncode != 0:
        raise HTTPException(502, f"gh {' '.join(args[:2])} failed: {_redact(out, token or '')[-600:]}")
    return out


# ── request models ───────────────────────────────────────────────────────────

class DTEnv(BaseModel):
    DT_ENVIRONMENT: str
    DT_OPERATOR_TOKEN: str
    DT_INGEST_TOKEN: str


class ProvisionBody(BaseModel):
    dtUser: str
    repo: str
    machine: str
    ref: str | None = None
    dtEnv: DTEnv


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/api/codespace/machines")
async def list_machines(repo: str, dtUser: str = "", ref: str | None = None):
    """Machine types available to the learner for this repo:
    `gh api repos/{repo}/codespaces/machines`."""
    if not repo:
        raise HTTPException(400, "repo is required (owner/repo).")
    path = f"repos/{repo}/codespaces/machines"
    if ref:
        path += f"?ref={ref}"
    out = await _gh(dtUser, "api", path)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise HTTPException(502, "Unexpected response from GitHub machines API.")
    return {"machines": data.get("machines", data)}


@router.post("/api/codespace/provision")
async def provision(body: ProvisionBody):
    """Set the 3 DT_* values as the learner's repo-scoped Codespaces secrets, then create a
    Codespace as the learner. Records a job:running:{name} hash and returns {jobId, status,
    webUrl}."""
    if "/" not in body.repo:
        raise HTTPException(400, "repo must be owner/repo.")

    # a. Inject the Dynatrace tokens as USER codespaces secrets scoped to the repo.
    #    The gh CLI handles libsodium public-key sealing for us.
    secrets_map = {
        "DT_ENVIRONMENT": body.dtEnv.DT_ENVIRONMENT,
        "DT_OPERATOR_TOKEN": body.dtEnv.DT_OPERATOR_TOKEN,
        "DT_INGEST_TOKEN": body.dtEnv.DT_INGEST_TOKEN,
        # Signals to the framework (variables.sh) that this Codespace is orchestrated
        # by Orbital → INSTANTIATION_TYPE=orbital_codespaces → setUpTerminal installs
        # the SSH server so the in-app terminal relay can attach.
        "ORBITAL_ENVIRONMENT": "true",
    }
    for name, value in secrets_map.items():
        if not value:
            raise HTTPException(400, f"dtEnv.{name} is required.")
        await _gh(
            body.dtUser, "secret", "set", name,
            "--user", "--app", "codespaces", "--repos", body.repo,
            "--body", value,
        )

    # b. Create the Codespace as the user via REST so we get JSON incl. name + web_url.
    create_args = ["api", "-X", "POST", f"repos/{body.repo}/codespaces",
                   "-f", f"machine={body.machine}"]
    if body.ref:
        create_args += ["-f", f"ref={body.ref}"]
    out = await _gh(body.dtUser, *create_args)
    try:
        cs = json.loads(out)
    except json.JSONDecodeError:
        raise HTTPException(502, "Unexpected response from GitHub create-codespace API.")
    name = cs.get("name")
    web_url = cs.get("web_url")
    if not name:
        raise HTTPException(502, "GitHub did not return a codespace name.")

    # c. Record the running job (mirrors the Arena daemon job hash shape so the
    #    dashboard Running tab, shell, and terminate plumbing all see it).
    now = datetime.now(timezone.utc)
    tenant_id, stage = _tenant_meta(body.dtEnv.DT_ENVIRONMENT)
    redis_meta = {
        "job_id": name,
        "provider": "codespace",
        "type": "codespace",
        "dtUser": body.dtUser,
        "repo": body.repo,
        "ref": body.ref or "",
        "machine": body.machine,
        "status": "provisioning",
        "created": now.isoformat(),
        "started_at": now.isoformat(),
        "worker_id": "github-codespaces",
        "arena_user": body.dtUser,
        "arena_tenant": tenant_id,
        "stage": stage,
        "web_url": web_url or "",
    }
    await _pool().hset(f"job:running:{name}", mapping=redis_meta)
    await _pool().expire(f"job:running:{name}", CODESPACE_JOB_TTL)

    ws_url = f"wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/{name}/shell"
    log.info("Codespace provisioned name=%s repo=%s dtUser=%s stage=%s", name, body.repo, body.dtUser, stage)
    # d.
    return {"jobId": name, "status": "provisioning", "webUrl": web_url, "wsUrl": ws_url}


@router.get("/api/codespace/sessions/{name}")
async def session_status(name: str, dtUser: str = ""):
    """Map the Codespace's GitHub state to our lifecycle status and refresh the running
    hash. `gh api user/codespaces/{name}`."""
    out = await _gh(dtUser, "api", f"user/codespaces/{name}")
    try:
        cs = json.loads(out)
    except json.JSONDecodeError:
        raise HTTPException(502, "Unexpected response from GitHub codespace API.")
    gh_state = cs.get("state", "Unknown")
    status = _STATE_MAP.get(gh_state, "provisioning")
    web_url = cs.get("web_url")
    key = f"job:running:{name}"
    if await _pool().exists(key):
        await _pool().hset(key, mapping={"status": status, "gh_state": gh_state})
    return {"jobId": name, "status": status, "ghState": gh_state, "webUrl": web_url}


@router.post("/api/codespace/sessions/{name}/terminate")
async def terminate(name: str, dtUser: str = ""):
    """Delete the learner's Codespace and clear the running record."""
    await delete_codespace(dtUser, name)
    return {"status": "terminated"}


@router.post("/api/codespace/sessions/{name}/port-public")
async def port_public(name: str, dtUser: str = "", port: int = 80):
    """Make a forwarded port public so the lab app URL is reachable.
    `gh codespace ports visibility {port}:public -c {name}`."""
    await _gh(dtUser, "codespace", "ports", "visibility", f"{port}:public", "-c", name)
    return {"status": "public", "port": port}

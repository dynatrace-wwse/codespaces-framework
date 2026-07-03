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
from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from webhook.config import REDIS_URL
from dashboard.github_oauth import get_user_token

log = logging.getLogger("ops-dashboard.codespace")

CODESPACE_JOB_TTL = int(os.environ.get("CODESPACE_JOB_TTL", "14400"))  # 4h, lazy-overridable
CODESPACE_IDLE_TIMEOUT_MIN = int(os.environ.get("CODESPACE_IDLE_TIMEOUT_MIN", "60"))  # stop after 60m idle
# Max seconds to keep reporting "provisioning" after GitHub says Available while
# waiting for sshd (installed by post-create) to answer. Fail open after that.
SSH_READY_MAX_HOLD = int(os.environ.get("SSH_READY_MAX_HOLD", "600"))
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
    """Delete a learner's Codespace, clear its running record, and DESTROY the learner's
    stored GitHub credential — the token is kept (encrypted, short-lived) only to spin +
    proxy the Codespace; once it's gone, so is the credential. Shared by the
    /api/codespace terminate route and the dashboard's generic terminate path.

    Audit log records user + repo + machine only — NEVER the credential."""
    meta = await _pool().hgetall(f"job:running:{name}")
    repo, machine = meta.get("repo", ""), meta.get("machine", "")
    await _gh(dtUser, "api", "-X", "DELETE", f"user/codespaces/{name}")  # needs the token
    await _pool().delete(f"job:running:{name}")
    await _pool().delete(f"gh:token:{dtUser}")  # destroy the credential now the Codespace is gone
    log.info("Codespace deleted name=%s user=%s repo=%s machine=%s (credential destroyed)",
             name, dtUser, repo, machine)


async def reap_codespace_if_idle(dtUser: str, name: str, max_idle_min: int | None = None) -> str | None:
    """Throw away a Codespace that GitHub has stopped, or that has been idle past
    ``max_idle_min``. Returns the reason ('shutdown'/'archived'/'idle') if it deleted
    the Codespace, else None. Used by the expiry reaper — training sessions should not
    leave a stopped/abandoned Codespace lingering on the learner's account."""
    if not dtUser or not name:
        return None
    max_idle_min = max_idle_min or CODESPACE_IDLE_TIMEOUT_MIN
    try:
        cs = json.loads(await _gh(dtUser, "api", f"user/codespaces/{name}"))
    except Exception:
        return None  # transient / token gone — leave it for the next sweep
    state = (cs.get("state") or "").lower()
    reason = None
    if state in ("shutdown", "archived"):
        reason = state
    else:
        last_used = cs.get("last_used_at")
        if last_used:
            try:
                lu = datetime.fromisoformat(last_used.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - lu).total_seconds() > max_idle_min * 60:
                    reason = "idle"
            except ValueError:
                pass
    if reason:
        try:
            await _gh(dtUser, "api", "-X", "DELETE", f"user/codespaces/{name}")
            await _pool().delete(f"gh:token:{dtUser}")  # credential dies with the Codespace
            log.info("Reaped Codespace name=%s user=%s reason=%s", name, dtUser, reason)
        except Exception as exc:
            log.warning("Idle-reap delete failed for %s: %s", name, exc)
            return None
    return reason


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
    machine: str | None = None  # omit → GitHub uses the repo's devcontainer default machine
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
    #    Machine is optional: omit it so GitHub picks the repo's devcontainer default
    #    (the smallest type satisfying hostRequirements) — the repo runs as designed.
    create_args = ["api", "-X", "POST", f"repos/{body.repo}/codespaces"]
    if body.machine:
        create_args += ["-f", f"machine={body.machine}"]
    if body.ref:
        create_args += ["-f", f"ref={body.ref}"]
    # Stop the Codespace after 60 min idle (training sessions shouldn't burn the
    # learner's compute while abandoned); the expiry reaper then throws it away.
    create_args += ["-F", f"idle_timeout_minutes={CODESPACE_IDLE_TIMEOUT_MIN}"]
    out = await _gh(body.dtUser, *create_args)
    try:
        cs = json.loads(out)
    except json.JSONDecodeError:
        raise HTTPException(502, "Unexpected response from GitHub create-codespace API.")
    name = cs.get("name")
    web_url = cs.get("web_url")
    if not name:
        raise HTTPException(502, "GitHub did not return a codespace name.")

    # Capture the ACTUAL machine GitHub assigned. When body.machine is omitted the
    # create response carries the chosen machine (name + human size), so the size is
    # visible in the dashboard UI + logs instead of "default"/None.
    m = cs.get("machine") or {}
    machine_name = m.get("name") or body.machine or "default"

    def _gib(n):
        try:
            return f"{int(n) / (1024 ** 3):.0f}GB"
        except (TypeError, ValueError):
            return "?"

    # GitHub's display_name is already a clean size string ("2 cores, 8 GB RAM,
    # 32 GB storage"); only synthesize one from the raw specs if it's missing.
    if m.get("display_name"):
        machine_display = m["display_name"]
    else:
        machine_display = machine_name
        if m.get("cpus"):
            machine_display += f" • {m['cpus']} vCPU"
        if m.get("memory_in_bytes"):
            machine_display += f" • {_gib(m['memory_in_bytes'])} RAM"
        if m.get("storage_in_bytes"):
            machine_display += f" • {_gib(m['storage_in_bytes'])} disk"

    # c. Record the running job (mirrors the Arena daemon job hash shape so the
    #    dashboard Running tab, shell, and terminate plumbing all see it).
    now = datetime.now(timezone.utc)
    tenant_id, stage = _tenant_meta(body.dtEnv.DT_ENVIRONMENT)
    # The Running tab reads `branch` and `arch` — resolve the actual branch the
    # Codespace checked out (explicit ref → git_status.ref → repo default) and
    # record the machine architecture (GitHub Codespaces machines are all x86_64).
    branch = (
        body.ref
        or (cs.get("git_status") or {}).get("ref")
        or (cs.get("repository") or {}).get("default_branch")
        or ""
    )
    expires_at = (now.replace(microsecond=0) + timedelta(seconds=CODESPACE_JOB_TTL)).isoformat()
    redis_meta = {
        "job_id": name,
        "provider": "codespace",
        "type": "codespace",
        "dtUser": body.dtUser,
        "repo": body.repo,
        "ref": body.ref or "",
        "branch": branch,
        "arch": "x86_64",
        "machine": machine_name,
        "machine_display": machine_display,
        "status": "provisioning",
        "created": now.isoformat(),
        "started_at": now.isoformat(),
        "expires_at": expires_at,
        "worker_id": "github-codespaces",
        "arena_user": body.dtUser,
        "arena_tenant": tenant_id,
        "stage": stage,
        "web_url": web_url or "",
    }
    await _pool().hset(f"job:running:{name}", mapping=redis_meta)
    await _pool().expire(f"job:running:{name}", CODESPACE_JOB_TTL)

    # Seed the Log tab (the /api/jobs/{id}/log endpoint reads job:log:{name}); without
    # this a Codespace session's Log tab is empty/404. Records creation metadata incl.
    # the machine size and tenant. Live devcontainer build logs are appended by the
    # session-status poller.
    creation_log = (
        f"[{now.isoformat()}] Codespace created\n"
        f"  name:    {name}\n"
        f"  repo:    {body.repo}@{body.ref or 'default'}\n"
        f"  machine: {machine_display}\n"
        f"  tenant:  {tenant_id} ({stage})\n"
        f"  web:     {web_url or '(pending)'}\n"
        f"  user:    {body.dtUser}\n"
        "Provisioning the devcontainer (Kubernetes + demo apps). This can take a "
        "few minutes; the shell and app become available once the environment is ready.\n"
    )
    await _pool().setex(f"job:log:{name}", CODESPACE_JOB_TTL, creation_log)

    ws_url = f"wss://autonomous-enablements.whydevslovedynatrace.com/ws/jobs/{name}/shell"
    # Audit: user + repo + machine + tenant/stage — NEVER the credential or DT tokens.
    log.info("Codespace provisioned name=%s user=%s repo=%s machine=%s tenant=%s stage=%s",
             name, body.dtUser, body.repo, machine_display, tenant_id, stage)
    # d.
    return {"jobId": name, "status": "provisioning", "webUrl": web_url, "wsUrl": ws_url}


async def _append_creation_log(dtUser: str, name: str) -> None:
    """Append the Codespace's devcontainer creation log (post-create output) to
    job:log:{name} so both the Orbital Log tab and the app's Logs tab show what
    actually happened while the repo was provisioning. Fetched once, when the
    Codespace first reaches ready — guarded by a flag on the running hash."""
    key = f"job:running:{name}"
    if await _pool().hget(key, "creation_log_fetched"):
        return
    # Mark first (best-effort at-most-once; a failed fetch clears the flag below
    # so the next status poll retries).
    await _pool().hset(key, "creation_log_fetched", "1")
    token = await get_user_token(_pool(), dtUser) if dtUser else None
    env = {**os.environ}
    if token:
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
    # GitHub persists the devcontainer build/post-create output here.
    creation_path = "/workspaces/.codespaces/.persistedshare/creation.log"
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "codespace", "ssh", "-c", name, "--",
            f"cat {creation_path} 2>/dev/null || true",
            env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        text = out_b.decode(errors="replace").strip()
    except Exception as exc:
        await _pool().hdel(key, "creation_log_fetched")
        log.warning("Could not fetch creation log for %s: %s", name, exc)
        return
    if proc.returncode == 0:
        # One SSH round-trip succeeded → sshd (installed by setUpTerminal during
        # post-create) is up. session_status holds "provisioning" until this flag
        # is set so the app never offers a terminal that cannot connect yet.
        await _pool().hset(key, "ssh_ready", "1")
    if not text:
        await _pool().hdel(key, "creation_log_fetched")
        return
    log_key = f"job:log:{name}"
    existing = await _pool().get(log_key) or ""
    ttl = await _pool().ttl(log_key)
    combined = (
        existing
        + "\n───────────── devcontainer creation log ─────────────\n"
        + _redact(text, token or "")
        + "\n"
    )
    await _pool().set(log_key, combined, ex=ttl if ttl and ttl > 0 else CODESPACE_JOB_TTL)
    log.info("Creation log appended for codespace %s (%d bytes)", name, len(text))


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
    expires_at = ""
    tracked = await _pool().exists(key)
    if status == "ready":
        # Pull the devcontainer creation log into job:log (once) so the Log tabs
        # show the repo's post-create output. Fire-and-forget — status polling
        # must stay fast.
        asyncio.ensure_future(_append_creation_log(dtUser, name))
        # A Codespace reports Available minutes before post-create's setUpTerminal
        # has installed sshd. Hold "provisioning" until one SSH round-trip (the
        # creation-log fetch above) has succeeded, so the app never offers a
        # terminal that `gh codespace ssh` cannot reach yet. Bounded: if sshd
        # never comes up (repo without the framework's installCodespaceSSH),
        # surface ready after SSH_READY_MAX_HOLD anyway — a failing terminal
        # beats a session stuck in provisioning forever.
        if tracked and not await _pool().hget(key, "ssh_ready"):
            first_seen = await _pool().hget(key, "ready_first_seen")
            now_ts = int(time.time())
            if not first_seen:
                await _pool().hset(key, "ready_first_seen", str(now_ts))
                first_seen = str(now_ts)
            if now_ts - int(first_seen) < SSH_READY_MAX_HOLD:
                status = "provisioning"
    if tracked:
        await _pool().hset(key, mapping={"status": status, "gh_state": gh_state})
        expires_at = await _pool().hget(key, "expires_at") or ""
    return {"jobId": name, "status": status, "ghState": gh_state, "webUrl": web_url,
            "expiresAt": expires_at}


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

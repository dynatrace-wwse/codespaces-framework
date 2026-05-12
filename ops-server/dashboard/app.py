"""Dashboard — web UI and API for the multi-arch ops platform."""

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import fcntl
import pty
import struct
import termios

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import redis.asyncio as redis

from webhook.config import REDIS_URL, FRAMEWORK_DIR

# GitHub token used to dispatch workflow_run events. Required for the
# /api/builds/trigger endpoint. Generate a fine-grained PAT with
# `actions:write` and `contents:read` for the org's repos.
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_API   = "https://api.github.com"
# Org used to gate writer-role checks. Anyone who is a member of this org
# (verified by oauth2-proxy + the GH /orgs/.../memberships endpoint) gets
# the 'writer' role and can execute actions; everyone else is 'guest'.
GH_ORG   = os.environ.get("OAUTH2_GITHUB_ORG", "dynatrace-wwse")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ops-dashboard")

app = FastAPI(title="Enablement Ops Dashboard", version="2.0.0")

DASHBOARD_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")

pool: redis.Redis | None = None


@app.on_event("startup")
async def startup():
    global pool
    pool = redis.from_url(REDIS_URL, decode_responses=True)
    log.info("Dashboard connected to Redis")


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.aclose()


# ── Role gating ──────────────────────────────────────────────────────────────
# oauth2-proxy authenticates the user (must be a GH_ORG member to sign in) and
# nginx forwards X-Auth-User on protected paths. Inside that set, we further
# split between 'writer' (org member with role admin/member, allowed to act)
# and 'guest' (read-only). The org-role check is cached 10 minutes per user
# in Redis to avoid hitting the GH API on every request.

async def _resolve_role(user: str) -> dict:
    """Return {role, org_role} for a GitHub username.

    Trust model: oauth2-proxy is configured with ``github_org = <GH_ORG>``,
    which means a valid session cookie already guarantees the caller is an
    active org member. nginx only sets ``X-Auth-User`` after that check
    succeeds, so by the time we see a username here the caller is already
    a member — they are a 'writer'.

    We additionally try ``/orgs/{org}/memberships/{user}`` to enrich the
    response with org_role (admin/member). The lookup needs a token with
    'Members: read' on the org; if the token lacks that scope (403/404)
    we still return writer because oauth2-proxy did the authoritative
    check already. role is only 'guest' when there is no authenticated
    user (empty username).
    """
    if not user:
        return {"role": "guest", "org_role": "", "user": ""}

    cache_key = f"auth:role:{user}"
    cached = await pool.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    org_role = ""
    if GH_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{GH_API}/orgs/{GH_ORG}/memberships/{user}",
                    headers={
                        "Authorization": f"Bearer {GH_TOKEN}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("state") == "active":
                        org_role = data.get("role", "member")
                elif resp.status_code in (403, 404):
                    # Token lacks Members:read scope, or user not found.
                    # Don't downgrade to guest — oauth2-proxy already vouched.
                    log.info(
                        "org-role enrich for %s skipped (HTTP %d) — "
                        "trusting oauth2-proxy session",
                        user, resp.status_code,
                    )
        except Exception as e:
            log.warning("org-role lookup for %s failed: %s", user, e)

    payload = {
        # Authenticated via oauth2-proxy ⇒ org member ⇒ writer.
        "role": "writer",
        "org_role": org_role or "member",
        "user": user,
    }
    try:
        await pool.set(cache_key, json.dumps(payload), ex=600)
    except Exception:
        pass
    return payload


async def _require_writer(request: Request) -> dict:
    """FastAPI dependency-style guard for action endpoints.

    Returns the resolved role payload. Raises 401/403 if the caller is not
    a writer. nginx sets X-Auth-User only after oauth2-proxy validates the
    session; without that header we treat the request as anonymous.
    """
    user = request.headers.get("x-auth-user", "")
    if not user:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "sign_in": "/oauth2/sign_in"},
        )
    role = await _resolve_role(user)
    if role.get("role") != "writer":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "user": user,
                "reason": f"User {user} is not a member of {GH_ORG}; "
                          "actions are restricted to org members.",
            },
        )
    return role


@app.get("/api/auth/role")
async def api_auth_role(request: Request):
    """Resolve the caller's role for the dashboard UI.

    Returns 'guest' if not signed in or not a member of the org; 'writer'
    if the user is a verified org member. The frontend uses this to hide
    or disable action buttons for guests.
    """
    user = request.headers.get("x-auth-user", "")
    if not user:
        return {"role": "guest", "org_role": "", "user": ""}
    return await _resolve_role(user)


# ── UI Routes ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Fleet overview dashboard."""
    return templates.TemplateResponse(request, "index.html")


# ── API Routes ───────────────────────────────────────────────────────────────


@app.get("/api/repos")
async def api_repos():
    """List all repos with the latest build matrix.

    Merges two data sources:
      - ``jobs:completed``     — local worker results (primary, links to /api/jobs/<id>/log)
      - ``ci:<repo>:*:main``   — GHA workflow_run events (used as fallback)
    """
    import yaml

    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    local_matrix: dict[str, dict] = {}
    for raw in completed_raw:
        job = json.loads(raw)
        if job.get("type") != "integration-test":
            continue
        repo = job["repo"]
        arch = job.get("arch") or job.get("result", {}).get("arch") or job.get("worker_arch") or "arm64"
        result = job.get("result", {}) or {}
        local_matrix.setdefault(repo, {})[arch] = {
            "passed": bool(result.get("passed")),
            "status": job.get("status", "completed"),
            "duration": int(result.get("duration_seconds", 0)),
            "finished_at": job.get("finished_at", ""),
            "job_id": job.get("job_id", ""),
            "source": "local",
        }

    # History sparklines: last 10 integration-test builds per (repo, arch)
    history_matrix: dict[str, dict[str, list]] = {}
    for raw in reversed(completed_raw):  # newest first
        try:
            hj = json.loads(raw)
        except Exception:
            continue
        if hj.get("type") != "integration-test":
            continue
        hr = hj.get("repo", "")
        ha = hj.get("arch") or hj.get("result", {}).get("arch") or hj.get("worker_arch") or "arm64"
        hres = hj.get("result", {}) or {}
        history_matrix.setdefault(hr, {}).setdefault(ha, [])
        if len(history_matrix[hr][ha]) < 10:
            history_matrix[hr][ha].append({
                "passed": bool(hres.get("passed")),
                "status": hj.get("status", "completed"),
                "finished_at": hj.get("finished_at", ""),
                "job_id": hj.get("job_id", ""),
            })

    # Pull latest_tag from fleet:release-tags (24 h TTL, populated by the
    # status-summary endpoint on each run so it survives the 5-min status cache).
    release_map: dict[str, str] = {}
    try:
        cached_tags = await pool.get("fleet:release-tags")
        if cached_tags:
            release_map = json.loads(cached_tags)
    except Exception:
        pass

    # Fallback: fetch latest releases directly from GitHub when the cache is empty.
    if not release_map and GH_TOKEN:
        import asyncio as _asyncio
        active_repos = [r["repo"] for r in data.get("repos", []) if r.get("status") == "active"]

        async def _fetch_latest_tag(repo_full: str) -> tuple[str, str]:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(
                        f"https://api.github.com/repos/{repo_full}/releases/latest",
                        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"},
                    )
                    if r.is_success:
                        return repo_full, r.json().get("tag_name", "")
            except Exception:
                pass
            return repo_full, ""

        results = await _asyncio.gather(*[_fetch_latest_tag(r) for r in active_repos])
        release_map = {repo: tag for repo, tag in results if tag}
        if release_map:
            try:
                await pool.set("fleet:release-tags", json.dumps(release_map), ex=86400)
            except Exception:
                pass

    repos_out = []
    for r in data.get("repos", []):
        if r.get("status") != "active":
            continue
        repo_full = r["repo"]
        builds: dict[str, dict] = dict(local_matrix.get(repo_full, {}))

        # Fall back to GHA workflow_run records for any arch we don't have locally
        async for key in pool.scan_iter(match=f"ci:{repo_full}:*:main"):
            wf_data = await pool.hgetall(key)
            if not wf_data:
                continue
            workflow = wf_data.get("workflow", "")
            arch = next((a for a in ("arm64", "amd64") if workflow.lower().endswith(a)), None)
            if not arch or arch in builds:
                continue
            builds[arch] = {
                "passed": wf_data.get("conclusion") == "success",
                "duration": int(wf_data.get("duration_seconds", 0)),
                "finished_at": wf_data.get("finished_at", ""),
                "run_url": wf_data.get("run_url", ""),
                "source": "github-actions",
            }

        repos_out.append({
            "name": r["name"],
            "repo": repo_full,
            "arch": r.get("arch", "both"),
            "duration": r.get("duration", "1h"),
            "ci": r.get("ci", True),
            "builds": builds,
            "history": history_matrix.get(repo_full, {}),
            "latest_tag": release_map.get(repo_full, ""),
        })

    return {"repos": repos_out, "total": len(repos_out)}


@app.get("/api/jobs/{job_id}/log")
async def api_job_log(job_id: str):
    """Plain-text log for a completed local worker job (7-day TTL)."""
    from fastapi.responses import PlainTextResponse
    content = await pool.get(f"job:log:{job_id}")
    if content is None:
        return PlainTextResponse(
            f"No log found for job {job_id}.\n"
            "Either the job hasn't finished, the 7-day TTL expired, "
            "or the job ran on GitHub Actions (use the run URL instead).",
            status_code=404,
        )
    return PlainTextResponse(content)


@app.get("/api/workers")
async def api_workers():
    """List registered workers and their status.

    Master ARM worker writes to ``worker:master-arm64`` with role=master;
    AMD agents write to ``worker:<id>`` with role=agent (default). Workers
    are sorted master-first so the dashboard pins the master at the top.
    """
    worker_keys = []
    async for key in pool.scan_iter("worker:*"):
        # Skip port-pool lists (worker:<id>:app_ports_free) — they are Redis
        # lists, not hashes, and would cause a WRONGTYPE error on hgetall.
        if key.endswith(":app_ports_free"):
            continue
        worker_keys.append(key)

    workers = []
    for key in worker_keys:
        try:
            data = await pool.hgetall(key)
        except Exception:
            continue
        if data:
            data["worker_id"] = key.replace("worker:", "")
            data.setdefault("role", "agent")
            workers.append(data)

    # Master first, then alphabetical
    workers.sort(key=lambda w: (0 if w.get("role") == "master" else 1, w["worker_id"]))
    return {"workers": workers, "total": len(workers)}


@app.get("/api/branches/all")
async def api_all_branches():
    """Aggregate the union of branches across all active repos.

    Returns ``{branches: [{name, repos: [...]}]}`` so the UI can offer a
    cross-repo branch picker that shows which repos have a given branch
    (e.g. ``fix/badges-and-rum-ids`` on 9 repos). Each repo's branch list
    is fetched from ``/api/repos/{owner}/{repo}/branches`` (Redis-cached).
    """
    import yaml
    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    active = [
        r["repo"] for r in data.get("repos", [])
        if r.get("status") == "active"
    ]

    branch_to_repos: dict[str, list[str]] = {}

    async def fetch_one(repo_full: str):
        cache_key = f"repo:branches:{repo_full}"
        cached = await pool.get(cache_key)
        if cached:
            try:
                payload = json.loads(cached)
                return repo_full, payload.get("branches", []) or []
            except Exception:
                pass
        owner, repo = repo_full.split("/", 1)
        proc = await asyncio.create_subprocess_exec(
            "gh", "api", f"/repos/{owner}/{repo}/branches", "--paginate",
            "--jq", "[.[] | .name]",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return repo_full, ["main"]
        try:
            branches = json.loads(stdout.decode())
        except Exception:
            branches = ["main"]
        await pool.set(cache_key, json.dumps({"branches": branches}), ex=600)
        return repo_full, branches

    results = await asyncio.gather(
        *(fetch_one(r) for r in active),
        return_exceptions=True,
    )
    for item in results:
        if isinstance(item, Exception):
            continue
        repo_full, branches = item
        for b in branches:
            branch_to_repos.setdefault(b, []).append(repo_full)

    rows = []
    for name, repos in branch_to_repos.items():
        rows.append({"name": name, "repos": sorted(repos), "count": len(repos)})
    # main first, then most-shared, then alpha
    rows.sort(key=lambda r: (0 if r["name"] == "main" else 1, -r["count"], r["name"]))
    return {"branches": rows, "total_repos": len(active)}


@app.post("/api/builds/trigger-fleet")
async def api_trigger_fleet(request: Request):
    """Trigger an integration test for a single branch across multiple repos.

    Body: ``{branch: "<name>", arch: "arm64|amd64|both", repos?: [...]}``

    If ``repos`` is omitted, queues a build for every active repo that has
    that branch (per the cached branch list). Returns the list of jobs
    queued and any repos that were skipped because the branch doesn't
    exist on them.
    """
    role = await _require_writer(request)
    body = await request.json()
    branch = (body.get("branch") or "").strip()
    arch = body.get("arch", "both")
    explicit = body.get("repos") or []
    if not branch:
        raise HTTPException(400, "branch is required")

    # Resolve the candidate repo set. If `repos` is provided, validate each
    # actually has the branch (using the cached branch list).
    aggregate = await api_all_branches()
    by_branch = {b["name"]: b["repos"] for b in aggregate["branches"]}
    candidates = explicit or by_branch.get(branch, [])
    has_branch = set(by_branch.get(branch, []))
    targets = [r for r in candidates if r in has_branch]
    skipped = [r for r in candidates if r not in has_branch]

    arches = ["arm64", "amd64"] if arch == "both" else [arch]
    timestamp = datetime.now(timezone.utc).isoformat()
    queued = []
    fleet_run_id = f"fleet-{int(datetime.now(timezone.utc).timestamp())}"
    for repo in targets:
        for a in arches:
            job = {
                "type": "integration-test",
                "repo": repo,
                "arch": a,
                "queue": f"test:{a}",
                "ref": branch,
                "timestamp": timestamp,
                "trigger": "fleet",
                "nightly_run_id": fleet_run_id,
                "requested_by": role["user"],
            }
            await pool.rpush(f"queue:test:{a}", json.dumps(job))
            queued.append({"repo": repo, "arch": a})

    return {
        "status": "queued",
        "branch": branch,
        "fleet_run_id": fleet_run_id,
        "queued": queued,
        "skipped_no_branch": skipped,
        "requested_by": role["user"],
    }


@app.post("/api/ghpages/trigger")
async def api_trigger_ghpages(request: Request):
    """Queue a local deploy-ghpages job for a single repo+branch.

    Body: ``{repo: "owner/name", ref: "<branch>"}``
    Runs the same steps as deploy-ghpages.yaml on the local worker.
    """
    role = await _require_writer(request)
    body = await request.json()
    repo = (body.get("repo") or "").strip()
    ref  = (body.get("ref") or "main").strip()
    if not repo:
        raise HTTPException(400, "repo is required")

    ts     = int(time.time() * 1000)
    job_id = f"deploy-ghpages-{repo.split('/')[-1]}-{ts}"
    job    = {
        "job_id":       job_id,
        "type":         "deploy-ghpages",
        "repo":         repo,
        "ref":          ref,
        "branch":       ref,
        "trigger":      "dashboard",
        "requested_by": role["user"],
        "timestamp":    datetime.utcnow().isoformat(),
    }
    await pool.rpush("queue:agent", json.dumps(job))
    log.info("GH Pages queued: %s @ %s by %s (job_id=%s)", repo, ref, role["user"], job_id)
    return {"status": "queued", "job_id": job_id, "repo": repo, "ref": ref}


@app.post("/api/ghpages/trigger-fleet")
async def api_trigger_ghpages_fleet(request: Request):
    """Queue local deploy-ghpages jobs for every fleet repo that has the chosen branch.

    Body: ``{branch: "<name>", repos?: [...]}``
    Each repo gets its own job queued to queue:agent.
    """
    role = await _require_writer(request)
    body = await request.json()
    branch   = (body.get("branch") or "").strip()
    explicit = body.get("repos") or []
    if not branch:
        raise HTTPException(400, "branch is required")

    aggregate = await api_all_branches()
    by_branch = {b["name"]: b["repos"] for b in aggregate["branches"]}
    candidates = explicit or by_branch.get(branch, [])
    has_branch = set(by_branch.get(branch, []))
    targets    = [r for r in candidates if r in has_branch]
    skipped    = [r for r in candidates if r not in has_branch]

    queued: list[str] = []
    ts = int(time.time() * 1000)
    for repo_full in targets:
        job_id = f"deploy-ghpages-{repo_full.split('/')[-1]}-{ts}"
        job    = {
            "job_id":       job_id,
            "type":         "deploy-ghpages",
            "repo":         repo_full,
            "ref":          branch,
            "branch":       branch,
            "trigger":      "dashboard",
            "requested_by": role["user"],
            "timestamp":    datetime.utcnow().isoformat(),
        }
        await pool.rpush("queue:agent", json.dumps(job))
        queued.append(repo_full)

    log.info(
        "GH Pages fleet queued: branch=%s queued=%d skipped=%d by=%s",
        branch, len(queued), len(skipped), role["user"],
    )
    return {
        "status":              "queued",
        "branch":              branch,
        "dispatched":          queued,
        "dispatched_count":    len(queued),
        "errors":              [],
        "skipped_no_branch":   skipped,
        "requested_by":        role["user"],
    }


@app.post("/api/agent/fix-ci")
async def api_agent_fix_ci(request: Request):
    """Queue a fix-ci agent job for a failed integration test."""
    role = await _require_writer(request)
    body = await request.json()
    repo        = (body.get("repo") or "").strip()
    branch      = (body.get("branch") or "main").strip()
    arch        = (body.get("arch") or "arm64").strip()
    failed_job_id = (body.get("failed_job_id") or "").strip()
    failed_step   = (body.get("failed_step") or "").strip()

    if not repo:
        raise HTTPException(400, "repo is required")

    # Fetch the failed log from Redis for the agent to analyze
    failed_log = ""
    if failed_job_id:
        raw = await pool.get(f"job:log:{failed_job_id}")
        if raw:
            # Cap at 12KB — enough context, won't blow up the prompt
            failed_log = raw[-12288:] if len(raw) > 12288 else raw

    import uuid as _uuid
    ts = int(time.time() * 1000)
    repo_name = repo.split("/")[-1]
    job_id = f"fix-ci-{repo_name}-{ts}-{_uuid.uuid4().hex[:6]}"

    job = {
        "job_id":        job_id,
        "type":          "fix-ci",
        "repo":          repo,
        "ref":           branch,
        "branch":        branch,
        "arch":          arch,
        "trigger":       "dashboard",
        "requested_by":  role["user"],
        "timestamp":     datetime.utcnow().isoformat(),
        "failed_job_id": failed_job_id,
        "failed_log":    failed_log,
        "failed_step":   failed_step,
    }

    await pool.rpush("queue:agent", json.dumps(job))
    log.info("Queued fix-ci agent job %s for %s@%s by %s", job_id, repo, branch, role["user"])
    return {"job_id": job_id, "status": "queued", "repo": repo, "branch": branch}


@app.post("/api/agent/fix-pr")
async def api_agent_fix_pr(request: Request):
    """Queue a fix-ci agent job scoped to an open PR. Restricted to sergiohinojosa."""
    role = await _require_writer(request)
    user = role.get("user", "")
    if user != "sergiohinojosa":
        raise HTTPException(status_code=403, detail="Fix-with-AI is currently restricted to sergiohinojosa")
    body = await request.json()
    repo         = (body.get("repo") or "").strip()
    pr_number    = body.get("pr_number")
    branch       = (body.get("branch") or "main").strip()
    instructions = (body.get("instructions") or "").strip()
    if not repo or not pr_number:
        raise HTTPException(400, "repo and pr_number are required")

    # Fetch the most recent failed integration-test log for this repo+branch
    failed_log = ""
    failed_job_id = ""
    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    for raw in reversed(completed_raw):
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("type") != "integration-test":
            continue
        if j.get("repo", "").lower() != repo.lower():
            continue
        ref = j.get("ref") or j.get("branch") or j.get("head_branch") or ""
        if branch and ref != branch:
            continue
        result = j.get("result", {}) or {}
        if result.get("passed"):
            continue
        failed_job_id = j.get("job_id", "")
        if failed_job_id:
            raw_log = await pool.get(f"job:log:{failed_job_id}")
            if raw_log:
                failed_log = raw_log[-16384:] if len(raw_log) > 16384 else raw_log
        break

    import uuid as _uuid
    ts = int(time.time() * 1000)
    repo_name = repo.split("/")[-1]
    job_id = f"fix-pr-{repo_name}-{ts}-{_uuid.uuid4().hex[:6]}"
    job = {
        "job_id":        job_id,
        "type":          "fix-ci",
        "repo":          repo,
        "ref":           branch,
        "branch":        branch,
        "arch":          "arm64",
        "trigger":       "dashboard",
        "requested_by":  user,
        "git_author_email": "hj.sergio@gmail.com",
        "timestamp":     datetime.utcnow().isoformat(),
        "pr_number":     pr_number,
        "failed_job_id": failed_job_id,
        "failed_log":    failed_log,
        "instructions":  instructions,
        "context":       "fix-pr",
    }
    await pool.rpush("queue:agent", json.dumps(job))
    log.info("Queued fix-pr job %s for %s PR#%s by %s", job_id, repo, pr_number, user)
    return {"job_id": job_id, "status": "queued", "repo": repo, "pr_number": pr_number}


@app.post("/api/agent/fix-issue")
async def api_agent_fix_issue(request: Request):
    """Queue a fix-issue agent job. Restricted to sergiohinojosa."""
    role = await _require_writer(request)
    user = role.get("user", "")
    if user != "sergiohinojosa":
        raise HTTPException(status_code=403, detail="Fix-with-AI is currently restricted to sergiohinojosa")
    body = await request.json()
    repo         = (body.get("repo") or "").strip()
    issue_number = body.get("issue_number")
    instructions = (body.get("instructions") or "").strip()
    if not repo or not issue_number:
        raise HTTPException(400, "repo and issue_number are required")

    import uuid as _uuid
    ts = int(time.time() * 1000)
    repo_name = repo.split("/")[-1]
    job_id = f"fix-issue-{repo_name}-{ts}-{_uuid.uuid4().hex[:6]}"
    job = {
        "job_id":        job_id,
        "type":          "fix-issue",
        "repo":          repo,
        "ref":           "main",
        "branch":        "main",
        "arch":          "arm64",
        "trigger":       "dashboard",
        "requested_by":  user,
        "git_author_email": "hj.sergio@gmail.com",
        "timestamp":     datetime.utcnow().isoformat(),
        "issue_number":  issue_number,
        "instructions":  instructions,
        "context":       "fix-issue",
    }
    await pool.rpush("queue:agent", json.dumps(job))
    log.info("Queued fix-issue job %s for %s #%s by %s", job_id, repo, issue_number, user)
    return {"job_id": job_id, "status": "queued", "repo": repo, "issue_number": issue_number}


@app.get("/api/builds/running")
async def api_builds_running():
    """Currently executing tests, plus pending queue depths.

    Workers write a ``job:running:<run_id>`` HASH when they pick up a job and
    delete it when done. Concurrency per (repo, branch, arch) is enforced via
    ``running:lock:<triple>`` STRING keys (see workers/manager.py and
    worker-agent/agent.py).
    """
    queues = {}
    for arch in ("arm64", "amd64"):
        queues[arch] = await pool.llen(f"queue:test:{arch}")
    queues["agent"] = await pool.llen("queue:agent")
    queues["sync"]  = await pool.llen("queue:sync")

    running = []
    async for key in pool.scan_iter(match="job:running:*"):
        # Tolerate the legacy STRING shape until all workers are on the
        # post-lock-fix code. New shape is HASH at job:running:{run_id};
        # legacy is STRING at job:running:{repo}:{arch}.
        key_type = await pool.type(key)
        if key_type == "hash":
            meta = await pool.hgetall(key)
            if not meta or not meta.get("repo"):
                continue
            running.append({
                "repo": meta.get("repo"),
                "arch": meta.get("arch"),
                "branch": meta.get("branch"),
                "job_id": meta.get("job_id"),
                "ref": meta.get("ref"),
                "started_at": meta.get("started_at"),
                "worker_id": meta.get("worker_id"),
                "type": meta.get("type", "integration-test"),
            })
        elif key_type == "string":
            parts = key.split(":", 3)
            if len(parts) < 4:
                continue
            repo, arch = parts[2], parts[3]
            raw = await pool.get(key)
            try:
                meta = json.loads(raw) if raw else {}
            except Exception:
                meta = {}
            running.append({
                "repo": repo,
                "arch": arch,
                "branch": meta.get("ref"),
                "job_id": meta.get("job_id"),
                "ref": meta.get("ref"),
                "started_at": meta.get("started_at"),
                "worker_id": meta.get("worker_id"),
                "type": meta.get("type", "integration-test"),
            })

    # Surface deferred jobs so the dashboard can show "queued behind a running test"
    deferred = []
    async for key in pool.scan_iter(match="deferred:*"):
        triple = key.split(":", 1)[1]
        depth = await pool.llen(key)
        if depth:
            deferred.append({"triple": triple, "depth": depth})

    return {"queues": queues, "running": running, "deferred": deferred}


@app.get("/api/jobs/{job_id}/livelog")
async def api_job_livelog(job_id: str):
    """Plain-text live log for an in-flight test (updated ~1s by the worker).

    Returns 404 if the job is no longer in the running set — even if the
    livelog Redis key hasn't expired yet — so the dashboard correctly falls
    back to the final stored log and hides the Terminate button.
    """
    from fastapi.responses import PlainTextResponse
    if not await pool.exists(f"job:running:{job_id}"):
        return PlainTextResponse("(no livelog — job may have finished)", status_code=404)
    content = await pool.get(f"job:livelog:{job_id}")
    if content is None:
        return PlainTextResponse("(no livelog — job may have finished)", status_code=404)
    return PlainTextResponse(content)


@app.post("/api/jobs/{job_id}/terminate")
async def api_terminate_job(job_id: str, request: Request):
    """Request termination of a running job.

    Publishes the job_id on the ``ops:terminate`` pub/sub channel; whichever
    worker owns the job kills its Sysbox container, marks status='terminated',
    and runs the normal cleanup path (DEL running:lock, DEL job:running, drain
    deferred). Returns 404 if the job is not currently running. Writer role
    required.
    """
    role = await _require_writer(request)
    if not await pool.exists(f"job:running:{job_id}"):
        # Check if it completed — gives a friendlier message than a bare 404.
        in_completed = await pool.exists(f"job:log:{job_id}")
        detail = (
            f"Job {job_id} has already completed — check the History tab for results."
            if in_completed else
            f"Job {job_id} is not running (it may have completed or never started)."
        )
        raise HTTPException(404, detail)
    requested_by = role["user"]
    await pool.publish("ops:terminate", job_id)
    log.info("Termination requested for %s by %s", job_id, requested_by)
    return {"status": "termination_requested", "job_id": job_id, "requested_by": requested_by}


@app.get("/api/queue/list")
async def api_queue_list():
    """Contents of the pending test queues (arm64 + amd64), unordered."""
    result = []
    for arch in ("arm64", "amd64"):
        items = await pool.lrange(f"queue:test:{arch}", 0, -1)
        for position, raw in enumerate(items):
            try:
                j = json.loads(raw)
            except Exception:
                continue
            result.append({
                "queue":        f"queue:test:{arch}",
                "arch":         arch,
                "position":     position,
                "job_id":       j.get("job_id", ""),
                "repo":         j.get("repo", ""),
                "ref":          j.get("ref") or j.get("branch") or j.get("head_branch") or "main",
                "type":         j.get("type", "integration-test"),
                "queued_at":    j.get("timestamp") or j.get("queued_at"),
                "requested_by": j.get("requested_by", ""),
            })
    return {"items": result, "total": len(result)}


@app.delete("/api/queue/item")
async def api_queue_delete_item(job_id: str, request: Request):
    """Remove a pending job from a test queue by job_id. Writer role required."""
    await _require_writer(request)
    removed = 0
    for arch in ("arm64", "amd64"):
        items = await pool.lrange(f"queue:test:{arch}", 0, -1)
        for raw in items:
            try:
                j = json.loads(raw)
                if j.get("job_id") == job_id:
                    count = await pool.lrem(f"queue:test:{arch}", 1, raw)
                    removed += count
                    break
            except Exception:
                pass
        if removed:
            break
    if not removed:
        raise HTTPException(404, f"Job {job_id} not found in any test queue")
    log.info("Queue item %s deleted", job_id)
    return {"removed": removed, "job_id": job_id}


@app.post("/api/builds/rerun/{job_id}")
async def api_rerun_job(job_id: str, request: Request):
    """Re-queue a completed job from history. Writer role required."""
    role = await _require_writer(request)
    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    original = None
    for raw in completed_raw:
        try:
            j = json.loads(raw)
            if j.get("job_id") == job_id:
                original = j
                break
        except Exception:
            pass
    if not original:
        raise HTTPException(404, f"Job {job_id} not found in history")
    repo = original.get("repo", "")
    ref = original.get("ref") or original.get("head_branch") or "main"
    arch = original.get("arch") or original.get("result", {}).get("arch") or "arm64"
    job_type = original.get("type", "integration-test")
    if job_type not in ("integration-test",):
        raise HTTPException(400, f"Re-run not supported for job type '{job_type}'")
    new_job_id = str(uuid.uuid4())
    new_job = {
        "job_id":    new_job_id,
        "repo":      repo,
        "ref":       ref,
        "arch":      arch,
        "type":      job_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger":   f"rerun-by-{role['user']}",
    }
    await pool.rpush(f"queue:test:{arch}", json.dumps(new_job))
    log.info("Re-queued %s@%s (%s) as %s by %s", repo, ref, arch, new_job_id, role["user"])
    return {"job_id": new_job_id, "status": "queued", "repo": repo, "ref": ref, "arch": arch}


@app.get("/log/{job_id}", response_class=HTMLResponse)
async def view_log_fullscreen(job_id: str):
    """Standalone fullscreen log viewer for a single job.

    Polls /api/jobs/{job_id}/livelog every 2s; falls back to /log on 404.
    Same ANSI-rendering pipeline as the in-dashboard modal, but its own page
    so users can pop logs out into a separate window/tab and tail at scale.
    """
    return HTMLResponse("""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>log: """ + job_id + """</title>
<style>
  body { margin:0; background:#0d1117; color:#c9d1d9; font:13px/1.5 ui-monospace,monospace; }
  header { padding:8px 14px; background:#161b22; border-bottom:1px solid #30363d;
           display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  header h1 { margin:0; font-size:14px; font-weight:600; flex:1; min-width:200px;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  header .status { padding:2px 8px; border-radius:10px; font-size:11px;
                   background:#1f6feb22; color:#58a6ff; }
  header .status.done { background:#23863622; color:#3fb950; }
  header .status.failed { background:#da363322; color:#f85149; }
  header .status.terminated { background:#d2932922; color:#d29922; }
  header input[type=search] {
    background:#0d1117; border:1px solid #30363d; color:#c9d1d9;
    padding:4px 8px; border-radius:4px; font-size:12px; width:220px;
    font-family:inherit;
  }
  header input[type=search]:focus { outline:none; border-color:#58a6ff; }
  header button {
    background:#21262d; border:1px solid #30363d; color:#c9d1d9;
    padding:3px 9px; border-radius:4px; font-size:11px; cursor:pointer;
    font-family:inherit;
  }
  header button:hover { background:#30363d; }
  header .count { font-family:ui-monospace,monospace; color:#8b949e; min-width:50px; font-size:11px; }
  pre { margin:0; padding:14px; white-space:pre-wrap; word-break:break-word;
        height:calc(100vh - 50px); overflow:auto; }
  pre.nowrap { white-space:pre; word-break:normal; overflow-x:auto; }
  mark.log-match { background:rgba(251,191,36,.32); color:inherit; border-radius:2px; padding:0; }
  mark.log-match.current { background:#58a6ff; color:#06121b; box-shadow:0 0 0 2px rgba(88,166,255,.5); }
  .ansi-bold { font-weight:bold; }
  .ansi-red { color:#f85149; } .ansi-green { color:#3fb950; }
  .ansi-yellow { color:#d29922; } .ansi-blue { color:#58a6ff; }
  .ansi-magenta { color:#bc8cff; } .ansi-cyan { color:#39c5cf; }
  .ansi-white { color:#c9d1d9; } .ansi-gray { color:#8b949e; }
</style>
</head><body>
<header>
  <h1>""" + job_id + """</h1>
  <input type="search" id="search" placeholder="Search… (Enter / Shift+Enter)" autocomplete="off">
  <button id="prev" title="Previous (Shift+Enter)">◀</button>
  <button id="next" title="Next (Enter)">▶</button>
  <span class="count" id="count"></span>
  <button id="wrap" title="Toggle wrap (W)">↩ Wrap</button>
  <span class="status" id="status">running</span>
</header>
<pre id="log">Loading…</pre>
<script>
const JOB_ID = """ + json.dumps(job_id) + """;
const ANSI_RE = /\\x1b\\[([0-9;]*)m/g;
const COLORS = {30:'gray',31:'red',32:'green',33:'yellow',34:'blue',35:'magenta',36:'cyan',37:'white',
                90:'gray',91:'red',92:'green',93:'yellow',94:'blue',95:'magenta',96:'cyan',97:'white'};
function escapeHtml(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function escapeRegex(s){return s.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&')}
function ansiToHtml(text){
  let out='', open=0, last=0;
  text.replace(ANSI_RE,(m,codes,i)=>{
    out += escapeHtml(text.slice(last,i));
    last = i + m.length;
    const parts = codes ? codes.split(';').map(Number) : [0];
    for(const c of parts){
      if(c===0){ while(open-->0) out+='</span>'; open=0; }
      else if(c===1){ out+='<span class="ansi-bold">'; open++; }
      else if(COLORS[c]){ out+='<span class="ansi-'+COLORS[c]+'">'; open++; }
    }
    return m;
  });
  out += escapeHtml(text.slice(last));
  while(open-->0) out+='</span>';
  return out;
}
const pre = document.getElementById('log');
const statusEl = document.getElementById('status');
const searchEl = document.getElementById('search');
const countEl = document.getElementById('count');
const WRAP_KEY = 'fullscreen-log-wrap';
let poll = null, currentHtml = '', term = '', idx = 0, total = 0;

function getWrap(){ return localStorage.getItem(WRAP_KEY) === '1'; }  // default: noWrap
function applyWrap(){
  const w = getWrap();
  pre.classList.toggle('nowrap', !w);
  document.getElementById('wrap').textContent = w ? '↩ Wrap' : '→ NoWrap';
}
function render(scroll){
  if(!term){ pre.innerHTML = currentHtml; total = 0; idx = -1; countEl.textContent = ''; return; }
  const tmp = document.createElement('div');
  tmp.innerHTML = currentHtml;
  const re = new RegExp(escapeRegex(term), 'gi');
  total = 0;
  function walk(n){
    if(n.nodeType === 3){
      const t = n.nodeValue;
      if(!re.test(t)) return;
      re.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0, m;
      while((m = re.exec(t)) !== null){
        if(m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
        const mk = document.createElement('mark');
        mk.className = 'log-match';
        mk.textContent = m[0];
        frag.appendChild(mk);
        total++; last = m.index + m[0].length;
        if(m[0].length === 0) re.lastIndex++;
      }
      if(last < t.length) frag.appendChild(document.createTextNode(t.slice(last)));
      n.parentNode.replaceChild(frag, n);
    } else {
      Array.from(n.childNodes).forEach(walk);
    }
  }
  walk(tmp);
  pre.innerHTML = '';
  while(tmp.firstChild) pre.appendChild(tmp.firstChild);
  if(total === 0){ countEl.textContent = '0 / 0'; return; }
  if(idx < 0 || idx >= total) idx = 0;
  highlight(scroll);
}
function highlight(scroll){
  const marks = document.querySelectorAll('#log mark.log-match');
  marks.forEach(m => m.classList.remove('current'));
  countEl.textContent = total ? (idx+1) + ' / ' + total : '0 / 0';
  if(!marks.length) return;
  const cur = marks[idx];
  if(cur){ cur.classList.add('current'); if(scroll) cur.scrollIntoView({block:'center', behavior:'smooth'}); }
}
function move(d){ if(!total) return; idx = (idx + d + total) % total; highlight(true); }

document.getElementById('prev').onclick = ()=>move(-1);
document.getElementById('next').onclick = ()=>move(1);
document.getElementById('wrap').onclick = ()=>{ localStorage.setItem(WRAP_KEY, getWrap() ? '0' : '1'); applyWrap(); };
searchEl.addEventListener('input', ()=>{ term = searchEl.value; idx = 0; render(true); });
searchEl.addEventListener('keydown', e=>{ if(e.key === 'Enter'){ e.preventDefault(); move(e.shiftKey ? -1 : 1); } });
document.addEventListener('keydown', e=>{
  if(e.target === searchEl) return;
  if(e.key === '/'){ e.preventDefault(); searchEl.focus(); }
  else if(e.key === 'w' || e.key === 'W'){ e.preventDefault(); document.getElementById('wrap').click(); }
});

async function tick(){
  try {
    let res = await fetch('/api/jobs/'+JOB_ID+'/livelog');
    let live = true;
    if(res.status===404){
      res = await fetch('/api/jobs/'+JOB_ID+'/log');
      live = false;
      if(poll){ clearInterval(poll); poll=null; }
      const t = await fetch('/api/jobs/'+JOB_ID+'/status').catch(()=>null);
      if(t && t.ok){
        const j = await t.json();
        statusEl.textContent = j.status || 'finished';
        statusEl.className = 'status ' + (j.status||'done');
      } else {
        statusEl.textContent = 'finished';
        statusEl.className = 'status done';
      }
    }
    if(res.ok){
      const text = await res.text();
      const wasAtBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      currentHtml = ansiToHtml(text);
      render(false);                     // don't scroll on auto-refresh
      if(wasAtBottom && !term) pre.scrollTop = pre.scrollHeight;
    }
  } catch(e){}
}
applyWrap();
tick();
poll = setInterval(tick, 2000);
</script>
</body></html>""")


@app.get("/api/jobs/{job_id}/status")
async def api_job_status(job_id: str):
    """Resolve the final status of a job for the fullscreen viewer header.

    Returns the most recent record from jobs:completed matching this id, or
    ``running`` if it's still in flight.
    """
    if await pool.exists(f"job:running:{job_id}"):
        return {"job_id": job_id, "status": "running"}
    completed = await pool.lrange("jobs:completed", -200, -1)
    for raw in reversed(completed):
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("job_id") == job_id:
            return {"job_id": job_id, "status": j.get("status", "completed")}
    return {"job_id": job_id, "status": "unknown"}


# Curated catalog of sync CLI commands surfaced in the Synchronizer tab.
# Destructive commands (tag, release, push-update) are listed but flagged for
# extra confirmation in the UI.
SYNC_COMMANDS = [
    {
        "id": "status",
        "label": "Status",
        "description": "Show framework-version drift across the fleet.",
        "args": ["status", "--json"],
        "destructive": False,
        "icon": "📊",
    },
    {
        "id": "list",
        "label": "List repos",
        "description": "List all registered repos (CI status, framework version pin).",
        "args": ["list"],
        "destructive": False,
        "icon": "📋",
    },
    {
        "id": "list-ci-enabled",
        "label": "List CI-enabled",
        "description": "Only repos with ci: true.",
        "args": ["list", "--ci-enabled"],
        "destructive": False,
        "icon": "✓",
    },
    {
        "id": "list-pr",
        "label": "Open PRs",
        "description": "List open framework-update PRs across the fleet.",
        "args": ["list-pr"],
        "destructive": False,
        "icon": "🔀",
    },
    {
        "id": "ci-status",
        "label": "CI status",
        "description": "Roll-up of CI run status per repo.",
        "args": ["ci-status"],
        "destructive": False,
        "icon": "🟢",
    },
    {
        "id": "validate",
        "label": "Validate",
        "description": "Validate repos.yaml and local repo state.",
        "args": ["validate"],
        "destructive": False,
        "icon": "✔️",
    },
    {
        "id": "diff",
        "label": "Diff (preview push-update)",
        "description": "Preview what push-update would change for the next version.",
        "args": ["diff"],
        "destructive": False,
        "icon": "🔍",
    },
    {
        "id": "list-issues",
        "label": "List issues",
        "description": "Open issues across repos with label filtering.",
        "args": ["list-issues"],
        "destructive": False,
        "icon": "🐛",
    },
    {
        "id": "clone",
        "label": "Clone all repos",
        "description": "Clone (or pull) every sync-managed repo locally.",
        "args": ["clone"],
        "destructive": False,
        "icon": "⬇️",
    },
]


@app.get("/api/sync/commands")
async def api_sync_commands():
    """List curated sync commands available in the UI."""
    return {"commands": SYNC_COMMANDS}


@app.post("/api/sync/run")
async def api_sync_run(request: Request):
    """Enqueue a sync command for execution.

    Body: {"command": "<id>"} where id matches one of SYNC_COMMANDS.
    Enqueues a sync-command job into queue:sync; the worker streams output
    to job:livelog:{job_id} and persists final log to job:log:{job_id}.
    """
    role = await _require_writer(request)
    body = await request.json()
    cmd_id = body.get("command", "")
    spec = next((c for c in SYNC_COMMANDS if c["id"] == cmd_id), None)
    if spec is None:
        raise HTTPException(400, f"Unknown sync command: {cmd_id}")

    requested_by = role["user"]
    timestamp = datetime.now(timezone.utc).isoformat()
    import uuid
    job_id = f"sync-{spec['id']}-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex[:6]}"

    job = {
        "type": "sync-command",
        "command_id": spec["id"],
        "command_label": spec["label"],
        "args": spec["args"],
        "queue": "sync",
        "timestamp": timestamp,
        "requested_by": requested_by,
        "repo": "dynatrace-wwse/codespaces-framework",  # synthetic for telemetry
        "job_id": job_id,
    }
    await pool.rpush("queue:sync", json.dumps(job))
    return {"status": "queued", "command": spec["id"], "job_id": job_id}


@app.get("/api/sync/history")
async def api_sync_history(limit: int = 50):
    """Past sync command runs from jobs:completed."""
    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    rows = []
    for raw in reversed(completed_raw):
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("type") != "sync-command":
            continue
        result = j.get("result", {}) or {}
        rows.append({
            "job_id": j.get("job_id", ""),
            "command_id": j.get("command_id", ""),
            "command_label": j.get("command_label", ""),
            "status": j.get("status", "completed"),
            "exit_code": result.get("exit_code"),
            "duration": int(result.get("duration_seconds", 0)),
            "started_at": j.get("timestamp"),
            "finished_at": j.get("finished_at"),
            "requested_by": j.get("requested_by", ""),
        })
        if len(rows) >= limit: break
    return {"rows": rows}


# ── Synchronizer live-data tabs ───────────────────────────────────────────────
# These endpoints power the Status / PRs / Issues sub-tabs inside the
# Synchronizer view.  They run gh CLI commands inline (not via the job queue)
# and cache results in Redis for 5 minutes so repeated tab-switches are free.

async def _gh_json(cache_key: str, *gh_args: str, ttl: int = 300) -> dict:
    """Run a gh command, cache JSON result in Redis, return parsed dict."""
    cached = await pool.get(cache_key)
    if cached:
        return json.loads(cached)
    proc = await asyncio.create_subprocess_exec(
        "gh", *gh_args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {"error": stderr.decode(errors="replace")[:500], "rows": []}
    try:
        data = json.loads(stdout.decode())
    except Exception:
        return {"error": "JSON parse error", "rows": [], "raw": stdout.decode()[:500]}
    payload = {"rows": data if isinstance(data, list) else data, "cached_at": datetime.now(timezone.utc).isoformat()}
    await pool.set(cache_key, json.dumps(payload), ex=ttl)
    return payload


@app.get("/api/sync/status-summary")
async def api_sync_status_summary():
    """Framework-version drift across the fleet via sync status --json.

    Runs ``python3 -m sync.cli status --json`` (cached 5 min) and returns the
    parsed rows so the UI can render a sortable drift table without opening a
    log stream.
    """
    cache_key = "sync:status-summary"
    cached = await pool.get(cache_key)
    if cached:
        payload = json.loads(cached)
        # Back-fill fleet:release-tags if it's missing (e.g. after a restart).
        if not await pool.exists("fleet:release-tags"):
            release_tags = {
                row["repo"]: row.get("latest_tag", "")
                for row in payload.get("rows", [])
                if row.get("repo") and row.get("latest_tag")
            }
            if release_tags:
                await pool.set("fleet:release-tags", json.dumps(release_tags), ex=86400)
        return payload

    sync_dir = FRAMEWORK_DIR
    proc = await asyncio.create_subprocess_exec(
        "python3", "-m", "sync.cli", "status", "--json",
        cwd=str(sync_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(sync_dir), "PYTHONUNBUFFERED": "1"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "sync status timed out after 60 s", "rows": []}
    if proc.returncode != 0:
        return {"error": stderr.decode(errors="replace")[:500], "rows": []}
    # Output may contain non-JSON preamble lines; find first '[' or '{'
    raw = stdout.decode(errors="replace")
    json_start = next((i for i, c in enumerate(raw) if c in ("[", "{")), None)
    if json_start is None:
        return {"error": "No JSON in sync status output", "rows": [], "raw": raw[:500]}
    try:
        data = json.loads(raw[json_start:])
    except Exception as exc:
        return {"error": f"JSON parse: {exc}", "rows": [], "raw": raw[:500]}
    rows = data if isinstance(data, list) else data.get("repos", data.get("rows", []))
    payload = {"rows": rows, "cached_at": datetime.now(timezone.utc).isoformat()}
    await pool.set(cache_key, json.dumps(payload), ex=300)
    # Also persist a long-lived repo→tag map used by /api/repos (survives the
    # 5-min status cache so the fleet page always shows release tags).
    release_tags = {
        row["repo"]: row.get("latest_tag", "")
        for row in rows
        if row.get("repo") and row.get("latest_tag")
    }
    if release_tags:
        await pool.set("fleet:release-tags", json.dumps(release_tags), ex=86400)
    return payload


@app.get("/api/sync/prs")
async def api_sync_prs():
    """Open PRs across the org with our integration-test CI status cross-referenced.

    Uses ``gh search prs`` (cached 5 min) then annotates each PR with
    ``_ci`` from Redis so the dashboard can show which PRs have a failing
    integration test and offer the Fix-with-AI button.
    """
    data = await _gh_json(
        "sync:prs",
        "search", "prs",
        "--owner", GH_ORG,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,repository,author,createdAt,updatedAt,url,labels",
    )
    if data.get("error") or not isinstance(data.get("rows"), list):
        return data

    # Cross-reference each PR with our Redis integration-test results.
    # gh search prs does not expose headRefName, so we key by repo only
    # (latest integration-test result per repo).
    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    ci_map: dict[str, dict] = {}
    for raw in reversed(completed_raw):  # newest first
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("type") != "integration-test":
            continue
        repo_k = j.get("repo", "")
        if repo_k not in ci_map:
            result = j.get("result", {}) or {}
            ci_map[repo_k] = {
                "passed": bool(result.get("passed")),
                "status": j.get("status", "completed"),
                "job_id": j.get("job_id", ""),
                "finished_at": j.get("finished_at", ""),
                "arch": j.get("arch") or result.get("arch") or "arm64",
            }

    for pr in data["rows"]:
        repo_nwo = (pr.get("repository") or {}).get("nameWithOwner", "")
        pr["_ci"] = ci_map.get(repo_nwo)

    return data


@app.post("/api/sync/prs/invalidate")
async def api_sync_prs_invalidate(request: Request):
    """Bust the PR cache so the next GET returns fresh data."""
    await _require_writer(request)
    await pool.delete("sync:prs")
    return {"status": "cache cleared"}


@app.get("/api/sync/issues")
async def api_sync_issues():
    """Open issues across the org (cached 5 min)."""
    return await _gh_json(
        "sync:issues",
        "search", "issues",
        "--owner", GH_ORG,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,repository,author,createdAt,updatedAt,url,labels",
    )


@app.post("/api/sync/issues/invalidate")
async def api_sync_issues_invalidate(request: Request):
    """Bust the issues cache."""
    await _require_writer(request)
    await pool.delete("sync:issues")
    return {"status": "cache cleared"}


@app.get("/api/repos/{owner}/{repo}/branches")
async def api_repo_branches(owner: str, repo: str):
    """List remote branches for a repo via GitHub API.

    Cached briefly (10 min) in Redis under ``repo:branches:{owner}/{repo}``
    to avoid hammering the GH API on every dashboard click.
    """
    cache_key = f"repo:branches:{owner}/{repo}"
    cached = await pool.get(cache_key)
    if cached:
        return json.loads(cached)

    branches: list[str] = []
    if GH_TOKEN:
        try:
            headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                page, per_page = 1, 100
                while True:
                    r = await client.get(
                        f"https://api.github.com/repos/{owner}/{repo}/branches",
                        headers=headers,
                        params={"per_page": per_page, "page": page},
                    )
                    if not r.is_success:
                        break
                    batch = [b["name"] for b in r.json()]
                    branches.extend(batch)
                    if len(batch) < per_page:
                        break
                    page += 1
        except Exception:
            pass

    if not branches:
        branches = ["main"]

    # Sort: main first, then alphabetical
    main_first = [b for b in branches if b == "main"]
    others = sorted([b for b in branches if b != "main"])
    branches = main_first + others
    payload = {"branches": branches}
    await pool.set(cache_key, json.dumps(payload), ex=600)
    return payload


def _infer_started_at(job: dict, result: dict) -> str:
    """Return the best available started_at timestamp for a completed job.

    Older job records may lack a 'timestamp' field (queue time). Fall back to
    computing start = finished_at - duration_seconds so the history table
    always shows a useful date instead of a blank.
    """
    ts = job.get("timestamp") or job.get("started_at")
    if ts:
        return ts
    finished = job.get("finished_at")
    dur = result.get("duration_seconds") or job.get("duration_seconds")
    if finished and dur:
        try:
            fin_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            return (fin_dt - timedelta(seconds=float(dur))).isoformat()
        except Exception:
            return finished
    return finished or ""


@app.get("/api/builds/history")
async def api_builds_history(
    repo: str | None = None,
    arch: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    type: str | None = None,
    limit: int = 200,
):
    """Past runs from ``jobs:completed``, filterable.

    No type param (or type=all) returns all job types.
    Pass ``type=integration-test``, ``type=deploy-ghpages``, etc. to filter.
    ``repo`` is a substring match (case-insensitive) so the search bar works.
    """
    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    rows = []
    distinct_repos: set[str] = set()
    distinct_branches: set[str] = set()
    distinct_arches: set[str] = set()
    repo_lower = repo.lower() if repo else ""
    for raw in reversed(completed_raw):  # newest first
        try:
            j = json.loads(raw)
        except Exception:
            continue
        job_type = j.get("type", "integration-test")
        if type and type != "all" and job_type != type:
            continue
        result = j.get("result", {}) or {}
        row_repo = j.get("repo", "")
        row_arch = j.get("arch") or result.get("arch") or j.get("worker_arch", "") or "unknown"
        row_branch = j.get("ref") or j.get("head_branch") or result.get("ref", "") or "main"
        row_status = j.get("status", "completed")
        distinct_repos.add(row_repo)
        if row_branch: distinct_branches.add(row_branch)
        if row_arch: distinct_arches.add(row_arch)
        if repo_lower and repo_lower not in row_repo.lower(): continue
        if arch and row_arch != arch: continue
        if branch and row_branch != branch: continue
        if status == 'failed':
            # FAIL = non-terminated jobs whose tests didn't pass
            if row_status == 'terminated': continue
            if result.get('passed'): continue
        elif status == 'passed':
            # PASS = completed jobs whose tests passed
            if row_status == 'terminated': continue
            if not result.get('passed'): continue
        elif status and row_status != status:
            continue
        # Trigger inference: nightly if id matches, else dashboard/webhook
        nightly_id = j.get("nightly_run_id", "")
        trigger = j.get("trigger") or (
            "nightly" if nightly_id.startswith("nightly-")
            else ("manual" if nightly_id.startswith("manual") else "")
        ) or "webhook"
        rows.append({
            "job_id": j.get("job_id", ""),
            "repo": row_repo,
            "arch": row_arch,
            "branch": row_branch,
            "status": row_status,
            "passed": bool(result.get("passed")),
            "duration": int(result.get("duration_seconds", 0)),
            "exit_code": result.get("exit_code"),
            "started_at": _infer_started_at(j, result),
            "finished_at": j.get("finished_at"),
            "trigger": trigger,
            "nightly_run_id": nightly_id,
            "worker_id": j.get("worker_id", "master"),
            "type": j.get("type", "integration-test"),
            "result": result,
        })
        if len(rows) >= limit: break
    return {
        "rows": rows,
        "total_returned": len(rows),
        "filters": {
            "repos": sorted(distinct_repos),
            "arches": sorted(distinct_arches),
            "branches": sorted(distinct_branches),
        },
    }


@app.get("/api/nightly/latest")
async def api_nightly_latest():
    """Latest nightly run results with per-repo build history for sparklines."""
    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    nightly_jobs = []
    for j in completed_raw:
        job = json.loads(j)
        if job.get("type") == "integration-test" and job.get("nightly_run_id", "").startswith("nightly-"):
            nightly_jobs.append(job)

    if not nightly_jobs:
        return {"run_id": None, "results": []}

    # Group by run_id
    runs: dict[str, list] = {}
    for job in nightly_jobs:
        rid = job["nightly_run_id"]
        runs.setdefault(rid, []).append(job)

    latest_id = sorted(runs.keys())[-1]
    latest = runs[latest_id]

    # Build per-(repo, arch) history across all nightly runs (oldest→newest)
    all_run_ids = sorted(runs.keys())
    repo_arch_history: dict[str, list] = {}
    for run_id in all_run_ids:
        for job in runs[run_id]:
            result = job.get("result", {}) or {}
            repo_k = job.get("repo", "")
            arch_k = job.get("arch") or result.get("arch") or job.get("worker_arch") or "arm64"
            key = f"{repo_k}|{arch_k}"
            repo_arch_history.setdefault(key, []).append({
                "passed": bool(result.get("passed")),
                "status": job.get("status", "completed"),
                "finished_at": job.get("finished_at", ""),
                "job_id": job.get("job_id", ""),
                "run_id": run_id,
            })

    results_out = []
    for job in sorted(latest, key=lambda j: j.get("repo", "")):
        result = job.get("result", {}) or {}
        arch_k = job.get("arch") or result.get("arch") or job.get("worker_arch") or "arm64"
        repo_k = job.get("repo", "")
        key = f"{repo_k}|{arch_k}"
        # History = previous nightly runs (exclude the current one), last 7
        hist = [h for h in repo_arch_history.get(key, []) if h["run_id"] != latest_id][-7:]
        results_out.append({**job, "history": hist})

    return {
        "run_id": latest_id,
        "total": len(latest),
        "passed": sum(1 for j in latest if j.get("result", {}).get("passed")),
        "failed": sum(1 for j in latest if not j.get("result", {}).get("passed")),
        "results": results_out,
    }


@app.post("/api/builds/trigger")
async def api_trigger_build(request: Request):
    """Push integration-test jobs into the local worker queue.

    For ``arch=both`` (default), pushes one job to ``queue:test:arm64`` AND
    ``queue:test:amd64`` so both architectures run in parallel.
    The local worker-manager (master ARM) and worker-agent (remote AMD)
    pick the jobs up and execute ``.devcontainer/test/integration.sh``.
    """
    role = await _require_writer(request)
    body = await request.json()
    repo = body["repo"]
    arch = body.get("arch", "both")              # arm64 | amd64 | both
    ref  = body.get("ref", "main")
    requested_by = role["user"]

    job_type = body.get("type", "integration-test")
    if job_type not in ("integration-test", "daemon"):
        raise HTTPException(400, "type must be integration-test or daemon")

    arches = ["arm64", "amd64"] if arch == "both" else [arch]
    timestamp = datetime.now(timezone.utc).isoformat()
    queued = []
    for a in arches:
        job = {
            "type": job_type,
            "repo": repo,
            "arch": a,
            "queue": f"test:{a}",
            "ref": ref,
            "timestamp": timestamp,
            "trigger": "dashboard",
            "nightly_run_id": f"manual-{int(datetime.now(timezone.utc).timestamp())}",
            "requested_by": requested_by,
        }
        await pool.rpush(f"queue:test:{a}", json.dumps(job))
        queued.append({"arch": a, "queue": f"queue:test:{a}"})

    return {"status": "queued", "repo": repo, "ref": ref, "type": job_type, "requested_by": requested_by, "jobs": queued}


@app.post("/api/jobs/{job_id}/shell-token")
async def api_shell_token(job_id: str, request: Request):
    """Issue a single-use, 60-second shell token for a running job.

    nginx guards this endpoint with auth_request (writer only).  The token
    is then passed as a query param to the WebSocket endpoint, which has no
    auth_request so nginx doesn't strip the Upgrade header.
    """
    await _require_writer(request)
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="job not running")
    token = secrets.token_hex(16)
    await pool.set(f"shell:token:{token}", job_id, ex=60)
    return {"token": token}


@app.websocket("/ws/jobs/{job_id}/shell")
async def job_shell_ws(ws: WebSocket, job_id: str, token: str = "", rows: int = 24, cols: int = 220):
    """PTY bridge: browser xterm.js ↔ docker exec inside the Sysbox container.

    Auth is via a single-use shell token (issued by /api/jobs/{id}/shell-token
    which is nginx-auth-gated).  The WebSocket location in nginx has no
    auth_request because that module is incompatible with WebSocket upgrades.
    """
    await ws.accept()

    # Validate single-use token atomically: delete it on first use.
    pipe = pool.pipeline(transaction=True)
    pipe.get(f"shell:token:{token}")
    pipe.delete(f"shell:token:{token}")
    stored_id, _ = await pipe.execute()
    if not stored_id or stored_id != job_id:
        await ws.send_bytes(b"\r\n\x1b[31mInvalid or expired shell token.\x1b[0m\r\n")
        await ws.close()
        return

    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        await ws.send_bytes(
            f"\r\n\x1b[31mJob {job_id} is not running or has already completed.\x1b[0m\r\n".encode()
        )
        await ws.close()
        return

    worker_id = meta.get("worker_id", "")
    repo = meta.get("repo", "")
    repo_name = repo.split("/")[-1] if "/" in repo else repo or "workspace"
    workspace = f"/workspaces/{repo_name}"

    # Sysbox container name mirrors executor.py: sb-{last 32 chars of job_id}
    sb_name = f"sb-{job_id[-32:]}"
    inner_exec = [
        "docker", "exec", "-it", sb_name,
        "docker", "exec", "-it",
        "-e", "TERM=xterm-256color",
        "-w", workspace,
        "dt", "zsh",
    ]

    if worker_id.startswith("worker-"):
        worker_hash = await pool.hgetall(f"worker:{worker_id}")
        ssh_host = worker_hash.get("ssh_host", "autonomous-enablements-worker")
        cmd = [
            "ssh", "-t",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            ssh_host,
        ] + inner_exec
    else:
        cmd = inner_exec

    log.info("Shell open: job=%s worker=%s sb=%s rows=%s cols=%s", job_id, worker_id or "local", sb_name, rows, cols)
    await _pty_bridge(ws, cmd, rows=rows, cols=cols)
    log.info("Shell closed: job=%s", job_id)


async def _pty_bridge(ws: WebSocket, cmd: list[str], rows: int = 24, cols: int = 220):
    """Create a PTY subprocess and bridge its I/O to the WebSocket.

    Uses loop.add_reader for non-blocking PTY output so the reader task is
    a proper asyncio coroutine that CAN be cancelled when the WebSocket
    disconnects — avoiding the deadlock that run_in_executor causes when
    os.read blocks in a thread that can't be interrupted.
    """
    master_fd, slave_fd = pty.openpty()
    # Set PTY size before starting the subprocess so applications (k9s, kubectl
    # completions, etc.) see the correct dimensions from the very first ioctl.
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
    except OSError:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave_fd)  # parent doesn't need the slave end
    except Exception as exc:
        try:
            os.close(slave_fd)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        await ws.send_bytes(f"\r\n\x1b[31mFailed to start shell: {exc}\x1b[0m\r\n".encode())
        return

    loop = asyncio.get_running_loop()
    pty_out: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_pty_readable():
        try:
            data = os.read(master_fd, 4096)
            pty_out.put_nowait(data)
        except OSError:
            # PTY EOF — subprocess exited or fd was closed
            pty_out.put_nowait(None)
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

    loop.add_reader(master_fd, _on_pty_readable)

    async def _pty_to_ws():
        while True:
            chunk = await pty_out.get()
            if chunk is None:
                break
            try:
                await ws.send_bytes(chunk)
            except Exception:
                break

    async def _ws_to_pty():
        while True:
            try:
                msg = await ws.receive()
            except (WebSocketDisconnect, Exception):
                break
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                text = msg["text"]
                try:
                    ev = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    ev = None
                if isinstance(ev, dict) and ev.get("type") == "resize":
                    try:
                        rows = max(1, int(ev.get("rows", 24)))
                        cols = max(1, int(ev.get("cols", 80)))
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0))
                    except (ValueError, OSError):
                        pass
                else:
                    try:
                        os.write(master_fd, text.encode())
                    except OSError:
                        break
            elif msg.get("bytes"):
                try:
                    os.write(master_fd, msg["bytes"])
                except OSError:
                    break

    t_out = asyncio.create_task(_pty_to_ws())
    t_in = asyncio.create_task(_ws_to_pty())
    try:
        await asyncio.wait({t_out, t_in}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        t_out.cancel()
        t_in.cancel()
        try:
            await asyncio.gather(t_out, t_in, return_exceptions=True)
        except Exception:
            pass
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass


async def _read_app_registry(job_id: str, meta: dict) -> list[dict]:
    """Read the .app-registry file from inside the running job's dt container.

    Uses the same SSH + docker exec chain as the shell bridge. Results are
    cached in Redis for 60 s to avoid exec overhead on every proxy request.
    """
    cache_key = f"job:apps:{job_id}"
    cached = await pool.get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    worker_id = meta.get("worker_id", "")
    sb_name = f"sb-{job_id[-32:]}"
    # App registry is written by the framework's registerApp() helper to
    # ${HOME}/.cache/dt-framework/app-registry (HOME=/home/vscode inside dt).
    registry_path = "/home/vscode/.cache/dt-framework/app-registry"

    cmd = ["docker", "exec", sb_name, "docker", "exec", "dt", "cat", registry_path]
    if worker_id.startswith("worker-"):
        worker_hash = await pool.hgetall(f"worker:{worker_id}")
        ssh_host = worker_hash.get("ssh_host", "")
        if ssh_host:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                "-o", "BatchMode=yes",
                ssh_host,
            ] + cmd

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:
        return []

    apps = []
    for line in stdout.decode().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 5:
            apps.append({
                "name": parts[0],
                "namespace": parts[1],
                "service": parts[2],
                "port": parts[3],
                "ingress_host": parts[4],
            })

    await pool.set(cache_key, json.dumps(apps), ex=60)
    return apps


@app.get("/api/jobs/{job_id}/apps")
async def api_job_apps(job_id: str):
    """List apps registered in the job's .app-registry with their proxy URLs."""
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="job not running")

    apps = await _read_app_registry(job_id, meta)
    return {
        "apps": [
            {**a, "proxy_url": f"/apps/{job_id}/{a['name']}/"}
            for a in apps
        ]
    }


def _rewrite_proxy_body(content: bytes, base_path: str, content_type: str) -> bytes:
    """Rewrite root-relative URLs in HTML/CSS proxy responses.

    For HTML: rewrites src/href/action attributes and injects a JS shim that
    patches fetch() and XMLHttpRequest so dynamic API calls (e.g. $.ajax('/todos'))
    are transparently prefixed with the proxy base path at runtime.

    For CSS: rewrites url(/...) patterns so background images load correctly.
    """
    import re as _re
    ct = content_type.lower()
    is_html = "html" in ct or "xhtml" in ct
    is_css = "css" in ct
    if not (is_html or is_css):
        return content

    charset = "utf-8"
    if "charset=" in ct:
        charset = ct.split("charset=")[-1].strip().split(";")[0].strip()
    try:
        text = content.decode(charset, errors="replace")
    except Exception:
        return content

    # Rewrite root-relative url(...) in CSS and inline HTML styles.
    # Excludes protocol-relative //... and data: URIs.
    def _rewrite_css_url(m: "_re.Match") -> str:
        val = m.group(1).strip("'\"")
        if val.startswith("/") and not val.startswith("//"):
            if not val.startswith(base_path):
                return f"url({base_path}{val})"
        elif _re.match(r'^https?://localhost(:\d+)?/', val):
            path_part = _re.sub(r'^https?://localhost(:\d+)?', '', val)
            path_part = _re.sub(r'^//+', '/', path_part)
            if not path_part.startswith(base_path):
                return f"url({base_path}{path_part})"
        return m.group(0)

    text = _re.sub(r"url\(([^)]*)\)", _rewrite_css_url, text)

    if is_html:
        # Rewrite src="/" href="/" action="/" data-src="/" attributes.
        # Also handles absolute http://localhost:PORT/... URLs that Next.js / some
        # apps emit (e.g. <img src="http://localhost:8080/icons/foo.svg">).
        def _rewrite_attr_value(val: str) -> str:
            # Root-relative /path
            if val.startswith("/") and not val.startswith("//"):
                if not val.startswith(base_path):
                    return base_path + val
            # Absolute localhost URL — strip the origin
            elif _re.match(r'^https?://localhost(:\d+)?/', val):
                path_part = _re.sub(r'^https?://localhost(:\d+)?', '', val)
                # Normalise accidental double-slash after stripping origin
                path_part = _re.sub(r'^//+', '/', path_part)
                if not path_part.startswith(base_path):
                    return base_path + path_part
            return val

        # Rewrite resource-loading attributes on all tags.
        # Deliberately excludes href — <a href> must NOT be rewritten because
        # Next.js/React reads those values to determine client-side routes; if we
        # prefix them the router navigates to the wrong path and renders a blank
        # page.  href on <link> tags (stylesheets, icons, preloads) is handled
        # separately below.
        for attr in ("src", "action", "data-src"):
            text = _re.sub(
                rf'{attr}="([^"]*)"',
                lambda m, a=attr: f'{a}="{_rewrite_attr_value(m.group(1))}"',
                text,
            )
            text = _re.sub(
                rf"{attr}='([^']*)'",
                lambda m, a=attr: f"{a}='{_rewrite_attr_value(m.group(1))}'",
                text,
            )

        # Rewrite href only on <link> tags (CSS, icons, preloads, canonical).
        # Single-line tag assumption holds for all known SSR frameworks.
        text = _re.sub(
            r'(<link\b[^>]*?\bhref=")([^"]*?)(")',
            lambda m: m.group(1) + _rewrite_attr_value(m.group(2)) + m.group(3),
            text,
            flags=_re.IGNORECASE,
        )
        text = _re.sub(
            r"(<link\b[^>]*?\bhref=')([^']*?)(')",
            lambda m: m.group(1) + _rewrite_attr_value(m.group(2)) + m.group(3),
            text,
            flags=_re.IGNORECASE,
        )

        # srcset has comma-separated "URL [descriptor]" pairs — rewrite each URL.
        def _rewrite_srcset(m: "_re.Match") -> str:
            quote = m.group(1)
            parts = []
            for entry in m.group(2).split(","):
                entry = entry.strip()
                if not entry:
                    continue
                tokens = entry.split(None, 1)
                rewritten = _rewrite_attr_value(tokens[0])
                parts.append(rewritten + (" " + tokens[1] if len(tokens) > 1 else ""))
            return f'srcset={quote}{", ".join(parts)}{quote}'

        text = _re.sub(r'srcset=(["\'])([^"\']*)\1', _rewrite_srcset, text, flags=_re.IGNORECASE)

        # Also rewrite Location: root-relative in meta refresh tags.
        text = _re.sub(
            r'(content="\d+;\s*url=)(/(?!/)[^"]*)',
            lambda m: f"{m.group(1)}{base_path}{m.group(2)}",
            text,
        )

        # Inject a JS shim that:
        # - Rewrites root-relative and absolute localhost URLs in fetch() / XHR
        #   so dynamic API calls go through the proxy base path.
        # - Patches history.pushState / history.replaceState so Next.js-style
        #   client-side navigation stays inside the proxy path (prevents iframe
        #   URL from escaping to the ops dashboard root).
        shim = (
            f"<script>"
            f"(function(){{"
            f"var B='{base_path}';"
            f"function r(u){{"
            f"if(typeof u!=='string')return u;"
            f"if(u.charAt(0)==='/'&&u.charAt(1)!=='/'&&u.indexOf(B)!==0)return B+u;"
            f"if(/^https?:\\/\\/localhost(:\\d+)?\\//.test(u)){{"
            f"try{{var p=new URL(u);var q=p.pathname.replace(/^\\/\\//,'/');if(q.indexOf(B)!==0)return B+q+(p.search||'')+(p.hash||'');}}catch(e){{}}"
            f"}}"
            f"return u;"
            f"}}"
            f"var _f=window.fetch;"
            f"window.fetch=function(i,o){{return _f.call(this,typeof i==='string'?r(i):i,o);}};"
            f"var _x=XMLHttpRequest.prototype.open;"
            f"XMLHttpRequest.prototype.open=function(m,u){{arguments[1]=r(String(u));return _x.apply(this,arguments);}};"
            f"var _ps=history.pushState.bind(history);"
            f"history.pushState=function(s,t,u){{return _ps(s,t,u!=null?r(String(u)):u);}};"
            f"var _rs=history.replaceState.bind(history);"
            f"history.replaceState=function(s,t,u){{return _rs(s,t,u!=null?r(String(u)):u);}};"
            f"}})();"
            f"</script>"
        )
        if "</head>" in text:
            text = text.replace("</head>", shim + "</head>", 1)
        elif "<head>" in text:
            text = text.replace("<head>", "<head>" + shim, 1)
        else:
            text = shim + text

    try:
        return text.encode(charset, errors="replace")
    except Exception:
        return content


@app.api_route(
    "/apps/{job_id}/{app_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@app.api_route(
    "/apps/{job_id}/{app_name}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def proxy_job_app(job_id: str, app_name: str, request: Request, path: str = ""):
    """Reverse-proxy to an app running inside a job's k3d cluster.

    Connects directly to the Sysbox's published port (allocated at job start)
    on the worker host, setting the Host header so nginx ingress can route
    to the right service.  No SSH tunnel required — the master's private IP
    is allowed inbound on the port range via the worker security group.
    """
    meta = await pool.hgetall(f"job:running:{job_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="job not running")

    app_proxy_port = meta.get("app_proxy_port")
    if not app_proxy_port:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:32px;background:#050810;color:#d0d7de'>"
            "<h3 style='color:#f0b429'>App proxy not available</h3>"
            "<p>This job was started before app port forwarding was added, or the worker agent "
            "needs to be updated.</p>"
            "<p>Terminate this job and start a new Training session to enable app preview.</p>"
            "</body></html>",
            status_code=503,
        )

    apps = await _read_app_registry(job_id, meta)
    app_info = next((a for a in apps if a["name"] == app_name), None)
    if not app_info:
        raise HTTPException(status_code=404, detail=f"app '{app_name}' not found in registry")

    worker_id = meta.get("worker_id", "")
    if worker_id.startswith("worker-"):
        worker_hash = await pool.hgetall(f"worker:{worker_id}")
        target_ip = worker_hash.get("host", "127.0.0.1")
    else:
        target_ip = "127.0.0.1"

    target_url = f"http://{target_ip}:{app_proxy_port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    # Forward a minimal set of headers; always override Host for ingress routing.
    forward_headers = {
        "Host": app_info["ingress_host"],
    }
    for h in ("accept", "accept-language", "cookie",
               "content-type", "cache-control", "x-requested-with"):
        if h in request.headers:
            forward_headers[h] = request.headers[h]
    # Don't forward accept-encoding: we decode the response body for URL
    # rewriting, so the upstream should send uncompressed content.

    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body,
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="could not connect to app — is the cluster ready?")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="upstream app timed out")

    # Strip hop-by-hop headers and security headers that must not be forwarded.
    # x-frame-options is stripped here so nginx's SAMEORIGIN (set in the
    # /apps/ location block) is the only value the browser sees.
    skip = {"transfer-encoding", "connection", "keep-alive", "upgrade",
            "proxy-authenticate", "proxy-authorization", "te", "trailers",
            "x-frame-options", "content-security-policy",
            "content-security-policy-report-only"}
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in skip
    }

    # Rewrite Location redirects so the browser stays within the proxy.
    location = resp_headers.get("location", "")
    if location:
        if location.startswith("http://") or location.startswith("https://"):
            # Absolute redirect from upstream — keep it inside the proxy.
            resp_headers["location"] = f"/apps/{job_id}/{app_name}/"
        elif location.startswith("/") and not location.startswith("//"):
            # Root-relative redirect — prefix with proxy base.
            resp_headers["location"] = f"/apps/{job_id}/{app_name}{location}"

    # Rewrite root-relative URLs in HTML/CSS so assets and API calls resolve
    # through the proxy instead of hitting the ops dashboard root.
    content_type = upstream.headers.get("content-type", "")
    body = _rewrite_proxy_body(upstream.content, f"/apps/{job_id}/{app_name}", content_type)

    # Remove content-encoding now that we've decoded/re-encoded the body.
    resp_headers.pop("content-encoding", None)
    resp_headers.pop("content-length", None)

    from fastapi.responses import Response as PlainResponse
    return PlainResponse(
        content=body,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


@app.get("/api/health")
async def api_health():
    """Platform health overview."""
    try:
        await pool.ping()

        # Worker count (skip port-pool list keys)
        worker_count = 0
        async for key in pool.scan_iter("worker:*"):
            if not key.endswith(":app_ports_free"):
                worker_count += 1

        # Queue depths
        queues = {
            "test:arm64": await pool.llen("queue:test:arm64"),
            "test:amd64": await pool.llen("queue:test:amd64"),
            "agent": await pool.llen("queue:agent"),
            "sync": await pool.llen("queue:sync"),
        }

        return {
            "status": "healthy",
            "redis": "connected",
            "workers": worker_count,
            "queues": queues,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


def start():
    """Entry point for systemd service."""
    import uvicorn
    uvicorn.run(
        "dashboard.app:app",
        host="127.0.0.1",
        port=8080,
        log_level="info",
    )


if __name__ == "__main__":
    start()

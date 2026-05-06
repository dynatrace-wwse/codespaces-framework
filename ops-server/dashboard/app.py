"""Dashboard — web UI and API for the multi-arch ops platform."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import redis.asyncio as redis

from webhook.config import REDIS_URL, FRAMEWORK_DIR

# GitHub token used to dispatch workflow_run events. Required for the
# /api/builds/trigger endpoint. Generate a fine-grained PAT with
# `actions:write` and `contents:read` for the org's repos.
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GH_API   = "https://api.github.com"

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


# ── UI Routes ────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Fleet overview dashboard."""
    return templates.TemplateResponse(request, "index.html")


# ── API Routes ───────────────────────────────────────────────────────────────


@app.get("/api/repos")
async def api_repos():
    """List all repos with their latest GitHub Actions build matrix.

    Reads ``ci:<repo>:<workflow>:main`` hashes that the webhook server
    populates from ``workflow_run.completed`` events. The convention is
    one workflow per arch — ``Integration Test arm64`` and
    ``Integration Test amd64`` — but any workflow whose name ends in
    ``arm64`` or ``amd64`` is picked up.
    """
    import yaml

    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    repos_out = []
    for r in data.get("repos", []):
        if r.get("status") != "active":
            continue
        repo_full = r["repo"]
        builds: dict[str, dict] = {}
        # Find any ci:<repo>:*:main keys for this repo
        async for key in pool.scan_iter(match=f"ci:{repo_full}:*:main"):
            wf_data = await pool.hgetall(key)
            if not wf_data:
                continue
            # Detect arch from workflow name suffix
            workflow = wf_data.get("workflow", "")
            arch = None
            for a in ("arm64", "amd64"):
                if workflow.lower().endswith(a):
                    arch = a
                    break
            if not arch:
                # Fall back: trust whatever's in the record
                arch = wf_data.get("arch") or "amd64"
            builds[arch] = {
                "passed": wf_data.get("conclusion") == "success",
                "conclusion": wf_data.get("conclusion", "unknown"),
                "duration": int(wf_data.get("duration_seconds", 0)),
                "finished_at": wf_data.get("finished_at", ""),
                "run_url": wf_data.get("run_url", ""),
                "run_number": wf_data.get("run_number", ""),
            }

        repos_out.append({
            "name": r["name"],
            "repo": repo_full,
            "arch": r.get("arch", "both"),
            "duration": r.get("duration", "1h"),
            "ci": r.get("ci", True),
            "builds": builds,
        })

    return {"repos": repos_out, "total": len(repos_out)}


@app.get("/api/workers")
async def api_workers():
    """List registered workers and their status."""
    worker_keys = []
    async for key in pool.scan_iter("worker:*"):
        worker_keys.append(key)

    workers = []
    for key in worker_keys:
        data = await pool.hgetall(key)
        if data:
            data["worker_id"] = key.replace("worker:", "")
            workers.append(data)

    return {"workers": workers, "total": len(workers)}


@app.get("/api/builds/running")
async def api_builds_running():
    """Currently executing jobs (from all workers)."""
    # Active jobs are tracked in worker hashes — we can't see them directly
    # Instead, show queue depths as a proxy
    queues = {}
    for arch in ("arm64", "amd64"):
        queues[arch] = await pool.llen(f"queue:test:{arch}")
    queues["agent"] = await pool.llen("queue:agent")
    queues["sync"] = await pool.llen("queue:sync")

    return {"queues": queues}


@app.get("/api/nightly/latest")
async def api_nightly_latest():
    """Latest nightly run results."""
    completed_raw = await pool.lrange("jobs:completed", -200, -1)
    nightly_jobs = []
    for j in completed_raw:
        job = json.loads(j)
        if job.get("type") == "integration-test" and job.get("nightly_run_id", "").startswith("nightly-"):
            nightly_jobs.append(job)

    if not nightly_jobs:
        return {"run_id": None, "results": []}

    # Group by run_id, get latest
    runs: dict[str, list] = {}
    for job in nightly_jobs:
        rid = job["nightly_run_id"]
        runs.setdefault(rid, []).append(job)

    latest_id = sorted(runs.keys())[-1]
    latest = runs[latest_id]

    return {
        "run_id": latest_id,
        "total": len(latest),
        "passed": sum(1 for j in latest if j.get("result", {}).get("passed")),
        "failed": sum(1 for j in latest if not j.get("result", {}).get("passed")),
        "results": sorted(latest, key=lambda j: j.get("repo", "")),
    }


@app.post("/api/builds/trigger")
async def api_trigger_build(request: Request):
    """Trigger GitHub Actions workflow_dispatch for the given repo.

    Convention: each repo has ``integration-arm64.yml`` and ``integration-amd64.yml``
    workflows. Calling with ``arch=both`` dispatches both; ``arm64`` or ``amd64``
    dispatches just one. See ``ops-server/templates/integration-{arch}.yml``.
    """
    if not GH_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="GH_TOKEN not configured on the server — cannot dispatch workflows.",
        )

    body = await request.json()
    repo = body["repo"]                          # "dynatrace-wwse/codespaces-framework"
    arch = body.get("arch", "both")              # arm64 | amd64 | both
    ref  = body.get("ref", "main")
    requested_by = body.get("requested_by",
                            request.headers.get("x-auth-user", "dashboard"))

    arches = ["arm64", "amd64"] if arch == "both" else [arch]
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for a in arches:
            workflow = f"integration-{a}.yml"
            url = f"{GH_API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
            payload = {"ref": ref, "inputs": {"requested_by": requested_by}}
            r = await client.post(url, json=payload, headers=headers)
            ok = r.status_code in (201, 204)
            results.append({
                "arch": a,
                "workflow": workflow,
                "status_code": r.status_code,
                "ok": ok,
                "error": None if ok else r.text[:200],
            })
            if ok:
                log.info("Dispatched %s/%s ref=%s by %s", repo, workflow, ref, requested_by)
            else:
                log.warning("Dispatch failed %s/%s: %s %s",
                            repo, workflow, r.status_code, r.text[:200])

    failed = [x for x in results if not x["ok"]]
    if failed and len(failed) == len(results):
        raise HTTPException(status_code=502, detail={"results": results})

    return {
        "status": "dispatched" if not failed else "partial",
        "repo": repo,
        "ref": ref,
        "requested_by": requested_by,
        "results": results,
    }


@app.get("/api/health")
async def api_health():
    """Platform health overview."""
    try:
        await pool.ping()

        # Worker count
        worker_count = 0
        async for _ in pool.scan_iter("worker:*"):
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

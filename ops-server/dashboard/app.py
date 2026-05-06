"""Dashboard — web UI and API for the multi-arch ops platform."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import redis.asyncio as redis

from webhook.config import REDIS_URL, FRAMEWORK_DIR

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
    """List all repos with their build matrix (last ARM/AMD result)."""
    import yaml

    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    # Get recent completed jobs for build matrix
    completed_raw = await pool.lrange("jobs:completed", -200, -1)
    completed = [json.loads(j) for j in completed_raw]

    # Build a map: repo → {arm64: result, amd64: result}
    build_matrix: dict[str, dict] = {}
    for job in completed:
        if job.get("type") != "integration-test":
            continue
        repo = job["repo"]
        arch = job.get("result", {}).get("arch", job.get("worker_arch", "arm64"))
        if repo not in build_matrix:
            build_matrix[repo] = {}
        # Keep the latest result per arch
        build_matrix[repo][arch] = {
            "passed": job.get("result", {}).get("passed", False),
            "duration": job.get("result", {}).get("duration_seconds", 0),
            "finished_at": job.get("finished_at", ""),
            "job_id": job.get("job_id", ""),
        }

    repos_out = []
    for r in data.get("repos", []):
        if r.get("status") != "active":
            continue
        repo_full = r["repo"]
        repos_out.append({
            "name": r["name"],
            "repo": repo_full,
            "arch": r.get("arch", "both"),
            "duration": r.get("duration", "1h"),
            "ci": r.get("ci", True),
            "builds": build_matrix.get(repo_full, {}),
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
    """Manually trigger a build for a repo on specified architecture."""
    body = await request.json()
    repo = body["repo"]
    arch = body.get("arch", "both")
    requested_by = body.get("requested_by", "dashboard")

    jobs_queued = []
    timestamp = datetime.now(timezone.utc).isoformat()

    arches = [arch] if arch != "both" else ["arm64", "amd64"]
    for a in arches:
        job = {
            "type": "integration-test",
            "repo": repo,
            "arch": a,
            "queue": "test",
            "timestamp": timestamp,
            "nightly_run_id": "manual",
            "requested_by": requested_by,
        }
        await pool.rpush(f"queue:test:{a}", json.dumps(job))
        jobs_queued.append({"arch": a, "queue": f"queue:test:{a}"})

    return {"status": "queued", "repo": repo, "jobs": jobs_queued}


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

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
    """List all repos with the latest build matrix.

    Merges two data sources:
      - ``jobs:completed``     — local worker results (primary, links to /api/jobs/<id>/log)
      - ``ci:<repo>:*:main``   — GHA workflow_run events (used as fallback)
    """
    import yaml

    repos_path = FRAMEWORK_DIR / "repos.yaml"
    with open(repos_path) as f:
        data = yaml.safe_load(f)

    completed_raw = await pool.lrange("jobs:completed", -200, -1)
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
    """Plain-text live log for an in-flight test (updated ~1s by the worker)."""
    from fastapi.responses import PlainTextResponse
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
    deferred). Returns 404 if the job is not currently running.
    """
    if not await pool.exists(f"job:running:{job_id}"):
        raise HTTPException(404, f"Job {job_id} is not currently running")
    requested_by = request.headers.get("x-auth-user", "dashboard")
    await pool.publish("ops:terminate", job_id)
    log.info("Termination requested for %s by %s", job_id, requested_by)
    return {"status": "termination_requested", "job_id": job_id, "requested_by": requested_by}


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
           display:flex; align-items:center; gap:14px; }
  header h1 { margin:0; font-size:14px; font-weight:600; flex:1;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  header .status { padding:2px 8px; border-radius:10px; font-size:11px;
                   background:#1f6feb22; color:#58a6ff; }
  header .status.done { background:#23863622; color:#3fb950; }
  header .status.failed { background:#da363322; color:#f85149; }
  header .status.terminated { background:#d2932922; color:#d29922; }
  pre { margin:0; padding:14px; white-space:pre-wrap; word-break:break-word;
        height:calc(100vh - 41px); overflow:auto; }
  .ansi-bold { font-weight:bold; }
  .ansi-red { color:#f85149; } .ansi-green { color:#3fb950; }
  .ansi-yellow { color:#d29922; } .ansi-blue { color:#58a6ff; }
  .ansi-magenta { color:#bc8cff; } .ansi-cyan { color:#39c5cf; }
  .ansi-white { color:#c9d1d9; } .ansi-gray { color:#8b949e; }
</style>
</head><body>
<header><h1>""" + job_id + """</h1><span class="status" id="status">running</span></header>
<pre id="log">Loading…</pre>
<script>
const JOB_ID = """ + json.dumps(job_id) + """;
const ANSI_RE = /\\x1b\\[([0-9;]*)m/g;
const COLORS = {30:'gray',31:'red',32:'green',33:'yellow',34:'blue',35:'magenta',36:'cyan',37:'white',
                90:'gray',91:'red',92:'green',93:'yellow',94:'blue',95:'magenta',96:'cyan',97:'white'};
function escapeHtml(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
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
let poll = null;
async function tick(){
  try {
    let res = await fetch('/api/jobs/'+JOB_ID+'/livelog');
    let live = true;
    if(res.status===404){
      res = await fetch('/api/jobs/'+JOB_ID+'/log');
      live = false;
      if(poll){ clearInterval(poll); poll=null; }
      const term = await fetch('/api/jobs/'+JOB_ID+'/status').catch(()=>null);
      if(term && term.ok){
        const j = await term.json();
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
      pre.innerHTML = ansiToHtml(text);
      if(wasAtBottom) pre.scrollTop = pre.scrollHeight;
    }
  } catch(e){}
}
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


@app.get("/api/builds/history")
async def api_builds_history(
    repo: str | None = None,
    arch: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    limit: int = 200,
):
    """Past integration-test runs from ``jobs:completed``, filterable.

    Source of truth is the trimmed ``jobs:completed`` LIST (last 500). When
    Phase 2 of the design doc lands, this should switch to ``builds:by_time``
    ZSET for richer time-range queries.
    """
    completed_raw = await pool.lrange("jobs:completed", -500, -1)
    rows = []
    distinct_repos: set[str] = set()
    distinct_branches: set[str] = set()
    distinct_arches: set[str] = set()
    for raw in reversed(completed_raw):  # newest first
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if j.get("type") != "integration-test":
            continue
        result = j.get("result", {}) or {}
        row_repo = j.get("repo", "")
        row_arch = j.get("arch") or result.get("arch") or j.get("worker_arch", "") or "unknown"
        row_branch = j.get("ref") or j.get("head_branch") or result.get("ref", "") or "main"
        row_status = j.get("status", "completed")
        distinct_repos.add(row_repo)
        if row_branch: distinct_branches.add(row_branch)
        if row_arch: distinct_arches.add(row_arch)
        if repo and row_repo != repo: continue
        if arch and row_arch != arch: continue
        if branch and row_branch != branch: continue
        if status and row_status != status: continue
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
            "started_at": j.get("timestamp"),
            "finished_at": j.get("finished_at"),
            "trigger": trigger,
            "nightly_run_id": nightly_id,
            "worker_id": j.get("worker_id", "master"),
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
    """Push integration-test jobs into the local worker queue.

    For ``arch=both`` (default), pushes one job to ``queue:test:arm64`` AND
    ``queue:test:amd64`` so both architectures run in parallel.
    The local worker-manager (master ARM) and worker-agent (remote AMD)
    pick the jobs up and execute ``.devcontainer/test/integration.sh``.
    """
    body = await request.json()
    repo = body["repo"]
    arch = body.get("arch", "both")              # arm64 | amd64 | both
    ref  = body.get("ref", "main")
    requested_by = body.get("requested_by",
                            request.headers.get("x-auth-user", "dashboard"))

    arches = ["arm64", "amd64"] if arch == "both" else [arch]
    timestamp = datetime.now(timezone.utc).isoformat()
    queued = []
    for a in arches:
        job = {
            "type": "integration-test",
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

    return {"status": "queued", "repo": repo, "ref": ref, "requested_by": requested_by, "jobs": queued}


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

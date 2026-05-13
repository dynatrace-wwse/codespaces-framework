# Ops-Server — Architecture & Implementation Notes

Quick reference for Claude so context doesn't need to be re-derived every session.

---

## Stack layout

```
Browser
  └─▶ nginx (443, HTTP/1.1 only — see §Nginx below)
        ├─▶ oauth2-proxy (4180) — GitHub SSO, sets _oauth2_proxy cookie
        ├─▶ FastAPI / uvicorn (8080) — dashboard API + WebSocket PTY bridge
        └─▶ webhook server (8443) — HMAC-signed GitHub webhooks

FastAPI (app.py)
  └─▶ Redis — job queues, running state, history, logs, auth role cache

Worker agents (worker-agent/agent.py)  ←──── Redis ────────▶ master
  └─▶ executor.py — pulls jobs, runs Sysbox containers, publishes logs
```

**Redis key space (important ones):**
- `queue:test:{arch}` — FIFO job queue per arch
- `job:running:{job_id}` — hash: repo, branch, arch, started_at, worker_id
- `jobs:completed` — list (capped 500) of JSON job records
- `worker:{worker_id}` — hash: arch, capacity, active_jobs, host, ssh_host
- `job:log:{job_id}` — raw log text, 7-day TTL
- `shell:token:{token}` — single-use WebSocket auth token, 60-second TTL
- `deferred:{repo}:{branch}:{arch}` — jobs waiting behind a running lock
- `running:lock:{repo}:{branch}:{arch}` — concurrency lock, 2h TTL

---

## Auth flow

nginx uses `auth_request /oauth2/auth` (oauth2-proxy sub-request) to gate write
endpoints. On success nginx injects `X-Auth-User` and `X-Auth-Email` headers.
FastAPI reads `X-Auth-User` and calls `_resolve_role(user)` which:
1. Checks `auth:role:{user}` Redis cache (10 min TTL).
2. On cache miss, calls GitHub `/orgs/{GH_ORG}/memberships/{user}` with `GH_TOKEN`.
3. Returns `{role: "writer"}` if org member, `{role: "guest"}` otherwise.

`_require_writer(request)` is the FastAPI dependency used by all write endpoints.

---

## Shell / WebSocket PTY bridge

### Why a two-step token flow

`auth_request` in nginx is **incompatible with WebSocket upgrades**. After the
auth sub-request completes, nginx does not properly forward `Upgrade: websocket`
to the backend — FastAPI sees a plain HTTP GET and returns 404.

The fix is a two-step flow:

1. **Token endpoint** (`POST /api/jobs/{job_id}/shell-token`) — normal HTTP,
   guarded by nginx `auth_request`. FastAPI issues a 60-second single-use token
   stored in Redis as `shell:token:{token} → job_id`.

2. **WebSocket endpoint** (`GET /ws/jobs/{job_id}/shell?token=…`) — no
   `auth_request` in nginx (plain proxy). FastAPI validates and atomically
   deletes the token from Redis via a `MULTI/EXEC` pipeline before proceeding.

### Why nginx must NOT use HTTP/2 (`http2` removed from listen)

With `listen 443 ssl http2`, nginx advertises `h2` as the preferred ALPN
protocol. Chrome reuses its existing H2 connection for WebSocket. Nginx 1.24
does **not** support RFC 8441 (WebSocket over HTTP/2 extended CONNECT), so the
upgrade is silently dropped — the browser sees a protocol error and fires
`ws.onerror` immediately.

Fix: `listen 443 ssl;` (no `http2`). Nginx then negotiates `http/1.1` in ALPN,
the standard WebSocket upgrade (`HTTP/1.1 101 Switching Protocols`) works.

### PTY bridge internals (`_pty_bridge` in app.py)

```
ws.receive()  ←──── browser xterm.js
     │
     ▼
os.write(master_fd)          ─────┐
pty.openpty()                      │  PTY pair
os.read(master_fd) via add_reader  │
     │                        ─────┘
     ▼
subprocess: ssh -t {worker} docker exec -it sb-{id} docker exec -it -w /workspaces/{repo} dt zsh
```

- Uses `loop.add_reader(master_fd, callback)` + asyncio Queue for non-blocking
  PTY reads (avoids the `run_in_executor` deadlock when WS disconnects while
  the thread is blocked on `os.read`).
- `asyncio.wait({t_out, t_in}, FIRST_COMPLETED)` + `task.cancel()` ensures
  clean shutdown when either side disconnects first.
- Resize events are JSON `{type: "resize", rows, cols}` sent as text frames;
  all other text/binary frames are raw terminal input forwarded to `master_fd`.

### Container naming

The Sysbox outer container is always named `sb-{job_id[-32:]}` (last 32 chars
of job_id). The inner DinD container is always named `dt`. Full exec chain:

```
docker exec -it sb-{id} docker exec -it -w /workspaces/{repo} dt zsh
```

For **remote workers** (AMD): `job:running:{id}` has `worker_id` starting with
`worker-`. FastAPI looks up `worker:{worker_id}` in Redis to get `ssh_host`,
then prepends `ssh -t -o StrictHostKeyChecking=no -o ConnectTimeout=10 {host}`.

---

## Job types

| Type | Handler | Sysbox | Locks | Shell | Description |
|------|---------|--------|-------|-------|-------------|
| `integration-test` | `_run_integration_test` | yes | per-triple | yes (while running) | Full CI: postCreate + postStart + integration.sh |
| `daemon` | `_run_daemon` | yes | none | yes (indefinitely) | postCreate + postStart, then blocks until terminated — for training sessions |
| `fix-ci` / `fix-issue` / etc. | `_run_agent` | no | none | no | Claude Code agents |
| `sync-command` | `_run_sync_command` | no | none | no | Sync CLI commands |

**Important:** shell sessions into `integration-test` jobs disconnect when the test finishes and the container is torn down.  Use `daemon` jobs for interactive training sessions — the container stays alive until manually terminated.

### Path layout — canonical

Both master and AMD worker use the same layout under the `ops` user:

```
/home/ops/enablement-framework/codespaces-framework/   ← services run here (ops user)
/home/ubuntu/enablement-framework/codespaces-framework/ ← edits happen here (master only)
```

**Master only** has both paths (edit + production). The AMD worker only has the `ops` path.

AMD worker git pull (future):
```bash
ssh autonomous-enablements-worker \
  "sudo -u ops git -C /home/ops/enablement-framework/codespaces-framework pull"
```

After editing on master (`ubuntu` path), sync to production (`ops` path) and restart:
```bash
sudo cp /home/ubuntu/enablement-framework/codespaces-framework/ops-server/workers/manager.py \
        /home/ops/enablement-framework/codespaces-framework/ops-server/workers/manager.py
sudo cp /home/ubuntu/enablement-framework/codespaces-framework/ops-server/dashboard/app.py \
        /home/ops/enablement-framework/codespaces-framework/ops-server/dashboard/app.py
# ... static files, templates, etc.
sudo systemctl restart ops-dashboard ops-worker
```

> **Note:** AMD worker was historically at `/home/ops/codespaces-framework/` (no wrapper dir).
> Migration to the canonical path: stop service → clone to new path → update systemd WorkingDirectory → restart.

---

## Nginx location order (relevant blocks)

All regex locations (`~`) take priority over `location /`.

| Location | Auth | Notes |
|----------|------|-------|
| `/api/auth/role` | opportunistic (error_page 401 → anonymous fallback) | returns guest if not signed in |
| `/api/builds/trigger`, `/api/sync/run`, etc. | `auth_request` hard | writer-only POST endpoints |
| `/api/jobs/[^/]+/shell-token` | `auth_request` hard | issues single-use WS token |
| `/api/jobs/[^/]+/terminate` | `auth_request` hard | kills a running job |
| `/ws/jobs/[^/]+/shell` | **none** (token validated in FastAPI) | `proxy_buffering off`, `proxy_read_timeout 3600` |
| `/` | none | public read-only pass-through |

---

## Worker registration

Workers call `_register()` on startup, writing a hash to `worker:{WORKER_ID}`:
- `arch`, `capacity`, `active_jobs`, `status`, `host`, `ssh_host`
- `WORKER_HOST` is auto-detected via UDP connect trick (`socket → 8.8.8.8:80`).
- `WORKER_SSH_HOST` overrides via env var (useful when the SSH-reachable address
  differs from the private IP, e.g. when using an SSH alias).

---

## One-time setup — shell PTY bridge prerequisites

The shell bridge SSHes from the master's `ops` user into the workers as `ubuntu`.
Two manual steps are required after initial provisioning:

**1. Give `ops` on the master its SSH config and key:**
```bash
sudo mkdir -p /home/ops/.ssh
sudo cp /home/ubuntu/.ssh/emea-eu-west-2.pem /home/ops/.ssh/
sudo bash -c 'cat >> /home/ops/.ssh/config << EOF
Host autonomous-enablements-worker
  HostName ec2-35-176-167-153.eu-west-2.compute.amazonaws.com
  User ubuntu
  IdentityFile /home/ops/.ssh/emea-eu-west-2.pem
  StrictHostKeyChecking no
EOF'
sudo chown -R ops:ops /home/ops/.ssh
sudo chmod 700 /home/ops/.ssh && sudo chmod 600 /home/ops/.ssh/*
```

**2. `ubuntu` on the worker must be in the docker group** (setup-worker.sh does
this automatically now, but if the worker was provisioned before this fix):
```bash
ssh autonomous-enablements-worker 'sudo usermod -aG docker ubuntu'
```

Test both from the master:
```bash
sudo -u ops ssh autonomous-enablements-worker "groups && docker ps"
```

---

## Deploying changes

```bash
# nginx config
sudo cp ops-server/nginx/ops-server.conf /etc/nginx/sites-available/ops-server
sudo nginx -t && sudo systemctl reload nginx

# FastAPI dashboard (app.py, templates, static)
sudo systemctl restart ops-dashboard

# Worker agent (on the worker node)
sudo systemctl restart ops-worker
```

**Watch logs:**
```bash
sudo journalctl -fu ops-dashboard       # FastAPI
sudo tail -f /var/log/nginx/access.log  # nginx
sudo tail -f /var/log/nginx/error.log   # nginx errors
```

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- ALWAYS read graphify-out/GRAPH_REPORT.md before reading any source files, running grep/glob searches, or answering codebase questions. The graph is your primary map of the codebase.
- IF graphify-out/wiki/index.md EXISTS, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

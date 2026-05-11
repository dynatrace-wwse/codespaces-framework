# Multi-Arch Ops Platform — Architecture

## Overview

A master-worker CI/CD platform supporting ARM (Graviton3) and AMD (x86_64) builds.
Single control plane, distributed test execution across architectures.

**DNS**: `autonomous-enablements.whydevslovedynatrace.com` → Master node (ARM)

---

## Topology

```
                    ┌──────────────────────────────────────────────────┐
                    │  autonomous-enablements.whydevslovedynatrace.com  │
                    └──────────────────────┬───────────────────────────┘
                                           │
                    ┌──────────────────────────────────────────────────┐
                    │       MASTER NODE (ARM — c7g.2xlarge)            │
                    │                                                  │
                    │  ┌──────────┐  ┌───────┐  ┌─────────────────┐   │
                    │  │ Nginx    │  │ Redis │  │ Webhook Server  │   │
                    │  │ :443/:80 │  │ :6379 │  │ :8443           │   │
                    │  └────┬─────┘  └───┬───┘  └────────┬────────┘   │
                    │       │            │               │             │
                    │  ┌────▼────────────▼───────────────▼──────────┐  │
                    │  │            Dashboard (FastAPI)              │  │
                    │  │  - Repo list + build status                │  │
                    │  │  - Schedule builds (ARM/AMD/both)          │  │
                    │  │  - Live job monitoring                     │  │
                    │  │  - Worker health + registration            │  │
                    │  └───────────────────────────────────────────┘  │
                    │                                                  │
                    │  ┌───────────────────────────────────────────┐  │
                    │  │  Scheduler + Worker Manager (local ARM)    │  │
                    │  │  - Nightly scheduler                       │  │
                    │  │  - Claude agent jobs (run local)           │  │
                    │  │  - Sync / Gen2 scan                        │  │
                    │  └───────────────────────────────────────────┘  │
                    └──────────────────────┬───────────────────────────┘
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
              ▼                            ▼                            ▼
┌─────────────────────────┐  ┌─────────────────────────┐  ┌────────────────────┐
│  WORKER: ARM (local)    │  │  WORKER: AMD (remote)   │  │  WORKER: ARM #2    │
│  c7g.2xlarge Graviton3  │  │  c5.2xlarge             │  │  (future scale)    │
│                         │  │                         │  │                    │
│  - k3d clusters (arm64) │  │  - k3d clusters (amd64) │  │  - k3d clusters    │
│  - Docker builds (arm64)│  │  - Docker builds (amd64)│  │                    │
│  - Integration tests    │  │  - Integration tests    │  │                    │
│  Connects → master Redis│  │  Connects → master Redis│  │                    │
└─────────────────────────┘  └─────────────────────────┘  └────────────────────┘
```

---

## Components

### 1. Master Node (ARM — `c7g.2xlarge`)

The control plane. Runs everything except remote test execution.

| Component | Port | Purpose |
|-----------|------|---------|
| **Nginx** | 443, 80 | TLS termination, reverse proxy to dashboard + webhook |
| **Dashboard** | 8080 (internal) | Web UI: repos, builds, scheduling, worker status |
| **Webhook** | 8443 (internal) | GitHub event receiver → Redis queues |
| **Redis** | 6379 | Job queues, worker registration, results, state |
| **Worker Manager** | — | Local ARM worker + remote worker coordination |
| **Nightly Scheduler** | — | Queues nightly tests with arch tags |
| **Claude Agents** | — | AI agent sessions (always run on master) |
| **Sync/Gen2 Scan** | — | Periodic fleet maintenance |

### 2. Worker Nodes (ARM + AMD)

Lightweight — just Docker, k3d, and the worker agent. Pull jobs from master Redis.

| Component | Purpose |
|-----------|---------|
| **Worker Agent** | Connects to master Redis, pulls `queue:test:<arch>` jobs |
| **Docker** | Build and run containers natively |
| **k3d** | Spin up k8s clusters for integration tests |
| **Heartbeat** | Reports health every 30s to master Redis |

### 3. Dashboard

Web UI at `https://autonomous-enablements.whydevslovedynatrace.com`

**Views:**
- **Fleet overview**: All 27 repos, last build status per arch, framework version
- **Build matrix**: ARM ✓/✗ × AMD ✓/✗ per repo
- **Schedule**: Trigger builds on specific arch or both
- **Workers**: Connected workers, health, capacity, current jobs
- **Logs**: Per-job logs with streaming output
- **Nightly report**: Pass/fail trends over time

---

## Job Routing

### Queue Structure (Redis)

```
queue:test:arm64     ← ARM integration tests
queue:test:amd64     ← AMD integration tests
queue:agent          ← Claude agent jobs (master only)
queue:sync           ← Sync/validation jobs (master only)
```

### Routing Rules

| Job Type | Arch | Routed To |
|----------|------|-----------|
| `integration-test` | `arm64` | `queue:test:arm64` → ARM worker |
| `integration-test` | `amd64` | `queue:test:amd64` → AMD worker |
| `integration-test` | `both` | Queued to BOTH arch queues |
| `fix-issue` | — | Master (Claude agent) |
| `review-pr` | — | Master (Claude agent) |
| `fix-ci` | — | Master (Claude agent) |
| `migrate-gen3` | — | Master (Claude agent) |
| `scaffold-lab` | — | Master (Claude agent) |
| `validate-after-push` | — | Master (sync) |

### Job Schema (extended)

```json
{
  "type": "integration-test",
  "repo": "dynatrace-wwse/enablement-dql-301",
  "arch": "both",
  "queue": "test",
  "timestamp": "2026-05-06T02:05:00Z",
  "nightly_run_id": "nightly-20260506-020000",
  "requested_by": "nightly-scheduler | dashboard | webhook"
}
```

---

## Worker Registration Protocol

Workers self-register via Redis on startup. Master tracks health.

### Registration (worker → Redis)

```
HSET worker:<worker-id> arch "arm64"
HSET worker:<worker-id> hostname "ip-10-0-1-42"
HSET worker:<worker-id> capacity 6
HSET worker:<worker-id> active_jobs 2
HSET worker:<worker-id> last_heartbeat "2026-05-06T12:00:00Z"
HSET worker:<worker-id> status "ready"

EXPIRE worker:<worker-id> 120  # Auto-remove if no heartbeat for 2min
```

### Heartbeat (every 30s)

```
HSET worker:<worker-id> active_jobs 3
HSET worker:<worker-id> last_heartbeat "2026-05-06T12:00:30Z"
EXPIRE worker:<worker-id> 120
```

### Master Worker Discovery

```
KEYS worker:*  →  list all registered workers
HGETALL worker:<id>  →  get worker details
```

---

## Network Architecture

### DNS

```
autonomous-enablements.whydevslovedynatrace.com  →  Master public IP (Elastic IP)
```

### Security Groups

**Master (sg-master):**

| Port | Source | Purpose |
|------|--------|---------|
| 443 | 0.0.0.0/0 | Dashboard (HTTPS) |
| 80 | 0.0.0.0/0 | HTTP → HTTPS redirect |
| 8443 | GitHub IPs | Webhook (internal, nginx proxied) |
| 6379 | sg-workers | Redis from worker nodes |
| 22 | Your IP | SSH |

**Workers (sg-workers):**

| Port | Source | Purpose |
|------|--------|---------|
| 22 | Your IP | SSH |
| — | (outbound only) | Pulls from master Redis, Docker Hub, GitHub |

### Internal Communication

```
Workers → Master:6379 (Redis)     # Job pull, heartbeat, results
Master  → Workers: NONE           # Workers pull, master never pushes
```

Workers are stateless and disposable. If a worker dies, its jobs time out and get re-queued.

---

## EC2 Instances

| Role | Instance | Arch | vCPU | RAM | Storage | Cost (reserved 1yr) |
|------|----------|------|------|-----|---------|---------------------|
| Master | c7g.2xlarge | arm64 | 8 | 16 GB | 200 GB gp3 | ~$132/mo |
| Worker ARM | c7g.2xlarge | arm64 | 8 | 16 GB | 200 GB gp3 | ~$132/mo |
| Worker AMD | c5.2xlarge | amd64 | 8 | 16 GB | 200 GB gp3 | ~$155/mo |
| **Total** | | | **24** | **48 GB** | **600 GB** | **~$419/mo** |

> The ARM worker is co-located on the master node initially (same machine).
> Only the AMD worker is a separate instance.
> Scale to dedicated ARM worker when nightly runs take too long.

### Optimized starting config (2 machines)

```
Machine 1 (ARM): Master + ARM Worker   c7g.2xlarge   $132/mo
Machine 2 (AMD): AMD Worker only       c5.2xlarge    $155/mo
                                                    ─────────
                                        Total:       ~$287/mo
```

---

## Nightly Schedule (Multi-Arch)

```
02:00 UTC  Scheduler wakes up
           │
           ├─ For each repo in repos.yaml:
           │    arch: arm64|amd64|both (from repos.yaml)
           │    │
           │    ├─ arch=arm64  → queue:test:arm64
           │    ├─ arch=amd64  → queue:test:amd64
           │    └─ arch=both   → queue:test:arm64 AND queue:test:amd64
           │
           └─ Stagger: 5 min between starts (per arch queue)
```

### repos.yaml extension

```yaml
repos:
  - name: enablement-dql-301
    repo: dynatrace-wwse/enablement-dql-301
    status: active
    ci: true
    arch: both        # ← NEW: arm64 | amd64 | both
    duration: 30m

  - name: enablement-kubernetes
    repo: dynatrace-wwse/enablement-kubernetes
    status: active
    ci: true
    arch: arm64       # Only needs ARM
    duration: 45m
```

---

## Dashboard API

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Dashboard UI (HTML) |
| GET | `/api/repos` | List all repos with build matrix |
| GET | `/api/repos/{name}/builds` | Build history for a repo |
| GET | `/api/workers` | Connected workers and status |
| GET | `/api/builds/running` | Currently executing jobs |
| GET | `/api/nightly/latest` | Latest nightly results |
| GET | `/api/nightly/history` | Nightly pass rate trends |
| POST | `/api/builds/trigger` | Schedule a build |
| GET | `/api/health` | Platform health |

### POST `/api/builds/trigger`

```json
{
  "repo": "dynatrace-wwse/enablement-dql-301",
  "arch": "both",
  "requested_by": "sergio"
}
```

---

## Nginx Configuration

```nginx
server {
    listen 443 ssl;
    server_name autonomous-enablements.whydevslovedynatrace.com;

    ssl_certificate     /etc/letsencrypt/live/autonomous-enablements.whydevslovedynatrace.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/autonomous-enablements.whydevslovedynatrace.com/privkey.pem;

    # Dashboard UI + API
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # WebSocket for live log streaming
    location /ws/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # GitHub webhook endpoint
    location /webhook {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}

server {
    listen 80;
    server_name autonomous-enablements.whydevslovedynatrace.com;
    return 301 https://$host$request_uri;
}
```

---

## Worker Agent Design

The worker agent is a lightweight Python process that:
1. Connects to master Redis
2. Registers itself with arch + capacity
3. Polls its arch-specific queue
4. Executes integration tests in Docker/k3d
5. Reports results back to Redis
6. Sends heartbeats every 30s

```
ops-server/
└── worker-agent/
    ├── agent.py          # Main loop: register, poll, execute, heartbeat
    ├── executor.py       # Docker/k3d test execution
    ├── config.py         # Master Redis URL, worker ID, capacity
    ├── setup-worker.sh   # Bootstrap script for worker nodes
    └── systemd/
        └── ops-worker-agent.service
```

### Worker Setup (AMD machine)

```bash
# On the AMD machine:
sudo bash setup-worker.sh

# Requires only:
#   - Docker
#   - k3d, kubectl, helm
#   - Python 3 + redis client
#   - Network access to master:6379
```

---

## Failure Handling

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Worker dies | Heartbeat expires (2 min) | Job times out → re-queued with retry count |
| Master Redis dies | Workers reconnect loop | Workers buffer locally, retry every 5s |
| Test hangs | 15 min timeout | Process killed, job marked failed |
| Agent hangs | 10 min timeout | Process killed, job marked failed |
| Network partition | Heartbeat stops | Worker marked offline, jobs re-routed |

### Re-queue Policy

```
max_retries: 2
retry_delay: 60s
dead_letter: queue:failed  (for inspection)
```

---

## Telemetry (Extended)

All events enriched with `arch` and `worker_id`:

```json
{
  "event": "integration-test.completed",
  "repo": "dynatrace-wwse/enablement-dql-301",
  "arch": "amd64",
  "worker_id": "worker-amd64-01",
  "passed": true,
  "duration_seconds": 340,
  "nightly_run_id": "nightly-20260506-020000"
}
```

---

## Migration Path (Current → Multi-Arch)

### Phase 1: Refactor queues (no new hardware)
- Split `queue:test` → `queue:test:arm64` + `queue:test:amd64`
- Add `arch` field to repos.yaml
- Update worker manager to filter by arch
- ARM worker still runs on master

### Phase 2: Add AMD worker
- Launch c5.2xlarge (AMD)
- Run `setup-worker.sh`
- Configure `MASTER_REDIS_URL` to point at master
- Worker auto-registers, starts pulling `queue:test:amd64`

### Phase 3: Dashboard
- Build web UI (FastAPI + HTMX or simple SPA)
- Nginx TLS with Let's Encrypt
- WebSocket for live log streaming

### Phase 4: Scale (optional)
- Separate ARM worker from master if needed
- Add worker auto-scaling (Lambda or spot instances)
- Add build caching (shared Docker layer cache)

---

## Security

- **Redis**: Bind to private VPC IP, require AUTH password, sg-restricted
- **Dashboard**: Basic auth or GitHub OAuth (restrict to dynatrace-wwse org members)
- **Workers**: No inbound ports, outbound-only to master Redis
- **TLS**: Let's Encrypt via certbot for dashboard
- **Secrets**: Never in Redis — workers get DT tokens from local `.env`
- **Webhook**: HMAC-SHA256 signature verification (existing)

---

## File Structure (Final)

```
ops-server/
├── ARCHITECTURE.md           # This file
├── README.md                 # Setup guide
├── setup.sh                  # Master bootstrap
├── requirements.txt          # Python deps
├── webhook/
│   ├── __init__.py
│   ├── config.py
│   └── server.py
├── workers/
│   ├── __init__.py
│   └── manager.py            # Master-side worker coordination
├── worker-agent/             # ← NEW: runs on remote workers
│   ├── __init__.py
│   ├── agent.py              # Worker main loop
│   ├── executor.py           # Test execution (Docker/k3d)
│   ├── config.py             # Worker config (master URL, arch, capacity)
│   ├── setup-worker.sh       # Worker-only bootstrap
│   └── systemd/
│       └── ops-worker-agent.service
├── dashboard/                # ← NEW: web UI
│   ├── __init__.py
│   ├── app.py                # FastAPI dashboard server
│   ├── api.py                # REST API endpoints
│   ├── ws.py                 # WebSocket live logs
│   ├── templates/
│   │   ├── index.html        # Fleet overview
│   │   ├── builds.html       # Build matrix
│   │   ├── workers.html      # Worker status
│   │   └── logs.html         # Job logs
│   └── static/
│       ├── style.css
│       └── app.js
├── nightly/
│   ├── __init__.py
│   └── scheduler.py          # Updated: arch-aware scheduling
├── telemetry/
│   ├── __init__.py
│   └── reporter.py
├── agents/
│   ├── CLAUDE.md
│   ├── mcp.json
│   ├── dtctl.yaml
│   └── env.template
├── nginx/
│   └── ops-server.conf       # ← NEW: nginx site config
└── systemd/
    ├── ops-webhook.service
    ├── ops-worker.service
    ├── ops-dashboard.service  # ← NEW
    ├── ops-nightly.service
    ├── ops-nightly.timer
    ├── ops-sync-daemon.service
    ├── ops-sync-daemon.timer
    ├── ops-gen2scan.service
    └── ops-gen2scan.timer
```

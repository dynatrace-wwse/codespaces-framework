
!!! example "Orbital — Autonomous Operations Platform"
    **Orbital** is the autonomous CI/CD and operations layer that keeps the entire enablement fleet healthy — testing every repo nightly across ARM and AMD hardware, isolating each run in its own sandboxed Kubernetes cluster, and dispatching Claude Code agents to diagnose and fix failures without human intervention.

    Live at **[autonomous-enablements.whydevslovedynatrace.com](https://autonomous-enablements.whydevslovedynatrace.com)**

---

## What is Orbital?

Orbital is the name for the **Autonomous Enablement Operations Platform** built alongside the Dynatrace Enablement Framework. While the framework handles _how_ a single lab environment runs, Orbital handles _all_ of them at scale — continuously.

It is:

- A **multi-architecture CI/CD engine** that runs integration tests in full, isolated Kubernetes environments
- A **fleet-aware scheduler** that orchestrates nightly builds across all 27 managed repositories
- An **autonomous agent platform** that dispatches Claude Code to auto-fix bugs, review PRs, migrate documentation, and scaffold new labs
- A **live ops dashboard** with streaming logs, interactive shells into running containers, and a real-time build matrix
- An **observable system** that reports every build, agent action, and sync event to Dynatrace as structured BizEvents

The name _Orbital_ captures how the platform works: worker nodes orbit a central control plane, each integration test runs inside its own isolated orbital container, and the system moves in continuous cycles — nightly tests, hourly sync checks, and always-on agents — perpetually watching over the fleet.

---

## Architecture

```
                    ┌──────────────────────────────────────────────────────────┐
                    │   autonomous-enablements.whydevslovedynatrace.com         │
                    └──────────────────────────────────────────────────────────┘
                                               │
                    ┌──────────────────────────────────────────────────────────┐
                    │        CONTROL PLANE — Master Node (ARM · c7g.2xlarge)   │
                    │                                                          │
                    │  Nginx (443/80) ─── oauth2-proxy ─── FastAPI (8080)     │
                    │                          │                               │
                    │                        Redis                             │
                    │                          │                               │
                    │       ┌──────────────────┼──────────────────┐            │
                    │  Webhook   Worker Manager   Nightly Scheduler            │
                    │  Server    (job dispatch)   (02:00 UTC)      Claude      │
                    │  :8443                                       Agents      │
                    └──────────────────────────┬───────────────────────────────┘
                                               │  Redis queues
                    ┌──────────────────────────┼───────────────────────────────┐
                    │                          │                               │
          ┌─────────┴──────────┐   ┌───────────┴────────────┐   ┌─────────────┐
          │  ARM Worker        │   │  AMD Worker             │   │  ARM Worker │
          │  (co-located)      │   │  (remote · c5.2xlarge)  │   │  #2 future  │
          │  arm64 Graviton3   │   │  amd64 Intel/AMD        │   │             │
          │                   │   │                         │   │             │
          │  Sysbox containers │   │  Sysbox containers      │   │  ...        │
          │  k3d clusters      │   │  k3d clusters           │   │             │
          └───────────────────┘   └─────────────────────────┘   └─────────────┘
                    │                          │
                    └──────────────────────────┘
                                   │
             codespaces-tracker (GKE) ──▶ Dynatrace COE Tenant
                   BizEvents: build.started · build.completed
                              agent.action · nightly.summary
                              sync.drift · worker.heartbeat
```

### Control Plane Components

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Nginx** | nginx 1.24 | TLS termination, reverse proxy, auth gating |
| **Dashboard** | FastAPI + uvicorn | Web UI, REST API, WebSocket PTY bridge |
| **Webhook Server** | FastAPI | Receives GitHub org-level webhooks, routes to Redis |
| **Worker Manager** | `workers/manager.py` — Python asyncio | Co-located ARM worker (capacity 4); also dispatches agent/sync jobs to the master node |
| **Worker Agent** | `worker-agent/agent.py` — Python asyncio | Remote AMD workers (capacity 6 each, warm Sysbox pool); pulls from `queue:test:amd64`, reports logs back to master Redis |
| **Nightly Scheduler** | systemd timer | Staggered nightly build orchestration at 02:00 UTC |
| **Sync Daemon** | systemd timer | Hourly framework-version drift detection |
| **Gen2 Scanner** | systemd timer | Daily Gen2→Gen3 documentation drift scan |
| **Claude Agents** | Claude Code CLI | Autonomous fix/review/migrate/scaffold sessions |
| **Redis** | Redis 7 | Job queues, running state, build history, logs, worker registry |
| **oauth2-proxy** | oauth2-proxy | GitHub SSO — restricts write actions to org members |

---

## Breakthrough: Sysbox Isolation

!!! tip "The Core Innovation"
    Every integration test runs inside a **fully isolated, hardware-separated Kubernetes cluster**. No test can see another test's processes, networks, or filesystems. A broken test cannot contaminate a passing one.

This is achieved through **[Sysbox](https://github.com/nestybox/sysbox)**, a container runtime that enables secure Docker-in-Docker without `--privileged` mode:

```
Host OS (Ubuntu 24.04)
└── Sysbox outer container (docker:25-dind runtime)
      └── Inner dockerd (full Docker daemon)
            └── dt-enablement container
                  └── k3d cluster (k3s + Kubernetes)
                        └── Dynatrace Operator
                        └── Demo applications
                        └── integration.sh assertions
```

Each integration test follows this pipeline:

```bash
# 1. Sysbox outer container starts — isolated Docker daemon
docker run -d --name sb-{job_id} --runtime=sysbox-runc docker:25-dind

# 2. Wait for inner dockerd to be ready
# 3. Pull the framework image inside the Sysbox
docker exec sb-{job_id} docker pull shinojosa/dt-enablement:v1.2

# 4. Start the lab environment container (detached)
docker exec sb-{job_id} docker run -d --name dt \
  -v /workspaces/{repo}:/workspaces/{repo} \
  shinojosa/dt-enablement:v1.2

# 5. Run post-create → post-start → integration tests
docker exec sb-{job_id} docker exec dt bash -lc "source post-create.sh"
docker exec sb-{job_id} docker exec dt bash -lc "source post-start.sh"
docker exec sb-{job_id} docker exec dt bash -lc "source integration.sh"

# 6. Sysbox container removal tears down everything cleanly
docker rm -f sb-{job_id}
```

### Port configuration inside Sysbox

The host machine (master or AMD worker) already has its own nginx on port 80. Each Sysbox container runs its k3d cluster on **non-default ports** to avoid collision:

```bash
K3D_LB_HTTP_PORT=30080    # host port → k3d LB → nginx ingress HTTP
K3D_LB_HTTPS_PORT=30443   # host port → k3d LB → nginx ingress HTTPS
K3D_API_PORT=6444          # host port → k3s API server
```

The framework's `assertRunningApp` function reads `K3D_LB_HTTP_PORT` and probes the app via **Host-header curl** — no browser required:

```bash
# assertRunningApp "todoapp" inside a Sysbox container
curl --silent --fail --max-time 5 \
  -H "Host: todoapp.172.16.0.10.sslip.io" \
  http://localhost:30080
```

nginx ingress inside k3d matches the Host header to the sslip.io ingress rule and forwards to the `todoapp` service. The catch-all ingress rule (no host) is also present but is not used in Orbital — Host-header routing is more specific and reliable for CI assertions.

### Warm Sysbox Pool — fast startup

!!! tip "80% faster job start"
    Before: every job paid a **60-120s** setup tax (start Sysbox → wait inner dockerd → load image).
    After: jobs start in **13-18s** — the Sysbox containers are pre-warmed at agent startup.

At startup the worker agent pre-warms one Sysbox container per capacity slot in parallel. Each slot runs the full lifecycle once — outer container up, inner dockerd ready, `TEST_IMAGE` loaded — and then waits idle. Jobs claim a slot from the pool queue the moment they're dequeued from Redis.

```
Agent startup (one-time, ~60s parallel):
  Slot 0: sysbox run → wait dockerd → docker save|load  ┐
  Slot 1: sysbox run → wait dockerd → docker save|load  ├─ all in parallel
  ...                                                    │
  Slot 5: sysbox run → wait dockerd → docker save|load  ┘

Per job (critical path after warm pool):
  1. pool.acquire()          ~0s  (blocks only if all slots in use)
  2. git clone --depth 1    ~5-10s
  3. docker exec sb → dt    ~3s
  4. wait vscode/docker      ~5s
  5. postCreate + test       (lab-specific)
  ─────────────────────────────────────────────────────────
  Setup overhead: 13-18s   (was 60-120s)
```

**Between jobs** the slot is cleaned — inner `dt` container removed, volumes and non-default networks pruned inside the Sysbox — and returned to the pool queue. The outer Sysbox and its cached `TEST_IMAGE` stay alive. If `TEST_IMAGE` is updated on the outer daemon the new layers are piped into the slot's inner docker at release time.

**Port assignment** is fixed per slot: slot `i` always publishes `APP_PROXY_PORT_START + i` on the host. This eliminates the dynamic Redis port pool for slotted jobs.

**Slot recovery**: if a slot becomes unhealthy (executor exception, inner dockerd crash) the pool re-initializes it from scratch in the background. A lost slot reduces temporary parallelism but does not stall the agent.

```
Slot lifecycle:
  ┌─ agent.start() ──────────────────────────────────────┐
  │  SysboxPool.init() → 6 slots started in parallel     │
  └──────────────────────────────────────────────────────┘
          │
          ▼  slot in queue
  ┌─ job arrives ────────────────────────────────────────┐
  │  pool.acquire()  → claims slot from queue            │
  │  git clone → start dt → run                         │
  │  pool.release()  → rm dt + prune → queue.put(slot)  │
  └──────────────────────────────────────────────────────┘
          │  termination signal
          ▼
  _kill_job_container → docker rm -fv sb-slot-*
  pool.release(healthy=False) → _init_slot() → re-queue
```

**Disk hygiene**: each release runs `docker volume prune -f` and `docker network prune -f` inside the Sysbox, preventing volume/network accumulation across jobs. The outer daemon retains only `docker:25-dind` and `TEST_IMAGE` — nothing else accumulates.

### Why Sysbox changes everything

Before Sysbox, running nested Kubernetes clusters required `--privileged` containers that shared the host's kernel namespaces. Running six such containers simultaneously on one machine caused network conflicts, process namespace collisions, and unpredictable failures.

With Sysbox, each outer container gets its own independent `systemd`, `dockerd`, network namespace, and mount namespace. Six parallel tests run as if each has its own machine — because from the kernel's perspective, they do.

**This enables:**
- **True parallelism**: 4–6 simultaneous integration tests on a single c7g.2xlarge
- **Clean teardown**: removing the outer container removes everything inside, including the k3d cluster and all Kubernetes state
- **No cross-contamination**: a test that OOM-kills its k3d cluster cannot affect adjacent tests
- **Reproducible results**: the isolation layer makes test outcomes architecture-only, not schedule-dependent

---

## Multi-Architecture Support

Orbital runs tests natively on **both ARM (arm64) and AMD (amd64)** hardware. This is critical because the framework ships a multi-arch Docker image (`shinojosa/dt-enablement:v1.2`) and enablements must work on both platforms — including GitHub Codespaces (AMD), Apple Silicon (ARM), and AWS Graviton (ARM).

### Architecture-aware job routing

```
repos.yaml entry:
  arch: both          # test on ARM AND AMD
  arch: arm64         # ARM only (faster, cheaper)
  arch: amd64         # AMD only (Codespaces parity)

Redis queues:
  queue:test:arm64    ──▶  manager.py   (ARM, co-located on master, capacity 4)
  queue:test:amd64    ──▶  agent.py     (AMD, remote c5.2xlarge, capacity 2)
```

When a repo is configured with `arch: both`, a single trigger fans out to **both** queues simultaneously. The build matrix in the dashboard shows ARM ✓/✗ and AMD ✓/✗ independently.

For manual test runs, split work proportional to worker capacity to use both machines efficiently:

- **~15 repos** → `arm64` queue (ARM capacity 4, 2× parallelism headroom)
- **~8 repos** → `amd64` queue (AMD capacity 2, smaller batch avoids a long tail)

### Two worker implementations

| Implementation | File | Runs on | Queue | Capacity | Extra responsibilities |
|----------------|------|---------|-------|----------|------------------------|
| **Worker Manager** | `ops-server/workers/manager.py` | Master (ARM) | `queue:test:arm64` | 4 | Also dispatches `queue:agent` and `queue:sync` jobs |
| **Worker Agent** | `ops-server/worker-agent/agent.py` | Remote (AMD) | `queue:test:amd64` | 2 | Integration tests and daemon jobs only |

Both use the same `semaphore.locked()` back-pressure pattern and publish job logs to master Redis under `job:log:{job_id}` so the dashboard can serve them from a single location regardless of where the job ran.

### Adding a new worker node

Scaling Orbital to additional architecture nodes requires only three steps:

```bash
# 1. Bootstrap the new node (installs Docker, k3d, kubectl, Sysbox, Python)
sudo bash ops-server/worker-agent/setup-worker.sh

# 2. Configure it to reach the master Redis
echo "MASTER_REDIS_URL=redis://:password@master-ip:6379" >> ~/.env
echo "WORKER_ARCH=arm64"    >> ~/.env   # or amd64
echo "WORKER_CAPACITY=4"    >> ~/.env   # 4 for ARM (c7g.2xlarge), 2 for AMD (c5.2xlarge)

# 3. Start the worker agent
sudo systemctl start ops-worker-agent
```

The worker auto-registers in Redis, begins sending heartbeats, and immediately starts pulling jobs from the matching arch queue. The dashboard reflects the new node within 30 seconds. No configuration changes are needed on the master.

### Worker health protocol

Every worker publishes a `worker:{worker_id}` hash to Redis on startup, refreshing every 30 seconds:

```
arch:           arm64
capacity:       4        # 4 for ARM manager.py, 2 for AMD agent.py
active_jobs:    2
status:         ready
host:           ip-10-0-1-42
ssh_host:       ec2-hostname.compute.amazonaws.com
last_heartbeat: 2026-05-08T02:14:30Z
TTL:            120s (auto-expires if heartbeat stops)
```

If a worker node goes down, its Redis key expires in 120 seconds. Any jobs it was running are detected as orphaned during the next worker startup and re-queued automatically.

### Queue back-pressure and dashboard visibility

A critical design requirement is that queued jobs **stay visible in Redis** until a worker slot is actually free. Without this, the dashboard's queue counter (`llen queue:test:{arch}`) drops to zero immediately — even though 20 jobs are waiting as in-memory asyncio tasks.

Both `manager.py` and `agent.py` implement back-pressure using an asyncio `Semaphore`:

```python
async def _consume_queue(self, ...):
    while True:
        # Back-pressure: leave jobs in Redis until a slot is free.
        # semaphore.locked() is True when all capacity slots are taken.
        if self.semaphore.locked():
            await asyncio.sleep(1)
            continue

        result = await self.pool.blpop(queue_key, timeout=5)
        ...
        asyncio.create_task(self._run_with_semaphore(semaphore, job))
        # Yield to the event loop so the task acquires the semaphore
        # BEFORE we check semaphore.locked() again on the next iteration.
        await asyncio.sleep(0)
```

The `await asyncio.sleep(0)` after `create_task` is essential: without it, the event loop doesn't run the new task before the consumer re-enters the loop. The task hasn't acquired the semaphore yet, so `semaphore.locked()` falsely reports a free slot and the consumer drains the entire queue into in-memory tasks in microseconds. After the fix, at capacity-4 the ARM worker holds exactly 4 jobs in memory and the remaining items stay in `queue:test:arm64` — correctly reflected in the dashboard queue counter.

---

## Job Types

Orbital supports four distinct job types routed through Redis:

| Type | Queue | Sysbox | Lock | Interactive Shell | Description |
|------|-------|--------|------|-------------------|-------------|
| `integration-test` | `queue:test:{arch}` | Yes | per-triple | While running | Full CI: postCreate + postStart + integration.sh |
| `daemon` | `queue:test:{arch}` | Yes | None | Indefinitely | Full setup, then stays alive for interactive sessions |
| `fix-ci` / `fix-issue` / `review-pr` | `queue:agent` | No | None | No | Claude Code agent sessions |
| `sync-command` | `queue:sync` | No | None | No | Sync CLI commands (status, validate, clone) |

### Integration test jobs

The standard CI job. It runs the complete lab environment setup and then executes `integration.sh` assertions. A per-triple concurrency lock (`running:lock:{repo}:{branch}:{arch}`) prevents the same repo+branch+arch combination from running twice simultaneously — duplicate triggers are deferred to a queue and run after the current build completes.

### Daemon jobs

A daemon job runs the same full setup as an integration test but **never exits**. Once `post-create.sh` and `post-start.sh` complete and the lab environment is ready, the Sysbox container stays alive indefinitely. A heartbeat loop refreshes the job's Redis state every 15 seconds to prevent expiry.

This enables **interactive training sessions**: a trainer or developer can open a shell directly into a running lab environment (with a full k3d cluster, Dynatrace agent, and demo apps) without triggering any CI assertions. The daemon is terminated via the dashboard's `⏹ Terminate` button, which sends `docker rm -f sb-{id}` to cleanly remove the entire isolation stack.

### Agentic jobs

When a webhook event matches an agentic trigger (e.g., an issue labeled `bug` or a failed CI run), Orbital dispatches a Claude Code agent session on the master node. The agent has access to:

- `gh` — GitHub CLI for PRs, issues, repo exploration
- `dtctl` — Dynatrace CLI for querying the COE tenant
- `sync` — Fleet management CLI
- `docker`, `kubectl`, `helm` — container and Kubernetes operations
- Dynatrace MCP server — DQL queries, entity lookups, problem analysis

---

## Agentic Capabilities

!!! tip "Self-Healing Fleet"
    Orbital's most powerful capability is its ability to act — not just observe. When something breaks, an agent investigates, diagnoses, and creates a fix PR, often without any human involvement.

### Webhook-driven triggers

GitHub org-level webhooks route to specific agent behaviors:

| GitHub Event | Label / Condition | Agent Action |
|---|---|---|
| `issues.opened` | label: `bug` | Investigate root cause → fix branch → PR |
| `issues.opened` | label: `gen3-migration` | Migrate Gen2 docs to Gen3 → PR |
| `issues.opened` | label: `new-enablement` | Scaffold new lab from template → PR |
| `pull_request.opened` | any | Review diff for framework compliance, security, test coverage |
| `check_suite.completed` | status: `failure` | Read CI logs → diagnose failure → push fix to PR branch |
| `push` (to main) | any | Sync: validate repo state against repos.yaml |

### Autonomous diagnose loop

When a build fails, the closed-loop flow is:

```
1. build.completed (status=fail) emitted to Dynatrace
2. Worker publishes failed_step + failure_summary to Redis
3. Agent picks up job from queue:agent
4. Agent reads build:{run_id} hash for context
5. Agent queries Dynatrace MCP:
   - Problems for the test's time window
   - Pod logs for the failing namespace
   - CPU/memory metrics — compare to last green build
6. Agent posts PR comment with:
   - Failure summary (from result.jsonl)
   - Probable cause (metrics diff vs. last green)
   - Last-green commit SHA + Dynatrace dashboard deep link
   - Suggested fix (clearly labelled as model output)
7. Agent emits ops.agent.diagnose BizEvent
```

This loop requires `consecutive_failure_count >= 2` before posting — preventing noise from transient failures.

### Framework compliance patterns

The agent enforces specific rules when reviewing or creating PRs:

- **Image**: `shinojosa/dt-enablement:v1.2` (pinned)
- **RunArgs**: `["--init", "--privileged", "--network=host"]`
- **RemoteUser**: `vscode`
- **Category A files** (framework-owned) must not be modified in repos
- **integration.sh** must use `_assert` wrappers for structured result output
- DT tokens must never appear in logs or committed files

---

## Interactive Shell

Every running Sysbox container exposes an interactive shell through Orbital's dashboard. This is backed by a **WebSocket PTY bridge** — a full terminal emulator in the browser, connected via xterm.js to a PTY process on the server.

### Shell architecture

```
Browser (xterm.js, MesloLGS NF font)
   │  Binary WebSocket frames (keystrokes)
   │  JSON frames {type:"resize", rows, cols}
   ▼
nginx (HTTP/1.1, no h2 — required for WebSocket upgrade)
   ▼
FastAPI PTY bridge (_pty_bridge)
   │
   ├── os.openpty()
   ├── loop.add_reader(master_fd) — non-blocking reads
   └── subprocess:
         ssh -t worker docker exec -it sb-{id} \
             docker exec -it -e TERM=xterm-256color \
                 -w /workspaces/{repo} dt zsh
```

### Why HTTP/2 is disabled for WebSocket

Nginx 1.24 does not implement RFC 8441 (WebSocket over HTTP/2 extended CONNECT). With `http2` in the listen directive, Chrome reuses its existing H2 connection for the WebSocket upgrade — which nginx silently drops. The fix is `listen 443 ssl;` without `http2`, forcing HTTP/1.1 ALPN negotiation and standard 101 Switching Protocols.

### Auth flow for WebSocket endpoints

`auth_request` (oauth2-proxy) is incompatible with WebSocket upgrades — nginx does not properly forward `Upgrade: websocket` after an auth sub-request. The solution is a two-step token flow:

1. **`POST /api/jobs/{job_id}/shell-token`** — normal HTTP, guarded by `auth_request`. Issues a 60-second single-use token stored in Redis as `shell:token:{token} → job_id`.
2. **`GET /ws/jobs/{job_id}/shell?token=…`** — no auth_request. FastAPI validates and atomically deletes the token via a Redis `MULTI/EXEC` pipeline before opening the PTY.

### Features

| Feature | Description |
|---------|-------------|
| **Fullscreen mode** | Expands terminal to full viewport; PTY is resized via `TIOCSWINSZ` after Chrome's CSS transition completes |
| **New Window popup** | Opens a self-contained HTML popup with its own token + WebSocket connection, sharing the auth cookie with the parent window |
| **Correct initial size** | `rows` and `cols` passed in WebSocket URL; PTY is sized before subprocess starts so TUI apps (k9s, htop) render correctly |
| **Nerd Font icons** | MesloLGS NF loaded from jsdelivr CDN; font-ready check before `fitAddon.fit()` prevents blank-line rendering bugs |
| **Resize events** | JSON `{type:"resize"}` frames resync PTY dimensions after fullscreen transitions |

---

## Nightly Operations

The nightly scheduler runs at **02:00 UTC** as a systemd oneshot service. It reads `repos.yaml`, stagers builds with a 5-minute gap per arch queue to avoid resource spikes, and fans out to both arch queues for `arch: both` repos.

```
02:00 UTC  Scheduler wakes
   │
   ├── Read repos.yaml (27 repos, active + ci: true)
   │
   ├── For each repo:
   │     arch=arm64 → RPUSH queue:test:arm64
   │     arch=amd64 → RPUSH queue:test:amd64
   │     arch=both  → RPUSH both queues
   │
   ├── Stagger: 5 min between enqueues (per arch queue)
   │
   └── Emit ops.nightly.summary BizEvent when all complete
```

### Concurrency lock

A `running:lock:{repo}:{branch}:{arch}` key (2h TTL) prevents the same triple from running simultaneously. If a build is triggered while one is already running, the new job is pushed to a `deferred:{triple}` list and picked up automatically when the lock is released.

Crash recovery on worker startup reconciles `running:lock:*` keys against live `job:running:{run_id}` hashes and removes any orphaned locks.

---

## Observability Pipeline

Every Orbital operation emits structured BizEvents to the Dynatrace COE tenant via `codespaces-tracker`. This closes the loop between CI health and student impact.

### BizEvent schema

All events carry the full context needed for cross-pipeline joins:

| Field | Present In | Value |
|-------|-----------|-------|
| `framework.version` | all | e.g. `v1.2.7` |
| `repository.name` | all | e.g. `enablement-dql-301` |
| `arch` | build events | `arm64` or `amd64` |
| `branch` | build events | git ref |
| `commit_sha` | build events | short SHA |
| `worker_id` | build events | worker identifier |
| `triggered_by` | build events | `nightly` / `dashboard` / `webhook` |

### Event types

| BizEvent | Trigger | Key Fields |
|----------|---------|-----------|
| `build.started` | Worker picks up job | `run_id`, `queue_wait_ms`, `worker_id` |
| `build.completed` | Worker finishes | `passed`, `duration_s`, `failed_step`, `failure_summary` |
| `build.assertion.failed` | Each failed assertion | `step`, `description`, `error` |
| `build.deferred` | Concurrency lock blocks enqueue | `wait_for_run_id` |
| `agent.{type}` | Agent session completes | `success`, `details` (JSON) |
| `nightly.summary` | Nightly run finishes | `total`, `passed`, `failed`, `pass_rate` |
| `sync.drift` | Hourly version check | `current_version`, `target_version`, `drifted` |

### Student impact correlation

The killer observability story is the cross-pipeline join between `build.completed` and `codespace.creation` events. Both carry `framework.version` and `repository.name`:

```sql
-- Per-repo, per-version: how many students opened a codespace
-- while there was no green build for that version?
fetch bizevents
| filter event.type in ("codespace.creation", "build.completed")
| join kind=inner
       (fetch bizevents | filter event.type == "build.completed")
       on framework.version, repository.name
```

This answers: _"When a student opened `obslab-livedebugger-petclinic` on v1.2.7, was there a green build for that version in the last 24 hours?"_ If not — it's a risk window.

### Dynatrace SLO

An SLO in the COE tenant tracks build health:

```
Target:  95% pass rate, rolling 7 days
Metric:  countIf(build.completed, passed) / count(build.completed) * 100
Alert:   Davis problem when SLO < 95% → autonomous-diagnose agent triggered
```

---

## Dashboard

The Orbital dashboard is a single-page FastAPI app with real-time WebSocket updates.

### Views

| View | What it shows |
|------|--------------|
| **Fleet** | All 27 repos, last build per arch (ARM ✓/✗ · AMD ✓/✗), framework version, GitHub release link |
| **Running** | Live jobs in progress, worker assignment, elapsed time, streaming log tail |
| **History** | Reverse-chronological build feed, filterable by repo / arch / status |
| **Triage Queue** | Repos with consecutive failures, ranked by severity — the first thing to check every morning |
| **Workers** | Connected workers, arch, capacity, active jobs, last heartbeat |
| **Synchronizer** | Fleet version drift, open PRs, open issues — tabular view with sub-tabs |
| **Agents** | Claude agent session log — what the agent did and what it concluded |

### Authentication

The dashboard uses GitHub SSO via `oauth2-proxy`. Read-only views are public. Write actions — triggering builds, running sync commands, terminating jobs — require org membership in `dynatrace-wwse`.

---

## Two-Stage Test Pipeline

Tests run in two stages designed to fail fast:

```
Stage 1 (fast · ~30s)    BATS unit tests
                         make test → bats test/unit/
                         101 test cases across 5 files
                         On failure: skip Stage 2 (save 10 minutes)

Stage 2 (slow · ~10m)    Integration tests
                         make integration → integration.sh
                         Full k3d cluster + Dynatrace + demo apps
                         Structured assertions via _assert wrapper
```

BATS tests cover: Dynakube config, environment variable management, ingress, framework sourcing, and app guard logic. Integration tests verify the full stack: running pods, Dynatrace operator health, ingress reachability, and (with `dtctl`) data flowing into the COE tenant.

The `_assert` wrapper in `test_functions.sh` emits structured `result.jsonl` output that the worker reads to populate the build record with `failed_step` and `failure_summary` — enabling the triage queue and agentic diagnose loop.

---

## Infrastructure

### Current deployment

| Role | Instance | Arch | vCPU | RAM | Cost (1yr reserved) |
|------|----------|------|------|-----|---------------------|
| Master + ARM Worker | c7g.2xlarge | arm64 | 8 | 16 GB | ~$132/mo |
| AMD Worker | c5.2xlarge | amd64 | 8 | 16 GB | ~$155/mo |
| **Total** | | | **16** | **32 GB** | **~$287/mo** |

### Scaling horizontally

Additional workers need only Docker, k3d, Sysbox, Python 3, and network access to the master's Redis port. There is no configuration change on the master side — workers self-register.

To add an ARM worker for higher parallelism:

```bash
# On the new node:
sudo bash ops-server/worker-agent/setup-worker.sh
# Set MASTER_REDIS_URL, WORKER_ARCH=arm64, WORKER_CAPACITY=4 in ~/.env
sudo systemctl start ops-worker-agent
```

To add an AMD worker for x86_64 parity testing — same script, `WORKER_ARCH=amd64`, `WORKER_CAPACITY=2`.

Future scaling options:
- **Spot instance workers**: workers are stateless and disposable; spot termination causes job re-queue, not data loss
- **Auto-scaling**: a Lambda trigger on Redis queue depth can launch spot workers during nightly fan-out
- **Shared Docker layer cache**: a shared registry (or `--cache-from`) would cut image-pull time from ~3 min to ~30s per build

---

## Setup Reference

### Bootstrap the master node

```bash
# Clone and run setup (installs Docker, Sysbox, Redis, Claude Code, dtctl, gh, nginx, systemd units)
git clone https://github.com/dynatrace-wwse/codespaces-framework.git
sudo bash codespaces-framework/ops-server/setup.sh
```

### Configure secrets

```bash
cp ops-server/agents/env.template ~/.env
# Fill in:
#   WEBHOOK_SECRET        openssl rand -hex 32
#   ANTHROPIC_API_KEY     console.anthropic.com
#   DT_ENVIRONMENT        https://geu80787.apps.dynatrace.com
#   DT_OPERATOR_TOKEN     Dynatrace operator token
#   DT_INGEST_TOKEN       Dynatrace ingest token
#   DT_API_TOKEN          Dynatrace API token
#   OAUTH2_CLIENT_ID      GitHub OAuth App Client ID
#   OAUTH2_CLIENT_SECRET  GitHub OAuth App Client Secret
#   REDIS_PASSWORD        from setup.sh output
```

### Start all services

```bash
sudo systemctl start ops-webhook ops-worker ops-dashboard
sudo systemctl start ops-nightly.timer ops-sync-daemon.timer ops-gen2scan.timer
```

### Day-to-day operations

```bash
# Watch live worker output
sudo journalctl -fu ops-worker

# Watch dashboard
sudo journalctl -fu ops-dashboard

# Manually trigger the nightly run
sudo systemctl start ops-nightly

# Queue a single repo test
cd ~/enablement-framework/codespaces-framework/ops-server
PYTHONPATH=. python3 -m nightly.scheduler single dynatrace-wwse/enablement-dql-301

# Check fleet sync status
cd ~/enablement-framework/codespaces-framework
python3 -m sync.cli status
```

### Deploy changes to the running ops user

Since edits happen as `ubuntu` and services run as `ops`:

```bash
sudo cp ops-server/workers/manager.py      /home/ops/enablement-framework/codespaces-framework/ops-server/workers/manager.py
sudo cp ops-server/dashboard/app.py        /home/ops/enablement-framework/codespaces-framework/ops-server/dashboard/app.py
sudo cp ops-server/dashboard/static/app.js /home/ops/enablement-framework/codespaces-framework/ops-server/dashboard/static/app.js
sudo systemctl restart ops-worker ops-dashboard
```

---

## Key Links

| Resource | URL |
|----------|-----|
| **Orbital Dashboard** | https://autonomous-enablements.whydevslovedynatrace.com |
| **Framework Docs** | https://dynatrace-wwse.github.io/codespaces-framework/ |
| **Lab Registry** | https://dynatrace-wwse.github.io/ |
| **COE Tenant** | https://geu80787.apps.dynatrace.com |
| **Monitoring Dashboard** | https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.dashboards/dashboard/041e6584-bdae-4fa0-9fa1-18731850cf20 |
| **Codespaces Tracker** | https://codespaces-tracker.whydevslovedynatrace.com |

---

<div class="grid cards" markdown>
- [← Monitoring](monitoring.md)
- [Enablement App →](enablement-app.md)
</div>

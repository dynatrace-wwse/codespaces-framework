# Enablement Framework — Project Status

**Date:** May 2026
**Owner:** Sergio Hinojosa (@shinojosa)
**Org:** [dynatrace-wwse](https://github.com/dynatrace-wwse)

---

## What is the Enablement Framework?

An open-source platform that turns Dynatrace training repositories into a managed fleet
of self-service, self-healing lab environments. Students open a GitHub Codespace (or
DevContainer) and get a fully provisioned Kubernetes cluster with Dynatrace monitoring,
demo applications, and step-by-step documentation — all ready in under 3 minutes.

### The fleet

**27 active repositories** across enablements, workshops, and demos:

| Type | Count | Examples |
|------|-------|---------|
| Enablement labs | 13 | Kubernetes & OTel, DQL, Logs, Business Observability, Gen AI/LLM, Live Debugger |
| Workshops | 2 | Log Analytics, Destination Automation |
| Demos | 5 | Agentic AI + NVIDIA, MCP Unguard, Astroshop Runtime Optimization |
| Infrastructure | 5 | codespaces-framework, codespaces-tracker, ace-integration, bug-busters, remote-environment |

Each repo pins a `FRAMEWORK_VERSION` and pulls shared infrastructure from
`codespaces-framework` at startup via a versioned cache mechanism.

### How it works

```
Student opens Codespace
  → devcontainer.json pulls shinojosa/dt-enablement:v1.2
  → post-create.sh sources the framework (cached by version)
  → Framework provisions: k3d cluster, Dynatrace operator, demo apps, ingress
  → MkDocs site serves step-by-step instructions
  → codespaces-tracker logs creation telemetry → Dynatrace BizEvents
```

### The Sync CLI

A 24-command Python CLI (`sync`) manages the fleet:

| Command | Purpose |
|---------|---------|
| `sync push-update` | Deploy new framework version to all repos (branch → PR) |
| `sync status` | Show version drift across fleet |
| `sync list-pr` | Monitor CI, approve/merge passing PRs |
| `sync tag` | Tag repos with combined version (vFramework_RepoVersion) |
| `sync release` | Create GitHub Release with auto-generated changelog |
| `sync validate` | Validate repos.yaml and repo state |
| `sync clone` | Clone all repos locally |
| `sync ci-status` | Show CI run status across fleet |
| `sync list-issues` | List open issues with label filtering |

---

## What has been done

### Phase 0 — Framework Extraction (Complete)

Extracted shared infrastructure from 27 repos into a versioned library:

- Split files into Category A (framework-owned, cached) and Custom (repo-specific)
- Built the Sync CLI for fleet-wide version management
- Created the versioned cache mechanism (container → host → git clone fallback)
- Published `shinojosa/dt-enablement:v1.2` multi-arch image (amd64 + arm64)
- Deployed codespaces-tracker on GKE with MaxMind geo-enrichment
- Set up OpenPipeline for BizEvent extraction from creation logs
- Current version: **v1.2.7**

### Phase 2 — Core Refactoring (In Progress)

| Task | Status | Branch |
|------|--------|--------|
| Environment variable management (`variablesNeeded`) | Done | merged to main |
| MCP Server opt-in (`enableMCP` / `disableMCP`) | Done | merged to main |
| App exposure via NGINX ingress + sslip.io | Done | merged to main |
| Config-driven Dynakube deployment (defaults + per-repo overrides) | Done | `rfe/phase2-k3s-engine` |
| K3d engine (replacing Kind — 3x faster, less resources) | Done | `rfe/phase2-k3s-engine` |
| MkDocs RUM refactoring (remove per-page JS injection) | Done | `rfe/mkdocs-jscript` |
| Enhanced error handling + error payload to tracker | Done | `rfe/phase2-k3s-engine` |
| Ops Server (autonomous CI/CD platform) | Done | `rfe/ops-server` |
| 78 unit tests (BATS) | Done | merged to main |

### Ops Server — `autonomous-enablements.whydevslovedynatrace.com`

Self-hosted CI/CD + autonomous agent platform running on EC2 (Graviton3 ARM):

| Component | Status |
|-----------|--------|
| FastAPI webhook listener (GitHub events → Redis queues) | Built |
| Worker manager (parallel test execution, k3d clusters) | Built |
| Multi-arch support (ARM master + AMD remote workers) | Built |
| Nightly scheduler (staggered, arch-aware) | Built |
| Web dashboard (fleet overview, build matrix, live logs) | Built |
| GitHub SSO via oauth2-proxy | Built |
| Nginx + TLS (Let's Encrypt) | Built |
| Claude Code agent integration (MCP + dtctl) | Built |
| Telemetry reporter (results → codespaces-tracker → DT BizEvents) | Built |
| Systemd services (webhook, worker, dashboard, nightly timer, sync, gen2scan) | Built |
| Worker agent for remote AMD nodes | Built |
| GitHub Actions integration (per-arch workflows) | Built |

### Session — May 2026 (Shell PTY + Daemon job improvements)

All changes are committed on branch `rfe/phase2-k3s-engine` (7 commits ahead of origin, **not yet pushed**).

#### Daemon job type — ARM master (`workers/manager.py`)

- Added daemon routing in `_dispatch()` → calls `_run_daemon()`
- `_run_daemon()` runs full Sysbox setup (postCreate + postStart) then blocks on `docker wait sb_name` indefinitely
- No concurrency lock (unlike integration-test); uses `job:running:{id}` with 24h TTL
- Heartbeat loop refreshes the running key every 15s so it never expires during a session
- Terminate action (`docker rm -f sb_name`) unblocks `docker wait` and cleanly exits

#### Daemon job type — AMD remote worker (`worker-agent/agent.py` + `executor.py`)

- Both files already had daemon support added in a prior session but had not been pushed to GitHub
- AMD worker was 7 commits behind (`origin` at `9892208`; local at `bb84500`)
- SSH-piped updated files directly to the worker (`cat | sudo -u ops ssh ... tee`) and restarted `ops-worker-agent`
- **Long-term fix needed**: push branch to GitHub then `git pull` on AMD worker

#### PTY bridge improvements (`app.py`)

- Fixed crash when typing numbers: `json.loads("1")` returns `int`, not `dict`; `ev.get("type")` threw `AttributeError`. Fixed with `isinstance(ev, dict)` guard
- WebSocket now accepts `?rows=N&cols=N` query params; PTY `TIOCSWINSZ` set before subprocess starts so TUI apps (k9s, htop) get correct size at launch
- Added `-e TERM=xterm-256color` to inner docker exec command
- Subprocess env now includes `TERM=xterm-256color`
- Shell exec chain:
  ```
  docker exec -it sb-{id} docker exec -it -e TERM=xterm-256color -w /workspaces/{repo} dt zsh
  ```

#### Frontend improvements (`app.js`)

- Keystrokes sent as binary WebSocket frames (`TextEncoder`) to avoid the JSON parsing code path entirely
- Wait for `MesloLGS NF` font before calling `fitAddon.fit()` — fixes terminal line-wrap/blank issue when typing long commands
- Dimensions (`rows`, `cols`) passed in WebSocket URL so PTY starts at correct size
- Changed loading message: `Initializing isolation container…` (was `Loading…`)
- Added "Connecting…" and "Tunnel established" status lines in xterm.js
- Fullscreen handler uses `setTimeout(300)` (was `requestAnimationFrame`) to wait for Chrome's fullscreen CSS transition before re-fitting terminal
- After fullscreen fit, sends JSON resize event to server so PTY is resized
- Added `shellJobId` variable tracked separately from `currentJobId` (live-log job ID) — fixes New Window getting 404
- New Window popup generates self-contained HTML (`shellPopupHtml(jobId, title)`) that fetches its own token, connects its own WebSocket, and shares the auth cookie with the parent window

#### UI (`index.html`, `style.css`)

- Shell modal header now shows three buttons: `⧉ New Window`, `⛶ Fullscreen`, `✕ Close`
- Fullscreen CSS uses `position: absolute; inset: 0` on `#shell-terminal` inside `:fullscreen` — fixes k9s not rendering at full viewport size
- Modal header hidden in fullscreen mode
- `@font-face` declarations for MesloLGS NF (Regular, Bold, Italic, Bold Italic) loaded from `cdn.jsdelivr.net/gh/romkatv/powerlevel10k-media@master/` — enables p10k Nerd Font icons in xterm.js
- Terminal font stack: `"MesloLGS NF", "Cascadia Code NF", "Hack Nerd Font", ui-monospace, Menlo, monospace`

#### Modified files (uncommitted changes on top of the 7 committed commits)

| File | Changes |
|------|---------|
| `ops-server/CLAUDE.md` | Job types table, two-repo deployment note, `dt zsh` fix, shell session lifecycle |
| `ops-server/dashboard/app.py` | PTY fixes: JSON guard, TERM env, initial size, binary frames |
| `ops-server/dashboard/static/app.js` | Font wait, binary keystrokes, shellJobId, fullscreen timing, new window popup |
| `ops-server/dashboard/static/style.css` | MesloLGS NF @font-face, fullscreen CSS |
| `ops-server/dashboard/templates/index.html` | New Window + Fullscreen + Close buttons in shell modal header |
| `ops-server/nginx/ops-server.conf` | HTTP/2 removed from listen directive (WebSocket over H2 not supported by nginx 1.24) |
| `ops-server/workers/manager.py` | Daemon routing, `_run_daemon()`, heartbeat, terminate via `docker rm -f` |

---

## What is left to do

### Immediate — before next session

| # | Task | Priority | Notes |
|---|------|----------|-------|
| 1 | **Commit + push branch to GitHub** | Critical | 7 committed + uncommitted changes on `rfe/phase2-k3s-engine`. AMD worker needs this to stay in sync |
| 2 | **`git pull` on AMD worker** | Critical | After push: `ssh autonomous-enablements-worker "cd /home/ops/codespaces-framework && git pull"` |
| 3 | **Deploy CLAUDE.md to ops path** | Low | `sudo cp ops-server/CLAUDE.md /home/ops/enablement-framework/codespaces-framework/ops-server/CLAUDE.md` |

### Phase 2 — Remaining tasks

| # | Task | Priority | Effort | Dependencies |
|---|------|----------|--------|--------------|
| 4 | **Merge k3d engine to main** | High | 1d | Review + test the 63+ commits on `rfe/phase2-k3s-engine` |
| 5 | **Merge ops-server to main** | High | 1d | Review + test the 37+ commits on `rfe/ops-server` |
| 6 | **Sync Dynakube logic to all repos** | High | 2d | Merge k3d first. `sync push-update` to deploy config-driven Dynakube |
| 7 | **Sync MkDocs refactoring to all repos** | Medium | 1d | Merge mkdocs-jscript. Remove per-page BizEvent JS snippets |
| 8 | **Unify Astroshop deployments** | Medium | 2d | Consolidate to 2 canonical apps: Astroshop (demo.live) + OTel Demo (CNCF) |
| 9 | **Improve integration tests for all repos** | High | 5d | Write mature `integration.sh` for each of the 27 repos |
| 10 | **Nightly regression tests (live)** | High | 1d | Ops server running. Configure nightly schedule, verify results in DT dashboard |
| 11 | **Update framework documentation** | Medium | 2d | Document: ingress, Dynakube config, variablesNeeded, MCP, Make targets, k3d |
| 12 | **Fix monitoring — agentless app IDs** | Medium | 1d | Ensure all repos use unique `codespace.app_id`, create missing agentless apps in DT |
| 13 | **Enhance monitoring dashboard** | Low | 1d | Add worldmap, nightly pass/fail heatmap, error trends |

### Gen2 → Gen3 Migration

Migrate all documentation from Dynatrace Classic UI to Native Apps:

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 14 | **Run phase1-scan across fleet** | Medium | 1d | Static keyword analysis: classify each doc page as GREEN/YELLOW/RED |
| 15 | **Migrate RED docs (highest risk)** | Medium | 3d | Rewrite classic entity references, navigation paths, deprecated DQL |
| 16 | **Migrate YELLOW docs (mixed)** | Low | 3d | Partial updates, validate remaining Gen3 patterns |
| 17 | **Re-capture screenshots** | Low | 5d | ~500 screenshots across 27 repos showing current Dynatrace UI |
| 18 | **Validate DQL queries** | Medium | 2d | Run all DQL from docs against COE tenant via dtctl/MCP |
| 19 | **Monthly drift monitoring** | Low | 1d | Set up ops-server Gen2 scanner cron + auto-create issues on drift |

### Autonomous Agent Capabilities

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 20 | **Auto-fix bugs from GitHub issues** | Medium | 2d | Claude agent reads issue → investigates → creates fix PR |
| 21 | **Auto-review PRs** | Medium | 1d | Claude agent reviews diffs for framework compliance, security, test coverage |
| 22 | **Auto-scaffold new labs** | Low | 2d | Claude agent creates repo from template, configures, adds to repos.yaml |
| 23 | **Auto CI failure diagnosis** | Medium | 2d | Claude agent reads failed logs → diagnoses → pushes fix to PR branch |
| 24 | **Auto Gen3 migration via Claude** | Medium | 3d | Claude agent runs migration using dt-migration + dt-dql-essentials skills |

### Infrastructure

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 25 | **AMD worker git automation** | High | 30m | After push: `git pull` on AMD worker + verify daemon jobs work end-to-end |
| 26 | **Badge + RUM PR review** | Medium | 1d | 10 pending PRs for badge and RUM fixes across repos |
| 27 | **Create 7 agentless apps in DT** | Medium | 1h | Missing agentless app definitions in COE tenant |

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                     dynatrace-wwse GitHub Org                        │
│                                                                      │
│  27 repos ──→ GitHub Webhooks ──→ autonomous-enablements server      │
│      │                                     │                         │
│      │ sync push-update              ┌─────┴──────┐                  │
│      │ (framework versions)          │   Master    │                  │
│      │                               │  c7g.2xlarge│                  │
│      ▼                               │  (ARM)      │                  │
│  ┌─────────┐                         │             │                  │
│  │ Student  │                        │  Webhook    │                  │
│  │ opens    │                        │  Dashboard  │                  │
│  │ Codespace│                        │  Claude     │                  │
│  │          │                        │  Scheduler  │                  │
│  │ k3d +    │                        │  Sync       │                  │
│  │ DT agent │                        └──────┬──────┘                  │
│  │ apps     │                               │                         │
│  └────┬─────┘                        ┌──────┴──────┐                  │
│       │                              │ ARM Worker  │  AMD Worker     │
│       │                              │ (co-located)│  (c5.2xlarge)   │
│       │                              └─────────────┘  └──────────┘   │
│       │                                                               │
│       ▼                                                               │
│  codespaces-tracker (GKE) ──→ Dynatrace COE Tenant (geu80787)       │
│       │                              │                                │
│       │  BizEvents:                  │  Monitoring:                   │
│       │  - codespace.creation        │  - Nightly pass/fail heatmap   │
│       │  - ops.test.result           │  - Framework version drift     │
│       │  - ops.agent.action          │  - Error trends per repo       │
│       │  - ops.nightly.summary       │  - Geo: where labs run         │
│       │  - ops.sync.drift            │  - Agent activity log          │
│       └──────────────────────────────┘                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Key Links

| Resource | URL |
|----------|-----|
| Framework docs | https://dynatrace-wwse.github.io/codespaces-framework/ |
| Lab registry | https://dynatrace-wwse.github.io/ |
| Ops dashboard | https://autonomous-enablements.whydevslovedynatrace.com |
| COE tenant | https://geu80787.apps.dynatrace.com |
| Monitoring dashboard | https://geu80787.apps.dynatrace.com/ui/apps/dynatrace.dashboards/dashboard/041e6584-bdae-4fa0-9fa1-18731850cf20 |
| Codespaces tracker | https://codespaces-tracker.whydevslovedynatrace.com |
| repos.yaml | codespaces-framework/repos.yaml |
| Phase 2 plan | codespaces-framework/audit/FRAMEWORK_PHASE2.md |
| Validation plan | codespaces-framework/audit/VALIDATION-PLAN.md |

---

## Branches

| Branch | Commits ahead of origin | Status |
|--------|------------------------|--------|
| `main` | — | Stable. Phase 2 tasks 1-2 merged. v1.2.7 |
| `rfe/phase2-k3s-engine` | 7 committed + uncommitted changes | Shell PTY, daemon jobs, font, fullscreen, new window. **Not yet pushed** |
| `rfe/ops-server` | 37 | Ops server platform. Running on EC2 |
| `rfe/mkdocs-jscript` | — | MkDocs RUM refactoring. Ready to merge |

---

## Quick deploy reference

```bash
# After editing on ubuntu path, deploy to ops path:
sudo cp ops-server/dashboard/app.py            /home/ops/enablement-framework/codespaces-framework/ops-server/dashboard/app.py
sudo cp ops-server/dashboard/static/app.js     /home/ops/enablement-framework/codespaces-framework/ops-server/dashboard/static/app.js
sudo cp ops-server/dashboard/static/style.css  /home/ops/enablement-framework/codespaces-framework/ops-server/dashboard/static/style.css
sudo cp ops-server/dashboard/templates/index.html /home/ops/enablement-framework/codespaces-framework/ops-server/dashboard/templates/index.html
sudo cp ops-server/workers/manager.py          /home/ops/enablement-framework/codespaces-framework/ops-server/workers/manager.py
sudo cp ops-server/nginx/ops-server.conf       /etc/nginx/sites-available/ops-server

# Restart services:
sudo systemctl restart ops-dashboard
sudo systemctl restart ops-worker
sudo nginx -t && sudo systemctl reload nginx

# AMD worker — after pushing to GitHub:
ssh autonomous-enablements-worker \
  "cd /home/ops/codespaces-framework && sudo -u ops git pull && sudo systemctl restart ops-worker-agent"

# Watch logs:
sudo journalctl -fu ops-dashboard
sudo journalctl -fu ops-worker
ssh autonomous-enablements-worker "sudo journalctl -fu ops-worker-agent"
```

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

---

## What is left to do

### Phase 2 — Remaining tasks

| # | Task | Priority | Effort | Dependencies |
|---|------|----------|--------|--------------|
| 1 | **Merge k3d engine to main** | High | 1d | Review + test the 63 commits on `rfe/phase2-k3s-engine` |
| 2 | **Merge ops-server to main** | High | 1d | Review + test the 37 commits on `rfe/ops-server` |
| 3 | **Sync Dynakube logic to all repos** | High | 2d | Merge k3d first. `sync push-update` to deploy config-driven Dynakube |
| 4 | **Sync MkDocs refactoring to all repos** | Medium | 1d | Merge mkdocs-jscript. Remove per-page BizEvent JS snippets |
| 5 | **Unify Astroshop deployments** | Medium | 2d | Consolidate to 2 canonical apps: Astroshop (demo.live) + OTel Demo (CNCF) |
| 6 | **Improve integration tests for all repos** | High | 5d | Write mature `integration.sh` for each of the 27 repos |
| 7 | **Nightly regression tests (live)** | High | 1d | Ops server running. Configure nightly schedule, verify results in DT dashboard |
| 8 | **Update framework documentation** | Medium | 2d | Document: ingress, Dynakube config, variablesNeeded, MCP, Make targets, k3d |
| 9 | **Fix monitoring — agentless app IDs** | Medium | 1d | Ensure all repos use unique `codespace.app_id`, create missing agentless apps in DT |
| 10 | **Enhance monitoring dashboard** | Low | 1d | Add worldmap, nightly pass/fail heatmap, error trends |

### Gen2 → Gen3 Migration

Migrate all documentation from Dynatrace Classic UI to Native Apps:

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 11 | **Run phase1-scan across fleet** | Medium | 1d | Static keyword analysis: classify each doc page as GREEN/YELLOW/RED |
| 12 | **Migrate RED docs (highest risk)** | Medium | 3d | Rewrite classic entity references, navigation paths, deprecated DQL |
| 13 | **Migrate YELLOW docs (mixed)** | Low | 3d | Partial updates, validate remaining Gen3 patterns |
| 14 | **Re-capture screenshots** | Low | 5d | ~500 screenshots across 27 repos showing current Dynatrace UI |
| 15 | **Validate DQL queries** | Medium | 2d | Run all DQL from docs against COE tenant via dtctl/MCP |
| 16 | **Monthly drift monitoring** | Low | 1d | Set up ops-server Gen2 scanner cron + auto-create issues on drift |

### Autonomous Agent Capabilities

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 17 | **Auto-fix bugs from GitHub issues** | Medium | 2d | Claude agent reads issue → investigates → creates fix PR |
| 18 | **Auto-review PRs** | Medium | 1d | Claude agent reviews diffs for framework compliance, security, test coverage |
| 19 | **Auto-scaffold new labs** | Low | 2d | Claude agent creates repo from template, configures, adds to repos.yaml |
| 20 | **Auto CI failure diagnosis** | Medium | 2d | Claude agent reads failed logs → diagnoses → pushes fix to PR branch |
| 21 | **Auto Gen3 migration via Claude** | Medium | 3d | Claude agent runs migration using dt-migration + dt-dql-essentials skills |

### Infrastructure

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 22 | **Provision AMD worker EC2** | High | 1d | c5.2xlarge for x86_64 integration tests |
| 23 | **Set up TLS (Let's Encrypt)** | High | 1h | `certbot --nginx -d autonomous-enablements.whydevslovedynatrace.com` |
| 24 | **Configure GitHub org webhook** | High | 30m | Point dynatrace-wwse org webhook to ops server |
| 25 | **Resolve GitHub Actions ban** | High | — | Escalation with GitHub Support (pending) |
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

| Branch | Commits ahead | Status |
|--------|--------------|--------|
| `main` | — | Stable. Phase 2 tasks 1-2 merged. v1.2.7 |
| `rfe/phase2-k3s-engine` | 63 | K3d engine, config-driven Dynakube, error handling. Ready for review |
| `rfe/ops-server` | 37 | Ops server platform. Running on EC2 |
| `rfe/mkdocs-jscript` | — | MkDocs RUM refactoring. Ready to merge |

# Enablement Framework — Phases & Tasks

Full phased roadmap. Start each session by reading `CLAUDE.md` + memory files. End each session by updating `ops-server/PROJECT-STATUS.md` and the relevant memory file.

---

## Immediate — Before Any New Feature Work

| # | Task | Command / Notes |
|---|------|-----------------|
| I-1 | **Commit + push `rfe/phase2-k3s-engine`** | Uncommitted changes: CLAUDE.md, app.py, app.js, style.css, index.html, nginx/ops-server.conf, manager.py |
| I-2 | **AMD worker sync** | `ssh autonomous-enablements-worker "cd /home/ops/codespaces-framework && git pull"` |
| I-3 | **Test locally** | `cd .devcontainer && make clean-start && make integration` |
| I-4 | **Merge `rfe/mkdocs-jscript`** | MkDocs RUM refactoring — ready |
| I-5 | **Merge `rfe/phase2-k3s-engine` to main** | After full validation |
| I-6 | **Bump to v1.3.0 + sync all repos** | `sync tag --framework-version 1.3.0 && sync push-update --framework-version 1.3.0` |
| I-7 | **Review + merge 10 pending badge/RUM PRs** | fix/badges-and-rum-ids branch |
| I-8 | **Create 7 agentless RUM apps** | COE tenant geu80787 — one per repo with unique dynatrace-wwse-{repo-name} ID |
| I-9 | **OpenPipeline geo fields** | Add content.geo.country.isoCode, .latitude, .longitude to codespaces-tracker pipeline |

---

## Phase 2 — Core Framework (remaining)

### 2-6: Integration tests for all 27 repos

For each repo in repos.yaml, write a mature `integration.sh` using the full assertion toolkit:

```bash
assertRunningPod kube-system coredns
assertRunningPod dynatrace operator
assertRunningPod dynatrace activegate
assertRunningPod <app-ns> <app-name>
assertRunningApp <app-name>
assertEnvVariable DT_ENVIRONMENT
```

Run via Orbital nightly tests after merge.

### 2-7: Gen2 → Gen3 migration

1. Run `phase1-scan.py` across fleet → classify pages GREEN/YELLOW/RED
2. Migrate RED pages (classic entity references, deprecated DQL)
3. Migrate YELLOW pages (partial updates)
4. Re-capture ~500 screenshots showing current DT UI
5. Validate all DQL queries via dtctl/MCP

### 2-8: Documentation updates (in progress)

- [x] testing.md — updated: new assertion functions, BATS unit tests
- [x] functions.md — updated: K3d cluster engine, unified cluster API
- [ ] framework.md — update K3d as default engine, ingress strategy
- [ ] instantiation-types.md — update for K3d vs Kind guidance
- [ ] ops-platform.md — add daemon job lifecycle for training sessions

---

## Phase 3 — Autonomous Agent Capabilities

All agent jobs run on master node via `queue:agent`. Claude Code invoked with CLAUDE.md-aware workspace + DT MCP.

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 3-1 | Auto-fix bugs from GitHub issues | Medium | 2d |
| 3-2 | Auto-review PRs (framework compliance) | Medium | 1d |
| 3-3 | Auto-scaffold new labs from template | Low | 2d |
| 3-4 | Auto-diagnose CI failures + push fix | Medium | 2d |
| 3-5 | Auto Gen3 migration via Claude | Medium | 3d |
| 3-6 | Dashboard: agent job UI + log streaming | Low | 1d |

---

## Phase 4 — Dynatrace Training App

Self-service training delivery inside a Dynatrace App. Daemon jobs in Observer are the training environment backbone.

### Phase 4A — API & Architecture Design

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 4A-1 | REST API contract (start/status/stop/validate-module) | Critical | 0.5d |
| 4A-2 | Architecture data-flow spec (DT App ↔ ops-server ↔ Sysbox) | Critical | 1d |
| 4A-3 | Auth mechanism (DT App identity → ops-server) | Critical | 0.5d |
| 4A-4 | Module validation protocol (DQL, kubectl, HTTP assertions) | High | 1d |

### Phase 4B — Ops-Server Extensions

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 4B-1 | `training` job type (daemon + lab metadata) | Critical | 1d |
| 4B-2 | Training lifecycle REST API | Critical | 2d |
| 4B-3 | Module validation endpoint (runs assertions in Sysbox) | High | 1d |
| 4B-4 | Training session registry (Redis: student→job_id, TTL) | High | 1d |
| 4B-5 | Session expiry + nightly orphan cleanup | High | 0.5d |
| 4B-6 | Training catalog endpoint (from repos.yaml) | Medium | 0.5d |
| 4B-7 | BizEvents: training.start, module.complete, training.complete | Medium | 0.5d |

### Phase 4C — DT App (TypeScript/React + DT App SDK)

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 4C-1 | Scaffold DT App with App SDK | Critical | 1d |
| 4C-2 | Training catalog view (list labs, filter by tag/duration) | Critical | 2d |
| 4C-3 | Training session view (steps + embedded iframe + xterm.js shell) | Critical | 3d |
| 4C-4 | Module progress + validation trigger ("Check my work") | High | 2d |
| 4C-5 | Assessment/quiz component between modules | Medium | 2d |
| 4C-6 | Badge issuance on completion | Medium | 1d |
| 4C-7 | GStack E2E tests for full user flow | High | 2d |

### Phase 4D — Integration & Hardening

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 4D-1 | SSO auth bridge (DT App user → ops-server session) | Critical | 1d |
| 4D-2 | Resource limits per session (CPU/RAM on Sysbox) | High | 0.5d |
| 4D-3 | Isolation validation (cross-session leakage test) | High | 1d |
| 4D-4 | Load testing (N parallel sessions, resource usage) | Medium | 1d |

---

## How to Work Efficiently With Memory

### Start of session

```bash
# Memory auto-loads from CLAUDE.md. Then:
cat /home/ubuntu/.claude/projects/-home-ubuntu-enablement-framework/memory/project_status.md
# Load specific file(s) for today's work
```

### Key memory files by work type

| Work type | Load these memory files |
|-----------|------------------------|
| Framework / sync CLI | project_framework.md, project_status.md |
| Ops-server / dashboard | project_ops_server.md, feedback.md |
| Training app design | project_vision.md, project_ops_server.md |
| Any PR / branch work | project_status.md |

### End of session

1. Update `project_status.md` — branch state, completed tasks
2. Update `feedback.md` — new conventions or gotchas learned
3. Commit documentation changes
4. Push branch if work is ready

# Documentation & Screenshot Validation Plan

Systematic validation of all enablement repo documentation against the live Dynatrace platform. Prioritizes identifying Gen2 (classic) vs Gen3 (native app) drift as Dynatrace migrates features.

## Phase 1: Static Analysis (no tenant needed)

**Goal:** Classify every doc page and screenshot by Gen2/Gen3 risk level.

| Step | Task | Method |
|------|------|--------|
| 1a | Inventory all docs | Fetch every `docs/*.md` from all repos via GitHub API. Build registry: repo, file, section count, image count |
| 1b | Screenshot inventory | List every `docs/img/*` per repo. Map screenshot-to-step via markdown image references |
| 1c | Gen2 keyword scan | Grep all docs for classic UI terms (see keyword lists below) |
| 1d | Gen3 keyword scan | Grep all docs for modern platform terms |
| 1e | Risk classification | Flag each doc page as GREEN / YELLOW / RED. Output risk matrix per repo |

### Gen2 Keywords (Classic UI)

- "Data Explorer", "Create custom chart", "Multidimensional analysis"
- "Classic UI", "Classic view"
- "entity selector" (classic query syntax)
- "Management Zone" (replaced by ownership/segments in Gen3)
- Navigation: "Observe and explore >", "Transactions & services", "Technologies and processes"
- "Hosts" as menu navigation (classic infra view)
- "Settings >" patterns (classic settings, replaced by Apps)
- "Deployment status" (classic)
- "Real User Monitoring" or "RUM" as menu item
- "Synthetic" as menu navigation
- "custom device", "Process group", "Host group" (classic entity types)
- "Application detection" (classic setting)
- "Web application" as entity type (classic naming)
- "Smartscape" referring to classic topology view

### Gen3 Keywords (Native Apps / Grail)

- "Notebooks", "Grail", "DQL" (Dynatrace Query Language)
- "Apps >" or "Launcher" in navigation context
- "OpenPipeline" (data processing)
- "Ownership" (replaces Management Zones)
- "Davis CoPilot", "Davis AI"
- "Automations", "Workflows" (Gen3)
- "Hub" (app marketplace)
- "Segments", "Buckets" (Grail storage)

### Risk Classification Logic

| Risk | Criteria |
|------|----------|
| GREEN | gen2_count == 0 |
| YELLOW | gen2_count > 0 AND gen3_count > 0 (mixed, transitioning) |
| RED | gen2_count > 0 AND gen3_count == 0 (fully classic) |
| RED | gen2_count > 5 regardless of gen3 (heavy classic usage) |

**Output:** Per-repo `phase1.json` in `audit/data/{repo}/`, aggregated in `audit/phase1-results.json`, and integrated into `audit/master-table.html`.

---

## Phase 2: Live UI Validation (needs tenant + browse tool)

**Goal:** Compare screenshots and navigation steps against the actual current Dynatrace UI.

| Step | Task | Method |
|------|------|--------|
| 2a | Set up authentication | Configure browser cookies or SSO login to access the Dynatrace tenant via the browse tool |
| 2b | Navigation path validation | For each doc step saying "Navigate to X > Y > Z", follow that exact path in the live tenant. Flag if menu items don't exist or have moved |
| 2c | Screenshot comparison | Navigate to the same screen in the live tenant, capture a screenshot, compare visually. Flag: layout changes, missing features, new undocumented features, renamed elements |
| 2d | DQL query validation | Extract every DQL query from docs, run via dtctl or MCP server, flag syntax errors or deprecated functions |

### Approach per repo

The browse tool navigates the tenant step-by-step following the lab instructions. At each step:

1. Read the instruction from the markdown
2. Attempt to follow it in the live UI
3. Take a screenshot of the current state
4. Compare against the doc screenshot
5. Log pass/fail with evidence

### Prerequisites

- Dynatrace tenant URL (the environment labs point to)
- API token with scopes: `Read entities`, `Read settings`, `Read SRG`, `Read metrics`
- User login credentials for the browse tool
- dtctl (`brew install dynatrace-oss/tap/dtctl`) or Dynatrace MCP server

---

## Phase 3: Automated Fixes (needs write access)

| Step | Task | Method |
|------|------|--------|
| 3a | Text updates | Replace outdated navigation paths, feature names, and terminology. Use `dt-migration` skill for classic-to-Gen3 mapping |
| 3b | DQL query fixes | Update deprecated DQL syntax using `dt-dql-essentials` patterns |
| 3c | Screenshot re-capture | Navigate to correct screen in live tenant, capture fresh screenshots, replace old images in repo |
| 3d | PR per repo | One PR per repo with all fixes. Each PR includes a changelog of what changed and why |

---

## Phase 4: Ongoing Monitoring

| Step | Task | Method |
|------|------|--------|
| 4a | Monthly validation cron | Scheduled agent runs Phase 1 static analysis monthly, alerts on new drift |
| 4b | Dynatrace release tracking | When Dynatrace ships a new release, run Phase 2 against repos tagged with affected features |

---

## Tools

| Tool | Purpose |
|------|---------|
| Browse skill | Visual UI comparison against live tenant |
| dtctl | kubectl-style CLI for Dynatrace platform API |
| Dynatrace MCP server | API access via MCP protocol |
| Dynatrace for AI skills | `dt-migration` (classic to Gen3 mapping), `dt-dql-essentials` (DQL validation), `dt-obs-*` (observability queries). Source: https://github.com/Dynatrace/dynatrace-for-ai |
| GitHub API / sync CLI | Fetch doc content, coordinate updates across repos |
| Phase 1 scanner | `audit/phase1-scan.py` — automated Gen2/Gen3 keyword analysis |
| Master audit table | `audit/generate-html.py` — generates `master-table.html` with all results |

---

## Running the Analysis

```bash
# Phase 1: Fetch docs and run static analysis
bash audit/fetch-docs.sh
python3 audit/phase1-scan.py

# Regenerate master table with Phase 1 results
python3 audit/generate-html.py
```

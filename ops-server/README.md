# Enablement Ops Server

Autonomous CI/CD and operations platform for the dynatrace-wwse enablement fleet.

## What it does

| Service | Description | Schedule |
|---------|-------------|----------|
| **Webhook Listener** | Receives GitHub events, routes to job queue | Always-on |
| **Worker Manager** | Executes integration tests and Claude agent sessions | Always-on |
| **Nightly Tests** | Staggered integration tests across all 27 repos | Daily 02:00 UTC |
| **Sync Status** | Detects framework version drift | Hourly |
| **Gen2 Scanner** | Detects Gen2→Gen3 documentation drift | Daily 06:00 UTC |
| **Claude Agents** | Auto-fix bugs, review PRs, migrate docs, scaffold labs | On-demand (webhook) |

## Architecture

```
GitHub Webhooks (org-level)
  │
  ├─ issue.opened (label:bug)      → Claude agent: investigate + fix + PR
  ├─ issue.opened (label:gen3)     → Claude agent: migrate docs + PR
  ├─ issue.opened (label:new-lab)  → Claude agent: scaffold from template
  ├─ pull_request.opened           → Claude agent: review + comment
  ├─ check_suite.completed (fail)  → Claude agent: diagnose + fix CI
  └─ push (to main)               → Sync: validate repo state
                │
                ▼
        ┌──────────────┐     ┌──────────────┐
        │ Redis Queue   │────▶│ Worker Mgr   │
        │ agent|test|sync│    │ (concurrent) │
        └──────────────┘     └──────┬───────┘
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
              Integration      Claude Code       Sync CLI
              Tests (Docker    Agent Session     (validate,
              + k3d cluster)   (MCP + dtctl)     status)
                    │                │                │
                    └────────────────┼────────────────┘
                                     ▼
                            Codespaces Tracker
                            → DT BizEvents
                            → Ops Dashboard
```

---

## EC2 Machine Requirements

### Recommended: `c7g.2xlarge` (ARM / Graviton3)

| Spec | Value | Why |
|------|-------|-----|
| **Arch** | arm64 (Graviton3) | Faster CPU, required for ARM cross-compilation |
| **vCPU** | 8 | 6 parallel k3d clusters + webhook + worker manager |
| **RAM** | 16 GB | Each k3d cluster needs ~1-1.5 GB |
| **Storage** | 200 GB gp3 | Docker images (~50 GB built, ~20 GB cache, ~10 GB vendor), repos, logs |
| **OS** | Ubuntu 24.04 LTS (arm64) | |
| **Network** | Public IP + security group | Port 8443 (webhook), 22 (SSH) |

### Alternative: `c7g.4xlarge` (16 vCPU, 32 GB)

For maximum parallelism (8+ concurrent tests) or if running Claude agents
alongside integration tests simultaneously.

### Budget option: `c7g.xlarge` (4 vCPU, 8 GB)

Supports 2-3 parallel tests. Good for starting out.

### Estimated monthly cost

| Instance | On-Demand | Reserved (1yr) | Spot |
|----------|-----------|-----------------|------|
| c7g.xlarge | ~$105 | ~$66 | ~$31 |
| c7g.2xlarge | ~$210 | ~$132 | ~$63 |
| c7g.4xlarge | ~$420 | ~$264 | ~$126 |

> The setup script auto-detects architecture — works on both ARM and x86.

---

## Security Group (AWS)

| Port | Source | Purpose |
|------|--------|---------|
| 22 | Your IP | SSH access |
| 8443 | `140.82.112.0/20`, `185.199.108.0/22`, `192.30.252.0/22` | GitHub webhooks |

> The source CIDRs above are [GitHub's webhook IP ranges](https://api.github.com/meta).

---

## Setup Steps

### 1. Launch EC2 Instance

```bash
# AWS CLI example
aws ec2 run-instances \
  --image-id ami-0xxxxxxxxxxxxxxxxx \  # Ubuntu 24.04 LTS
  --instance-type c5.2xlarge \
  --key-name your-key \
  --security-group-ids sg-xxxxxxxxx \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":100,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=enablement-ops}]'
```

### 2. SSH in and run the bootstrap

```bash
ssh -i your-key.pem ubuntu@autonomous-enablements.whydevslovedynatrace.com

# Clone the framework
git clone https://github.com/dynatrace-wwse/codespaces-framework.git ~/enablement-framework/codespaces-framework

# Run setup (installs Docker, Claude Code, dtctl, gh, Redis, Python deps)
sudo bash ~/enablement-framework/codespaces-framework/ops-server/setup.sh
```

### 3. Authenticate tools (as the `ops` user)

```bash
sudo su - ops

# GitHub CLI — authenticate with the dynatrace-wwse org
gh auth login

# Verify
gh auth status
```

### 4. Configure secrets

```bash
# Copy and fill in the env template
cp ~/enablement-framework/codespaces-framework/ops-server/agents/env.template ~/.env
nano ~/.env

# Required values:
#   WEBHOOK_SECRET        — generate with: openssl rand -hex 32
#   ANTHROPIC_API_KEY     — from console.anthropic.com
#   DT_ENVIRONMENT        — https://geu80787.apps.dynatrace.com
#   DT_OPERATOR_TOKEN     — Dynatrace operator token
#   DT_INGEST_TOKEN       — Dynatrace ingest token
#   DT_API_TOKEN          — Dynatrace API token (for MCP + dtctl)
#   OAUTH2_CLIENT_ID      — GitHub OAuth App Client ID (see step 4a)
#   OAUTH2_CLIENT_SECRET  — GitHub OAuth App Client Secret
#   OAUTH2_GITHUB_ORG     — GitHub org allowed to sign in (default: dynatrace-wwse)
#   OAUTH2_COOKIE_SECRET  — generate with: openssl rand -base64 32 | tr -- '+/' '-_'
```

### 4a. Create the GitHub OAuth App (for dashboard SSO)

The dashboard reads are public, but **build triggers** require GitHub SSO via
[oauth2-proxy](https://oauth2-proxy.github.io/). Members of the configured GitHub
org can sign in; everyone else can view but cannot trigger anything.

1. Go to: `https://github.com/organizations/dynatrace-wwse/settings/applications/new`
2. Fill in:
   | Field | Value |
   |---|---|
   | Application name | `Enablement Ops Dashboard` |
   | Homepage URL | `https://autonomous-enablements.whydevslovedynatrace.com` |
   | Authorization callback URL | `https://autonomous-enablements.whydevslovedynatrace.com/oauth2/callback` |
3. Save the **Client ID** and click **Generate a new client secret**. Copy both into `~/.env`.
4. Re-run `sudo bash ~/enablement-framework/codespaces-framework/ops-server/setup.sh` —
   the script will render `/etc/oauth2-proxy/oauth2-proxy.cfg` from
   `~/.env` and install the `oauth2-proxy.service` systemd unit.

> The OAuth secret is **never** committed. The repo only ships
> `ops-server/oauth2-proxy/oauth2-proxy.cfg.template`, which `setup.sh`
> renders via `envsubst` from `~/.env`.

### 5. Configure Claude Code MCP

```bash
# Copy MCP config and substitute your values
mkdir -p ~/.claude
cp ~/enablement-framework/codespaces-framework/ops-server/agents/mcp.json ~/.claude/mcp.json

# Copy the CLAUDE.md for agent sessions
cp ~/enablement-framework/codespaces-framework/ops-server/agents/CLAUDE.md \
   ~/.claude/CLAUDE.md
```

### 6. Configure dtctl

```bash
mkdir -p ~/.config/dtctl
cp ~/enablement-framework/codespaces-framework/ops-server/agents/dtctl.yaml \
   ~/.config/dtctl/config.yaml

# Verify connectivity
dtctl config get-contexts
dtctl query 'fetch logs | limit 1'
```

### 7. Clone all repos

```bash
cd ~/enablement-framework/codespaces-framework
PYTHONPATH=. python3 -m sync.cli clone --all
```

### 8. Set up GitHub organization webhook

Go to: `github.com/organizations/dynatrace-wwse/settings/hooks`

| Setting | Value |
|---------|-------|
| Payload URL | `https://autonomous-enablements.whydevslovedynatrace.com:8443/webhook` |
| Content type | `application/json` |
| Secret | Same value as `WEBHOOK_SECRET` in `.env` |
| Events | Issues, Pull requests, Check suites, Pushes |

### 9. Start services

```bash
# Start all services
sudo systemctl start ops-webhook
sudo systemctl start ops-worker
sudo systemctl start ops-nightly.timer
sudo systemctl start ops-sync-daemon.timer
sudo systemctl start ops-gen2scan.timer

# Verify everything is running
sudo systemctl status ops-webhook
sudo systemctl status ops-worker
sudo systemctl status ops-nightly.timer

# Check webhook health
curl http://localhost:8443/health
```

### 10. Verify end-to-end

```bash
# Test the nightly scheduler (dry run)
cd ~/enablement-framework/codespaces-framework/ops-server
PYTHONPATH=. python3 -m nightly.scheduler schedule

# Queue a single test
PYTHONPATH=. python3 -m nightly.scheduler single enablement-codespaces-template

# Check queue status
curl http://localhost:8443/status

# Watch logs
journalctl -u ops-worker -f
```

---

## Operations

### View logs

```bash
# Webhook server
journalctl -u ops-webhook -f

# Worker manager
journalctl -u ops-worker -f

# Last nightly run
journalctl -u ops-nightly --since today

# Sync status
journalctl -u ops-sync-daemon --since today
```

### Run nightly manually

```bash
sudo systemctl start ops-nightly
```

### Queue a test for a single repo

```bash
cd ~/enablement-framework/codespaces-framework/ops-server
PYTHONPATH=. python3 -m nightly.scheduler single dynatrace-wwse/enablement-dql-301
```

### View nightly results

```bash
PYTHONPATH=. python3 -m nightly.scheduler report
```

### Check webhook queue depth

```bash
curl http://localhost:8443/health
curl http://localhost:8443/status
```

### Update the ops server

```bash
cd ~/enablement-framework/codespaces-framework
git pull
sudo systemctl restart ops-webhook ops-worker
```

---

## GitHub Labels for Automation

Create these labels in each repo (or at the org level) to trigger automation:

| Label | Color | Triggers |
|-------|-------|----------|
| `bug` | `#d73a4a` | Claude agent investigates and creates fix PR |
| `gen3-migration` | `#0075ca` | Claude agent migrates Gen2 docs to Gen3 |
| `new-enablement` | `#008672` | Claude agent scaffolds new lab from template |

---

## File Structure

```
ops-server/
├── README.md                  # This file
├── setup.sh                   # EC2 bootstrap script
├── requirements.txt           # Python dependencies
├── webhook/
│   ├── __init__.py
│   ├── config.py              # Environment and path configuration
│   └── server.py              # FastAPI webhook listener + routing
├── workers/
│   ├── __init__.py
│   └── manager.py             # Job queue consumer + handler dispatch
├── nightly/
│   ├── __init__.py
│   └── scheduler.py           # Staggered nightly test orchestrator
├── telemetry/
│   ├── __init__.py
│   └── reporter.py            # Report results to codespaces-tracker
├── agents/
│   ├── CLAUDE.md              # Claude Code agent instructions
│   ├── mcp.json               # Dynatrace MCP server config
│   ├── dtctl.yaml             # dtctl CLI config template
│   └── env.template           # Environment variables template (incl. OAUTH2_*)
├── oauth2-proxy/
│   └── oauth2-proxy.cfg.template  # GitHub SSO config (rendered by setup.sh)
└── systemd/
    ├── ops-webhook.service    # Webhook server (always-on)
    ├── ops-worker.service     # Worker manager (always-on)
    ├── ops-dashboard.service  # Dashboard UI (always-on)
    ├── oauth2-proxy.service   # GitHub SSO sidecar (always-on)
    ├── ops-nightly.service    # Nightly test runner (oneshot)
    ├── ops-nightly.timer      # Nightly cron (02:00 UTC)
    ├── ops-sync-daemon.service # Sync status check (oneshot)
    ├── ops-sync-daemon.timer  # Hourly sync cron
    ├── ops-gen2scan.service   # Gen2 drift scanner (oneshot)
    └── ops-gen2scan.timer     # Daily Gen2 scan (06:00 UTC)
```

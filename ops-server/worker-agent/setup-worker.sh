#!/usr/bin/env bash
# =============================================================================
# Worker Node Bootstrap — Lightweight setup for remote test workers
# =============================================================================
# Run on a fresh Ubuntu 24.04 EC2 instance (ARM or AMD).
# Usage: sudo bash setup-worker.sh
# =============================================================================

set -euo pipefail

OPS_USER="ops"
OPS_HOME="/home/${OPS_USER}"
VENV_DIR="${OPS_HOME}/ops-venv"

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Pre-flight ───────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo bash setup-worker.sh"

# ── Detect architecture ──────────────────────────────────────────────────────
ARCH=$(uname -m)
case "${ARCH}" in
    aarch64|arm64) ARCH_DEB="arm64"; ARCH_K8S="arm64"; ARCH_GO="arm64" ;;
    x86_64)        ARCH_DEB="amd64"; ARCH_K8S="amd64"; ARCH_GO="amd64" ;;
    *)             error "Unsupported architecture: ${ARCH}" ;;
esac

info "Worker node setup (arch: ${ARCH} → ${ARCH_DEB})..."

# ── Create ops user ──────────────────────────────────────────────────────────
if ! id -u "${OPS_USER}" &>/dev/null; then
    info "Creating user: ${OPS_USER}"
    useradd -m -s /bin/bash "${OPS_USER}"
fi

# ── System packages (minimal — no Redis, no Nginx, no Claude) ────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    docker.io \
    docker-compose-plugin \
    git \
    curl \
    wget \
    jq \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    gnupg \
    socat \
    iptables

# ── Enable Docker ────────────────────────────────────────────────────────────
systemctl enable --now docker
usermod -aG docker "${OPS_USER}"

# ── Install GitHub CLI ───────────────────────────────────────────────────────
if ! command -v gh &>/dev/null; then
    info "Installing GitHub CLI..."
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt-get update -qq && apt-get install -y -qq gh
fi

# ── Install kubectl ──────────────────────────────────────────────────────────
if ! command -v kubectl &>/dev/null; then
    info "Installing kubectl..."
    curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH_K8S}/kubectl" \
        -o /usr/local/bin/kubectl
    chmod +x /usr/local/bin/kubectl
fi

# ── Install Helm ─────────────────────────────────────────────────────────────
if ! command -v helm &>/dev/null; then
    info "Installing Helm..."
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

# ── Install k3d ──────────────────────────────────────────────────────────────
if ! command -v k3d &>/dev/null; then
    info "Installing k3d..."
    curl -fsSL https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
fi

# ── Clone worker-agent code ─────────────────────────────────────────────────
AGENT_DIR="${OPS_HOME}/worker-agent"
if [[ ! -d "${AGENT_DIR}" ]]; then
    info "Cloning worker-agent code..."
    sudo -u "${OPS_USER}" git clone \
        --depth 1 --filter=blob:none --sparse \
        https://github.com/dynatrace-wwse/codespaces-framework.git \
        "${OPS_HOME}/codespaces-framework"
    cd "${OPS_HOME}/codespaces-framework"
    sudo -u "${OPS_USER}" git sparse-checkout set ops-server/worker-agent ops-server/requirements.txt
    ln -sf "${OPS_HOME}/codespaces-framework/ops-server/worker-agent" "${AGENT_DIR}"
fi

# ── Python virtual environment ──────────────────────────────────────────────
info "Setting up Python venv..."
sudo -u "${OPS_USER}" python3 -m venv "${VENV_DIR}"
sudo -u "${OPS_USER}" "${VENV_DIR}/bin/pip" install -q --upgrade pip
sudo -u "${OPS_USER}" "${VENV_DIR}/bin/pip" install -q \
    redis python-dotenv

# ── Create directories ──────────────────────────────────────────────────────
sudo -u "${OPS_USER}" mkdir -p \
    "${OPS_HOME}/repos" \
    "${OPS_HOME}/logs" \
    "${OPS_HOME}/workdir"

# ── Install systemd service ─────────────────────────────────────────────────
cat > /etc/systemd/system/ops-worker-agent.service << 'UNIT'
[Unit]
Description=Enablement Ops Worker Agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=ops
Group=ops
WorkingDirectory=/home/ops/codespaces-framework/ops-server
ExecStart=/home/ops/ops-venv/bin/python -m worker-agent.agent
EnvironmentFile=/home/ops/.env
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ops-worker-agent

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable ops-worker-agent.service

# ── Summary ──────────────────────────────────────────────────────────────────
info "=========================================="
info "  Worker Node installed! (${ARCH_DEB})"
info "=========================================="
echo ""
echo "  Next steps (as ops user):"
echo ""
echo "  1. Configure .env:"
echo "     cat > ${OPS_HOME}/.env << EOF"
echo "     MASTER_REDIS_URL=redis://<master-private-ip>:6379/0"
echo "     MASTER_REDIS_PASSWORD=<redis-auth-password>"
echo "     WORKER_ARCH=${ARCH_DEB}"
echo "     WORKER_CAPACITY=6"
echo "     DT_ENVIRONMENT=https://geu80787.apps.dynatrace.com"
echo "     DT_OPERATOR_TOKEN=<token>"
echo "     DT_INGEST_TOKEN=<token>"
echo "     EOF"
echo ""
echo "  2. Authenticate GitHub CLI:"
echo "     sudo -u ops gh auth login"
echo ""
echo "  3. Start the worker:"
echo "     sudo systemctl start ops-worker-agent"
echo "     journalctl -u ops-worker-agent -f"
echo ""

#!/usr/bin/env bash
# =============================================================================
# Worker Node Bootstrap — Lightweight setup for remote test workers
# =============================================================================
# Run on a fresh Ubuntu 24.04 EC2 instance (ARM or AMD).
# Usage: sudo bash setup-worker.sh
# =============================================================================

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

OPS_USER="ops"
OPS_HOME="/home/${OPS_USER}"
VENV_DIR="${OPS_HOME}/ops-venv"
REPO_DIR="${OPS_HOME}/enablement-framework/codespaces-framework"
SYSBOX_VERSION="0.6.7"

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
    aarch64|arm64) ARCH_DEB="arm64"; ARCH_K8S="arm64" ;;
    x86_64)        ARCH_DEB="amd64"; ARCH_K8S="amd64" ;;
    *)             error "Unsupported architecture: ${ARCH}" ;;
esac

info "Worker node setup (arch: ${ARCH} → ${ARCH_DEB})..."

# ── Create ops user ──────────────────────────────────────────────────────────
if ! id -u "${OPS_USER}" &>/dev/null; then
    info "Creating user: ${OPS_USER}"
    useradd -m -s /bin/bash "${OPS_USER}"
fi

# ── System packages ──────────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    docker.io \
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
    iptables \
    rsync

# ── Enable Docker ────────────────────────────────────────────────────────────
systemctl enable --now docker
usermod -aG docker "${OPS_USER}"
# ubuntu user needs docker access: master ops-dashboard SSHes in as ubuntu
# to run 'docker exec' into Sysbox containers for the shell PTY bridge.
usermod -aG docker ubuntu 2>/dev/null || true

# ── Install Sysbox CE ────────────────────────────────────────────────────────
if ! command -v sysbox-runc &>/dev/null; then
    info "Installing Sysbox CE ${SYSBOX_VERSION} (${ARCH_DEB})..."
    SYSBOX_DEB="sysbox-ce_${SYSBOX_VERSION}.linux_${ARCH_DEB}.deb"
    curl -fsSL -o "/tmp/${SYSBOX_DEB}" \
        "https://github.com/nestybox/sysbox/releases/download/v${SYSBOX_VERSION}/${SYSBOX_DEB}"
    apt-get install -y "/tmp/${SYSBOX_DEB}"
    rm -f "/tmp/${SYSBOX_DEB}"
    systemctl enable --now sysbox
fi

# ── Configure Docker daemon (sysbox runtime + stable CIDR) ──────────────────
info "Configuring Docker daemon..."
cat > /etc/docker/daemon.json << 'DAEMON'
{
    "runtimes": {
        "sysbox-runc": {
            "path": "/usr/bin/sysbox-runc"
        }
    },
    "bip": "172.20.0.1/16",
    "default-address-pools": [
        {
            "base": "172.25.0.0/16",
            "size": 24
        }
    ]
}
DAEMON
systemctl restart docker

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

# ── Clone worker-agent code (canonical path) ─────────────────────────────────
AGENT_SYMLINK="${OPS_HOME}/worker-agent"
if [[ ! -d "${REPO_DIR}" ]]; then
    info "Cloning codespaces-framework (sparse)..."
    sudo -u "${OPS_USER}" mkdir -p "${OPS_HOME}/enablement-framework"
    sudo -u "${OPS_USER}" git clone \
        --depth 1 --filter=blob:none --sparse \
        https://github.com/dynatrace-wwse/codespaces-framework.git \
        "${REPO_DIR}"
    cd "${REPO_DIR}"
    sudo -u "${OPS_USER}" git sparse-checkout init --no-cone
    sudo -u "${OPS_USER}" git sparse-checkout set \
        'ops-server/worker-agent/**' \
        'ops-server/requirements.txt' \
        'ops-server/systemd/**' \
        'ops-server/ops-docker-cleanup.sh' \
        'ops-server/tools/**'
    # tools/ holds app_layer_driver.py, which the worker-agent copies into the repo
    # when running an app-layer-test (amd64/AstroShop labs run here) — without it the
    # driver copy fails and the job silently falls back to integration.sh.
fi
# Convenience symlink: /home/ops/worker-agent → …/ops-server/worker-agent
ln -sfn "${REPO_DIR}/ops-server/worker-agent" "${AGENT_SYMLINK}"

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
cat > /etc/systemd/system/ops-worker-agent.service << UNIT
[Unit]
Description=Enablement Ops Worker Agent
After=network-online.target docker.service sysbox.service
Wants=network-online.target

[Service]
Type=simple
User=ops
Group=ops
WorkingDirectory=${REPO_DIR}/ops-server
ExecStart=${VENV_DIR}/bin/python -m worker-agent.agent
EnvironmentFile=${OPS_HOME}/.env
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ops-worker-agent

[Install]
WantedBy=multi-user.target
UNIT

cp "${REPO_DIR}/ops-server/ops-docker-cleanup.sh" /usr/local/bin/ops-docker-cleanup
chmod +x /usr/local/bin/ops-docker-cleanup
cp "${REPO_DIR}/ops-server/systemd/ops-docker-cleanup.service" /etc/systemd/system/
cp "${REPO_DIR}/ops-server/systemd/ops-docker-cleanup.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable ops-worker-agent.service
systemctl enable --now ops-docker-cleanup.timer

# ── Summary ──────────────────────────────────────────────────────────────────
info "=========================================="
info "  Worker Node installed! (${ARCH_DEB})"
info "  Repo: ${REPO_DIR}"
info "=========================================="
echo ""
echo "  Next steps:"
echo ""
echo "  1. Create /home/ops/.env:"
echo "     MASTER_REDIS_URL=redis://172.31.36.172:6379/0"
echo "     MASTER_REDIS_PASSWORD=<password>"
echo "     WORKER_ARCH=${ARCH_DEB}"
echo "     WORKER_INSTANCE=001   # worker id becomes w{amd|arm}{NNN}, e.g. wamd001 (must be unique per box)"
echo "     WORKER_CAPACITY=6"
echo "     WORKER_SSH_HOST=<alias in master ops ~/.ssh/config>"
echo "     DT_ENVIRONMENT=https://geu80787.apps.dynatrace.com"
echo "     DT_OPERATOR_TOKEN=<token>"
echo "     DT_INGEST_TOKEN=<token>"
echo "     TEST_TIMEOUT=1800"
echo ""
echo "  2. sudo systemctl start ops-worker-agent"
echo "     sudo journalctl -u ops-worker-agent -f"
echo ""

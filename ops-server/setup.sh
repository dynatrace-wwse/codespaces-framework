#!/usr/bin/env bash
# =============================================================================
# Enablement Ops Server — Bootstrap Script
# =============================================================================
# Run on a fresh Ubuntu 24.04 EC2 instance (c7g.2xlarge ARM recommended).
# Usage: sudo bash setup.sh
# =============================================================================

set -euo pipefail

OPS_USER="ops"
OPS_HOME="/home/${OPS_USER}"
OPS_DIR="${OPS_HOME}/enablement-framework"
VENV_DIR="${OPS_HOME}/ops-venv"

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Pre-flight ───────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo bash setup.sh"

# ── Detect architecture ──────────────────────────────────────────────────────
ARCH=$(uname -m)
case "${ARCH}" in
    aarch64|arm64) ARCH_DEB="arm64"; ARCH_K8S="arm64"; ARCH_GO="arm64" ;;
    x86_64)        ARCH_DEB="amd64"; ARCH_K8S="amd64"; ARCH_GO="amd64" ;;
    *)             error "Unsupported architecture: ${ARCH}" ;;
esac

info "Starting Enablement Ops Server setup (arch: ${ARCH} → ${ARCH_DEB})..."

# ── Create ops user ──────────────────────────────────────────────────────────
if ! id -u "${OPS_USER}" &>/dev/null; then
    info "Creating user: ${OPS_USER}"
    useradd -m -s /bin/bash "${OPS_USER}"
    usermod -aG docker "${OPS_USER}" 2>/dev/null || true
fi

# ── System packages ─────────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    docker.io \
    docker-compose-plugin \
    git \
    curl \
    wget \
    unzip \
    jq \
    python3 \
    python3-pip \
    python3-venv \
    redis-server \
    nginx \
    certbot \
    python3-certbot-nginx \
    ca-certificates \
    gnupg \
    socat \
    iptables

# ── Enable services ─────────────────────────────────────────────────────────
systemctl enable --now docker
systemctl enable --now redis-server

# Add ops user to docker group
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

# ── Install Node.js 22 ──────────────────────────────────────────────────────
if ! command -v node &>/dev/null; then
    info "Installing Node.js 22..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y -qq nodejs
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

# ── Install Claude Code ─────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
    info "Installing Claude Code..."
    npm install -g @anthropic-ai/claude-code
fi

# ── Install dtctl ────────────────────────────────────────────────────────────
if ! command -v dtctl &>/dev/null; then
    info "Installing dtctl..."
    DTCTL_VERSION=$(curl -fsSL "https://api.github.com/repos/dynatrace-oss/dynatrace-cli/releases/latest" | jq -r .tag_name)
    curl -fsSL -o /tmp/dtctl.tar.gz \
        "https://github.com/dynatrace-oss/dynatrace-cli/releases/download/${DTCTL_VERSION}/dtctl_linux_${ARCH_GO}.tar.gz"
    tar -xzf /tmp/dtctl.tar.gz -C /usr/local/bin/ dtctl
    chmod +x /usr/local/bin/dtctl
    rm /tmp/dtctl.tar.gz
fi

# ── Clone enablement-framework ──────────────────────────────────────────────
if [[ ! -d "${OPS_DIR}" ]]; then
    info "Cloning enablement-framework..."
    sudo -u "${OPS_USER}" git clone \
        https://github.com/dynatrace-wwse/codespaces-framework.git \
        "${OPS_DIR}/codespaces-framework"
fi

# ── Python virtual environment ──────────────────────────────────────────────
info "Setting up Python venv..."
sudo -u "${OPS_USER}" python3 -m venv "${VENV_DIR}"
sudo -u "${OPS_USER}" "${VENV_DIR}/bin/pip" install -q --upgrade pip
sudo -u "${OPS_USER}" "${VENV_DIR}/bin/pip" install -q \
    -r "${OPS_DIR}/codespaces-framework/ops-server/requirements.txt"

# ── Install systemd services ────────────────────────────────────────────────
info "Installing systemd services..."
cp "${OPS_DIR}/codespaces-framework/ops-server/systemd/"*.service /etc/systemd/system/
cp "${OPS_DIR}/codespaces-framework/ops-server/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable ops-webhook.service
systemctl enable ops-nightly.timer
systemctl enable ops-sync-daemon.service

# ── Create directories ──────────────────────────────────────────────────────
sudo -u "${OPS_USER}" mkdir -p \
    "${OPS_HOME}/.claude" \
    "${OPS_HOME}/.config/dtctl" \
    "${OPS_HOME}/repos" \
    "${OPS_HOME}/logs" \
    "${OPS_HOME}/workdir"

# ── Summary ──────────────────────────────────────────────────────────────────
info "=========================================="
info "  Enablement Ops Server installed!"
info "=========================================="
echo ""
echo "  Next steps (as the ops user):"
echo ""
echo "  1. Authenticate GitHub CLI:"
echo "     sudo -u ${OPS_USER} gh auth login"
echo ""
echo "  2. Set the Anthropic API key:"
echo "     echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ${OPS_HOME}/.bashrc"
echo ""
echo "  3. Configure Dynatrace credentials:"
echo "     cp ops-server/agents/env.template ${OPS_HOME}/.env"
echo "     # Edit ${OPS_HOME}/.env with your DT tokens"
echo ""
echo "  4. Configure Claude Code MCP:"
echo "     cp ops-server/agents/mcp.json ${OPS_HOME}/.claude/mcp.json"
echo "     # Edit with your DT environment URL and API token"
echo ""
echo "  5. Configure dtctl:"
echo "     cp ops-server/agents/dtctl.yaml ${OPS_HOME}/.config/dtctl/config.yaml"
echo "     # Edit with your DT environment and token"
echo ""
echo "  6. Set up GitHub webhook:"
echo "     - Go to github.com/organizations/dynatrace-wwse/settings/hooks"
echo "     - Payload URL: https://<server-ip>:8443/webhook"
echo "     - Content type: application/json"
echo "     - Secret: <generate with: openssl rand -hex 32>"
echo "     - Events: Issues, Pull requests, Check suites, Pushes"
echo "     - Save the secret in ${OPS_HOME}/.env as WEBHOOK_SECRET"
echo ""
echo "  7. Clone all repos:"
echo "     cd ${OPS_DIR}/codespaces-framework && sync clone --all"
echo ""
echo "  8. Start services:"
echo "     sudo systemctl start ops-webhook"
echo "     sudo systemctl start ops-nightly.timer"
echo "     sudo systemctl start ops-sync-daemon"
echo ""

#!/bin/bash
# ======================================================================
#          ------- Util Functions -------                              #
#  A set of util functions for logging, validating and                 #
#  executing commands.                                                 #
# ======================================================================


# VARIABLES DECLARATION
# Dynatrace versions (operator, AG, OA images) are defined in dynakube-defaults.yaml
# and resolved dynamically from ECR at Dynakube generation time (getLatestEcrTag).

ENDPOINT_CODESPACES_TRACKER=https://codespaces-tracker.whydevslovedynatrace.com/api/receive
CODESPACES_TRACKER_TOKEN_STRING="ilovedynatrace"

# Helm Version
HELM_VERSION=3.17.0
export HELM_VERSION=$HELM_VERSION

#https://cert-manager.io/docs/release-notes/
CERTMANAGER_VERSION=1.15.3

# RUNME Version
RUNME_CLI_VERSION=3.13.2

# Ingress configuration — apps are exposed exclusively via nginx ingress.
# App registry file — tracks deployed apps for ingress routing
export APP_REGISTRY="${APP_REGISTRY:-${HOME}/.cache/dt-framework/app-registry}"
# nginx ingress controller version
export INGRESS_NGINX_VERSION="1.12.1"
# Magic DNS domain — resolves subdomains to embedded IP (e.g. app.1.2.3.4.sslip.io → 1.2.3.4)
# Alternatives: nip.io, sslip.io, or your own wildcard domain
export MAGIC_DOMAIN="${MAGIC_DOMAIN:-sslip.io}"

# Setting up the variable since its not set when instantiating the vscode folder.
#CODESPACE_VSCODE_FOLDER="$REPO_PATH"
# Codespace Persisted share folder
CODESPACE_PSHARE_FOLDER="/workspaces/.codespaces/.persistedshare"

# Dynamic Variables between phases
COUNT_FILE="$REPO_PATH/.devcontainer/util/.count"
export COUNT_FILE=$COUNT_FILE

# Env file (needed for MCP and local runs)
ENV_FILE="$REPO_PATH/.devcontainer/.env"
export ENV_FILE=$ENV_FILE

if [ -e "$ENV_FILE" ]; then
  # file exists
  source $ENV_FILE
fi

# Calculating GH Repository
# -C "$REPO_PATH" + stderr suppressed: a shell opened outside the repo dir must
# not spam "fatal: not a git repository" on every terminal open.
if [ -z "$GITHUB_REPOSITORY" ]; then
  GITHUB_REPOSITORY=$(git -C "${REPO_PATH:-.}" remote get-url origin 2>/dev/null)
  export GITHUB_REPOSITORY=$GITHUB_REPOSITORY
fi

# Calculating instantiation type
# Order matters: the combined (Codespace + Orbital) case must be tested BEFORE the
# plain orbital and plain github-codespaces cases so it isn't mislabeled as either.
if [[ $CODESPACES == true ]] && [[ "${ORBITAL_ENVIRONMENT:-}" == "true" ]]; then
  # GitHub Codespace orchestrated by Orbital (ORBITAL_ENVIRONMENT set as a Codespace secret)
  INSTANTIATION_TYPE="orbital_codespaces"
elif [[ "${ORBITAL_ENVIRONMENT:-}" == "true" ]]; then
  INSTANTIATION_TYPE="orbital"
elif [[ $CODESPACES == true ]]; then
  INSTANTIATION_TYPE="github-codespaces"
elif [[ $REMOTE_CONTAINERS == true ]]; then
  INSTANTIATION_TYPE="remote-container"
elif [[ -n $GITHUB_WORKFLOW ]] || [[ -n $GITHUB_STEP_SUMMARY ]]; then
  INSTANTIATION_TYPE="github-workflow"
else
  INSTANTIATION_TYPE="local-docker-container"
fi
export INSTANTIATION_TYPE=$INSTANTIATION_TYPE

if [ -e "$COUNT_FILE" ]; then
  # file exists
  source $COUNT_FILE
elif [ -d "$(dirname "$COUNT_FILE")" ]; then
  # create .env file and add variables
  echo -e "DURATION=0\nERROR_COUNT=0" > $COUNT_FILE
  source $COUNT_FILE
else
  # REPO_PATH invalid (shell opened outside the repo) — defaults, no "//..." error
  DURATION=0
  ERROR_COUNT=0
fi

# Calculating architecture
ARCH=$(arch)
export ARCH=$ARCH

# Cluster engine: k3d (default) or kind. Used by startCluster/stopCluster/deleteCluster.
# NOTE: only AppOnly DT monitoring works on K3d inside Sysbox. CloudNative requires Kind.
export CLUSTER_ENGINE="${CLUSTER_ENGINE:-k3d}"

# Kind configuration (used when CLUSTER_ENGINE=kind)
export KINDIMAGE="kind-control-plane"

# Detect cluster status independent of CLUSTER_ENGINE — probes both engines and
# falls back to kubectl so detection works regardless of how the cluster was started.
_k3d_st=$(docker inspect -f '{{.State.Status}}' k3d-enablement-server-0 2>/dev/null)
_kind_st=$(docker inspect -f '{{.State.Status}}' "${KINDIMAGE}" 2>/dev/null)
if [[ "$_k3d_st" == "running" ]]; then
  CLUSTER_STATUS="running"
  CLUSTER_TYPE="K3d (K3s)"
elif [[ "$_kind_st" == "running" ]]; then
  CLUSTER_STATUS="running"
  CLUSTER_TYPE="Kind"
elif kubectl cluster-info 2>/dev/null 1>/dev/null; then
  CLUSTER_STATUS="running"
  CLUSTER_TYPE="Kubernetes"
else
  CLUSTER_STATUS=""
  CLUSTER_TYPE=""
fi
unset _k3d_st _kind_st
export CLUSTER_STATUS CLUSTER_TYPE
# Legacy variable for backward compatibility
export KIND_STATUS=$CLUSTER_STATUS

CODESPACES_TRACKER_TOKEN=$(echo -n $CODESPACES_TRACKER_TOKEN_STRING | base64)
export CODESPACES_TRACKER_TOKEN=$CODESPACES_TRACKER_TOKEN

# ColorCoding

# ✅ Green shades
GREEN="\e[32m"               # Standard green
GREENL="\e[1;33m"            # Light green (note: this is actually bright yellow in many terminals)

# ✅ Blue and purple shades
BLUE="\e[34m"                # Standard blue
LILA="\e[35m"                # Purple (same as MAGENTA)
MAGENTA="\033[35m"           # Magenta (same as LILA)
CYAN="\033[36m"              # Cyan / light blue

# ✅ Warm colours
YELLOW="\e[38;5;226m"        # Bright yellow
ORANGE="\e[38;5;208m"        # Bright orange
RED="\e[38;5;196m"           # Bright red
LIGHT_RED="\e[38;5;203m"     # Light red
DARK_RED="\e[38;5;88m"       # Dark red

# ✅ Neutral colours
NORMAL="\033[37m"            # Normal grey/white
WHITE="\033[37m"             # White (same as NORMAL)
RESET="\033[0m"              # Reset to default terminal colour

# ✅ Symbols
HEART="\u2665"               # Unicode heart symbol ♥
STAR_FILLED="\u2605"         # filled star
STAR_EMPTY="\u2606"          # empty star
SUN="\u2600"                 # sun
CLOUD="\u2601"               # cloud
UMBRELLA="\u2602"            # umbrella
COFFEE="\u2615"              # hot beverage (coffee)
WARNING="\u26A0"             # warning sign
CHECK="\u2705"               # check mark
CROSS="\u274C"               # cross mark
ARROW="\u27A4"               # arrow bullet
FIRE="\U0001F525"            # fire emoji
TOOLS="\U0001F6E0"           # hammer and wrench
PACKAGE="\U0001F4E6"         # package box



thickline="=========================================================================================="
halfline="=============="
thinline="___________________________________________________________________________________________"
LOGNAME="dynatrace.enablement"

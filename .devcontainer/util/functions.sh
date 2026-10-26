#!/bin/bash
# Functions file of the codespaces framework. Functions are loaded into the shell so the user can easily call them in a dynamic fashion.
# This file contains all core functions used for deploying applications, tools or dynatrace components. 
# Brief descrition of files:
#  - functions.sh - core functions
#  - greeting.sh -zsh/bash greeting (similar to MOTD)
#  - source_framework.sh helper file to load the framework from different places (Codespaces, VSCode Extention, plain Docker container)
#  - variables.sh - variable definitions

# ======================================================================
#          ------- Util Functions -------                              #
#  A set of util functions for logging, validating and                 #
#  executing commands.                                                 #
# ======================================================================
# THis is needed when opening a terminal and the variable is not set
if [ -z "$REPO_PATH" ]; then
  export REPO_PATH="$(pwd)"
  export RepositoryName=$(basename "$REPO_PATH")
fi

# VARIABLES DECLARATION
if [ -n "$FRAMEWORK_CACHE" ]; then
  source "${FRAMEWORK_CACHE}/.devcontainer/util/variables.sh"
else
  source "$REPO_PATH/.devcontainer/util/variables.sh"
fi

# LOAD TEST FUNCTIONS
if [ -n "$FRAMEWORK_CACHE" ]; then
  source "${FRAMEWORK_CACHE}/.devcontainer/test/test_functions.sh"
else
  source "$REPO_PATH/.devcontainer/test/test_functions.sh"
fi


# FUNCTIONS DECLARATIONS
timestamp() {
  date +"[%Y-%m-%d %H:%M:%S]"
}

printInfo() {
  # The second argument defines if the log should be printed out or not
  if [ "$2" = "false" ]; then
    return 0
  fi
  echo -e "${GREEN}[$LOGNAME| ${BLUE}INFO${CYAN} |$(timestamp) ${LILA}|${RESET} $1 ${LILA}|"
}

printInfoSection() {
  if [ "$2" = "false" ]; then
    return 0
  fi
  echo -e "${GREEN}[$LOGNAME| ${BLUE}INFO${CYAN} |$(timestamp) ${LILA}|$thickline"
  echo -e "${GREEN}[$LOGNAME| ${BLUE}INFO${CYAN} |$(timestamp) ${LILA}|$halfline ${RESET}$1${LILA} $halfline"
  echo -e "${GREEN}[$LOGNAME| ${BLUE}INFO${CYAN} |$(timestamp) ${LILA}|$thinline"
}

printWarn() {
  if [ "$2" = "false" ]; then
    return 0
  fi
  echo -e "${GREEN}[$LOGNAME| ${YELLOW}WARN${CYAN} |$(timestamp) ${LILA}| ${RESET}$1${LILA}  |"
}

printError() {
  if [ "$2" = "false" ]; then
    return 0
  fi
  echo -e "${GREEN}[$LOGNAME| ${RED}ERROR${CYAN} |$(timestamp) ${LILA}| ${RESET}$1${LILA}  |"
}

detectRunEnvironment(){
  # Returns "orbital_codespaces", "orbital", "codespaces", or "local" based on signals.
  # orbital_codespaces: a GitHub Codespace orchestrated by Orbital (CODESPACES=true AND
  #                     ORBITAL_ENVIRONMENT=true) — surfaced so monitoring can see that a
  #                     training ran in Codespaces-mode under Orbital. Tested FIRST.
  # orbital:   ORBITAL_ENVIRONMENT=true is set by the Orbital worker manager
  #            or K3D_CLUSTER_NAME starts with "master-" (worker port-override pattern)
  # codespaces: CODESPACE_NAME is set by GitHub Codespaces
  # local:     fallback
  if [[ "${CODESPACES:-}" == "true" ]] && [[ "${ORBITAL_ENVIRONMENT:-}" == "true" ]]; then
    echo "orbital_codespaces"
  elif [[ "${ORBITAL_ENVIRONMENT:-}" == "true" ]] || [[ "${K3D_CLUSTER_NAME:-}" == master-* ]]; then
    echo "orbital"
  elif [[ -n "${CODESPACE_NAME:-}" ]]; then
    echo "codespaces"
  else
    echo "local"
  fi
}

postCodespaceTracker(){

  printInfo "Sending bizevent for $RepositoryName with $ERROR_COUNT issues built in $DURATION seconds"

  # Sanitize error detail for JSON (escape quotes, newlines)
  local error_detail=""
  if [[ -n "$CODESPACE_ERRORS" ]]; then
    error_detail=$(printf '%s' "$CODESPACE_ERRORS" | tr '\n' ' ' | sed 's/"/\\"/g' | head -c 500)
  fi

  # Unique app ID for RUM monitoring: dynatrace-wwse-{repo-name}
  local app_id="dynatrace-wwse-${RepositoryName}"
  local run_env
  run_env=$(detectRunEnvironment)

  curl -s -X POST "$ENDPOINT_CODESPACES_TRACKER" \
  -H "Content-Type: application/json" \
  -H "Authorization: $CODESPACES_TRACKER_TOKEN" \
  -d "{
  \"repository\": \"$GITHUB_REPOSITORY\",
  \"repository.name\": \"$RepositoryName\",
  \"codespace.errors\": \"$ERROR_COUNT\",
  \"codespace.errors_detail\": \"$error_detail\",
  \"codespace.creation\": \"$DURATION\",
  \"codespace.type\": \"$INSTANTIATION_TYPE\",
  \"codespace.arch\": \"$ARCH\",
  \"codespace.name\": \"$CODESPACE_NAME\",
  \"codespace.app_id\": \"$app_id\",
  \"run.environment\": \"$run_env\",
  \"environment\": \"$DT_ENVIRONMENT\",
  \"tenant\": \"$DT_TENANT\",
  \"framework.version\": \"$FRAMEWORK_VERSION\"
  }"
}

printGreeting(){
  if [ -n "$FRAMEWORK_CACHE" ]; then
    bash "${FRAMEWORK_CACHE}/.devcontainer/util/greeting.sh"
  else
    bash "$REPO_PATH/.devcontainer/util/greeting.sh"
  fi
}

waitForPod() {
  # Function to filter by Namespace and POD string, default is ALL namespaces
  # If 2 parameters then the first is Namespace the second is Pod-String
  # If 1 parameters then Namespace == all-namespaces the first is Pod-String
  if [[ $# -eq 2 ]]; then
    namespace_filter="-n $1"
    pod_filter="$2"
  elif [[ $# -eq 1 ]]; then
    namespace_filter="--all-namespaces"
    pod_filter="$1"
  fi
  RETRY=0
  RETRY_MAX=60
  # Get all pods, count and invert the search for not running nor completed. Status is for deleting the last line of the output
  CMD="kubectl get pods $namespace_filter 2>&1 | grep -c -E '$pod_filter'"
  printInfo "Verifying that pods in \"$namespace_filter\" with name \"$pod_filter\" is scheduled in a workernode "
  while [[ $RETRY -lt $RETRY_MAX ]]; do
    pods_running=$(eval "$CMD")
    if [[ "$pods_running" != '0' ]]; then
      printInfo "\"$pods_running\" pods are running on \"$namespace_filter\" with name \"$pod_filter\" exiting loop."
      break
    fi
    RETRY=$(($RETRY + 1))
    printWarn "Retry: ${RETRY}/${RETRY_MAX} - No pods are running on  \"$namespace_filter\" with name \"$pod_filter\". Wait 10s for $pod_filter PoDs to be scheduled..."
    sleep 10
  done
  
  if [[ $RETRY == $RETRY_MAX ]]; then
    printError "No pods are running on  \"$namespace_filter\" with name \"$pod_filter\". Check their events. Exiting installation..."
    exit 1
  fi
}

# shellcheck disable=SC2120
# Print the state of a deployment that is not coming up: pods, their events and
# container states, the nodes' pressure and allocation, and the host's disk and
# memory. Read-only; never fails the caller.
printPodsDiagnostics() {
  local ns_filter="${1:---all-namespaces}"
  printInfoSection "Pod diagnostics (${ns_filter})"
  printInfo "Host: disk and memory"
  df -h / 2>/dev/null | tail -1 || true
  free -h 2>/dev/null || true
  printInfo "Nodes: conditions (MemoryPressure / DiskPressure / PIDPressure) and allocated resources"
  kubectl describe nodes 2>/dev/null | grep -E "^Name:|Pressure|Allocated resources|^  (cpu|memory|ephemeral-storage) " || true
  kubectl top nodes 2>/dev/null || true
  printInfo "Pods not Running/Completed"
  kubectl get pods $ns_filter -o wide 2>/dev/null | grep -v -E '(Running|Completed)' || true
  printInfo "Why: reason, last termination and recent events per pod"
  kubectl get pods $ns_filter --no-headers \
    -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason,WAITING:.status.containerStatuses[*].state.waiting.reason,LAST:.status.containerStatuses[*].lastState.terminated.reason,EXIT:.status.containerStatuses[*].lastState.terminated.exitCode' 2>/dev/null \
    | grep -v -E ' Running +<none> +<none> +<none>| Succeeded ' || true
  printInfo "Warning events (newest last)"
  kubectl get events $ns_filter --field-selector type=Warning --sort-by=.lastTimestamp 2>/dev/null | tail -40 || true
}

waitForAllPods() {
  # Function to filter by Namespace, default is ALL
  if [[ $# -eq 1 ]]; then
    namespace_filter="-n $1"
  else
    namespace_filter="--all-namespaces"
  fi
  RETRY=0
  RETRY_MAX=60
  # Get all pods, count and invert the search for not running nor completed. Status is for deleting the last line of the output
  CMD="kubectl get pods $namespace_filter 2>&1 | grep -c -v -E '(Running|Completed|Terminating|STATUS)'"
  printInfo "Checking and wait for all pods in \"$namespace_filter\" to run."
  while [[ $RETRY -lt $RETRY_MAX ]]; do
    pods_not_ok=$(eval "$CMD")
    if [[ "$pods_not_ok" == '0' ]]; then
      printInfo "All pods are running."
      break
    fi
    RETRY=$(($RETRY + 1))
    printWarn "Retry: ${RETRY}/${RETRY_MAX} - Wait 10s for $pods_not_ok PoDs to finish or be in state Running ..."
    # Every 2 minutes, show what is holding the pods, so a slow start is visible before it fails.
    if (( RETRY % 12 == 0 )); then
      printPodsDiagnostics "$namespace_filter"
    fi
    sleep 10
  done

  if [[ $RETRY == $RETRY_MAX ]]; then
    printError "Following pods are not still not running. Please check their events. Exiting installation..."
    kubectl get pods --field-selector=status.phase!=Running -A
    printPodsDiagnostics "--all-namespaces"
    exit 1
  fi
}

waitForAllReadyPods() {
  # Function to filter by Namespace, default is ALL
  if [[ $# -eq 1 ]]; then
    namespace_filter="-n $1"
  else
    namespace_filter="--all-namespaces"
  fi
  RETRY=0
  RETRY_MAX=60
  # Get all pods, count and invert the search for not running nor completed. Status is for deleting the last line of the output
  CMD="kubectl get pods $namespace_filter 2>&1 | grep -c -v -E '(1\/1|2\/2|3\/3|4\/4|5\/5|6\/6|READY)'"
  printInfo "Checking and wait for all pods in \"$namespace_filter\" to be running and ready (max of 6 containers per pod)"
  while [[ $RETRY -lt $RETRY_MAX ]]; do
    pods_not_ok=$(eval "$CMD")
    if [[ "$pods_not_ok" == '0' ]]; then
      printInfo "All pods are running."
      break
    fi
    RETRY=$(($RETRY + 1))
    printWarn "Retry: ${RETRY}/${RETRY_MAX} - Wait 10s for $pods_not_ok PoDs to finish or be in state Ready & Running ..."
    sleep 10
  done

  if [[ $RETRY == $RETRY_MAX ]]; then
    printError "Following pods are not still not running. Please check their events. Exiting installation..."
    printNotReadyPods "$namespace_filter"
    printPodsDiagnostics "$namespace_filter"
    exit 1
  fi
}

printNotReadyPods() {
  # The pods waitForAllReadyPods timed out on, by the SAME test it waits with
  # (READY column not n/n). A Running pod whose readiness probe never passes
  # counts here; a `status.phase!=Running` selector hides it and printed
  # "No resources found" for exactly that failure (BUG-158).
  local ns_filter="${1:---all-namespaces}"
  local rows
  rows=$(kubectl get pods $ns_filter -o wide 2>/dev/null | grep -v -E '(1/1|2/2|3/3|4/4|5/5|6/6)' || true)
  printInfo "Pods not Ready (Running but not Ready included)"
  echo "$rows"
  printInfo "Readiness and events per not-Ready pod"
  echo "$rows" | awk 'NR>1 {print}' | while read -r first second _; do
    local ns name
    if [[ "$ns_filter" == "--all-namespaces" ]]; then ns="$first"; name="$second"; else ns="${ns_filter#-n }"; name="$first"; fi
    [[ -n "$name" ]] || continue
    echo "--- $ns/$name"
    kubectl describe pod -n "$ns" "$name" 2>/dev/null \
      | grep -E "^ +(Ready|State|Reason|Restart Count|Readiness|Liveness):|^Events:|^  (Warning|Normal) " | tail -30 || true
  done
}

waitAppCanHandleRequests(){
  # Function to verify app can handle requests on a given port (localhost:PORT).
  # First parameter: PORT (default: 80, the ingress port)
  # Second parameter: RETRY_MAX (default: 5)
  # Usage examples:
  #   waitAppCanHandleRequests          - uses default port 80 and 5 retries
  #   waitAppCanHandleRequests 8080     - uses port 8080 and 5 retries
  #   waitAppCanHandleRequests 8080 10  - uses port 8080 and 10 retries
  if [[ $# -eq 0 ]]; then
    PORT="80"
    RETRY_MAX=5
  elif [[ $# -eq 1 ]]; then
    PORT="$1"
    RETRY_MAX=5
  elif [[ $# -eq 2 ]]; then
    PORT="$1"
    RETRY_MAX="$2"
  else
    PORT="80"
    RETRY_MAX=5
  fi
  
  RC="500"

  URL=http://localhost:$PORT
  RETRY=0
  # Get all pods, count and invert the search for not running nor completed. Status is for deleting the last line of the output
  CMD="curl --silent $URL > /dev/null"
  printInfo "Verifying that the app can handle HTTP requests on $URL (max retries: $RETRY_MAX)"
  while [[ $RETRY -lt $RETRY_MAX ]]; do
    RESPONSE=$(eval "$CMD")
    RC=$?
    #Common RC from cURL
    #0: Success
    #6: Could not resolve host
    #7: Failed to connect to host
    #28: Operation timeout
    #35: SSL connect error
    #56:Failure with receiving network data
    if [[ "$RC" -eq 0 ]]; then
      printInfo "App is running on $URL"
      break
    fi
    RETRY=$(($RETRY + 1))
    printWarn "Retry: ${RETRY}/${RETRY_MAX} - App can't handle HTTP requests on $URL. [cURL RC:$RC] Waiting 10s..."
    sleep 10
  done

  if [[ $RETRY == $RETRY_MAX ]]; then
    printError "App is still not able to handle requests. Please check the events"
  fi
}

installHelm() {
  # https://helm.sh/docs/intro/install/#from-script
  # DESIRED_VERSION="$HELM_VERSION" ##TODO: Helm version control from variables.sh
  printInfoSection "Installing Helm"
  # printInfo "Helm Desired Version: ${HELM_VERSION}"
  cd /tmp
  sudo curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/master/scripts/get-helm-3
  sudo chmod 700 get_helm.sh
  sudo /tmp/get_helm.sh

  printInfoSection "Helm version"
  helm version

  # https://helm.sh/docs/intro/quickstart/#initialize-a-helm-chart-repository
  printInfoSection "Helm add Bitnami repo"
  printInfoSection "helm repo add bitnami https://charts.bitnami.com/bitnami"
  helm repo add bitnami https://charts.bitnami.com/bitnami

  printInfoSection "Helm repo update"
  helm repo update

  printInfoSection "Helm search repo bitnami"
  helm search repo bitnami
}

installHelmDashboard() {

  printInfoSection "Installing Helm Dashboard"
  helm plugin install https://github.com/komodorio/helm-dashboard.git

  printInfoSection "Running Helm Dashboard"
  helm dashboard --bind=0.0.0.0 --port 8002 --no-browser --no-analytics >/dev/null 2>&1 &

}

installKubernetesDashboard() {
  # https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/
  printInfoSection " Installing Kubernetes dashboard"

  helm repo add kubernetes-dashboard https://kubernetes.github.io/dashboard/
  helm upgrade --install kubernetes-dashboard kubernetes-dashboard/kubernetes-dashboard --create-namespace --namespace kubernetes-dashboard

  # In the functions you can specify the amount of retries and the NS
  # shellcheck disable=SC2119
  waitForAllPods
  printInfoSection "kubectl -n kubernetes-dashboard port-forward svc/kubernetes-dashboard-kong-proxy 8001:443 --address=\"0.0.0.0\", (${attempts}/${max_attempts}) sleep 10s"
  kubectl -n kubernetes-dashboard port-forward svc/kubernetes-dashboard-kong-proxy 8001:443 --address="0.0.0.0" >/dev/null 2>&1 &
  # https://github.com/komodorio/helm-dashboard

  # Do we need this?
  printInfoSection "Create ServiceAccount and ClusterRoleBinding"
  kubectl apply -f /app/.devcontainer/etc/k3s/dashboard-adminuser.yaml
  kubectl apply -f /app/.devcontainer/etc/k3s/dashboard-rolebind.yaml

  printInfoSection "Get admin-user token"
  kubectl -n kube-system create token admin-user --duration=8760h
}

installK9s() {
  printInfoSection "Installing k9s CLI"
  curl -sS https://webinstall.dev/k9s | bash
}


ensureDockerGroupAccess() {
  # Recover from losing the entrypoint race for the docker group.
  #
  # /entrypoint.sh aligns the container's docker GID with the host socket:
  #   groupmod -g $(stat -c %g /var/run/docker.sock) docker
  #   usermod  -aG docker $USER
  #   exec sg docker "$*"
  # Measured in a Codespace, that takes 3-5s (15:05:41 -> 15:05:46). Its final
  # `exec sg` only re-groups the container's own CMD, which is how `make start`
  # and the VS Code dev container run post-create — those are never affected.
  #
  # Codespaces instead runs postCreateCommand as a SEPARATE `docker exec`, and the
  # devcontainer CLI fires it ~4s after the container starts (measured 15:05:45.197)
  # — inside the entrypoint's window. dockerd snapshots the exec's supplementary
  # groups at creation, so when the CLI wins the race that process tree never holds
  # the docker GID: every docker call in post-create fails with
  #   permission denied while trying to connect to the Docker daemon socket
  # k3d cannot create the cluster and post-create exits 1. Observed on 3 of 5
  # Codespaces. It reads as a phantom because `docker ps` works in every shell
  # opened afterwards — those execs are created after the entrypoint has finished.
  #
  # Orbital/Sysbox uses `docker run -d … sleep infinity` + `docker exec` too, but
  # only execs after cloning the repo, long past the entrypoint's window.
  #
  # Strictly a no-op whenever docker already answers, so healthy runs in all four
  # instantiation types return on the first line.
  local script="${1:-$0}"
  local verdict
  verdict="$(_dockerAccessVerdict "$script")"
  case "$verdict" in ok|no-socket) return 0 ;; esac

  # Only printed on the unhealthy path, and it is the evidence that identifies the
  # race: the group DB lists the membership while this process's credentials do not.
  printWarn "Docker socket is not reachable — waiting for the entrypoint's docker-group fix"
  printInfo "  socket gid      : $(stat -c '%g' "${DOCKER_SOCKET:-/var/run/docker.sock}" 2>/dev/null)"
  printInfo "  group database  : $(getent group docker 2>/dev/null)"
  printInfo "  this process    : $(id -G 2>/dev/null)"
  printInfo "  verdict         : $verdict"

  # The entrypoint may still be between groupmod and usermod; give it a bounded
  # chance to finish before deciding.
  local waited=0
  while [ "$verdict" = "no-membership" ] && [ "$waited" -lt 30 ]; do
    sleep 1
    waited=$((waited + 1))
    verdict="$(_dockerAccessVerdict "$script")"
  done

  case "$verdict" in
    ok)
      printInfo "Docker became reachable after ${waited}s"
      return 0
      ;;
    regroup)
      printInfo "Group membership is in place but this process predates it — re-executing $script under the docker group"
      export DT_DOCKER_REGROUP=1
      exec sg docker "$script"
      ;;
    no-membership)
      printWarn "$(id -un) is still not a member of the docker group after ${waited}s — continuing; docker calls will fail"
      ;;
    already-regrouped)
      printWarn "Already re-executed under the docker group and docker is still unreachable"
      ;;
    not-a-script)
      # Sourced into an interactive shell: re-execing would kill the user's terminal.
      printWarn "Group membership exists but this shell predates it. Open a new shell, or run 'newgrp docker'."
      ;;
    no-sg)
      printWarn "'sg' is unavailable — cannot re-acquire the docker group in this process"
      ;;
  esac
  return 1
}

_dockerAccessVerdict() {
  # Pure decision helper for ensureDockerGroupAccess: echoes exactly one token and
  # changes nothing, so every branch — including the re-exec — is unit-testable
  # without actually replacing the process.
  docker info >/dev/null 2>&1 && { echo "ok"; return 0; }
  [ -S "${DOCKER_SOCKET:-/var/run/docker.sock}" ] || { echo "no-socket"; return 0; }
  # `id -nG <user>` reads the group DB rather than this process's frozen
  # credentials — that discrepancy is exactly what we are detecting.
  id -nG "$(id -un)" 2>/dev/null | tr ' ' '\n' | grep -qx docker || { echo "no-membership"; return 0; }
  [ -z "${DT_DOCKER_REGROUP:-}" ] || { echo "already-regrouped"; return 0; }
  [ -f "${1:-$0}" ] || { echo "not-a-script"; return 0; }
  command -v sg >/dev/null 2>&1 || { echo "no-sg"; return 0; }
  echo "regroup"
}

setUpTerminal(){
  # First thing post-create.sh does, so the re-exec below costs nothing but a
  # repeated source of the framework.
  ensureDockerGroupAccess

  printInfoSection "Sourcing the DT-Enablement framework functions to the terminal, adding aliases, a Dynatrace greeting and installing power10k into .zshrc for user $USER "

  printInfoSection "Installing power10k into .zshrc for user $USER "
  
  #TODO: Verify if ohmyZsh is there so we can add this functionality to any server by loading the functions
  # source .devcontainer/util/source_framework.sh && setUpTerminal
  # or at least add ohmyzsh, power10k and no greeting
  git clone --depth=1 https://github.com/romkatv/powerlevel10k.git "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k"
  
  P10K_DIR="${FRAMEWORK_CACHE:+${FRAMEWORK_CACHE}/.devcontainer/p10k}"
  P10K_DIR="${P10K_DIR:-$REPO_PATH/.devcontainer/p10k}"

  if [[ $CODESPACES == true ]]; then
    printInfoSection "Power10k configuration is limited on web. If you open the devcontainer on an IDE type 'p10k configure' to reconfigure it."
    cp "$P10K_DIR/.p10k.zsh.web" "$HOME/.p10k.zsh"
  else
    printInfoSection "Power10k configuration with many icons added."
    cp "$P10K_DIR/.p10k.zsh" "$HOME/.p10k.zsh"
  fi

  cp "$P10K_DIR/.zshrc" "$HOME/.zshrc"
  
  bindFunctionsInShell

  setupAliases

  # MCP is opt-in — not auto-configured. Users can type 'enableMCP' to set it up.
  printInfo "Type 'enableMCP' to connect VS Code to a Dynatrace MCP Server"

  # Only an Orbital-orchestrated Codespace needs an SSH server: the Enablement App
  # relays its terminal in over `gh codespace ssh`, and the stock image ships no
  # sshd. A plain Codespace opened from GitHub must NOT pay for that install, and
  # local, dev-container and Sysbox/orbital instantiations never needed it.
  #
  # This used to also fire for `github-codespaces`, as belt-and-braces against
  # GitHub failing to surface the ORBITAL_ENVIRONMENT secret in the post-create
  # environment. That belt is now in variables.sh, which reads the canonical
  # /workspaces/.codespaces/shared/.env-secrets *before* computing the type — so
  # the braces here only cost every manual Codespace an apt-get it cannot use.
  if [ "$INSTANTIATION_TYPE" = "orbital_codespaces" ]; then
    installCodespaceSSH
  fi
}

installCodespaceSSH() {
  # Idempotently install and prepare openssh-server for Orbital-orchestrated Codespaces.
  # Detect presence via the binary path (NOT `command -v sshd`) so a half-installed
  # PATH entry never makes us skip host-key generation.
  printInfoSection "Installing SSH server for Orbital Codespaces relay"
  if [ -x /usr/sbin/sshd ]; then
    printInfo "openssh-server already present at /usr/sbin/sshd — skipping install"
  else
    printInfo "openssh-server not found — installing via apt-get"
    # --no-install-recommends is load-bearing, not tidiness. openssh-server's hard
    # Depends are all small and mostly already in the image; its *Recommends* line
    # ("default-logind | logind | libpam-systemd, ncurses-term, xauth, ssh-import-id")
    # drags in systemd, systemd-sysv, systemd-resolved, systemd-timesyncd and dbus.
    # That took 7 minutes, rewrote /etc/nsswitch.conf, tried to take over
    # /etc/resolv.conf and replaced /sbin/init inside the devcontainer. Nothing here
    # needs a logind session: the Codespaces agent starts sshd itself, we only have
    # to put the binary and its host keys on disk.
    sudo apt-get update -qq \
      && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
           --no-install-recommends openssh-server \
      || printWarn "Failed to install openssh-server"
  fi

  if [ -x /usr/sbin/sshd ]; then
    printInfo "Generating SSH host keys and preparing /run/sshd"
    sudo ssh-keygen -A || printWarn "ssh-keygen -A reported an issue"
    sudo mkdir -p /run/sshd || printWarn "Could not create /run/sshd"
    printInfo "SSH server ready for gh codespace ssh relay"
  else
    printWarn "sshd not available after install — Orbital terminal relay will not work"
  fi
}


setUpHostTerminal() {
  printInfoSection "Setting up Zsh + Oh My Zsh + Powerlevel10k on host Ubuntu server for user $USER"
  printInfo "Usage: source .devcontainer/util/source_framework.sh && setUpHostTerminal"

  if ! grep -qi ubuntu /etc/os-release 2>/dev/null; then
    printWarn "This function is designed for Ubuntu. Proceeding anyway but results may vary."
  fi

  local P10K_DIR
  P10K_DIR="${FRAMEWORK_CACHE:+${FRAMEWORK_CACHE}/.devcontainer/p10k}"
  P10K_DIR="${P10K_DIR:-$REPO_PATH/.devcontainer/p10k}"

  if [ ! -d "$P10K_DIR" ]; then
    printError "Powerlevel10k config directory not found at $P10K_DIR"
    return 1
  fi

  # Install zsh
  if ! command -v zsh >/dev/null 2>&1; then
    printInfo "Installing zsh..."
    sudo apt-get update -y && sudo apt-get install -y zsh
  else
    printInfo "✅ zsh already installed: $(zsh --version)"
  fi

  # Install Oh My Zsh (non-interactive, do not switch shell yet, do not run zsh)
  if [ ! -d "$HOME/.oh-my-zsh" ]; then
    printInfo "Installing Oh My Zsh..."
    RUNZSH=no CHSH=no sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)"
  else
    printInfo "✅ Oh My Zsh already installed"
  fi

  # Clone Powerlevel10k theme
  local P10K_THEME_DIR="${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k"
  if [ ! -d "$P10K_THEME_DIR" ]; then
    printInfo "Cloning Powerlevel10k theme..."
    git clone --depth=1 https://github.com/romkatv/powerlevel10k.git "$P10K_THEME_DIR"
  else
    printInfo "✅ Powerlevel10k theme already present"
  fi

  # Deploy framework p10k config and zshrc template
  printInfo "Copying Powerlevel10k configuration from $P10K_DIR"
  cp "$P10K_DIR/.p10k.zsh" "$HOME/.p10k.zsh"
  cp "$P10K_DIR/.zshrc" "$HOME/.zshrc"

  # Add aliases only — no framework functions bound to the shell
  setupAliases

  # Set zsh as default shell
  local ZSH_BIN
  ZSH_BIN="$(command -v zsh)"
  if [ "$(getent passwd "$USER" | cut -d: -f7)" != "$ZSH_BIN" ]; then
    printInfo "Changing default shell to zsh for $USER..."
    sudo chsh -s "$ZSH_BIN" "$USER"
  else
    printInfo "✅ Default shell is already zsh"
  fi

  printInfoSection "✅ Host terminal setup complete for $USER"
  printInfo "Run 'exec zsh' to start your new shell, or log out and back in."
  printInfo "Type 'p10k configure' inside zsh to customize your prompt."
}


enableMCP(){
  # Generates .vscode/mcp.json so VS Code connects to the Dynatrace MCP Server.
  # Uses DT_ENVIRONMENT from env vars or prompts via selectEnvironment.
  printInfoSection "Enabling Dynatrace 🧠 MCP Server for VS Code"

  # Ensure .env file exists
  if [ ! -f "$ENV_FILE" ]; then
    touch "$ENV_FILE"
  fi

  # If DT_ENVIRONMENT is not set, prompt the user
  if [ -z "$DT_ENVIRONMENT" ]; then
    # Check .env file for DT_ENVIRONMENT
    if [ -f "$ENV_FILE" ]; then
      local env_val
      env_val=$(grep -E "^DT_ENVIRONMENT=" "$ENV_FILE" | head -1 | cut -d'=' -f2-)
      if [ -n "$env_val" ]; then
        export DT_ENVIRONMENT="$env_val"
      fi
    fi
  fi

  if [ -z "$DT_ENVIRONMENT" ]; then
    printWarn "DT_ENVIRONMENT is not set. Launching environment selector..."
    selectEnvironment
    if [ -z "$DT_ENVIRONMENT" ]; then
      printError "No environment selected. MCP not enabled."
      return 1
    fi
  fi

  # Ensure DT_ENVIRONMENT is in .env file (needed by MCP server)
  if ! grep -qE "^DT_ENVIRONMENT=" "$ENV_FILE" 2>/dev/null; then
    echo "DT_ENVIRONMENT=$DT_ENVIRONMENT" >> "$ENV_FILE"
  fi

  # Generate .vscode/mcp.json
  local vscode_dir="$REPO_PATH/.vscode"
  mkdir -p "$vscode_dir"

  cat > "$vscode_dir/mcp.json" <<MCPEOF
{
  "servers": {
    "dynatrace-mcp-server": {
      "type": "stdio",
      "command": "npx",
      "cwd": "\${workspaceFolder}",
      "args": ["-y", "@dynatrace-oss/dynatrace-mcp-server@latest"],
      "envFile": "\${workspaceFolder}/.devcontainer/.env"
    }
  }
}
MCPEOF

  printInfo "MCP Server enabled for $DT_ENVIRONMENT"
  printInfo "Settings location: $vscode_dir/mcp.json"
  printInfo "Environment variables location: $ENV_FILE"
  printInfo "VS Code should detect the MCP server automatically. If not, go to Extensions > MCP Servers."
  printInfo "To switch environments, type 'selectEnvironment'"
  printInfo "To disable MCP, type 'disableMCP'"
}

disableMCP(){
  # Removes .vscode/mcp.json to disable the Dynatrace MCP Server.
  local mcp_file="$REPO_PATH/.vscode/mcp.json"
  if [ -f "$mcp_file" ]; then
    rm "$mcp_file"
    printInfo "MCP Server disabled — removed $mcp_file"
  else
    printInfo "MCP Server is not enabled (no mcp.json found)"
  fi
}

setupMCPServer(){
  # DEPRECATED: Use enableMCP instead. Kept for backward compatibility.
  printWarn "setupMCPServer is deprecated. Use 'enableMCP' to enable or 'disableMCP' to disable the MCP Server."
  enableMCP
}

# ======================================================================
#          ------- dtctl & Monaco -------                              #
#  installDevTools (run by finalizePostCreation in every repo; off     #
#  with FRAMEWORK_DEVTOOLS=off), installDtctl / enableDtctl /          #
#  disableDtctl, installMonaco, deployMonaco / undeployMonaco.         #
#                                                                      #
#  Every binary is checksum-verified before it is installed; a         #
#  mismatch refuses the install. No function prints a token value:     #
#  dtctl stores a ${VAR} reference, Monaco reads the variable by name. #
#                                                                      #
#  Written for bash AND zsh (the learner's shell): no word splitting   #
#  of unquoted variables, no globs, no `status`/`path` locals.         #
# ======================================================================

# Prints the sha256 of a file.
_sha256Of() {
  sha256sum "$1" 2>/dev/null | cut -d' ' -f1
}

verifySha256() {
  # verifySha256 <file> <expected-sha256> — returns 0 only when the file's
  # sha256 equals the expected one. A missing file, a malformed expected value
  # or a mismatch all refuse (return 1) — there is no "could not check" pass.
  local file="$1" expected actual
  expected=$(echo "$2" | tr '[:upper:]' '[:lower:]')
  if [ ! -f "$file" ]; then
    printError "verifySha256: $file does not exist"
    return 1
  fi
  if ! echo "$expected" | grep -qE '^[0-9a-f]{64}$'; then
    printError "INTEGRITY: verifySha256: no valid expected checksum for $(basename "$file") — refusing"
    return 1
  fi
  actual=$(_sha256Of "$file")
  if [ "$actual" != "$expected" ]; then
    printError "INTEGRITY: Checksum MISMATCH for $(basename "$file") — expected $expected, got ${actual:-<none>}. Refusing to install."
    return 1
  fi
  printInfo "Checksum OK for $(basename "$file") ($actual)"
  return 0
}

_isVarName() {
  # A shell variable name, matched as a whole string (no grep: a line-anchored
  # regex lets "X<newline>junk" through).
  case "$1" in
    ''|[!A-Za-z_]*|*[!A-Za-z0-9_]*) return 1 ;;
  esac
}

_isVersion() {
  case "${1#v}" in
    ''|*[!0-9.]*|.*|*.|*..*) return 1 ;;
  esac
  echo "${1#v}" | grep -qxE '[0-9]+\.[0-9]+\.[0-9]+'
}

_varIsSet() {
  # _varIsSet <NAME> — true when NAME is a valid variable name holding a
  # non-empty value. Never prints the value.
  _isVarName "$1" || return 1
  eval "[ -n \"\${$1:-}\" ]"
}

_releaseArch() {
  # Maps the machine architecture to the release asset suffix (amd64 | arm64).
  # Always the kernel's answer: $ARCH is a plain variable a repo or .env can
  # set, and a wrong one would install a binary that cannot run.
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    # Only a 64-bit kernel: armv8l is a 32-bit userland on an ARMv8 core and
    # cannot exec the arm64 binary.
    aarch64|arm64) echo "arm64" ;;
    *) return 1 ;;
  esac
}

_githubLatestTag() {
  # _githubLatestTag <owner/repo> — prints the latest release tag (e.g. v0.42.0).
  # GitHub API first; the releases/latest redirect as a fallback, because the
  # API is rate-limited for anonymous callers behind a shared egress IP.
  local repo="$1" tag
  tag=$(curl -fsSL --connect-timeout 10 --max-time 20 "https://api.github.com/repos/$repo/releases/latest" 2>/dev/null \
    | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
  if [ -z "$tag" ]; then
    tag=$(curl -fsSLI -o /dev/null -w '%{url_effective}' --connect-timeout 10 --max-time 20 \
      "https://github.com/$repo/releases/latest" 2>/dev/null | sed -E 's#.*/tag/##')
  fi
  _isVersion "$tag" || return 1
  echo "$tag"
}

_installBinary() {
  # _installBinary <src> <dir> <name> — installs 0755, with sudo only when the
  # target directory is not writable. sudo -n: without passwordless sudo it
  # fails at once instead of waiting on a password prompt nobody can answer.
  local src="$1" dir="$2" name="$3"
  mkdir -p "$dir" 2>/dev/null || sudo -n mkdir -p "$dir" || return 1
  if [ -w "$dir" ]; then
    install -m 0755 "$src" "$dir/$name"
  else
    sudo -n install -m 0755 "$src" "$dir/$name"
  fi
}

_removeBinary() {
  local file="$1"
  [ -e "$file" ] || return 0
  if [ -w "$(dirname "$file")" ]; then
    rm -f "$file"
  else
    sudo -n rm -f "$file"
  fi
}

_fetch() {
  # _fetch <url> <out> <max-seconds> — one download. --connect-timeout so a
  # dead route fails in seconds; --max-time capped by DEVTOOLS_DEADLINE (epoch
  # seconds, set by installDevTools) so both tools share one time budget.
  local url="$1" out="$2" mt="$3" left
  if [ -n "${DEVTOOLS_DEADLINE:-}" ]; then
    left=$(( DEVTOOLS_DEADLINE - $(date +%s) ))
    if [ "$left" -le 0 ]; then
      printError "download time budget exhausted — $(basename "$url") not fetched"
      return 1
    fi
    [ "$left" -lt "$mt" ] && mt="$left"
  fi
  curl -fsSL --connect-timeout 10 --max-time "$mt" -o "$out" "$url"
}

_pinnedSha() {
  # _pinnedSha <DTCTL|MONACO> <version> <amd64|arm64> [BIN_] — prints the
  # reviewed sha256 committed in variables.sh, only when <version> is that
  # tool's pinned version. Prints nothing for any other version.
  local pin arch_up
  case "$1" in DTCTL|MONACO) ;; *) return 1 ;; esac
  case "$3" in amd64) arch_up=AMD64 ;; arm64) arch_up=ARM64 ;; *) return 1 ;; esac
  case "${4:-}" in ''|BIN_) ;; *) return 1 ;; esac
  eval "pin=\${${1}_PIN_VERSION:-}"
  [ -n "$pin" ] && [ "${2#v}" = "${pin#v}" ] || return 0
  eval "echo \"\${${1}_PIN_${4:-}SHA256_${arch_up}:-}\""
}

_dtctlConfigWrite() {
  # _dtctlConfigWrite <dtctl> <args...> — runs one `dtctl config` write in an
  # ALLOWLIST environment. dtctl (0.42-0.43) rewrites the whole config with
  # every ${VAR} expanded on any config write, so ANY token variable in its
  # environment would land on disk — ours, or one another context references.
  # With only the path variables present, every expansion yields "".
  env -i PATH="$PATH" HOME="$HOME" XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-}" \
    DTCTL_CONFIG="${DTCTL_CONFIG:-}" DTCTL_TOKEN_STORAGE=file "$@"
}

_dtctlConfigFile() {
  echo "${DTCTL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/dtctl/config}"
}

_dtctlDropLeakedConfig() {
  # _dtctlDropLeakedConfig <config> <VAR> — when the config holds the VALUE of
  # $VAR, deletes the config and returns 0. Never prints the value.
  local cfg="$1" var="$2"
  [ -f "$cfg" ] && _varIsSet "$var" || return 1
  printenv "$var" | grep -qFf - "$cfg" || return 1
  rm -f "$cfg"
  return 0
}

_devToolReports() {
  # _devToolReports <binary> <version> <pinned-binary-sha256> — 0 only when
  # <binary>'s sha256 equals the reviewed pin AND its `version` output names
  # <version>. Lets an image that bakes the binary in skip the download;
  # anything else (no pin, other bytes, other version) gets a verified install.
  local bin="$1" want="${2#v}" pin="$3"
  [ -x "$bin" ] && [ -n "$want" ] && [ -n "$pin" ] || return 1
  [ "$(_sha256Of "$bin")" = "$(echo "$pin" | tr '[:upper:]' '[:lower:]')" ] || return 1
  "$bin" version 2>/dev/null | awk -v v="$want" '
    NR == 1 { for (i = 1; i <= NF; i++) { f = $i; sub(/^v/, "", f); if (f == v) ok = 1 } }
    END { exit !ok }'
}

_verifyAsset() {
  # _verifyAsset <DTCTL|MONACO> <version> <arch> <file> <release-sha256> — the
  # reviewed pin first, the release's own checksum file second; both must
  # match. For the framework's own pinned version a missing pin REFUSES: the
  # release file alone is not enough. Only a version the repo overrode (not
  # the framework's) falls back to the release file, and says so.
  local tool="$1" version="${2#v}" arch="$3" file="$4" release="$5" pin pinned
  eval "pin=\${${tool}_PIN_VERSION:-}"
  if [ -n "$pin" ] && [ "$version" = "${pin#v}" ]; then
    pinned=$(_pinnedSha "$tool" "$version" "$arch")
    if [ -z "$pinned" ]; then
      printError "INTEGRITY: $tool v$version is the framework's pinned version but no reviewed sha256 is pinned for $arch — refusing"
      return 1
    fi
    verifySha256 "$file" "$pinned" || return 1
  else
    printWarn "$tool v$version is not the framework's pinned version (${pin:-none}) — no reviewed sha256, verified against the release checksum file only"
  fi
  verifySha256 "$file" "$release"
}

installDtctl() {
  # installDtctl [dir] — installs dtctl only (no context), verified against the
  # release's checksums.txt. Version: DTCTL_VERSION, else the latest release.
  local dir="${1:-${DTCTL_INSTALL_DIR:-/usr/local/bin}}"
  local arch version tag
  if ! arch=$(_releaseArch); then
    printError "dtctl: unsupported architecture '$(uname -m)' (amd64 and arm64 only)"
    return 1
  fi
  if [ -n "$DTCTL_VERSION" ]; then
    if ! _isVersion "$DTCTL_VERSION"; then
      printError "dtctl: DTCTL_VERSION '$DTCTL_VERSION' is not a version (x.y.z)"
      return 1
    fi
    version="${DTCTL_VERSION#v}"
  else
    if ! tag=$(_githubLatestTag "dynatrace-oss/dtctl"); then
      printError "dtctl: could not resolve the latest release — set DTCTL_VERSION to pin one"
      return 1
    fi
    version="${tag#v}"
  fi

  if _devToolReports "$dir/dtctl" "$version" "$(_pinnedSha DTCTL "$version" "$arch" BIN_)"; then
    printInfo "dtctl v$version already present at $dir/dtctl (sha256 matches the pin)"
    return 0
  fi

  local asset="dtctl_${version}_linux_${arch}.tar.gz"
  local base="https://github.com/dynatrace-oss/dtctl/releases/download/v${version}"
  local tmp expected
  if ! tmp=$(mktemp -d); then
    printError "dtctl: could not create a temporary directory"
    return 1
  fi
  printInfo "Downloading $asset and checksums.txt (dtctl v$version)"
  if ! _fetch "$base/$asset" "$tmp/$asset" 120 \
     || ! _fetch "$base/checksums.txt" "$tmp/checksums.txt" 30; then
    printError "dtctl: download of v$version failed"
    rm -rf "$tmp"
    return 1
  fi
  expected=$(awk -v f="$asset" '$2 == f || $2 == "*"f {print $1; exit}' "$tmp/checksums.txt")
  if ! _verifyAsset DTCTL "$version" "$arch" "$tmp/$asset" "$expected"; then
    rm -rf "$tmp"
    return 1
  fi
  if ! tar -xzf "$tmp/$asset" -C "$tmp" dtctl 2>/dev/null || [ ! -f "$tmp/dtctl" ]; then
    printError "dtctl: archive $asset holds no dtctl binary"
    rm -rf "$tmp"
    return 1
  fi
  if ! _installBinary "$tmp/dtctl" "$dir" dtctl; then
    printError "dtctl: could not install into $dir"
    rm -rf "$tmp"
    return 1
  fi
  rm -rf "$tmp"
  printInfo "dtctl v$version installed at $dir/dtctl"
}

enableDtctl() {
  # enableDtctl [--readwrite] [--token-var NAME]
  # Installs the latest dtctl (pin with DTCTL_VERSION), verified against the
  # release's checksums.txt, and configures a non-interactive context:
  #   environment  = $DT_ENVIRONMENT
  #   token        = a literal ${NAME} reference (default DT_DTCTL_TOKEN), which
  #                  dtctl resolves at run time — the value is never written
  #   safety level = readonly; --readwrite opts into readwrite-mine
  # DTCTL_TOKEN_STORAGE=file: a container has no keyring.
  local level="readonly" token_var="${DTCTL_TOKEN_VAR:-DT_DTCTL_TOKEN}"
  local ctx="${DTCTL_CONTEXT:-enablement}" dir="${DTCTL_INSTALL_DIR:-/usr/local/bin}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --readwrite) level="readwrite-mine" ;;
      --token-var) shift; token_var="$1" ;;
      *) printError "enableDtctl: unknown argument '$1'. Usage: enableDtctl [--readwrite] [--token-var NAME]"; return 2 ;;
    esac
    shift
  done

  printInfoSection "Enabling dtctl ($level) for ${DT_ENVIRONMENT:-<no environment>}"

  if [ -z "$DT_ENVIRONMENT" ]; then
    printError "DT_ENVIRONMENT is not set — dtctl not enabled. Run selectEnvironment first."
    return 1
  fi
  if ! _isVarName "$token_var"; then
    printError "enableDtctl: '$token_var' is not a valid variable name"
    return 1
  fi

  installDtctl "$dir" || return 1

  # The literal ${NAME} is stored in the config; dtctl expands it per command.
  # dtctl (0.42-0.43) rewrites the whole config with every ${VAR} EXPANDED on
  # any config write. So every write here runs in an allowlist environment
  # (_dtctlConfigWrite): no token variable is present, an expansion can only
  # ever produce "", never a value, not even for a moment. Other contexts'
  # ${VAR} references are blanked too — the framework owns only its own.
  # set-credentials MUST be the last write — it puts back the reference that
  # use-context blanked.
  local token_ref='${'"$token_var"'}' cfg
  cfg=$(_dtctlConfigFile)
  export DTCTL_TOKEN_STORAGE=file
  # A previous enableDtctl left the config read-only (see below).
  [ -f "$cfg" ] && chmod u+w "$cfg" 2>/dev/null
  _showCmd "dtctl config set-context $ctx --environment \$DT_ENVIRONMENT --token-ref $ctx --safety-level $level"
  _showCmd "dtctl config use-context $ctx"
  _showCmd "dtctl config set-credentials $ctx --token '$token_ref'"
  if ! _dtctlConfigWrite "$dir/dtctl" config set-context "$ctx" --environment "$DT_ENVIRONMENT" \
         --token-ref "$ctx" --safety-level "$level" \
     || ! _dtctlConfigWrite "$dir/dtctl" config use-context "$ctx" \
     || ! _dtctlConfigWrite "$dir/dtctl" config set-credentials "$ctx" --token "$token_ref"; then
    # use-context may already have expanded a ${VAR} into the file.
    _dtctlDropLeakedConfig "$cfg" "$token_var"
    printError "dtctl: configuring context '$ctx' failed"
    return 1
  fi

  # Prove the config says what we asked. The value check runs FIRST so that
  # no failure path leaves a token value on disk.
  if _dtctlDropLeakedConfig "$cfg" "$token_var"; then
    printError "dtctl: $cfg held the token VALUE — removed dtctl's config"
    return 1
  fi
  if ! grep -qF "safety-level: $level" "$cfg" 2>/dev/null \
     || ! grep -qF "token: $token_ref" "$cfg" 2>/dev/null; then
    printError "dtctl: $cfg does not show safety-level $level with token reference $token_ref"
    return 1
  fi

  # Read-only from here: dtctl (0.43) rewrites the WHOLE config, every ${VAR}
  # expanded, on any later config write — `use-context`, or `set-credentials`
  # for another context — which would put the token value on disk. Read
  # commands only read the file; a config write now fails instead of leaking.
  # This holds because dtctl 0.43.0 writes the config IN PLACE (strace:
  # openat O_WRONLY|O_TRUNC on the file, no temp file, no rename). A dtctl that
  # switched to temp+rename would bypass a file mode — re-check on every bump.
  if ! chmod 0400 "$cfg" 2>/dev/null; then
    printWarn "dtctl: could not make $cfg read-only — a later 'dtctl config' write would store the token value"
  fi

  if ! _varIsSet "$token_var"; then
    printWarn "$token_var is not set — dtctl is configured, but commands fail until it is"
  fi
  printInfo "dtctl context '$ctx' → $DT_ENVIRONMENT, safety level $level, token from \$$token_var"
  printInfo "Try: dtctl get dashboards   ·   disable with: disableDtctl"
  printInfo "Note: the config is read-only; to change the context re-run enableDtctl, not 'dtctl config'"
}

disableDtctl() {
  # Removes the dtctl binary installed by enableDtctl and dtctl's config and
  # file-stored tokens.
  local dir="${DTCTL_INSTALL_DIR:-/usr/local/bin}" cfg
  cfg=$(_dtctlConfigFile)
  _removeBinary "$dir/dtctl"
  rm -f "$cfg"
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/dtctl" "${XDG_DATA_HOME:-$HOME/.local/share}/dtctl"
  if [ -e "$dir/dtctl" ] || [ -e "$cfg" ]; then
    printError "disableDtctl: $dir/dtctl or $cfg is still present"
    return 1
  fi
  printInfo "dtctl disabled — removed $dir/dtctl and its config"
}

installMonaco() {
  # Installs Monaco at MONACO_VERSION (the latest release when it is empty).
  # The framework's pinned version installs the framework's PATCHED build
  # (ENH-114) from MONACO_PIN_URL, verified against the reviewed sha256 in
  # variables.sh only — a mismatch refuses, and the stock release is never
  # installed in its place: it cannot deploy with a learner token. Any other
  # version installs the upstream release, verified against its .sha256 file.
  local dir="${MONACO_INSTALL_DIR:-/usr/local/bin}" arch version tag patched="" label
  if ! arch=$(_releaseArch); then
    printError "monaco: unsupported architecture '$(uname -m)' (amd64 and arm64 only)"
    return 1
  fi
  if [ -n "$MONACO_VERSION" ]; then
    if ! _isVersion "$MONACO_VERSION"; then
      printError "monaco: MONACO_VERSION '$MONACO_VERSION' is not a version (x.y.z)"
      return 1
    fi
    version="${MONACO_VERSION#v}"
  else
    if ! tag=$(_githubLatestTag "Dynatrace/dynatrace-configuration-as-code"); then
      printError "monaco: could not resolve the latest release — set MONACO_VERSION to pin one"
      return 1
    fi
    version="${tag#v}"
  fi
  label="$version"
  if [ -n "${MONACO_PIN_URL:-}" ] && [ "$version" = "${MONACO_PIN_VERSION#v}" ]; then
    patched=1
    label="$version${MONACO_PIN_BUILD:++$MONACO_PIN_BUILD}"
  fi
  if _devToolReports "$dir/monaco" "$label" "$(_pinnedSha MONACO "$version" "$arch" BIN_)"; then
    printInfo "Monaco v$label already present at $dir/monaco (sha256 matches the pin)"
    return 0
  fi
  printInfoSection "Installing Monaco v$label ($arch)"

  local asset="monaco-linux-${arch}"
  local base="https://github.com/Dynatrace/dynatrace-configuration-as-code/releases/download/v${version}"
  local tmp expected
  if [ -n "$patched" ]; then
    case "$MONACO_PIN_URL" in
      https://*) base="${MONACO_PIN_URL%/}" ;;
      *) _monacoRefusePatched "$dir" "INTEGRITY: monaco: MONACO_PIN_URL '$MONACO_PIN_URL' is not https:// — refusing"; return 1 ;;
    esac
    expected=$(_pinnedSha MONACO "$version" "$arch")
    if [ -z "$expected" ]; then
      _monacoRefusePatched "$dir" "INTEGRITY: monaco: no reviewed sha256 is pinned for the patched v$label on $arch — refusing"
      return 1
    fi
  fi
  if ! tmp=$(mktemp -d); then
    printError "monaco: could not create a temporary directory"
    [ -n "$patched" ] && _monacoRefusePatched "$dir"
    return 1
  fi
  if [ -n "$patched" ]; then
    if ! _fetch "$base/$asset" "$tmp/$asset" 120; then
      rm -rf "$tmp"
      _monacoRefusePatched "$dir" "monaco: download of the patched v$label failed — the stock release is not installed instead"
      return 1
    fi
    if ! verifySha256 "$tmp/$asset" "$expected"; then
      rm -rf "$tmp"
      _monacoRefusePatched "$dir" "INTEGRITY: monaco: the patched v$label does not match the reviewed sha256 — refusing; the stock release is not installed instead"
      return 1
    fi
  else
    if ! _fetch "$base/$asset" "$tmp/$asset" 120 \
       || ! _fetch "$base/$asset.sha256" "$tmp/$asset.sha256" 30; then
      printError "monaco: download of v$version failed"
      rm -rf "$tmp"
      return 1
    fi
    expected=$(awk '{print $1; exit}' "$tmp/$asset.sha256")
    if ! _verifyAsset MONACO "$version" "$arch" "$tmp/$asset" "$expected"; then
      rm -rf "$tmp"
      return 1
    fi
  fi
  if ! _installBinary "$tmp/$asset" "$dir" monaco; then
    printError "monaco: could not install into $dir"
    rm -rf "$tmp"
    [ -n "$patched" ] && _monacoRefusePatched "$dir"
    return 1
  fi
  rm -rf "$tmp"
  printInfo "Monaco v$label installed at $dir/monaco"
}

_monacoRefusePatched() {
  # _monacoRefusePatched <dir> [message] — the patched install refused: print
  # the reason and remove $dir/monaco, so no stock binary baked into the image
  # (which cannot run with a learner token) is left for deployMonaco to find.
  [ -n "${2:-}" ] && printError "$2"
  [ -e "$1/monaco" ] || return 0
  if _removeBinary "$1/monaco" && [ ! -e "$1/monaco" ]; then
    printError "monaco: removed $1/monaco — no unverified Monaco is left installed"
  else
    printError "monaco: could not remove $1/monaco — it is NOT the verified patched build; deployMonaco refuses to run it"
  fi
}

_monacoIsPatched() {
  # _monacoIsPatched — 0 when the `monaco` on PATH may run with a learner token.
  # With the patched pin configured (MONACO_PIN_URL) that means the framework's
  # build: sha256 equal to the pinned binary hash for this arch, or a
  # `monaco version` naming +$MONACO_PIN_BUILD. No patched pin: any monaco.
  local bin arch pin=""
  bin=$(command -v monaco 2>/dev/null) || return 1
  [ -n "${MONACO_PIN_URL:-}" ] || return 0
  arch=$(_releaseArch) && pin=$(_pinnedSha MONACO "${MONACO_PIN_VERSION:-}" "$arch" BIN_)
  if [ -n "$pin" ] && [ "$(_sha256Of "$bin")" = "$(echo "$pin" | tr '[:upper:]' '[:lower:]')" ]; then
    return 0
  fi
  "$bin" version 2>/dev/null | grep -qF "+${MONACO_PIN_BUILD:-wwse}"
}

_monacoReady() {
  # _monacoReady <what-happens-instead> — makes sure the verified Monaco is on
  # PATH before deployMonaco/undeployMonaco run it: one installMonaco when it
  # is missing or not the patched build, then the check again. Still not
  # verified → fails LOUDLY: stock Monaco asks
  # /platform/metadata/v1/classic-environment-domain, which needs
  # 'app-engine:apps:run', and would fail against a learner token anyway.
  _monacoIsPatched && return 0
  installMonaco
  _monacoIsPatched && return 0
  local bin
  bin=$(command -v monaco 2>/dev/null || echo "(none on PATH)")
  printError "MONACO NOT VERIFIED: $bin is not the framework's patched Monaco v${MONACO_PIN_VERSION:-?}+${MONACO_PIN_BUILD:-?}"
  printError "  its sha256 is not the pinned one and \`monaco version\` does not name +${MONACO_PIN_BUILD:-?}."
  printError "  Stock Monaco needs 'app-engine:apps:run' (classic-environment-domain discovery), which a learner token does not carry."
  if [ -n "${MONACO_VERSION:-}" ] && [ "${MONACO_VERSION#v}" != "${MONACO_PIN_VERSION#v}" ]; then
    printError "  MONACO_VERSION=$MONACO_VERSION overrides the framework pin ${MONACO_PIN_VERSION:-} — the patched build exists only for the pin."
  fi
  printError "  $1"
  return 1
}

installDevTools() {
  # installDevTools — called from finalizePostCreation, so every repo gets
  # dtctl and Monaco at the framework's pinned versions (DTCTL_VERSION,
  # MONACO_VERSION in variables.sh), checksum-verified. When DT_DTCTL_TOKEN is
  # set it also configures a read-only dtctl context (enableDtctl).
  # FRAMEWORK_DEVTOOLS=off (environment or .devcontainer/.env) skips it all.
  # Never fatal: a failure is a warning and post-create carries on.
  case "$(echo "${FRAMEWORK_DEVTOOLS:-on}" | tr '[:upper:]' '[:lower:]')" in
    off|false|no|0)
      printInfo "Dev tools (dtctl, Monaco) skipped: FRAMEWORK_DEVTOOLS=$FRAMEWORK_DEVTOOLS"
      return 0 ;;
  esac
  printInfoSection "Installing dev tools: dtctl ${DTCTL_VERSION:-latest}, Monaco ${MONACO_VERSION:-latest}"
  # One download budget for both tools (seconds), read by _fetch. Anything but
  # a plain non-negative integer falls back to 180: $(( )) would otherwise
  # evaluate the value as an expression. Digits are read as base 10 (a
  # leading 0 is not octal) and capped at 900; a value too long to be a sane
  # number of seconds is capped BEFORE arithmetic, which would overflow it.
  local DEVTOOLS_DEADLINE budget="${DEVTOOLS_TIME_BUDGET:-180}"
  case "$budget" in
    ''|*[!0-9]*)
      printWarn "DEVTOOLS_TIME_BUDGET '$budget' is not a number of seconds — using 180"
      budget=180 ;;
  esac
  if [ "${#budget}" -gt 6 ] || [ $(( 10#$budget )) -gt 900 ]; then
    printWarn "DEVTOOLS_TIME_BUDGET '$budget' is above the 900 s cap — using 900"
    budget=900
  fi
  budget=$(( 10#$budget ))
  DEVTOOLS_DEADLINE=$(( $(date +%s) + budget ))
  if _varIsSet DT_DTCTL_TOKEN && [ -n "$DT_ENVIRONMENT" ]; then
    _devToolStep "dtctl" enableDtctl
  else
    _devToolStep "dtctl" installDtctl
    _varIsSet DT_DTCTL_TOKEN && printWarn "DT_DTCTL_TOKEN is set but DT_ENVIRONMENT is not — dtctl installed without a context"
  fi
  _devToolStep "Monaco" installMonaco
  return 0
}

_maskTokens() {
  # _maskTokens <text> — prints <text> with the VALUES of the dev-tool token
  # variables replaced (fixed string, not a pattern) by <redacted>.
  local text="$1" var val
  for var in DT_DTCTL_TOKEN DT_MONACO_TOKEN "${DTCTL_TOKEN_VAR:-}" "${MONACO_TOKEN_VAR:-}"; do
    _varIsSet "$var" || continue
    eval "val=\${$var}"
    text="${text//"$val"/<redacted>}"
  done
  printf '%s\n' "$text"
}

_devToolStep() {
  # _devToolStep <name> <command...> — runs one install step. Its output is
  # replayed with token values masked (a child tool may echo one). On failure
  # each line is tagged "devtools (non-fatal)", which _creationNoiseFilter
  # drops: a missing dev tool must not mark the whole container as failed.
  # An INTEGRITY refusal (checksum mismatch, missing pin, non-https pin URL) is
  # tagged "devtools INTEGRITY FAILED" instead, which the filter KEEPS: a
  # tampered download is a real error, not noise.
  local name="$1" out
  shift
  if out=$("$@" 2>&1); then
    _maskTokens "$out"
    return 0
  fi
  out=$(_maskTokens "$out")
  printf '%s\n' "$out" | sed -e '/INTEGRITY:/s/^/devtools INTEGRITY FAILED: /' -e '/INTEGRITY:/!s/^/devtools (non-fatal): /'
  printWarn "devtools (non-fatal): $name not installed — post-create continues without it"
  return 1
}

_monacoUnsupportedTypes() {
  # _monacoUnsupportedTypes <dir> — prints "<file>: <type>" for every config
  # whose type is not settings or document. v1 deploys only those two; a
  # scalar type (`type: synthetic-monitor`) is a classic API and needs a
  # classic token. Only the `type:` key of a config item is read, never a
  # parameter's `type: environment`. A file the parser cannot read (no
  # top-level block `configs:` list) is reported too: it fails closed.
  find "$1" -type f \( -name '*.yaml' -o -name '*.yml' \) 2>/dev/null | sort | while IFS= read -r f; do
    awk -v F="$f" '
      function ind(s) { match(s, /^ */); return RLENGTH }
      function check(v) {
        sub(/^[ \t]+/, "", v); sub(/[ \t]+(#.*)?$/, "", v)
        if (v == "") { pending = 1; return }
        if (v ~ /^\{/) { v = substr(v, 2); sub(/^[ \t]+/, "", v); sub(/[ \t:].*$/, "", v) }
        if (v != "settings" && v != "document") print F ": " v
      }
      {
        line = $0; sub(/\r$/, "", line)
        if (line ~ /^[ \t]*(#.*)?$/) next
        i = ind(line)
        if (pending) {
          pending = 0
          if (i > col) { k = line; sub(/^ *(- *)?/, "", k); sub(/:.*$/, "", k)
                         if (k != "settings" && k != "document") print F ": " k; next }
          print F ": (empty type)"
        }
        if (line ~ /^configs:/) {
          v = line; sub(/^configs:[ \t]*/, "", v); sub(/[ \t]*#.*$/, "", v)
          # A flow list cannot be checked line by line: fail closed.
          if (v != "") { print F ": (configs: not a block list — cannot check its types)"; exit }
          inconf = 1; sawconf = 1; dash = -1; next
        }
        if (i == 0 && line !~ /^-/) { inconf = 0; next }
        if (!inconf) next
        rest = line
        if (line ~ /^ *- /) {
          if (dash < 0) dash = i
          if (i == dash) { col = i + 2; rest = substr(line, i + 3); sub(/^ +/, "", rest)
                           if (rest ~ /^type:/) check(substr(rest, 6)); next }
        }
        if (i == col && line ~ /^ *type:/) { v = line; sub(/^ *type:/, "", v); check(v) }
      }
      END {
        if (pending) print F ": (empty type)"
        if (!sawconf) print F ": (no top-level configs: list — cannot check its types)"
      }
    ' "$f"
  done
}

_monacoMergeDeleteFiles() {
  # _monacoMergeDeleteFiles <new> <old> — appends to <new> every entry of <old>
  # it does not already hold. Entries are the "- ..." items under `delete:`.
  local merged
  merged=$(mktemp)
  awk '
    /^delete:/ { next }
    /^- / { if (cur != "") keep(); cur = $0; next }
    { cur = cur "\n" $0 }
    END { if (cur != "") keep(); if (n == 0) { print "delete: []"; exit }
          print "delete:"; for (i = 1; i <= n; i++) print out[i] }
    function keep() { if (!(cur in seen)) { seen[cur] = 1; out[++n] = cur }; cur = "" }
  ' "$1" "$2" > "$merged" && mv "$merged" "$1"
}

_monacoRunDir() {
  echo "${MONACO_RUN_DIR:-$REPO_PATH/.devcontainer/.monaco}/$1"
}

_monacoProjectName() {
  local repo session
  repo=$(echo "${RepositoryName:-$(basename "$REPO_PATH")}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g')
  session="${DT_SESSION_ID:-$(getDtSessionId)}"
  [ -n "$session" ] || return 1
  # Written into the generated manifest: lowercase, digits and dashes only.
  echo "${repo}-${session}" | grep -qxE '[a-z0-9][a-z0-9-]*' || return 1
  echo "${repo}-${session}"
}

_monacoGenerationFile() {
  echo "$(dirname "$(_monacoRunDir "$1")")/.$1-generation"
}

_monacoGeneration() {
  # _monacoGeneration <phase> — how many times this phase's documents were
  # left in the tenant's trash (0 when never). Monaco derives a document's id
  # from <project>:document:<configId>, and a trashed document keeps holding
  # its id: the next create of that id answers 409 "id already exists". The
  # learner token cannot purge the trash (ENH-114), so deployMonaco moves to
  # a new project name, <repo>-<session>-g<N>, and with it to fresh ids.
  local g
  g=$(cat "$(_monacoGenerationFile "$1")" 2>/dev/null)
  case "$g" in ''|*[!0-9]*) echo 0 ;; *) echo "$g" ;; esac
}

_monacoBumpGeneration() {
  local f
  f=$(_monacoGenerationFile "$1")
  mkdir -p "$(dirname "$f")" && echo $(( $(_monacoGeneration "$1") + 1 )) > "$f"
}

_monacoTrashCollision() {
  # _monacoTrashCollision <monaco-deploy-output> — 0 when a document create
  # was refused because its id is still held (by a trashed document).
  grep -q 'failed with status code 409.*id already exists: monaco-' "$1"
}

_showCmd() {
  # _showCmd <command> — prints the exact command a helper runs, before it
  # runs it, so a learner sees the real monaco / dtctl call. Callers pass it
  # in SINGLE quotes: a variable shows as $NAME, never its value.
  printInfo "→ $1"
}

_monacoDocsUrl() {
  echo "${MONACO_DOCS_URL:-https://dynatrace-wwse.github.io/codespaces-framework/functions/#the-monaco-folder}"
}

_monacoHelp() {
  # _monacoHelp deploy|undeploy — the -h text. Prints only: no tenant call,
  # no file written, no variable value (names only).
  local token_var="${MONACO_TOKEN_VAR:-DT_MONACO_TOKEN}"
  if [ "$1" = "undeploy" ]; then
    cat <<HELPEOF
Usage: undeployMonaco [tenant|env]
       undeployMonaco -h | --help

Deletes what deployMonaco created in your Dynatrace environment.
  (no argument)  both phases, env first, then tenant
  tenant | env   only that phase

What it does, step by step:
  1. Reads the delete file deployMonaco wrote BEFORE it deployed:
       .devcontainer/.monaco/<phase>/delete.yaml
     No delete file -> nothing was deployed for that phase; nothing to do.
  2. Runs: monaco delete --manifest manifest.yaml --file delete.yaml -e learner
  3. RUM frontends deployMonaco created (rum-frontends.txt): deletes each one
       DELETE /platform/rum/v1/frontends/<FRONTEND id>
     and PROVES it gone by reading its builtin:rum.frontend.name back (the
     DELETE's status alone proves nothing). Every API call is printed.
  4. On success removes .devcontainer/.monaco/<phase>. On failure the delete
     file is KEPT, so you can run undeployMonaco again.
     Documents land in the tenant's trash (the learner token cannot purge it,
     by design) — reported as a warning, not an error. A trashed document
     keeps its id, so the next deploy uses a new project name
     (<repo>-<DT_SESSION_ID>-g<N>) and with it fresh document ids.
  Every monaco command is printed before it runs (variables as \$NAME).

Required variables (names only):
  DT_ENVIRONMENT  the environment URL
  ${token_var}  platform token (override the name with MONACO_TOKEN_VAR)

Redeploy with: deployMonaco <tenant|env>
Docs: $(_monacoDocsUrl)
HELPEOF
    return 0
  fi
  cat <<HELPEOF
Usage: deployMonaco <tenant|env>
       deployMonaco -h | --help

Deploys the Monaco project in monaco/<phase>/ of this repo to your Dynatrace
environment, as configuration-as-code.
  tenant   monaco/tenant/ — configuration for the whole environment
  env      monaco/env/    — configuration for this lab's environment

What it does, step by step:
  1. Checks monaco/<phase>/ exists and holds only supported config types
     (v1: settings and documents).
  2. Checks DT_ENVIRONMENT and the platform token are set, and that the
     framework's verified Monaco build is installed (installs it if not).
  3. Copies monaco/<phase>/ to .devcontainer/.monaco/<phase>/ and writes a
     manifest.yaml naming ONE project per learner: <repo>-<DT_SESSION_ID>.
     LEARNER_PREFIX=<DT_SESSION_ID> is exported for config names, so your
     objects never collide with another learner's.
  4. RUM frontends: for each line of monaco/<phase>/rum-frontends.txt
       <ENV_VAR> <name> <display name>     e.g.  TODO_RUM_APP_ID todoapp TODOApp
     creates YOUR web frontend <name>-<seat id> ("<display name> <session>")
       POST /platform/rum/v1/frontends   (reused if it already exists)
     and exports <ENV_VAR>=APPLICATION-… (its classic id, what an app-detection
     rule takes). Every API call is printed, method and path; never the token.
  5. Runs: monaco generate deletefile manifest.yaml -o . --file delete.yaml
     — BEFORE deploying, so even a partial deploy can be undone.
  6. Runs: monaco deploy manifest.yaml -e learner
     Every monaco command is printed before it runs (variables as \$NAME).
     After an undeploy left documents in the trash the project is
     <repo>-<DT_SESSION_ID>-g<N>: a trashed document keeps its id, and a
     create of that id answers 409 "id already exists".

Required variables (names only):
  DT_ENVIRONMENT  the environment URL
  ${token_var}  platform token (override the name with MONACO_TOKEN_VAR)
  DT_SESSION_ID   your learner id (derived from DT_HOSTGROUP or GITHUB_USER)
  (rum-frontends.txt only) the token needs rum:frontends:write and
  rum:frontends:delete; jq

Undo: undeployMonaco <tenant|env>   (undeployMonaco -h for details)
Docs: $(_monacoDocsUrl)
HELPEOF
}

# ── RUM frontends (ENH-130 M5) ────────────────────────────────────────────
# A phase folder may declare 3rd-gen RUM web frontends in
# monaco/<phase>/rum-frontends.txt, one per line:
#     <ENV_VAR> <name> <display name...>
#     TODO_RUM_APP_ID todoapp TODOApp
# deployMonaco creates each one for THIS seat before Monaco runs
# (POST /platform/rum/v1/frontends, frontendName <name>-<seat id>) and exports
# <ENV_VAR> with its classic id (APPLICATION-…), which is what a
# builtin:rum.web.app-detection rule takes — the FRONTEND-… id is refused.
# Monaco has no type for a frontend, so the framework owns its lifecycle;
# undeployMonaco deletes it after monaco delete and proves it is gone by
# reading it back (the DELETE answers 204 or 404 for an id that is already
# gone, so its status proves nothing). Every API call is printed, method and
# path; the token never is. State: <run>/rum-frontends.state, one line per
# frontend: <ENV_VAR>|<frontendName>|<FRONTEND id>|<APPLICATION id>.

_rumSeatId() {
  # The seat's own id, so two sessions of one learner never share a frontend:
  # the Orbital job, else the Codespace, else this host.
  local id="${ORBITAL_JOB_ID:-${CODESPACE_NAME:-$(hostname 2>/dev/null)}}"
  id=$(printf '%s' "$id" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//')
  [ -n "$id" ] || return 1
  printf '%s\n' "$id"
}

_rumApi() {
  # _rumApi <METHOD> <path> [json-body] — one platform API call with the Monaco
  # token. Prints "Dynatrace API: <METHOD> <path>" (never the token); sets
  # RUM_HTTP_CODE and RUM_HTTP_BODY. The token goes to curl on a file
  # descriptor, not on its command line.
  local method="$1" api_path="$2" data="$3" token_var="${MONACO_TOKEN_VAR:-DT_MONACO_TOKEN}" tok out
  eval "tok=\${$token_var}"
  printInfo "Dynatrace API: $method $(printf '%s' "$api_path" | cut -c1-160)"
  out=$(mktemp) || { RUM_HTTP_CODE=000; RUM_HTTP_BODY=""; return 1; }
  if [ -n "$data" ]; then
    RUM_HTTP_CODE=$(curl -sS --max-time 60 -o "$out" -w '%{http_code}' -X "$method" \
      -H @<(printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$tok") \
      --data-binary @<(printf '%s' "$data") "${DT_ENVIRONMENT%/}$api_path" 2>/dev/null)
  else
    RUM_HTTP_CODE=$(curl -sS --max-time 60 -o "$out" -w '%{http_code}' -X "$method" \
      -H @<(printf 'Authorization: Bearer %s\n' "$tok") "${DT_ENVIRONMENT%/}$api_path" 2>/dev/null)
  fi
  [ -n "$RUM_HTTP_CODE" ] || RUM_HTTP_CODE=000
  RUM_HTTP_BODY=$(cat "$out" 2>/dev/null)
  rm -f "$out"
}

_rumAppIdsByName() {
  # _rumAppIdsByName <frontendName> — every classic APPLICATION id whose
  # builtin:rum.frontend.name is <frontendName>, one per line. Fails when the
  # settings cannot be read (not the same as "none"), and when the listing
  # cannot be completed: more than RUM_MAX_PAGES (20) pages, or a nextPageKey
  # that repeats — an incomplete listing proves nothing about absence.
  local name="$1" api_path="/platform/classic/environment-api/v2/settings/objects?schemaIds=builtin:rum.frontend.name&fields=scope,value&pageSize=500" next ids="" seen=" " pages=0
  while :; do
    pages=$((pages + 1))
    [ "$pages" -le "${RUM_MAX_PAGES:-20}" ] || { printError "reading builtin:rum.frontend.name: more than ${RUM_MAX_PAGES:-20} pages — listing not complete" >&2; return 1; }
    _rumApi GET "$api_path" >&2
    [ "$RUM_HTTP_CODE" = 200 ] || { printError "reading builtin:rum.frontend.name failed (HTTP $RUM_HTTP_CODE)" >&2; return 1; }
    ids="$ids$(printf '%s' "$RUM_HTTP_BODY" | jq -r --arg n "$name" '.items[]? | select(.value.frontendName == $n) | .scope')
"
    next=$(printf '%s' "$RUM_HTTP_BODY" | jq -r '.nextPageKey // empty')
    [ -n "$next" ] || break
    case "$seen" in *" $next "*) printError "reading builtin:rum.frontend.name: nextPageKey repeated — listing not complete" >&2; return 1 ;; esac
    seen="$seen$next "
    api_path="/platform/classic/environment-api/v2/settings/objects?nextPageKey=$next"
  done
  printf '%s' "$ids" | grep -E '^APPLICATION-[0-9A-F]+$'
  return 0
}

_rumAppIdExists() {
  # _rumAppIdExists <APPLICATION id> — 0 present, 1 gone, 2 unreadable.
  _rumApi GET "/platform/classic/environment-api/v2/settings/objects?schemaIds=builtin:rum.frontend.name&scopes=$1&fields=scope"
  [ "$RUM_HTTP_CODE" = 200 ] || return 2
  [ "$(printf '%s' "$RUM_HTTP_BODY" | jq -r '.items | length')" -gt 0 ]
}

_rumSmartscape() {
  # _rumSmartscape <DQL filter> — "<FRONTEND id> <APPLICATION id>" of live
  # FRONTEND nodes matching it. Smartscape lags a new frontend by ~30 s.
  local q="smartscapeNodes FRONTEND | filter $1 | fields id, id_classic" body token
  body=$(jq -nc --arg q "$q" '{query: $q, requestTimeoutMilliseconds: 30000}')
  printInfo "  DQL: $q" >&2
  _rumApi POST "/platform/storage/query/v1/query:execute" "$body" >&2
  local tries=0
  while [ "$RUM_HTTP_CODE" = 202 ] || { [ "$RUM_HTTP_CODE" = 200 ] && [ "$(printf '%s' "$RUM_HTTP_BODY" | jq -r '.state // "SUCCEEDED"')" = RUNNING ]; }; do
    token=$(printf '%s' "$RUM_HTTP_BODY" | jq -r '.requestToken // empty')
    [ -n "$token" ] && [ "$tries" -lt 30 ] || break
    tries=$((tries + 1)); sleep 1
    _rumApi GET "/platform/storage/query/v1/query:poll?request-token=$(jq -rn --arg t "$token" '$t|@uri')" >&2
  done
  [ "$RUM_HTTP_CODE" = 200 ] || { printError "Smartscape query failed (HTTP $RUM_HTTP_CODE)" >&2; return 1; }
  printf '%s' "$RUM_HTTP_BODY" | jq -r '.result.records[]? | select(.id and .id_classic) | "\(.id) \(.id_classic)"'
}

_rumSmartscapeWait() {
  # _rumSmartscapeWait <DQL filter> — _rumSmartscape, asked again every
  # RUM_RETRY_SLEEP (10) s for up to ~180 s while it finds nothing: a frontend
  # created moments ago is not in Smartscape yet (~80 s measured on PVF).
  local found="" waited=0 step="${RUM_RETRY_SLEEP:-10}"
  while :; do
    found=$(_rumSmartscape "$1") || return 1
    [ -n "$found" ] && break
    [ "$waited" -ge "${RUM_SMARTSCAPE_WAIT:-180}" ] && break
    printInfo "  not in Smartscape yet — asking again in ${step}s" >&2
    sleep "$step"; waited=$((waited + step))
  done
  printf '%s\n' "$found" | head -1
}

_rumStateSet() {
  # _rumStateSet <state-file> <var> <fname> <fid> <aid> — the ONE line for
  # <var>|<fname>, replacing any earlier (pending or partial) line for it.
  local f="$1" tmp
  tmp=$(mktemp) || return 1
  [ -f "$f" ] && grep -v "^$2|$3|" "$f" > "$tmp"
  printf '%s|%s|%s|%s\n' "$2" "$3" "$4" "$5" >> "$tmp"
  mv "$tmp" "$f"
}

_rumReservedVar() {
  # A rum-frontends.txt <ENV_VAR> must not overwrite anything the shell, the
  # framework or a token lives in.
  case "$1" in
    *TOKEN*|*SECRET*|*PASSWORD*|*KEY*|DT_*|ORBITAL_*|MONACO_*|DTCTL_*|GITHUB_*|CODESPACE*|RUM_*) return 0 ;;
    PATH|HOME|USER|LOGNAME|SHELL|PWD|OLDPWD|IFS|PS1|PS2|PS4|TERM|LANG|TMPDIR|HOSTNAME) return 0 ;;
    KUBECONFIG|LEARNER_PREFIX|REPO_PATH|RepositoryName|ENV_FILE|LD_*|BASH*|ZSH*|FPATH|PROMPT_COMMAND) return 0 ;;
  esac
  return 1
}

_monacoRumFrontends() {
  # _monacoRumFrontends <phase> <run-dir> [previous-state] — the deploy pre-hook.
  # Creates (or reuses) every frontend monaco/<phase>/rum-frontends.txt
  # declares, exports its <ENV_VAR>, records it in <run>/rum-frontends.state.
  local phase="$1" run="$2" prev="$3" decl="$2/project/rum-frontends.txt" seat session
  [ -f "$decl" ] || return 0
  command -v jq >/dev/null 2>&1 || { printError "rum-frontends.txt needs jq, which is not installed — nothing was deployed"; return 1; }
  seat=$(_rumSeatId) || { printError "No seat id (ORBITAL_JOB_ID, CODESPACE_NAME or hostname) for the RUM frontend name — nothing was deployed"; return 1; }
  session="${DT_SESSION_ID:-$(getDtSessionId)}"
  local var name display fname ids n fid aid line rc=0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    var=$(printf '%s\n' "$line" | awk '{print $1}')
    name=$(printf '%s\n' "$line" | awk '{print $2}')
    display=$(printf '%s\n' "$line" | awk '{$1=""; $2=""; sub(/^ +/, ""); print}')
    [ -n "$var" ] || continue
    if ! _isVarName "$var" || ! printf '%s' "$name" | grep -qxE '[a-z0-9][a-z0-9-]*' || [ -z "$display" ]; then
      printError "rum-frontends.txt: '$line' is not '<ENV_VAR> <name> <display name>' (name: lowercase, digits, dashes)"; rc=1; break
    fi
    if _rumReservedVar "$var"; then
      printError "rum-frontends.txt: '$var' is a reserved name (shell, framework or token variable) — pick another, e.g. TODO_RUM_APP_ID"; rc=1; break
    fi
    fname=$(printf '%s-%s' "$name" "$seat" | cut -c1-100)
    display="$display $session${ORBITAL_JOB_ID:+ ($seat)}"
    printInfo "RUM frontend '$fname' (\"$display\") for $var"
    ids=$(_rumAppIdsByName "$fname") || { rc=1; break; }
    n=$(printf '%s' "$ids" | grep -c . )
    fid="" aid=""
    if [ "$n" -gt 0 ]; then
      # Reuse. A second POST of the same name is NOT idempotent: it answers the
      # same FRONTEND id but creates another classic application.
      line=$([ -n "$prev" ] && [ -f "$prev" ] && grep "^$var|$fname|" "$prev" | head -1)
      if [ -n "$line" ] && printf '%s\n' "$ids" | grep -qx "$(printf '%s' "$line" | cut -d'|' -f4)"; then
        fid=$(printf '%s' "$line" | cut -d'|' -f3); aid=$(printf '%s' "$line" | cut -d'|' -f4)
      else
        line=$(_rumSmartscapeWait "frontend.name == \"$fname\"") || { rc=1; break; }
        fid="${line%% *}"; aid="${line#* }"
      fi
      if [ -z "$fid" ] || [ -z "$aid" ]; then
        printError "RUM frontend '$fname' exists ($n classic application(s)) but Smartscape names no live FRONTEND for it — not creating another. Delete it in the tenant, then deploy again."
        rc=1; break
      fi
      printInfo "  reusing $fid ($aid) — already created for this seat"
    else
      # Pending BEFORE the POST: a timeout, a 5xx or an answer without an id can
      # still have created it; undeployMonaco resolves a pending line by name.
      _rumStateSet "$run/rum-frontends.state" "$var" "$fname" "" ""
      _rumApi POST "/platform/rum/v1/frontends" \
        "$(jq -nc --arg f "$fname" --arg d "$display" '{frontendName: $f, displayName: $d, type: "WEB_AUTO_INJECTED", enableRum: true}')"
      if [ "$RUM_HTTP_CODE" = 403 ] || [ "$RUM_HTTP_CODE" = 401 ] || [ "$RUM_HTTP_CODE" = 400 ]; then
        # Refused outright: nothing was created, so nothing is pending.
        grep -v "^$var|$fname|" "$run/rum-frontends.state" > "$run/rum-frontends.state.tmp"; mv "$run/rum-frontends.state.tmp" "$run/rum-frontends.state"
        [ -s "$run/rum-frontends.state" ] || rm -f "$run/rum-frontends.state"
      fi
      if [ "$RUM_HTTP_CODE" = 403 ]; then
        printError "creating RUM frontend refused (HTTP 403): the token lacks rum:frontends:write (the Enablement app's seat token carries it since ENH-130 M5). Nothing was deployed."
        rc=1; break
      fi
      [ "$RUM_HTTP_CODE" = 201 ] || [ "$RUM_HTTP_CODE" = 200 ] || { printError "creating RUM frontend '$fname' failed (HTTP $RUM_HTTP_CODE): $(printf '%s' "$RUM_HTTP_BODY" | jq -r '.error.message // empty' 2>/dev/null)"; rc=1; break; }
      fid=$(printf '%s' "$RUM_HTTP_BODY" | jq -r '.id // empty')
      printf '%s' "$fid" | grep -qxE 'FRONTEND-[0-9A-F]+' || { printError "creating RUM frontend '$fname' answered no FRONTEND id"; rc=1; break; }
      # Recorded before the classic id is known: undeployMonaco can delete it either way.
      _rumStateSet "$run/rum-frontends.state" "$var" "$fname" "$fid" ""
      ids=$(_rumAppIdsByName "$fname") || { rc=1; break; }
      if [ "$(printf '%s' "$ids" | grep -c .)" -eq 1 ]; then
        aid="$ids"
      else
        # Ambiguous (or not yet visible): ask Smartscape which classic id is this FRONTEND's.
        aid=$(_rumSmartscapeWait "id == toSmartscapeId(\"$fid\")" | awk '{print $2}') || aid=""
      fi
      [ -n "$aid" ] || { printError "RUM frontend $fid created, but its classic APPLICATION id could not be resolved — undo with: undeployMonaco $phase"; rc=1; break; }
      printInfo "  created $fid ($aid)"
    fi
    _rumStateSet "$run/rum-frontends.state" "$var" "$fname" "$fid" "$aid"
    export "$var=$aid"
    printInfo "  $var=$aid (classic id — the app-detection rule's applicationId)"
  done < "$decl"
  return $rc
}

_rumResolve() {
  # _rumResolve <fname> <fid> — sets RUM_FID / RUM_AID for a state line whose
  # ids are not both known (a pending line, or a create whose classic id was
  # never resolved). Returns 0 resolved, 3 "nothing by that name exists" (only
  # for a pending line, after RUM_RESOLVE_TRIES (3) complete listings), 1 not
  # resolvable — a failed read, or a lag that outlasts the bound. Only 0 and 3
  # are evidence; 1 keeps the line.
  local fname="$1" fid="$2" ids n try=0 line
  RUM_FID="$fid" RUM_AID=""
  while :; do
    try=$((try + 1))
    ids=$(_rumAppIdsByName "$fname") || return 1
    n=$(printf '%s' "$ids" | grep -c .)
    if [ "$n" -eq 1 ] && [ -n "$fid" ]; then RUM_AID="$ids"; return 0; fi
    if [ "$n" -ge 1 ]; then
      if [ -n "$fid" ]; then
        line=$(_rumSmartscapeWait "id == toSmartscapeId(\"$fid\")") || return 1
      else
        line=$(_rumSmartscapeWait "frontend.name == \"$fname\"") || return 1
      fi
      [ -n "$line" ] || { printError "  '$fname' exists ($n classic app(s)) but Smartscape names no FRONTEND for it" >&2; return 1; }
      RUM_FID="${line%% *}"; RUM_AID="${line#* }"
      return 0
    fi
    [ "$try" -lt "${RUM_RESOLVE_TRIES:-3}" ] || break
    printInfo "  no builtin:rum.frontend.name '$fname' (yet?) — listing again in ${RUM_RETRY_SLEEP:-10}s" >&2
    sleep "${RUM_RETRY_SLEEP:-10}"
  done
  # A FRONTEND id we were given, but no classic app by that name: lag or a
  # renamed app — not evidence it is gone.
  [ -z "$fid" ] && return 3
  printError "  '$fname' ($fid): no classic id found after $try listings — not proven gone" >&2
  return 1
}

_monacoRumUndeploy() {
  # _monacoRumUndeploy <run-dir> — the undeploy post-hook. Deletes every
  # frontend in <run>/rum-frontends.state and proves each gone by re-reading
  # its builtin:rum.frontend.name. Deleting a frontend also removes the
  # detection rules that point at it, so it runs AFTER monaco delete.
  # The DELETE's status is never the proof. Measured on PVF: an id already gone
  # answers 204 or 404; a frontend created seconds before answers 200 and stays,
  # or 400 "no application with id …", and only a later DELETE removes it.
  # So: DELETE, read back, and again every RUM_RETRY_SLEEP (10) s, ~90 s at most.
  # The classic id is resolved BEFORE the delete (afterwards nothing is left to
  # resolve it from); a line it cannot be resolved for is kept, never dropped.
  local state="$1/rum-frontends.state" var fname fid aid left rc=0 try code gone rrc
  [ -f "$state" ] || return 0
  left=$(mktemp) || return 1
  while IFS='|' read -r var fname fid aid; do
    [ -n "$fname" ] || continue
    printInfo "RUM frontend '$fname' (${fid:-pending create}${aid:+, $aid})"
    gone=""
    if [ -z "$fid" ] || [ -z "$aid" ]; then
      _rumResolve "$fname" "$fid"; rrc=$?
      case "$rrc" in
        0) fid="$RUM_FID"; aid="$RUM_AID"; printInfo "  resolved: $fid ($aid)" ;;
        3) printInfo "  verified: no builtin:rum.frontend.name '$fname' in ${RUM_RESOLVE_TRIES:-3} complete listings — the create never landed"; gone=1 ;;
        *) printError "  kept for the next undeployMonaco (ids not resolvable)"
           printf '%s|%s|%s|%s\n' "$var" "$fname" "$fid" "$aid" >> "$left"; rc=1; continue ;;
      esac
    fi
    try=0
    while [ -z "$gone" ]; do
      _rumApi DELETE "/platform/rum/v1/frontends/$fid"
      code="$RUM_HTTP_CODE"
      case "$code" in
        200|204|404|400) ;;
        *) printError "  delete failed (HTTP $code)"; break ;;
      esac
      sleep "${RUM_VERIFY_SLEEP:-3}"
      _rumAppIdExists "$aid"
      case $? in
        1) printInfo "  verified gone: $aid has no builtin:rum.frontend.name any more (DELETE answered $code)"; gone=1; break ;;
        2) printError "  could not re-read $aid (HTTP $RUM_HTTP_CODE) — not proven gone"; break ;;
      esac
      try=$((try + 1))
      [ "$try" -lt 9 ] || { printError "  $aid still exists after $try DELETEs (last answered $code) — not gone"; break; }
      printInfo "  still there after DELETE answered $code (just created?) — again in ${RUM_RETRY_SLEEP:-10}s"
      sleep "${RUM_RETRY_SLEEP:-10}"
    done
    if [ -n "$gone" ]; then
      unset "$var"
    else
      printError "  kept for the next undeployMonaco"
      printf '%s|%s|%s|%s\n' "$var" "$fname" "$fid" "$aid" >> "$left"; rc=1
    fi
  done < "$state"
  if [ -s "$left" ]; then mv "$left" "$state"; else rm -f "$left" "$state"; fi
  return $rc
}

_monacoUndoHint() {
  # _monacoUndoHint <phase> <deployed|failed> <run-dir> — the closing block
  # of every deploy that reached the tenant, so the way back is the last
  # thing on screen (not buried under Monaco's log).
  local phase="$1" outcome="$2" run="$3"
  if [ "$outcome" = "deployed" ]; then
    printInfoSection "monaco/$phase deployed — to undo: undeployMonaco $phase"
  else
    printInfoSection "monaco/$phase FAILED part-way — undo what landed: undeployMonaco $phase"
  fi
  printInfo "Delete file: $run/delete.yaml"
  [ -s "$run/rum-frontends.state" ] && printInfo "RUM frontend(s) undeployMonaco also deletes: $(cut -d'|' -f2,3 "$run/rum-frontends.state" | tr '|\n' ' ;')"
  printInfo "How it works: deployMonaco -h · undeployMonaco -h · $(_monacoDocsUrl)"
}

deployMonaco() {
  # deployMonaco <tenant|env> — deploys monaco/<phase>/ from the repo as ONE
  # project per learner, named <repo>-<DT_SESSION_ID>. That name is Monaco's
  # isolation key for settings and documents; LEARNER_PREFIX=<DT_SESSION_ID>
  # is exported for config names. The delete file is generated BEFORE the
  # deploy, so even a partial deploy can be undone with undeployMonaco.
  # v1 deploys settings and documents with the platform token in
  # $DT_MONACO_TOKEN (override the name with MONACO_TOKEN_VAR).
  local phase="$1" token_var="${MONACO_TOKEN_VAR:-DT_MONACO_TOKEN}"
  case "$phase" in
    tenant|env) ;;
    -h|--help) _monacoHelp deploy; return 0 ;;
    *) printError "Usage: deployMonaco <tenant|env>   (deployMonaco -h for help)"; return 2 ;;
  esac

  local src="${MONACO_DIR:-$REPO_PATH/monaco}/$phase"
  if [ ! -d "$src" ]; then
    printInfo "No monaco/$phase folder in this repo — nothing to deploy"
    return 0
  fi
  printInfoSection "Deploying Monaco configuration: monaco/$phase"

  if ! _isVarName "$token_var"; then
    printError "deployMonaco: '$token_var' is not a valid variable name"
    return 1
  fi
  local unsupported
  unsupported=$(_monacoUnsupportedTypes "$src")
  if [ -n "$unsupported" ]; then
    printError "monaco/$phase holds config types that are not supported yet (v1: settings and document only; synthetic/RUM-app need a classic token):"
    echo "$unsupported" | while IFS= read -r u; do printError "  $u"; done
    printError "Nothing was deployed."
    return 1
  fi
  if [ -z "$DT_ENVIRONMENT" ]; then
    printError "DT_ENVIRONMENT is not set — nothing was deployed"
    return 1
  fi
  if ! _varIsSet "$token_var"; then
    printError "$token_var is not set — Monaco needs a platform token. Nothing was deployed."
    return 1
  fi
  local project session
  session="${DT_SESSION_ID:-$(getDtSessionId)}"
  if ! project=$(_monacoProjectName); then
    printError "No usable DT_SESSION_ID (DT_HOSTGROUP or GITHUB_USER; lowercase, digits, dashes) — refusing: without it every learner would share one Monaco project. Nothing was deployed."
    return 1
  fi
  local generation
  generation=$(_monacoGeneration "$phase")
  [ "$generation" -gt 0 ] && project="$project-g$generation"
  _monacoReady "Nothing was deployed." || return 1

  # The manifest's project path must sit below the manifest (Monaco reads the
  # project relative to it), so the run dir holds a snapshot of the configs.
  # It also makes undeployMonaco independent of later edits to monaco/.
  local run previous=""
  run=$(_monacoRunDir "$phase")
  mkdir -p "$(dirname "$run")"
  echo '*' > "$(dirname "$run")/.gitignore"
  # Keep what an earlier deploy created: a config removed from monaco/ since
  # then must still be deletable. monaco delete skips objects already gone.
  if [ -f "$run/delete.yaml" ]; then
    previous=$(mktemp)
    cp "$run/delete.yaml" "$previous"
  fi
  local prev_rum=""
  if [ -f "$run/rum-frontends.state" ]; then
    prev_rum=$(mktemp)
    cp "$run/rum-frontends.state" "$prev_rum"
  fi
  rm -rf "$run"
  mkdir -p "$run"
  cp -R "$src" "$run/project"
  # Frontends an earlier deploy created stay recorded until undeployMonaco proves them gone.
  [ -n "$prev_rum" ] && cp "$prev_rum" "$run/rum-frontends.state"
  cat > "$run/manifest.yaml" <<MANIFESTEOF
manifestVersion: "1.0"
projects:
  - name: $project
    path: project
environmentGroups:
  - name: default
    environments:
      - name: learner
        url:
          type: environment
          value: DT_ENVIRONMENT
        auth:
          platformToken:
            type: environment
            name: $token_var
MANIFESTEOF
  printInfo "Monaco project '$project' (LEARNER_PREFIX=$session) → $DT_ENVIRONMENT"

  # Pre-hook: the RUM frontends monaco/<phase>/rum-frontends.txt declares (ENH-130 M5).
  if ! _monacoRumFrontends "$phase" "$run" "$prev_rum"; then
    rm -f "$prev_rum"
    [ -n "$previous" ] && mv "$previous" "$run/delete.yaml"
    printError "monaco/$phase: RUM frontend step failed — Monaco did not run."
    [ -s "$run/rum-frontends.state" ] && printError "  What it created is recorded; undo with: undeployMonaco $phase"
    return 1
  fi
  rm -f "$prev_rum"

  local rel="${run#"$REPO_PATH"/}"
  printInfo "manifest.yaml reads the URL from \$DT_ENVIRONMENT and the token from \$$token_var, by name"
  _showCmd "cd $rel"
  _showCmd 'LEARNER_PREFIX=$DT_SESSION_ID monaco generate deletefile manifest.yaml -o . --file delete.yaml'
  if ! (cd "$run" && LEARNER_PREFIX="$session" monaco generate deletefile manifest.yaml -o . --file delete.yaml) \
     || [ ! -f "$run/delete.yaml" ]; then
    [ -n "$previous" ] && mv "$previous" "$run/delete.yaml"
    printError "monaco: could not generate $run/delete.yaml — refusing to deploy what could not be undone"
    return 1
  fi
  if [ -n "$previous" ]; then
    _monacoMergeDeleteFiles "$run/delete.yaml" "$previous"
    rm -f "$previous"
  fi
  _showCmd 'LEARNER_PREFIX=$DT_SESSION_ID monaco deploy manifest.yaml -e learner'
  # Shown as it runs (tee) and kept in a file: a trash collision is told apart
  # by monaco's output. The exit code travels in a file: zsh and bash name
  # the pipeline status differently.
  local out
  out=$(mktemp) || { printError "monaco/$phase: mktemp failed — nothing was deployed"; return 1; }
  { (cd "$run" && LEARNER_PREFIX="$session" monaco deploy manifest.yaml -e learner) 2>&1; echo $? > "$out.rc"; } | tee "$out"
  if [ "$(cat "$out.rc" 2>/dev/null)" != 0 ]; then
    if _monacoTrashCollision "$out"; then
      _monacoBumpGeneration "$phase"
      printError "monaco/$phase: a document id is still held by a document in the tenant's trash (HTTP 409 \"id already exists\")."
      printError "  An earlier undeploy left it there; the learner token cannot purge the trash (by design)."
      printError "  The next deploy uses a new project name, $(_monacoProjectName)-g$(_monacoGeneration "$phase"), and with it new document ids."
      printError "  Run again: deployMonaco $phase"
    else
      printError "monaco deploy of monaco/$phase failed — undo what landed with: undeployMonaco $phase"
    fi
    rm -f "$out" "$out.rc"
    _monacoUndoHint "$phase" failed "$run"
    return 1
  fi
  rm -f "$out" "$out.rc"
  _monacoUndoHint "$phase" deployed "$run"
}

_monacoOnlyTrashRefused() {
  # _monacoOnlyTrashRefused <monaco-delete-output> — succeeds, printing the
  # count, when the ONLY failures in a failed `monaco delete` are trash purges
  # refused with 403: monaco deletes a document first (it is then in the
  # trash) and purges it second, and a learner token holds no
  # document:trash.documents:delete (ENH-114). Every other ERROR line —
  # anything but those purges and monaco's own summary lines — means a real
  # failure, and so does seeing no refused purge at all.
  local log="$1" trashed
  trashed=$(grep 'level=ERROR msg="Failed to delete document"' "$log" \
    | grep -c 'failed to DELETE in resource documents-trash with id .* failed with status code 403')
  [ "$trashed" -gt 0 ] || return 1
  grep 'level=ERROR' "$log" \
    | grep -v 'msg="Failed to delete document".*failed to DELETE in resource documents-trash with id .* failed with status code 403' \
    | grep -v "msg=\"Error during deletion\".*error.details=\"failed to delete $trashed document(s)\"" \
    | grep -v 'msg="Failed to delete all configurations from environment' \
    | grep -v 'msg="Error: encountered deletion errors for the following environments: learner"' \
    | grep -v 'msg="error logs written to ' \
    | grep -q . && return 1
  echo "$trashed"
}

undeployMonaco() {
  # undeployMonaco [tenant|env] — deletes what deployMonaco created, using the
  # delete file persisted at deploy time. No argument: both phases, env first.
  local phases rc=0 phase run out trashed drc token_var="${MONACO_TOKEN_VAR:-DT_MONACO_TOKEN}"
  case "$1" in
    "") phases="env tenant" ;;
    tenant|env) phases="$1" ;;
    -h|--help) _monacoHelp undeploy; return 0 ;;
    *) printError "Usage: undeployMonaco [tenant|env]   (undeployMonaco -h for help)"; return 2 ;;
  esac
  printInfoSection "Removing Monaco configuration ($phases)"
  # read lines, not `for p in $phases`: zsh does not word-split.
  while IFS= read -r phase; do
    run=$(_monacoRunDir "$phase")
    if [ ! -f "$run/delete.yaml" ] && [ ! -f "$run/rum-frontends.state" ]; then
      printInfo "monaco/$phase: nothing deployed (no $run/delete.yaml)"
      continue
    fi
    if ! _varIsSet "$token_var"; then
      printError "monaco/$phase: $token_var is not set — cannot delete. Delete file kept at $run/delete.yaml"
      rc=1
      continue
    fi
    if [ ! -f "$run/delete.yaml" ]; then
      # Only frontends (a deploy that stopped before Monaco ran).
      if _monacoRumUndeploy "$run"; then
        rm -rf "$run"
        printInfo "monaco/$phase removed from ${DT_ENVIRONMENT:-the tenant}"
      else
        printError "monaco/$phase: RUM frontend(s) not proven gone — kept in $run/rum-frontends.state"
        rc=1
      fi
      continue
    fi
    _monacoReady "Nothing was deleted. Delete file kept at $run/delete.yaml" || { rc=1; continue; }
    # Kept in a file: a refused trash purge is told apart by monaco's output.
    out=$(mktemp) || { printError "monaco/$phase: mktemp failed — nothing was deleted. Delete file kept at $run/delete.yaml"; rc=1; continue; }
    _showCmd "cd ${run#"$REPO_PATH"/}"
    _showCmd 'monaco delete --manifest manifest.yaml --file delete.yaml -e learner'
    (cd "$run" && monaco delete --manifest manifest.yaml --file delete.yaml -e learner) > "$out" 2>&1
    drc=$?
    cat "$out"
    trashed=""
    if [ "$drc" -eq 0 ] || trashed=$(_monacoOnlyTrashRefused "$out"); then
      # Documents now in the trash still hold their ids: the next deploy of
      # this phase must not create them again (_monacoGeneration).
      [ -n "$trashed" ] && _monacoBumpGeneration "$phase"
      # Post-hook: the frontends, after monaco delete (deleting one also drops its detection rules).
      if ! _monacoRumUndeploy "$run"; then
        rm -f "$run/delete.yaml"
        printError "monaco/$phase: configuration deleted, but RUM frontend(s) not proven gone — kept in $run/rum-frontends.state; run undeployMonaco $phase again"
        rc=1
      elif [ "$drc" -eq 0 ]; then
        rm -rf "$run"
        printInfo "monaco/$phase removed from ${DT_ENVIRONMENT:-the tenant}"
      else
        rm -rf "$run"
        printWarn "monaco/$phase removed from ${DT_ENVIRONMENT:-the tenant}: $trashed document(s) deleted but left in the tenant's trash — the learner token has no document:trash.documents:delete, by design (ENH-114). monaco reported this as an error; nothing else failed. They keep their ids, so the next deployMonaco $phase uses a new project name (-g$(_monacoGeneration "$phase")) and fresh ids."
      fi
    else
      printError "monaco/$phase: monaco delete failed — delete file kept at $run/delete.yaml"
      rc=1
    fi
    rm -f "$out"
  done < <(echo "$phases" | tr ' ' '\n')
  return $rc
}

selectEnvironment(){
  # Check if DT_ENVIRONMENT is already set
  if [ -n "$DT_ENVIRONMENT" ]; then
    printWarn "DT_ENVIRONMENT is already set to $DT_ENVIRONMENT. This function will override the DT_ENVIRONMENT environment variable and the entry in the $ENV_FILE file."
    printWarn "You should be careful if you have other variables needed for that environment such as API Tokens."
    printf "Do you want to override it? (y/n): "
    read override
    if [ "$override" != "y" ] && [ "$override" != "Y" ]; then
      printInfo "Keeping existing DT_ENVIRONMENT. Exiting function."
      return
    fi
  fi

  printInfoSection "🧠 Please select the Environment you want to connect to:"
  printInfo "1. playground (wkf10640)"
  printInfo "2. demo.live (guu84124)"
  printInfo "3. tacocorp (bwm98081)"
  printInfo "4. other, you'll be prompted to enter the full URL (Prod/Sprint/Dev)"
  printf "Enter your choice (1-4): "
  read choice
  case $choice in
    1)
      DT_ENVIRONMENT="https://wkf10640.apps.dynatrace.com"
      ;;
    2)
      DT_ENVIRONMENT="https://guu84124.apps.dynatrace.com"
      ;;
    3)
      DT_ENVIRONMENT="https://bwm98081.apps.dynatrace.com"
      ;;
    4)
      printf "Enter in the format eg. https://abc123.apps.dynatrace.com or for sprint -> https://abc123.sprint.apps.dynatracelabs.com\nURL to your Dynatrace Platform:"
      read -r DT_ENVIRONMENT
      # Basic validation to ensure it starts with https://
      if [[ ! "$DT_ENVIRONMENT" =~ ^https:// ]]; then
        printWarn "URL should start with 'https://'. Please try again."
        return 1
      fi
      ;;
    *)
      printWarn "Invalid choice. Defaulting to playground."
      DT_ENVIRONMENT="https://wkf10640.apps.dynatrace.com"
      ;;
  esac

  export DT_ENVIRONMENT=$DT_ENVIRONMENT
  if [ -f "$ENV_FILE" ]; then
    # Remove existing DT_ENVIRONMENT line if present (including lines with leading spaces)
    sed '/^[[:space:]]*DT_ENVIRONMENT=/d' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  fi
  echo "DT_ENVIRONMENT=$DT_ENVIRONMENT" >> "$ENV_FILE"

  # Parse the environment to derive DT_TENANT, DT_ENV_TYPE, DT_OTEL_ENDPOINT
  parseDynatraceEnvironment "$DT_ENVIRONMENT"

  printInfo "Selected Demo Environment: $DT_ENVIRONMENT"

  # Update MCP config if it's already enabled
  if [ -f "$REPO_PATH/.vscode/mcp.json" ]; then
    printInfo "Updating MCP Server configuration for $DT_ENVIRONMENT"
    enableMCP
  else
    printInfo "Type 'enableMCP' to connect VS Code to $DT_ENVIRONMENT"
  fi
}

setEnvironmentInEnv(){
  if [ -z "${DT_ENVIRONMENT}" ]; then
    printWarn "DT_ENVIRONMENT is missing as environment variable defaulting to playground "
    DT_ENVIRONMENT="https://wkf10640.apps.dynatrace.com"
  else
    printInfo "DT_ENVIRONMENT found as environment variable ($DT_ENVIRONMENT) and writing to file"
  fi
  echo -e "DT_ENVIRONMENT=$DT_ENVIRONMENT" >> "$ENV_FILE"
  export DT_ENVIRONMENT=$DT_ENVIRONMENT
}

bindFunctionsInShell() {
  printInfo "Binding source_framework.sh and adding a Greeting in the .zshrc"
  cat >> "$HOME/.zshrc" << 'ZSHRC_EOF'

#Making sure the Locale is set properly
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

# Loading all functions in CLI via source_framework.sh (sets up cache paths + sources functions)
ZSHRC_EOF
  # REPO_PATH must be expanded now (at write time) since it won't exist at shell-open time
  echo "source $REPO_PATH/.devcontainer/util/source_framework.sh" >> "$HOME/.zshrc"
  cat >> "$HOME/.zshrc" << 'ZSHRC_EOF'

#print greeting everytime a Terminal is opened
printGreeting

#supress p10k instant prompt
typeset -g POWERLEVEL9K_INSTANT_PROMPT=quiet
ZSHRC_EOF

}

setupAliases() {
  printInfo "Adding Bash and Kubectl Pro CLI aliases to .zshrc"
  cat >> "$HOME/.zshrc" << 'ZSHRC_EOF'

# Alias for ease of use of the CLI
alias las='ls -las'
alias c='clear'
alias hg='history | grep'
alias h='history'
alias gita='git add -A'
alias gitc='git commit -s -m'
alias gitp='git push'
alias gits='git status'
alias gith='git log --graph --pretty="%C(yellow)[%h] %C(reset)%s by %C(green)%an - %C(cyan)%ad %C(auto)%d" --decorate --all --date=human'
alias vaml='vi -c "set syntax:yaml" -'
alias vson='vi -c "set syntax:json" -'
alias pg='ps -aux | grep'
ZSHRC_EOF
}

installRunme() {
  printInfoSection "Installing Runme"
  mkdir runme_binary
  if [[ "$ARCH" == "x86_64" ]]; then
    printInfoSection "Installing Runme Version $RUNME_CLI_VERSION for AMD/x86"
    wget -O runme_binary/runme_linux_x86_64.tar.gz https://download.stateful.com/runme/${RUNME_CLI_VERSION}/runme_linux_x86_64.tar.gz
    tar -xvf runme_binary/runme_linux_x86_64.tar.gz --directory runme_binary
  elif [[ "$ARCH" == *"arm"* || "$ARCH" == *"aarch64"* ]]; then
    printInfoSection "Installing Runme Version $RUNME_CLI_VERSION for ARM"
    wget -O runme_binary/runme_linux_arm64.tar.gz https://download.stateful.com/runme/${RUNME_CLI_VERSION}/runme_linux_arm64.tar.gz
    tar -xvf runme_binary/runme_linux_arm64.tar.gz --directory runme_binary
  else 
    printWarn "Runme cant be installed, Architecture unknown"
  fi
  sudo mv runme_binary/runme /usr/local/bin
  rm -rf runme_binary
}

stopKindCluster(){
  printInfoSection "Stopping Kubernetes Cluster (kind-control-plane)"
  docker stop kind-control-plane 
}

startKindCluster(){
  export CLUSTER_ENGINE=kind
  # On Orbital (Sysbox), Kind pods get stuck ContainerCreating — too many
  # nesting levels (Sysbox→DinD→dt-enablement→kind-node→pod containers).
  # Use k3d (startK3dCluster) on Orbital instead.
  if [[ "$(detectRunEnvironment)" == "orbital" ]]; then
    printWarn "═══════════════════════════════════════════════════════════════════════════════"
    printWarn " Kind on Orbital (Sysbox): pod containers will be stuck ContainerCreating."
    printWarn " Sysbox cannot virtualise the 4-level container nesting Kind requires."
    printWarn " Use startK3dCluster instead — k3d works on Orbital (3 levels only)."
    printWarn " Kind is only supported in real VM environments (Codespaces, local)."
    printWarn "═══════════════════════════════════════════════════════════════════════════════"
  fi
  printInfoSection "Starting Kubernetes Cluster (kind-control-plane)"
  KIND_STATUS=$(docker inspect -f '{{.State.Status}}' $KINDIMAGE 2>/dev/null)
  if [ "$KIND_STATUS" = "exited" ] || [ "$KIND_STATUS" = "dead" ]; then
    printWarn "There is a stopped $KINDIMAGE, starting it..."
    docker start $KINDIMAGE
    attachKindCluster
  elif  [ "$KIND_STATUS" = "running" ]; then
    printWarn "A $KINDIMAGE is already running, attaching to it..."
    attachKindCluster
  else
    printInfo "No $KINDIMAGE was found, creating a new one..."
    createKindCluster
  fi
  # Install ingress controller — sole app-exposure mechanism
  installIngressController

  printInfo "Kind reachable under:"
  kubectl cluster-info --context kind-kind
  printInfo "-----"
  printInfo "The following functions are available for you to maximize your K8s experience:"
  printInfo "startKindCluster - will start, create or attach to a running Cluster"
  printInfo "other useful functions: stopKindCluster createKindCluster deleteKindCluster"
  printInfo "attachKindCluster "
  printInfo "-----"
  printInfo "Setting the current context to 'kube-system' instead of 'default' you can change it by typing"
  printInfo "kubectl config set-context --current --namespace=<namespace-name>"
  kubectl config set-context --current --namespace=kube-system
}

attachKindCluster(){
  printInfoSection "Attaching to running Kubernetes Cluster (kind-control-plane)"
  local KUBEDIR="$HOME/.kube"
  if [ -d $KUBEDIR ]; then
    printWarn "Kuberconfig $KUBEDIR exists, overriding Kubernetes conection"
  else
    printInfo "Kubeconfig $KUBEDIR does not exist, creating a new one"
    mkdir -p $HOME/.kube
  fi
  kind get kubeconfig > $KUBEDIR/config && printInfo "Connection created" || printWarn "Issue creating connection"
}


createKindCluster() {
  printInfoSection "Creating Kubernetes Cluster (kind-control-plane)"
  # Create k8s cluster
  printInfo "Creating Kind cluster"
  kind create cluster --config "${FRAMEWORK_CACHE:-${REPO_PATH}}/.devcontainer/yaml/kind/kind-cluster.yml" --wait 5m &&\
    printInfo "Kind cluster created successfully, reachabe under:" ||\
    printWarn "Kind cluster could not be created"
  kubectl cluster-info --context kind-kind
}

deleteKindCluster() {
  printInfoSection "Deleting Kubernetes Cluster (Kind)"
  kind delete cluster --name kind
  printInfo "Kind cluster deleted successfully."
}

# ======================================================================
#          ------- K3d Cluster Functions -------                         #
#  Lightweight Kubernetes via K3d (K3s in Docker). Default engine.       #
# ======================================================================

startK3dCluster(){
  export CLUSTER_ENGINE=k3d
  printInfoSection "Starting Kubernetes Cluster (K3d)"

  installK3d

  # Check if K3d cluster exists
  if k3d cluster list 2>/dev/null | grep -q "enablement"; then
    local cluster_state
    cluster_state=$(k3d cluster list -o json 2>/dev/null | python3 -c "
import sys, json
clusters = json.load(sys.stdin)
for c in clusters:
    if 'enablement' in c.get('name',''):
        nodes = c.get('nodes',[])
        running = sum(1 for n in nodes if n.get('state',{}).get('running',False))
        print('running' if running > 0 else 'stopped')
        break
" 2>/dev/null)

    if [[ "$cluster_state" == "running" ]]; then
      printWarn "K3d cluster already running, attaching..."
      attachK3dCluster
    else
      printInfo "K3d cluster exists but stopped, starting..."
      k3d cluster start enablement
      attachK3dCluster
    fi
  else
    printInfo "No K3d cluster found, creating a new one..."
    createK3dCluster
  fi

  # Install ingress controller — sole app-exposure mechanism
  installIngressController

  printInfo "K3d cluster reachable under:"
  kubectl cluster-info
  printInfo "-----"
  printInfo "Available functions: startK3dCluster stopK3dCluster deleteK3dCluster"
  printInfo "-----"
  kubectl config set-context --current --namespace=kube-system
}
# Backward compatibility aliases
startK3sCluster() { startK3dCluster "$@"; }

installK3d() {
  # Installs K3d if not already present.
  # Version pinned: v5.9.0 shipped 2026-06-02 with a missing ghcr.io proxy
  # image (manifest unknown). Pin to last known-good release.
  if command -v k3d &>/dev/null; then
    return 0
  fi
  local K3D_VERSION="${K3D_VERSION:-v5.8.3}"
  printInfo "Installing K3d ${K3D_VERSION}..."
  curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | TAG=${K3D_VERSION} bash
  k3d version
}

createK3dCluster() {
  # Configurable via env vars so multiple clusters can coexist on one host.
  # Defaults match prior behavior (single dev cluster owns 80/443).
  #
  #   K3D_CLUSTER_NAME      cluster name              (default: enablement)
  #   K3D_LB_HTTP_PORT      host port for ingress :80 (default: 80)
  #   K3D_LB_HTTPS_PORT     host port for ingress :443 (default: 443)
  #   K3D_API_PORT          host port for k8s API     (default: 6443)
  #
  # On the ops server (where 80/443 are nginx's), set:
  #   export K3D_LB_HTTP_PORT=30080 K3D_LB_HTTPS_PORT=30443 K3D_API_PORT=6444
  : "${K3D_CLUSTER_NAME:=enablement}"
  : "${K3D_LB_HTTP_PORT:=80}"
  : "${K3D_LB_HTTPS_PORT:=443}"
  : "${K3D_API_PORT:=6443}"

  printInfoSection "Creating K3d cluster ($K3D_CLUSTER_NAME)"
  printInfo "Ports — http:$K3D_LB_HTTP_PORT  https:$K3D_LB_HTTPS_PORT  api:$K3D_API_PORT"

  installK3d

  # Under heavy concurrent provisioning (many sessions cold-starting k3d on one
  # host at once) `k3d cluster create --wait` can return with the cluster listed
  # but the API server not actually reachable / kubeconfig context not attached.
  # The old code only checked `k3d cluster list | grep`, so a half-started cluster
  # slipped through and every downstream kubectl hit localhost:8080 → dead session.
  # Gate on the API actually answering, and retry the whole create once (a fresh
  # delete+create clears a wedged partial cluster).
  local attempt
  for attempt in 1 2; do
    k3d cluster create "$K3D_CLUSTER_NAME" \
      --api-port "$K3D_API_PORT" \
      -p "${K3D_LB_HTTP_PORT}:80@loadbalancer" \
      -p "${K3D_LB_HTTPS_PORT}:443@loadbalancer" \
      --k3s-arg "--disable=traefik@server:0" \
      --wait

    if k3d cluster list 2>/dev/null | grep -q "^${K3D_CLUSTER_NAME} "; then
      attachK3dCluster
      if waitK3dApiReady; then
        printInfo "K3d cluster '$K3D_CLUSTER_NAME' created — reach ingress at http://localhost:${K3D_LB_HTTP_PORT}/"
        return 0
      fi
    fi

    printWarn "K3d cluster '$K3D_CLUSTER_NAME' not reachable (attempt $attempt/2)"
    if [[ "$attempt" -lt 2 ]]; then
      printInfo "Tearing down partial cluster and retrying..."
      k3d cluster delete "$K3D_CLUSTER_NAME" 2>/dev/null
      sleep 5
    fi
  done

  printError "K3d cluster '$K3D_CLUSTER_NAME' failed to become reachable after 2 attempts"
  return 1
}

# Poll until the k3d API server answers (kubectl get nodes) or timeout.
# Guards against the "cluster listed but API not up / context unattached" race
# that silently breaks every subsequent kubectl call.
waitK3dApiReady() {
  : "${K3D_CLUSTER_NAME:=enablement}"
  local timeout="${K3D_API_READY_TIMEOUT:-90}"
  local waited=0
  while (( waited < timeout )); do
    if kubectl --context "k3d-${K3D_CLUSTER_NAME}" get nodes &>/dev/null; then
      # Ensure it is also the CURRENT context so context-less kubectl calls work.
      kubectl config use-context "k3d-${K3D_CLUSTER_NAME}" &>/dev/null
      return 0
    fi
    sleep 3
    waited=$(( waited + 3 ))
  done
  return 1
}
createK3sCluster() { createK3dCluster "$@"; }

attachK3dCluster(){
  : "${K3D_CLUSTER_NAME:=enablement}"
  printInfoSection "Attaching to K3d cluster ($K3D_CLUSTER_NAME)"
  local KUBEDIR="$HOME/.kube"
  mkdir -p "$KUBEDIR"

  k3d kubeconfig merge "$K3D_CLUSTER_NAME" --kubeconfig-merge-default 2>/dev/null
  kubectl config use-context "k3d-${K3D_CLUSTER_NAME}" 2>/dev/null

  if kubectl get nodes &>/dev/null; then
    printInfo "Connected to K3d cluster '$K3D_CLUSTER_NAME'"
    return 0
  else
    # Not fatal on its own — createK3dCluster's waitK3dApiReady gate will retry —
    # but surface it as a non-zero so callers can react instead of proceeding blind.
    printWarn "Could not connect to K3d cluster '$K3D_CLUSTER_NAME' yet"
    return 1
  fi
}
attachK3sCluster() { attachK3dCluster "$@"; }

stopK3dCluster(){
  : "${K3D_CLUSTER_NAME:=enablement}"
  printInfoSection "Stopping K3d cluster ($K3D_CLUSTER_NAME)"
  k3d cluster stop "$K3D_CLUSTER_NAME" 2>/dev/null
  printInfo "K3d cluster '$K3D_CLUSTER_NAME' stopped."
}
stopK3sCluster() { stopK3dCluster "$@"; }

deleteK3dCluster(){
  : "${K3D_CLUSTER_NAME:=enablement}"
  printInfoSection "Deleting K3d cluster ($K3D_CLUSTER_NAME)"
  k3d cluster delete "$K3D_CLUSTER_NAME" 2>/dev/null
  printInfo "K3d cluster '$K3D_CLUSTER_NAME' deleted."
}
deleteK3sCluster() { deleteK3dCluster "$@"; }

# ======================================================================
#          ------- Unified Cluster Functions -------                     #
#  Routes to K3d or Kind based on CLUSTER_ENGINE variable.              #
# ======================================================================

startCluster(){
  # Starts the Kubernetes cluster based on CLUSTER_ENGINE (k3d or kind)
  if [[ "$CLUSTER_ENGINE" == "kind" ]]; then
    startKindCluster
  else
    startK3dCluster
  fi
}

stopCluster(){
  if [[ "$CLUSTER_ENGINE" == "kind" ]]; then
    stopKindCluster
  else
    stopK3dCluster
  fi
}

deleteCluster(){
  if [[ "$CLUSTER_ENGINE" == "kind" ]]; then
    deleteKindCluster
  else
    deleteK3dCluster
  fi
}

certmanagerInstall() {
  printInfoSection "Install CertManager $CERTMANAGER_VERSION"
  kubectl apply -f https://github.com/jetstack/cert-manager/releases/download/v$CERTMANAGER_VERSION/cert-manager.yaml
  # shellcheck disable=SC2119
  waitForAllPods cert-manager
}

certmanagerDelete(){
  kubectl delete -f https://github.com/jetstack/cert-manager/releases/download/v$CERTMANAGER_VERSION/cert-manager.yaml
}

generateRandomEmail() {
  echo "email-$RANDOM-$RANDOM@dynatrace.ai"
}

certmanagerEnable() {
  printInfoSection "Installing ClusterIssuer with HTTP Letsencrypt "

  if [ -n "$CERTMANAGER_EMAIL" ]; then
    printInfo "Creating ClusterIssuer for $CERTMANAGER_EMAIL"
    # Simplecheck to check if the email address is valid
    if [[ $CERTMANAGER_EMAIL == *"@"* ]]; then
      echo "Email address is valid! - $CERTMANAGER_EMAIL"
      EMAIL=$CERTMANAGER_EMAIL
    else
      echo "Email address $CERTMANAGER_EMAIL is not valid. Email will be generated"
      EMAIL=$(generateRandomEmail)
    fi
  else
    echo "Email not passed.  Email will be generated"
    EMAIL=$(generateRandomEmail)
  fi

  printInfo "EmailAccount for ClusterIssuer $EMAIL, creating ClusterIssuer"
  YAML_SRC="${FRAMEWORK_CACHE:-${REPO_PATH}}/.devcontainer/yaml"
  YAML_GEN="$REPO_PATH/.devcontainer/yaml/gen"
  mkdir -p "$YAML_GEN"
  cat "$YAML_SRC/clusterissuer.yaml" | sed 's~email.placeholder~'"$EMAIL"'~' > "$YAML_GEN/clusterissuer.yaml"

  kubectl apply -f "$YAML_GEN/clusterissuer.yaml"

  printInfo "Let's Encrypt Process in kubectl for CertManager"
  printInfo " For observing the creation of the certificates: \n
              kubectl describe clusterissuers.cert-manager.io -A
              kubectl describe issuers.cert-manager.io -A
              kubectl describe certificates.cert-manager.io -A
              kubectl describe certificaterequests.cert-manager.io -A
              kubectl describe challenges.acme.cert-manager.io -A
              kubectl describe orders.acme.cert-manager.io -A
              kubectl get events
              "

  waitForAllPods cert-manager
  # Not needed
  #bashas "cd $K8S_PLAY_DIR/cluster-setup/resources/ingress && bash add-ssl-certificates.sh"
}

# ======================================================================
#          ------- Environment Variable Management -------              #
#  Functions for validating, parsing and managing environment           #
#  variables. Source-agnostic: works with Codespaces secrets,           #
#  .env files, or exported env vars.                                    #
# ======================================================================

parseDynatraceEnvironment() {
  # Parses DT_ENVIRONMENT URL and derives DT_TENANT, DT_ENV_TYPE, DT_OTEL_ENDPOINT.
  # Pure function — no K8s dependency.
  # Usage: parseDynatraceEnvironment "https://abc123.apps.dynatrace.com"
  #   or:  parseDynatraceEnvironment  (reads from $DT_ENVIRONMENT)
  local env_url="${1:-$DT_ENVIRONMENT}"

  if [ -z "$env_url" ]; then
    printError "parseDynatraceEnvironment: No DT_ENVIRONMENT provided"
    return 1
  fi

  # Validate URL format
  if ! echo "$env_url" | grep -qE "^https://.+(\.dynatrace\.com|\.dynatracelabs\.com)"; then
    printError "Invalid DT_ENVIRONMENT: must start with https:// and contain dynatrace.com or dynatracelabs.com"
    printError "Example: https://abc123.apps.dynatrace.com or https://abc123.sprint.apps.dynatracelabs.com"
    return 1
  fi

  local tenant="$env_url"
  local env_type=""

  # Detect environment type and transform URL for API usage
  if echo "$env_url" | grep -q "\.apps\.dynatrace\.com"; then
    # Production: https://abc123.apps.dynatrace.com -> https://abc123.live.dynatrace.com
    env_type="prod"
    tenant=$(echo "$env_url" | sed 's/\.apps\.dynatrace\.com.*$/.live.dynatrace.com/')
    printInfo "Production environment detected — tenant for API: $tenant"

  elif echo "$env_url" | grep -q "\.sprint\.apps\.dynatracelabs\.com"; then
    # Sprint: https://abc123.sprint.apps.dynatracelabs.com -> https://abc123.sprint.dynatracelabs.com
    env_type="sprint"
    tenant=$(echo "$env_url" | sed 's/\.apps\.dynatracelabs\.com.*$/.dynatracelabs.com/')
    printInfo "Sprint environment detected — tenant for API: $tenant"

  elif echo "$env_url" | grep -q "\.dev\.apps\.dynatracelabs\.com"; then
    # Dev: https://abc123.dev.apps.dynatracelabs.com -> https://abc123.dev.dynatracelabs.com
    env_type="dev"
    tenant=$(echo "$env_url" | sed 's/\.apps\.dynatracelabs\.com.*$/.dynatracelabs.com/')
    printInfo "Dev environment detected — tenant for API: $tenant"

  elif echo "$env_url" | grep -q "\.apps\.dynatracelabs\.com"; then
    # Generic labs (sprint/dev without prefix): remove .apps.
    env_type="labs"
    tenant=$(echo "$env_url" | sed 's/\.apps\.dynatracelabs\.com.*$/.dynatracelabs.com/')
    printInfo "Labs environment detected — tenant for API: $tenant"

  else
    # Direct tenant URL (already in API format)
    env_type="custom"
    printInfo "Custom environment detected — using as-is: $tenant"
  fi

  # Clean trailing paths after .com
  tenant=$(echo "$tenant" | sed 's/\.com\/.*$/.com/')

  export DT_ENVIRONMENT="$env_url"
  export DT_TENANT="$tenant"
  export DT_ENV_TYPE="$env_type"
  export DT_OTEL_ENDPOINT="${tenant}/api/v2/otlp"

  return 0
}

variablesNeeded() {
  # Declarative environment variable validation.
  # Usage in post-create.sh:
  #   variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:false
  #
  # - VAR_NAME:true  → required, error if missing
  # - VAR_NAME:false → optional, warning if missing
  # - DT_*TOKEN vars are validated for Dynatrace token format (dt0c01.*/dt0s01.* classic,
  #   dt0s16.* platform/gen3, dt0g02.* ActiveGate/gen3)
  # - DT_ENVIRONMENT is validated and parsed (derives DT_TENANT, DT_OTEL_ENDPOINT)
  #
  # Returns 0 if all required vars pass, 1 if any required var is missing/invalid.

  local has_errors=0
  local missing_required=()
  local missing_optional=()

  for var_spec in "$@"; do
    local var_name="${var_spec%%:*}"
    local required="${var_spec##*:}"

    # Get the value via indirect expansion — suppress all output
    local var_value=""
    var_value="$(eval "printf '%s' \"\${$var_name}\"")" 2>/dev/null

    if [ -z "$var_value" ]; then
      if [ "$required" = "true" ]; then
        printError "$var_name is required but not set"
        missing_required+=("$var_name")
        has_errors=1
      else
        printWarn "$var_name is not set (optional)"
        missing_optional+=("$var_name")
      fi
      continue
    fi

    # Validate DT_ENVIRONMENT
    if [ "$var_name" = "DT_ENVIRONMENT" ]; then
      parseDynatraceEnvironment "$var_value"
      if [ $? -ne 0 ]; then
        has_errors=1
      fi
      continue
    fi

    # Validate Dynatrace tokens (DT_*TOKEN pattern)
    if [[ "$var_name" == DT_*TOKEN ]]; then
      if [[ "$var_value" == dt0c01.* || "$var_value" == dt0s01.* || "$var_value" == dt0s16.* || "$var_value" == dt0g02.* ]] && [ ${#var_value} -gt 60 ]; then
        # dt0c01/dt0s01 = classic API tokens; dt0s16 = platform token (gen3); dt0g02 = ActiveGate token (gen3).
        # Never echo the token value (or any prefix of it) — avoid leaking secrets to the console.
        printInfo "$var_name: valid Dynatrace token format"
      else
        printError "$var_name: invalid token format. Expected dt0c01.*/dt0s01.*/dt0s16.*/dt0g02.* with min 60 chars (length: ${#var_value})"
        if [ "$required" = "true" ]; then
          has_errors=1
        fi
      fi
      continue
    fi

    # Generic variable — just confirm it's set
    printInfo "$var_name is set"
  done

  # Summary
  if [ ${#missing_required[@]} -gt 0 ]; then
    printError "Missing required variables: ${missing_required[*]}"
    printError "Set them in your Codespaces secrets or in .devcontainer/.env file"
  fi

  if [ ${#missing_optional[@]} -gt 0 ]; then
    printWarn "Missing optional variables: ${missing_optional[*]}"
  fi

  return $has_errors
}

validateSaveCredentials() {
  # Validates Dynatrace credentials using variablesNeeded + parseDynatraceEnvironment.
  # Backward compatible: accepts 3 args (DT_ENVIRONMENT, DT_OPERATOR_TOKEN, DT_INGEST_TOKEN)
  # or reads from environment variables.

  if [[ $# -eq 3 ]]; then
    DT_ENVIRONMENT="$1"
    DT_OPERATOR_TOKEN="$2"
    DT_INGEST_TOKEN="$3"
  fi

  printInfo "Validating Dynatrace credentials"

  variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:false
  local validation_result=$?

  if [[ $validation_result -ne 0 ]]; then
    printError "Credential validation failed"
    return 1
  fi

  export DT_ENVIRONMENT DT_TENANT DT_OPERATOR_TOKEN DT_INGEST_TOKEN DT_OTEL_ENDPOINT
  return 0
}

verifyParseSecret(){
  # DEPRECATED: Use parseDynatraceEnvironment for URL parsing and variablesNeeded for token validation.
  # Kept for backward compatibility — delegates to parseDynatraceEnvironment for URLs
  # and validates token format directly.
  local secret="$1"
  local print_log="${2:-false}"

  if [ -z "$secret" ]; then
    printError "Function to validate secrets was called but no secret was provided" $print_log
    return 1
  fi

  # Check if it's a URL (tenant)
  if echo "$secret" | grep -qE "^https:"; then
    # Use parseDynatraceEnvironment for URL transformation
    parseDynatraceEnvironment "$secret" >/dev/null 2>&1
    if [[ $? -eq 0 ]]; then
      printInfo "Tenant URL valid for API requests: $DT_TENANT" $print_log
      if [ "${print_log}" = "false" ]; then
        echo "$DT_TENANT"
      fi
      return 0
    else
      printError "Invalid tenant URL: $secret" $print_log
      return 1
    fi
  fi

  # Check if it's a token (dt0c01/dt0s01 classic; dt0s16 platform; dt0g02 ActiveGate)
  if [[ "$secret" == dt0c01.* || "$secret" == dt0s01.* || "$secret" == dt0s16.* || "$secret" == dt0g02.* ]] && [ ${#secret} -gt 60 ]; then
    printInfo "Valid Dynatrace Token format" $print_log
    if [ "${print_log}" = "false" ]; then
      echo "$secret"
    fi
    return 0
  fi

  printError "Invalid secret, this is not a valid Dynatrace tenant nor token: $secret" $print_log
  return 1
}

dynatraceEvalReadSaveCredentials() {
  # Evaluates, validates, and exports Dynatrace credentials.
  # Source: 1. Function arguments  2. Environment variables (from .env or Codespaces secrets)
  printInfoSection "Evaluating Dynatrace credentials"

  if [ "${DT_EVAL_SECRETS}" = "true" ]; then
    printInfo "Dynatrace secrets already evaluated. To re-evaluate: unset DT_EVAL_SECRETS"
    printInfo "To print secrets: printSecrets"
    return 0
  fi

  # Source 1: Function arguments
  if [[ $# -eq 3 ]]; then
    DT_ENVIRONMENT="$1"
    DT_OPERATOR_TOKEN="$2"
    DT_INGEST_TOKEN="$3"
    printInfo "Credentials passed as arguments"
  # Source 2: Environment variables (from .env file or Codespaces secrets)
  elif [[ -n "${DT_ENVIRONMENT}" ]]; then
    printInfo "Credentials found in environment variables"
    if [ -z "$DT_OPERATOR_TOKEN" ]; then
      printWarn "DT_OPERATOR_TOKEN is not set"
    fi
    if [ -z "$DT_INGEST_TOKEN" ]; then
      printWarn "DT_INGEST_TOKEN is not set"
    fi
  else
    printWarn "No Dynatrace credentials found. Set them as environment variables, in .devcontainer/.env, or as Codespaces secrets."
    unset DT_EVAL_SECRETS
    return 1
  fi

  # Validate and parse
  validateSaveCredentials "$DT_ENVIRONMENT" "$DT_OPERATOR_TOKEN" "$DT_INGEST_TOKEN"
  if [[ $? -ne 0 ]]; then
    unset DT_EVAL_SECRETS
    return 1
  fi

  export DT_EVAL_SECRETS=true
  printSecrets
  return 0
}

printSecrets(){
    printInfo "Dynatrace Environment: $DT_ENVIRONMENT"
    printInfo "Dynatrace Tenant (for API): $DT_TENANT"
    printInfo "Dynatrace Env Type: $DT_ENV_TYPE"
    printInfo "Dynatrace API & PaaS Token: ${DT_OPERATOR_TOKEN:0:14}xxx..."
    printInfo "Dynatrace Ingest Token: ${DT_INGEST_TOKEN:0:14}xxx..."
    printInfo "Dynatrace Otel Endpoint: $DT_OTEL_ENDPOINT"
}

# ── Sprint ActiveGate image workaround ──────────────────────────────────────────
# Sprint tenants resolve the ActiveGate image to a private build ECR
# (478983378254.dkr.ecr.us-east-1.amazonaws.com) that a public k3d cluster cannot
# authenticate to, so the AG pod stays in ImagePullBackOff and the DynaKube never
# finishes. Prod/gen2 tenants serve the AG from their own tenant registry and are
# unaffected. On sprint we swap the AG image to the latest PUBLIC ActiveGate build so
# the many internal/onboarding trainings that run on sprint still work.

isSprintTenant() {
  # Returns 0 if the tenant URL is a sprint tenant. $1 defaults to $DT_ENVIRONMENT.
  local url="${1:-$DT_ENVIRONMENT}"
  case "$url" in
    *sprint.apps.dynatracelabs.com*|*.sprint.dynatracelabs.com*) return 0 ;;
    *) return 1 ;;
  esac
}

_pickLatestActiveGateTag() {
  # Read a newline-delimited tag list on stdin; echo the newest clean version tag
  # (form N.N.N.N-N). Ignores signature/attestation (.sig/.att) and -fips/-raw variants.
  grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$' | sort -V | tail -1
}

latestPublicActiveGateImage() {
  # Echo the newest pullable image ref on public.ecr.aws/dynatrace/dynatrace-activegate,
  # or return non-zero if it can't be resolved (offline / API change).
  local repo="public.ecr.aws/dynatrace/dynatrace-activegate" token tag
  token="$(curl -fsS https://public.ecr.aws/token/ 2>/dev/null | jq -r '.token // empty')" || return 1
  [ -n "$token" ] || return 1
  tag="$(curl -fsS -H "Authorization: Bearer $token" \
        "https://public.ecr.aws/v2/dynatrace/dynatrace-activegate/tags/list" 2>/dev/null \
        | jq -r '.tags[]?' | _pickLatestActiveGateTag)"
  [ -n "$tag" ] || return 1
  echo "${repo}:${tag}"
}

_pickLatestCodeModulesTag() {
  # Read a newline-delimited tag list on stdin; echo the newest clean version tag
  # (form N.N.N.N-N).
  #
  # Deliberately its own picker rather than _pickLatestActiveGateTag: the
  # codemodules repository ALSO publishes single-language variants
  # (-java, -python, -nodejs, -php) alongside each multi-arch tag. They sort
  # adjacent to it, and selecting one would instrument only that runtime while
  # looking completely healthy. The anchored pattern is what excludes them.
  grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$' | sort -V | tail -1
}

latestPublicCodeModulesImage() {
  # Echo the newest pullable image ref on public.ecr.aws/dynatrace/dynatrace-codemodules,
  # or return non-zero if it can't be resolved (offline / API change).
  local repo="public.ecr.aws/dynatrace/dynatrace-codemodules" token tag
  token="$(curl -fsS https://public.ecr.aws/token/ 2>/dev/null | jq -r '.token // empty')" || return 1
  [ -n "$token" ] || return 1
  tag="$(curl -fsS -H "Authorization: Bearer $token" \
        "https://public.ecr.aws/v2/dynatrace/dynatrace-codemodules/tags/list" 2>/dev/null \
        | jq -r '.tags[]?' | _pickLatestCodeModulesTag)"
  [ -n "$tag" ] || return 1
  echo "${repo}:${tag}"
}

fixSprintActiveGateImage() {
  # Post-apply fallback. generateDynakube already pins the public AG image on sprint,
  # so this is a safety net for DynaKubes applied from outside the generator.
  # No-op unless the tenant is sprint. On sprint: warn and patch the deployed DynaKube's
  # spec.activeGate.image to the latest public build (the operator then recreates the AG
  # pod with a pullable image). Best-effort — never fails the deploy.
  isSprintTenant || return 0
  printWarn "ActiveGate images by default on Sprint won't be found, hence downgrading to the latest available ActiveGate image."
  local img dk
  if ! img="$(latestPublicActiveGateImage)"; then
    printWarn "Could not resolve a public ActiveGate image — leaving the default."
    return 0
  fi
  dk="$(kubectl get dynakube -n dynatrace --no-headers 2>/dev/null | awk '{print $1}' | head -1)"
  [ -n "$dk" ] || return 0
  printInfo "Pinning ActiveGate image to $img"
  kubectl -n dynatrace patch dynakube "$dk" --type=merge \
    -p "{\"spec\":{\"activeGate\":{\"image\":\"$img\"}}}" >/dev/null 2>&1
}

fixSprintCodeModulesImage() {
  # Post-apply fallback, mirroring fixSprintActiveGateImage. generateDynakube already
  # pins the public codemodules image on sprint, so this is a safety net for DynaKubes
  # applied from outside the generator.
  #
  # WHY: on sprint the DynaKube resolves its codemodules image from the tenant's PRIVATE
  # registry. No paasToken is issued for these sessions and no <dynakube>-pull-secret is
  # created, so the CSI provisioner's pull is anonymous and answers
  #   401 Unauthorized: Not Authorized
  # Nothing is extracted -- /data/codemodules stays completely empty -- and because
  # installAgentFromImage() swallows the pull failure and returns nil, the only error
  # ever raised is the downstream
  #   open /data/codemodules/<version>/agent/bin: no such file or directory
  # Every oneagent-bin mount is refused from then on and the learner's pod sits in Init
  # forever. Best-effort throughout — never fails the deploy.
  isSprintTenant || return 0
  local dk current img
  dk="$(kubectl get dynakube -n dynatrace --no-headers 2>/dev/null | awk '{print $1}' | head -1)"
  [ -n "$dk" ] || return 0
  # Already pinned by generateDynakube? Say so rather than patching a second time.
  current="$(kubectl -n dynatrace get dynakube "$dk" \
    -o jsonpath='{.spec.oneAgent.applicationMonitoring.codeModulesImage}{.spec.oneAgent.cloudNativeFullStack.codeModulesImage}' 2>/dev/null)"
  case "$current" in
    public.ecr.aws/*) printInfo "codeModulesImage already pinned to $current"; return 0 ;;
  esac
  if ! img="$(latestPublicCodeModulesImage)"; then
    printWarn "Could not resolve a public codemodules image — leaving the default."
    return 0
  fi
  printWarn "Sprint codemodules images are not pullable (401 from the tenant's private registry) — pinning the latest public build."
  printInfo "Pinning codeModulesImage to $img"
  # Patch whichever mode this DynaKube uses; the absent one is simply not present.
  if kubectl -n dynatrace get dynakube "$dk" -o jsonpath='{.spec.oneAgent.applicationMonitoring}' 2>/dev/null | grep -q .; then
    kubectl -n dynatrace patch dynakube "$dk" --type=merge \
      -p "{\"spec\":{\"oneAgent\":{\"applicationMonitoring\":{\"codeModulesImage\":\"$img\"}}}}" >/dev/null 2>&1 \
      || printWarn "Could not patch the DynaKube (continuing)"
  elif kubectl -n dynatrace get dynakube "$dk" -o jsonpath='{.spec.oneAgent.cloudNativeFullStack}' 2>/dev/null | grep -q .; then
    kubectl -n dynatrace patch dynakube "$dk" --type=merge \
      -p "{\"spec\":{\"oneAgent\":{\"cloudNativeFullStack\":{\"codeModulesImage\":\"$img\"}}}}" >/dev/null 2>&1 \
      || printWarn "Could not patch the DynaKube (continuing)"
  fi
  return 0
}

deployCloudNative() {
  # Warn if running on k3d — OneAgent DaemonSet will CrashLoopBackOff on container-based nodes
  if [[ "${CLUSTER_ENGINE:-k3d}" != "kind" ]]; then
    printWarn "═══════════════════════════════════════════════════════════════════════════════"
    printWarn " CloudNativeFullStack on K3d: OneAgent DaemonSet will NOT start properly."
    printWarn " K3d nodes are Docker containers — OneAgent host init fails inside them."
    printWarn " Application monitoring (code injection) will still work via CSI driver."
    printWarn ""
    printWarn " To get full OneAgent host monitoring, switch to Kind:"
    printWarn "   export CLUSTER_ENGINE=kind"
    printWarn "   # then re-create the cluster and redeploy:"
    printWarn "   deleteCluster && startCluster && dynatraceDeployOperator && deployCloudNative"
    printWarn ""
    printWarn " Or use application-only mode (no CrashLoopBackOff, full app observability):"
    printWarn "   deployApplicationMonitoring"
    printWarn "═══════════════════════════════════════════════════════════════════════════════"
  elif [[ "$(detectRunEnvironment)" == "orbital" ]]; then
    # Kind on Orbital: cluster runs inside Sysbox but OneAgent host module will fail
    printWarn "═══════════════════════════════════════════════════════════════════════════════"
    printWarn " CloudNativeFullStack on Orbital (Sysbox + Kind): OneAgent DaemonSet will"
    printWarn " CrashLoopBackOff. Sysbox restricts host-level syscalls needed by OneAgent's"
    printWarn " host monitoring module. Code-injection (CSI driver) still works."
    printWarn " This environment is flagged as 'orbital' in the tracker payload."
    printWarn "═══════════════════════════════════════════════════════════════════════════════"
  fi
  deployDynatrace cloudnative "$@"
}

deployApplicationMonitoring() {
  # Convenience wrapper — deploys DT in ApplicationMonitoring mode
  deployDynatrace apponly "$@"
}

deployDynatrace() {
  # Unified Dynatrace deployment function.
  # Usage: deployDynatrace [mode] [DT_ENVIRONMENT DT_OPERATOR_TOKEN DT_INGEST_TOKEN]
  #   mode: apponly | cloudnative | k8s-only
  #   No mode (or "") follows `mode:` from dynakube-defaults.yaml / the repo's
  #   dynakube-config.yaml — apponly by default. CloudNativeFullStack is opt-in:
  #   its OneAgent DaemonSet cannot start on k3d or under Sysbox (see deployCloudNative).
  loadDynakubeConfig
  local mode="${1:-${DK_MODE:-apponly}}"
  shift 2>/dev/null

  dynatraceEvalReadSaveCredentials "$@"

  if [ -z "${DT_TENANT}" ]; then
    printWarn "Not deploying Dynatrace — no credentials found"
    return 1
  fi

  # Generate the Dynakube YAML from config
  generateDynakube "$mode"

  # Wait for the webhook to be ready (operator must be deployed first)
  kubectl -n dynatrace wait pod --for=condition=ready \
    --selector=app.kubernetes.io/name=dynatrace-operator,app.kubernetes.io/component=webhook \
    --timeout=300s

  # Apply the generated Dynakube
  local gen_file="$REPO_PATH/.devcontainer/yaml/gen/dynakube.yaml"
  if [ ! -f "$gen_file" ]; then
    printError "Generated Dynakube not found at $gen_file"
    return 1
  fi

  kubectl -n dynatrace apply -f "$gen_file"

  # Sprint tenants ship an unpullable private-ECR ActiveGate image — swap it for the
  # latest public build so the AG can actually start (no-op on prod/gen2 tenants).
  fixSprintActiveGateImage

  # Same story one image over: the codemodules image is also a private-ECR build on
  # sprint, and an unpullable one leaves every injected pod stuck in Init.
  fixSprintCodeModulesImage

  # Wait for ActiveGate to be ready (the critical component for cluster monitoring)
  waitForPod dynatrace activegate

  # CloudNativeFullStack OneAgent DaemonSet may crash on container-based nodes (Kind, K3d)
  # because the host init procedure fails inside Docker containers.
  # Application monitoring via CSI code injection still works regardless.
  if [[ "$mode" == "cloudnative" ]]; then
    printInfo "Waiting for ActiveGate to be ready (OneAgent may take longer on container nodes)..."
    # Don't block on OneAgent — it may CrashLoop on container nodes
    sleep 10
  else
    waitForAllReadyPods dynatrace
  fi

  printInfo "Dynatrace deployed in $mode mode"
}

undeployDynakubes() {
    printInfoSection "Undeploying Dynakubes, OneAgent installation from Workernode if installed"

    kubectl -n dynatrace delete dynakube --all
    #FIXME: Test uninstalling Dynatracem good when changing monitoring modes. 
    #kubectl -n dynatrace wait pod --for=condition=delete --selector=app.kubernetes.io/name=oneagent,app.kubernetes.io/managed-by=dynatrace-operator --timeout=300s
    sudo bash /opt/dynatrace/oneagent/agent/uninstall.sh 2>/dev/null
}

uninstallDynatrace() {
    echo "Uninstalling Dynatrace"
    undeployDynakubes

    echo "Uninstalling Dynatrace"
    helm uninstall dynatrace-operator -n dynatrace

    kubectl delete namespace dynatrace
}

# shellcheck disable=SC2120
dynatraceDeployOperator() {
  # Deploys the Dynatrace Operator via Helm and generates the Dynakube.
  # Usage: dynatraceDeployOperator [DT_ENVIRONMENT DT_OPERATOR_TOKEN DT_INGEST_TOKEN]

  # Load operator version from dynakube config
  loadDynakubeConfig
  local operator_version="${DK_OPERATOR_VERSION:-1.8.1}"

  printInfoSection "Deploying Dynatrace Operator v${operator_version}"

  dynatraceEvalReadSaveCredentials "$@"

  if [ -z "${DT_TENANT}" ]; then
    printWarn "Not deploying the Dynatrace Operator — no credentials found"
    return 1
  fi

  # Deploy Operator via Helm
  helm install dynatrace-operator oci://public.ecr.aws/dynatrace/dynatrace-operator \
    --version "$operator_version" \
    --create-namespace --namespace dynatrace --atomic

  # Create the secret for Dynakube to use. Name must be lowercase (k8s RFC-1123)
  # and match the lowercased Dynakube name derived in generateDynakube.
  local dk_secret_name="$(echo "$RepositoryName" | tr '[:upper:]' '[:lower:]')"
  # Platform-token sessions (gen3 tenants): the operator token is a dt0s16 and
  # cannot self-mint the ActiveGate's token, so Orbital pre-mints one and hands
  # it over as DT_ACTIVEGATE_TOKEN — wire it into the secret (the operator
  # picks up the activeGateAuthToken key automatically). Without it the AG pod
  # never reaches Running on platform-token sessions.
  local ag_literal=()
  if [ -n "${DT_ACTIVEGATE_TOKEN}" ]; then
    ag_literal=(--from-literal="activeGateAuthToken=$DT_ACTIVEGATE_TOKEN")
  fi
  kubectl -n dynatrace create secret generic "$dk_secret_name" \
    --from-literal="apiToken=$DT_OPERATOR_TOKEN" \
    --from-literal="dataIngestToken=$DT_INGEST_TOKEN" \
    "${ag_literal[@]}" \
    2>/dev/null || true

  waitForAllPods dynatrace
}

getLatestEcrTag() {
  # Resolves the latest version tag from ECR public gallery for a Dynatrace image.
  # Usage: getLatestEcrTag <repository-name>
  # Example: getLatestEcrTag "dynatrace-k8s-node-config-collector" → "1.5.8"
  # Returns the highest semver tag (multi-arch, no -fips/-arm64/-s390x suffixes).
  local repo="$1"
  local tag=""

  # Get anonymous auth token
  local token
  token=$(curl -s --max-time 10 "https://public.ecr.aws/token/" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)

  if [[ -n "$token" ]]; then
    tag=$(curl -s --max-time 10 -H "Authorization: Bearer $token" \
      "https://public.ecr.aws/v2/dynatrace/${repo}/tags/list" | \
      python3 -c "
import sys, json, re
try:
    tags = json.load(sys.stdin).get('tags', [])
    # Multi-arch version tags only: X.Y.Z or X.Y.Z.TIMESTAMP, no arch/fips/platform suffixes
    version_tags = [t for t in tags if re.match(r'^[0-9]+\.[0-9]+', t) and not re.search(r'-(fips|arm|s390|amd|ppc)', t)]
    version_tags.sort(key=lambda v: [int(x) for x in re.findall(r'\d+', v)])
    print(version_tags[-1] if version_tags else '')
except:
    print('')
" 2>/dev/null)
  fi

  if [[ -z "$tag" ]]; then
    printWarn "Could not resolve latest tag for $repo from ECR — check network"
    echo "latest"
  else
    echo "$tag"
  fi
}

loadDynakubeConfig() {
  # Loads Dynakube configuration: defaults first, then repo overrides on top.
  # Defaults:    .devcontainer/yaml/dynakube-defaults.yaml (synced from framework)
  # Overrides:   .devcontainer/yaml/dynakube-config.yaml (repo-specific, never synced)
  # Values are exported as DK_* variables. Repo overrides win.
  local defaults_file="${FRAMEWORK_CACHE:-${REPO_PATH}}/.devcontainer/yaml/dynakube-defaults.yaml"
  local config_file="$REPO_PATH/.devcontainer/yaml/dynakube-config.yaml"

  # Step 1: Load defaults
  if [[ -f "$defaults_file" ]]; then
    _parseDynakubeYaml "$defaults_file"
  else
    printWarn "Dynakube defaults not found at $defaults_file"
  fi

  # Step 2: Overlay repo-specific overrides (only specified keys are overwritten)
  if [[ -f "$config_file" ]]; then
    printInfo "Applying repo-specific Dynakube overrides from: $config_file"
    _parseDynakubeYaml "$config_file"
  else
    printInfo "No repo-specific Dynakube config — using defaults"
  fi
}

_parseDynakubeYaml() {
  # Parses a flat YAML file into DK_* exported variables.
  local file="$1"
  while IFS=': ' read -r key value; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    # Remove quotes and leading spaces
    value=$(echo "$value" | sed 's/^["'\'']*//;s/["'\'']*$//' | xargs)
    [[ -z "$value" ]] && continue
    local var_name="DK_$(echo "$key" | tr '[:lower:]' '[:upper:]')"
    eval "export $var_name=\"$value\""
  done < "$file"
}

loadTokenConfig() {
  # Loads token migration configuration: framework defaults first, repo overrides on top.
  # Defaults:  .devcontainer/yaml/dt-tokens.yaml  (fetched from framework cache)
  # Overrides: .devcontainer/yaml/dt-tokens.yaml  (repo-specific, never synced to framework)
  # Top-level scalar fields are exported as TK_* variables. Repo overrides win.
  local defaults_file="${FRAMEWORK_CACHE:-${REPO_PATH}}/.devcontainer/yaml/dt-tokens.yaml"
  local config_file="$REPO_PATH/.devcontainer/yaml/dt-tokens.yaml"

  # Step 1: Load framework defaults
  if [[ -f "$defaults_file" ]]; then
    _parseTokenScalars "$defaults_file"
  else
    printWarn "Token defaults not found at $defaults_file"
  fi

  # Step 2: Overlay repo-specific overrides (only top-level scalars are overwritten)
  if [[ "$config_file" != "$defaults_file" && -f "$config_file" ]]; then
    printInfo "Applying repo-specific token overrides from: $config_file"
    _parseTokenScalars "$config_file"
  else
    printInfo "No repo-specific token config — using framework defaults"
  fi
}

_parseTokenScalars() {
  # Parses top-level scalar fields from dt-tokens.yaml into TK_* exported variables.
  # Stops at `tokens:` to avoid ingesting nested token definitions.
  # camelCase keys are converted to UPPER_SNAKE_CASE: migrationStatus -> TK_MIGRATION_STATUS.
  local file="$1"
  while IFS=': ' read -r key value; do
    [[ "$key" == "tokens" ]] && break
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    value=$(echo "$value" | sed 's/^["'\'']*//;s/["'\'']*$//' | xargs)
    [[ -z "$value" ]] && continue
    local var_name="TK_$(echo "$key" | sed 's/\([A-Z]\)/_\1/g' | tr '[:lower:]' '[:upper:]')"
    eval "export $var_name=\"$value\""
  done < "$file"
}

getDtSessionId() {
  # Per-user session identity: "<user>-<yyyymmdd>". Makes the DynaKube name and
  # hostGroup unique per user+day, so many students can run the SAME training
  # against ONE tenant and each can filter Grail (logs/pods/apps) to their own
  # cluster. Priority: DT_HOSTGROUP (injected by Orbital per job) >
  # GITHUB_USER-<date> (Codespaces) > empty (local/unknown: repo-scoped identity,
  # pre-1.9 behavior).
  local raw="${DT_HOSTGROUP:-}"
  if [[ -z "$raw" && -n "${GITHUB_USER:-}" ]]; then
    raw="${GITHUB_USER}-$(date +%Y%m%d)"
  fi
  [[ -z "$raw" ]] && return 0
  # Sanitize to RFC-1123: lowercase alphanumerics and dashes only
  echo "$raw" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//'
}

generateDynakube() {
  # Generates a Dynakube YAML from config into .devcontainer/yaml/gen/dynakube.yaml.
  # Usage: generateDynakube [mode]
  #   mode overrides the config file's mode setting
  local mode_override="$1"

  printInfoSection "Generating Dynakube"

  # Load config
  loadDynakubeConfig

  local mode="${mode_override:-${DK_MODE:-apponly}}"
  local api_version="${DK_DYNAKUBE_API_VERSION:-v1beta6}"
  # Kubernetes resource names must be lowercase RFC-1123. Repo names can be
  # mixed-case (e.g. Enablement-DTWiz-101), which the DynaKube validating webhook
  # rejects — silently breaking OneAgent injection. Lowercase for the Dynakube name.
  local base_name="$(echo "${RepositoryName:-$(hostname)}" | tr '[:upper:]' '[:lower:]')"
  # Every training repo is called "enablement-<something>", so those 11 characters
  # carry no information while eating the name budget below (they used to leave
  # "e-" or "enablement-kuber" as the visible half). Strip the constant prefix for
  # the DynaKube/cluster name only — networkZone below keeps the full repo name.
  local short_name="${base_name#enablement-}"
  local cluster_name="$base_name"
  local session_id="$(getDtSessionId)"
  # The operator's validating webhook enforces a HARD DynaKube name limit
  # (child resources like <name>-activegate-<hash> must fit the 63-char k8s
  # label limit). Operator >= 1.10 tightens it per enabled feature:
  # base 38, telemetryIngest 37 (-otel-collector), KSPM 35, extensions 31.
  local name_cap=38
  [[ "${DK_TELEMETRY_INGEST:-false}" == "true" && name_cap -gt 37 ]] && name_cap=37
  [[ "${DK_KSPM:-false}" == "true" && name_cap -gt 35 ]] && name_cap=35
  [[ "${DK_EXTENSIONS:-false}" == "true" && name_cap -gt 31 ]] && name_cap=31
  if [[ -n "$session_id" ]]; then
    # The session id MUST survive whole: every lab filters Grail with
    # `endsWith(k8s.cluster.name, "{{DT_SESSION_ID}}")`, so a truncated tail
    # silently returns zero records instead of the learner's own data.
    # Sacrifice repo chars — and never truncate the composed name afterwards.
    # Orbital bounds the id to 26 chars (HOSTGROUP_LOCAL_MAX=17 + '-' + date),
    # which leaves >= 10 repo chars at the fleet cap of 37.
    if (( ${#session_id} > name_cap )); then
      # Cannot be satisfied at all — say so instead of shipping a cut id that
      # makes every Grail check in the training return nothing, silently.
      printWarn "Session id '$session_id' (${#session_id} chars) does not fit the DynaKube name cap ($name_cap)"
      printWarn "Grail per-user filters (endsWith k8s.cluster.name) will NOT match — falling back to a repo-scoped cluster name"
      cluster_name="${base_name:0:$name_cap}"
      cluster_name="${cluster_name%-}"
    else
      local name_budget=$(( name_cap - ${#session_id} - 1 ))
      local repo_part=""
      if (( name_budget > 0 )); then
        repo_part="${short_name:0:$name_budget}"
        repo_part="${repo_part%-}"
      fi
      # No trailing truncation here: repo_part + '-' + session_id is <= name_cap
      # by construction, so the id always survives intact. A budget of 0 drops
      # the repo half entirely — the id is what has to survive.
      cluster_name="${repo_part:+${repo_part}-}${session_id}"
    fi
  else
    cluster_name="${cluster_name:0:$name_cap}"
    cluster_name="${cluster_name%-}"
  fi
  local api_url="${DT_TENANT}/api"

  local gen_dir="$REPO_PATH/.devcontainer/yaml/gen"
  mkdir -p "$gen_dir"
  local gen_file="$gen_dir/dynakube.yaml"

  printInfo "Mode: $mode | API: $api_url | Cluster: $cluster_name"

  # AG resources from config (Kind-optimized defaults)
  local ag_cpu_req="${DK_AG_CPU_REQUEST:-100m}"
  local ag_cpu_lim="${DK_AG_CPU_LIMIT:-500m}"
  local ag_mem_req="${DK_AG_MEMORY_REQUEST:-512Mi}"
  local ag_mem_lim="${DK_AG_MEMORY_LIMIT:-768Mi}"
  local ag_replicas="${DK_AG_REPLICAS:-1}"

  # Feature flags
  local kspm="${DK_KSPM:-false}"
  local log_mon="${DK_LOG_MONITORING:-true}"
  local telemetry="${DK_TELEMETRY_INGEST:-false}"
  local extensions="${DK_EXTENSIONS:-false}"
  local sensitive="${DK_SENSITIVE_DATA:-false}"

  # AG capability flags from config (independent of mode)
  local routing="${DK_ROUTING:-true}"
  local debugging="${DK_DEBUGGING:-true}"
  local dynatrace_api="${DK_DYNATRACE_API:-true}"

  # Build AG capabilities list
  local ag_capabilities="      - kubernetes-monitoring"

  if [[ "$routing" == "true" ]]; then
    ag_capabilities="${ag_capabilities}
      - routing"
  fi

  if [[ "$debugging" == "true" ]]; then
    ag_capabilities="${ag_capabilities}
      - debugging"
  fi

  if [[ "$dynatrace_api" == "true" ]]; then
    ag_capabilities="${ag_capabilities}
      - dynatrace-api"
  fi

  if [[ "$telemetry" == "true" ]]; then
    ag_capabilities="${ag_capabilities}
      - metrics-ingest"
  fi

  # ARM image overrides — resolve latest from ECR
  local ag_image_line=""
  local oa_image_line=""
  if [[ "$ARCH" == *"arm"* || "$ARCH" == *"aarch64"* ]]; then
    printInfo "ARM architecture detected — resolving latest AG and OA images from ECR..."
    local ag_tag
    ag_tag=$(getLatestEcrTag "dynatrace-activegate")
    local oa_tag
    oa_tag=$(getLatestEcrTag "dynatrace-oneagent")
    # NOTE: indentation is added by the template expansions below
    # (${ag_image_line:+    ...} and ${oa_image_line:+      ...}).
    # Putting spaces here too would double-indent and slot the line
    # under the previous list item, breaking YAML parsing.
    ag_image_line="image: \"public.ecr.aws/dynatrace/dynatrace-activegate:${ag_tag}\""
    oa_image_line="image: \"public.ecr.aws/dynatrace/dynatrace-oneagent:${oa_tag}\""
    printInfo "ActiveGate image: public.ecr.aws/dynatrace/dynatrace-activegate:${ag_tag}"
    printInfo "OneAgent image: public.ecr.aws/dynatrace/dynatrace-oneagent:${oa_tag}"
  fi

  # Sprint tenants resolve the ActiveGate to a private-ECR build that a k3d/kind
  # cluster cannot pull. Pin the AG to the latest PUBLIC build at GENERATION time so
  # the manifest is pullable the moment it is applied — including when a lab step
  # applies it by hand, where deployDynatrace's post-apply fixSprintActiveGateImage
  # never runs. No-op on prod/gen2 tenants. ARM already resolved an image above.
  if [[ -z "$ag_image_line" ]] && isSprintTenant "${DT_ENVIRONMENT:-$DT_TENANT}"; then
    local sprint_ag_img
    if sprint_ag_img="$(latestPublicActiveGateImage)"; then
      ag_image_line="image: \"${sprint_ag_img}\""
      printWarn "Sprint tenant — default ActiveGate image is not pullable, pinning to ${sprint_ag_img}"
    else
      printWarn "Sprint tenant but no public ActiveGate image resolved — AG may ImagePullBackOff."
    fi
  fi

  # Same story one image over. On sprint the codemodules image also resolves to a
  # private-ECR build, and no pull secret is issued for these sessions, so the CSI
  # provisioner's pull answers 401 and NOTHING is extracted. The only error raised is
  # the downstream "open /data/codemodules/<version>/agent/bin: no such file or
  # directory", every oneagent-bin mount is refused, and injected pods sit in Init
  # forever. Pin at GENERATION time so the manifest is pullable the moment it is
  # applied — including the documented `kubectl apply -f .../gen/dynakube.yaml` path,
  # where the post-apply fixSprintCodeModulesImage never runs. No-op on prod/gen2.
  local cm_image_line=""
  if isSprintTenant "${DT_ENVIRONMENT:-$DT_TENANT}"; then
    local sprint_cm_img
    if sprint_cm_img="$(latestPublicCodeModulesImage)"; then
      cm_image_line="codeModulesImage: \"${sprint_cm_img}\""
      printWarn "Sprint tenant — default codemodules image is not pullable, pinning to ${sprint_cm_img}"
    else
      printWarn "Sprint tenant but no public codemodules image resolved — injected pods may hang in Init."
    fi
  fi

  # --- Build the Dynakube YAML ---

  cat > "$gen_file" <<DKEOF
# Generated by the Dynatrace Enablement Framework — do not edit manually.
# Regenerate with: generateDynakube $mode
# Config: ${DK_DYNAKUBE_API_VERSION:-v1beta6} | Mode: $mode | Cluster: $cluster_name
---
apiVersion: v1
kind: Secret
metadata:
  name: ${cluster_name}
  namespace: dynatrace
data:
  apiToken: $(printf '%s' "$DT_OPERATOR_TOKEN" | base64 -w0)
  dataIngestToken: $(printf '%s' "$DT_INGEST_TOKEN" | base64 -w0)
type: Opaque
---
apiVersion: dynatrace.com/${api_version}
kind: DynaKube
metadata:
  name: ${cluster_name}
  namespace: dynatrace
spec:
  apiUrl: ${api_url}
  tokens: ${cluster_name}
  networkZone: ${base_name}
  skipCertCheck: true
  metadataEnrichment:
    enabled: true
  activeGate:
    capabilities:
${ag_capabilities}
${ag_image_line:+    ${ag_image_line}}
    replicas: ${ag_replicas}
    resources:
      requests:
        cpu: ${ag_cpu_req}
        memory: ${ag_mem_req}
      limits:
        cpu: ${ag_cpu_lim}
        memory: ${ag_mem_lim}
DKEOF

  # --- OneAgent section (mode-dependent) ---
  # Normalize mode aliases
  case "$mode" in
    app-only) mode="apponly" ;;
    cloud-native|cnfs) mode="cloudnative" ;;
  esac

  if [[ "$mode" == "cloudnative" ]]; then
    cat >> "$gen_file" <<CNFSEOF
  oneAgent:
    hostGroup: ${cluster_name}
    cloudNativeFullStack:
${oa_image_line:+      ${oa_image_line}}
${cm_image_line:+      ${cm_image_line}}
      tolerations:
        - effect: NoSchedule
          key: node-role.kubernetes.io/master
          operator: Exists
        - effect: NoSchedule
          key: node-role.kubernetes.io/control-plane
          operator: Exists
      env:
        - name: ONEAGENT_ENABLE_VOLUME_STORAGE
          value: "true"
CNFSEOF
  elif [[ "$mode" == "apponly" ]]; then
    # `{}` when there is nothing to pin: an empty mapping and a mapping with one key
    # are different YAML, so this cannot be collapsed into one heredoc.
    if [[ -n "$cm_image_line" ]]; then
      cat >> "$gen_file" <<AOEOF
  oneAgent:
    applicationMonitoring:
      ${cm_image_line}
AOEOF
    else
      cat >> "$gen_file" <<AOEOF
  oneAgent:
    applicationMonitoring: {}
AOEOF
    fi
  fi
  # k8s-only mode: no oneAgent section

  # --- Templates section (image refs required by operator validation) ---
  # Resolve latest version tags from ECR public gallery
  local has_templates=false
  local templates_block=""

  if [[ "$kspm" == "true" ]]; then
    local kspm_tag
    kspm_tag=$(getLatestEcrTag "dynatrace-k8s-node-config-collector")
    has_templates=true
    templates_block="${templates_block}
    kspmNodeConfigurationCollector:
      imageRef:
        repository: public.ecr.aws/dynatrace/dynatrace-k8s-node-config-collector
        tag: \"${kspm_tag}\""
    printInfo "KSPM node-config-collector: ${kspm_tag}"
  fi

  if [[ "$log_mon" == "true" ]]; then
    local logmod_tag
    logmod_tag=$(getLatestEcrTag "dynatrace-logmodule")
    has_templates=true
    templates_block="${templates_block}
    logMonitoring:
      imageRef:
        repository: public.ecr.aws/dynatrace/dynatrace-logmodule
        tag: \"${logmod_tag}\""
    printInfo "Log module: ${logmod_tag}"
  fi

  if [[ "$extensions" == "true" ]]; then
    local eec_tag
    eec_tag=$(getLatestEcrTag "dynatrace-eec")
    has_templates=true
    templates_block="${templates_block}
    extensionExecutionController:
      imageRef:
        repository: public.ecr.aws/dynatrace/dynatrace-eec
        tag: \"${eec_tag}\""
    printInfo "Extension Execution Controller: ${eec_tag}"
  fi

  if [[ "$telemetry" == "true" || "$extensions" == "true" ]]; then
    local otelcol_tag
    otelcol_tag=$(getLatestEcrTag "dynatrace-otel-collector")
    has_templates=true
    templates_block="${templates_block}
    otelCollector:
      imageRef:
        repository: public.ecr.aws/dynatrace/dynatrace-otel-collector
        tag: \"${otelcol_tag}\""
    printInfo "OTel Collector: ${otelcol_tag}"
  fi

  if [[ "$has_templates" == "true" ]]; then
    echo "  templates:${templates_block}" >> "$gen_file"
  fi

  # --- Optional features ---
  if [[ "$log_mon" == "true" ]]; then
    echo "  logMonitoring: {}" >> "$gen_file"
  fi

  if [[ "$telemetry" == "true" ]]; then
    cat >> "$gen_file" <<TELEOF
  telemetryIngest:
    protocols:
      - otlp
      - statsd
      - zipkin
TELEOF
  fi

  if [[ "$extensions" == "true" ]]; then
    echo "  extensions:" >> "$gen_file"
    echo "    prometheus: {}" >> "$gen_file"
  fi

  if [[ "$kspm" == "true" ]]; then
    cat >> "$gen_file" <<KSPMEOF
  kspm:
    mappedHostPaths:
      - /boot
      - /etc
      - /proc/sys/kernel
      - /sys/fs
      - /usr/lib/systemd/system
      - /var/lib
KSPMEOF
    # /sys/kernel/security/apparmor — not available in Kind (Docker-in-Docker, no apparmor)
  fi

  # --- Sensitive data ClusterRole (optional) ---
  if [[ "$sensitive" == "true" ]]; then
    cat >> "$gen_file" <<SENSEOF
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: dynatrace-kubernetes-monitoring-sensitive
  labels:
    rbac.dynatrace.com/aggregate-to-monitoring: "true"
rules:
  - apiGroups: [""]
    resources: ["configmaps", "secrets"]
    verbs: ["list", "watch", "get"]
SENSEOF
  fi

  printInfo "Dynakube generated: $gen_file"
  printInfo "Features: log_monitoring=$log_mon telemetry=$telemetry extensions=$extensions kspm=$kspm sensitive_data=$sensitive"
}

deployOperatorViaHelm(){
  # Legacy wrapper — calls dynatraceDeployOperator
  dynatraceDeployOperator "$@"
}

undeployOperatorViaHelm(){
  helm uninstall dynatrace-operator --namespace dynatrace
}


installMkdocs(){

  installRunme
  printInfo "Installing MKdocs"
  pip install --break-system-packages -r docs/requirements/requirements-mkdocs.txt
  fetchMkdocsBase
  exposeMkdocs
}

fetchMkdocsBase(){
  # A repo whose mkdocs.yaml uses INHERIT gets, for a local preview, the same
  # framework files the docs CI workflow (deploy-ghpages.yaml) fetches at
  # FRAMEWORK_VERSION: mkdocs-base.yaml and docs/stylesheets/extra.css.
  # Each is fetched only when absent, so a repo that ships its own copy, or a
  # file being edited locally, is never overwritten. Failures warn and return
  # 0 -- this file is sourced, and mkdocs still serves (unstyled) without CSS.
  grep -q '^INHERIT:' "${REPO_PATH}/mkdocs.yaml" 2>/dev/null || return 0
  if [ -z "$FRAMEWORK_VERSION" ]; then
    printWarn "FRAMEWORK_VERSION is not set, cannot fetch mkdocs-base.yaml / extra.css for the docs preview"
    return 0
  fi
  _fetchFrameworkAsset "mkdocs-base.yaml"
  _fetchFrameworkAsset "docs/stylesheets/extra.css"
  return 0
}

_fetchFrameworkAsset(){
  # Downloads <path> (relative to the repo root) from the framework at
  # FRAMEWORK_VERSION into the same path under REPO_PATH, unless it already
  # exists there or is tracked by git (committed, then deleted locally).
  local rel="$1"
  local dest="${REPO_PATH}/${rel}"
  local url="https://raw.githubusercontent.com/dynatrace-wwse/codespaces-framework/${FRAMEWORK_VERSION}/${rel}"
  [ -e "$dest" ] && return 0
  if git -C "$REPO_PATH" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
    printWarn "${rel} is committed in this repo but missing locally, not fetching the framework copy (git restore ${rel})"
    return 0
  fi
  printInfo "Fetching ${rel} from framework v${FRAMEWORK_VERSION}..."
  mkdir -p "$(dirname "$dest")"
  # Download beside the target and move it into place only on success, so a
  # failed transfer never leaves a partial file that the next run would keep.
  local tmp="${dest}.tmp.$$"
  if curl -fsSL "$url" -o "$tmp"; then
    mv -f "$tmp" "$dest"
  else
    rm -f "$tmp"
    printWarn "Could not fetch ${rel} from ${url} -- the local docs preview will be incomplete"
  fi
  return 0
}

exposeMkdocs(){
  printInfo "Exposing Mkdocs in your dev.container in port 8000 & running in the background, type 'jobs' to show the process."
  nohup mkdocs serve --dev-addr=0.0.0.0:8000 --watch-theme --dirtyreload --livereload > /dev/null 2>&1 &

  if [[ "$CODESPACES" == true ]]; then
    # Codespaces forwards port 8000 directly, so mkdocs needs no ingress — and
    # must not build one: registerMkdocs' Ingress has no host-less catch-all
    # rule (port 80 belongs to the training app), and its Service/Endpoints
    # resolve the backend IP with `docker exec kind-control-plane`, which is
    # Kind-only. Registry row only, so the greeting still lists the docs.
    _registerMkdocsRow "" 8000
    printInfo "Mkdocs registered and accessible at: $(getAppURL "docs" 8000)"
  elif kubectl get ns ingress-nginx &>/dev/null; then
    # Orbital + local: exposed through the ingress controller like any app.
    registerMkdocs
  else
    printInfo "Mkdocs available at: http://localhost:8000"
  fi
}


_exposeLabguide(){
  printInfo "Exposing Lab Guide in your dev.container"
  cd $REPO_PATH/lab-guide/
  nohup node bin/server.js --host 0.0.0.0 --port 3000 > /dev/null 2>&1 &
  cd -
}

_buildLabGuide(){
  printInfoSection "Building the Lab-guide in port 3000"
  cd $REPO_PATH/lab-guide/
  node bin/generator.js
  cd -
}

deployCertmanager(){
  certmanagerInstall
  certmanagerEnable
}

# ======================================================================
#          ------- Ingress & App Exposure -------                       #
#  Functions for nginx ingress, magic DNS routing, and app registration.#
#  Replaces the legacy NodePort (30100-30300) approach.                 #
# ======================================================================

detectIP() {
  # Returns the IP address used for magic DNS subdomains (sslip.io/nip.io).
  # Priority: $EXTERNAL_IP > auto-detect based on instantiation type.
  if [[ -n "$EXTERNAL_IP" ]]; then
    echo "$EXTERNAL_IP"
  elif [[ "$CODESPACES" == true ]]; then
    echo "127.0.0.1"
  else
    # Try public IP first, fall back to local IP
    local ip
    ip=$(curl -s --max-time 5 ifconfig.me 2>/dev/null)
    if [[ -n "$ip" && "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "$ip"
    else
      hostname -I 2>/dev/null | awk '{print $1}'
    fi
  fi
}

detectHostname() {
  # Returns a hostname-based subdomain used as a second ingress host.
  # Priority: $EXTERNAL_HOSTNAME > $(hostname) > "localhost".
  # Used so apps registered in parallel workers are reachable via both
  # the public-IP magic-DNS host AND a hostname-based host.
  if [[ -n "$EXTERNAL_HOSTNAME" ]]; then
    echo "$EXTERNAL_HOSTNAME"
  else
    local h
    h=$(hostname 2>/dev/null)
    if [[ -n "$h" ]]; then
      echo "$h"
    else
      echo "localhost"
    fi
  fi
}

computeOrbitalSubdomain() {
  # Returns the wildcard subdomain label for an app running in Orbital.
  # Format: {app_name}--{ORBITAL_JOB_ID}
  # ORBITAL_JOB_ID is the canonical short job id (base36 epoch_ms + hex, e.g.
  # "mk3p9aqz-7f3a") assigned by the Orbital master/worker. It is already
  # globally unique and well under the 63-char DNS label limit, so no
  # worker-prefix stripping or truncation is needed. Worker/repo/tenant/user
  # are resolved from the job record by this id — not encoded in the URL.
  # Used by getAppURL and registerApp to build the canonical URL
  # https://{app_name}--{job_id}.autonomous-enablements.whydevslovedynatrace.com
  # Returns empty outside Orbital (no ORBITAL_JOB_ID).
  local app_name="$1"
  local jid="${ORBITAL_JOB_ID:-}"
  if [[ -z "$jid" ]]; then echo ""; return 0; fi

  local label="${app_name}--${jid}"
  # Sanitize to a valid DNS label: lowercase, only [a-z0-9-], strip trailing hyphens.
  label=$(echo "$label" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-' | sed 's/-*$//')
  # Hard cap at the 63-char DNS label limit (short ids never reach it).
  echo "${label:0:63}"
}

installIngressController() {
  # Installs the nginx ingress controller — the sole app-exposure mechanism.
  #
  # Installed with Helm rather than the static deploy/static/provider manifests,
  # for one reason: the static manifests always ship the admission webhook, and
  # the webhook is what made this fail on GitHub Codespaces.
  #
  # The controller pod mounts the `ingress-nginx-admission` secret with
  # `optional: false`, and that secret is written by two kube-webhook-certgen
  # Jobs. So nothing about the controller can start until those Jobs finish.
  # Measured on a 4-core Codespace: the Jobs took 106s (a 25MB image, pulled
  # while k3d was still pulling coredns/metrics-server), then kubelet sat in
  # mount backoff for another ~45s, and the controller image only STARTED
  # pulling at t=148s — 28s after the old flat 120s wait had already given up.
  # The run then continued and the first registerApp died with the real symptom:
  #   failed calling webhook "validate.nginx.ingress.kubernetes.io" ...
  #   no endpoints available for service "ingress-nginx-controller-admission"
  # (the ValidatingWebhookConfiguration has failurePolicy: Fail).
  #
  # Disabling the webhook removes ~150s of that ~250s AND one of the two image
  # pulls, and makes that error impossible by construction. It is safe here:
  # the webhook only validates CREATE/UPDATE of Ingress objects at admission
  # time — routing is done by the controller watching those objects and is
  # unaffected — and every Ingress in this framework is generated by
  # registerApp / registerMkdocs / registerAstroshopIngress from fixed
  # templates. See .devcontainer/yaml/ingress/values-*.yaml for the full
  # rationale and the per-engine settings.
  printInfoSection "Installing nginx ingress controller"

  # Check if already installed
  if kubectl get ns ingress-nginx &>/dev/null && \
     kubectl get pod -n ingress-nginx -l app.kubernetes.io/component=controller --no-headers 2>/dev/null | grep -q "Running"; then
    printInfo "Ingress controller already running"
    return 0
  fi

  # One values file per engine, each reproducing what its static provider gave:
  #   k3d  -> provider/cloud (Service type LoadBalancer, fronted by klipper-lb)
  #   kind -> provider/kind  (hostPort 80/443, nodeSelector ingress-ready=true)
  local engine="k3d"
  [[ "$CLUSTER_ENGINE" == "kind" ]] && engine="kind"
  local values_file="${FRAMEWORK_CACHE:-$REPO_PATH}/.devcontainer/yaml/ingress/values-${engine}.yaml"
  if [[ ! -f "$values_file" ]]; then
    printError "Missing ingress values file: $values_file"
    return 1
  fi

  # Chart 4.X.Y ships controller 1.X.Y — keep INGRESS_NGINX_VERSION as the
  # single source of truth for the controller version.
  local chart_version="4.${INGRESS_NGINX_VERSION#1.}"

  printInfo "Deploying ingress-nginx via Helm (chart $chart_version, controller $INGRESS_NGINX_VERSION, engine $engine)"
  # --repo installs straight from the chart repo without mutating the user's
  # `helm repo` config.
  if ! helm upgrade --install ingress-nginx ingress-nginx \
       --repo https://kubernetes.github.io/ingress-nginx \
       --version "$chart_version" \
       --namespace ingress-nginx --create-namespace \
       --values "$values_file"; then
    printError "helm install of ingress-nginx (chart $chart_version) failed"
    return 1
  fi

  # With the webhook gone this is a single image pull plus the readinessProbe
  # (10s delay + a probe cycle). The budget stays generous because Codespaces
  # pull throughput is the remaining variable; a warm cache (Orbital) returns
  # in seconds, so it costs nothing there.
  printInfo "Waiting for ingress controller to be ready (a cold image pull can take a few minutes)..."
  if ! kubectl wait --namespace ingress-nginx \
       --for=condition=ready pod \
       --selector=app.kubernetes.io/component=controller \
       --timeout="${INGRESS_READY_TIMEOUT:-600}s"; then
    # Never report success here — pressing on is what turned a slow start into
    # an unrelated-looking failure several calls later.
    printError "Ingress controller did not become ready within ${INGRESS_READY_TIMEOUT:-600}s"
    printError "Apps cannot be exposed — deployApp/registerApp will fail until this is fixed"
    printError "Inspect with: kubectl describe pod -n ingress-nginx -l app.kubernetes.io/component=controller"
    kubectl get pod -n ingress-nginx -o wide 2>/dev/null
    return 1
  fi

  printInfo "Ingress controller installed and ready"
}

getAppURL() {
  # Returns the user-facing URL for an app based on environment type.
  # Usage: getAppURL <app-name> [codespaces-port]
  # Orbital:    wildcard subdomain {appname}--{job_slug}.autonomous-enablements.*
  # Codespaces: one forwarded port per service. Default 80 — the ingress
  #             catch-all rule routes it to the training app. Services that
  #             bypass the ingress (mkdocs on 8000) pass their own port.
  # Local/other: magic-DNS sslip.io URL
  local app_name="$1"
  # ":-" so both an omitted and an explicitly-empty 2nd arg fall back to 80
  # (registerApp deliberately stores an empty cs_port).
  local cs_port="${2:-80}"
  local detected_ip

  if [[ "$(detectRunEnvironment)" == "orbital" ]] && [[ -n "${ORBITAL_JOB_ID:-}" ]]; then
    local subdomain
    subdomain=$(computeOrbitalSubdomain "$app_name")
    echo "https://${subdomain}.${ORBITAL_APP_DOMAIN:-autonomous-enablements.whydevslovedynatrace.com}"
  elif [[ "$CODESPACES" == true ]]; then
    # Orgs can be issued a non-default forwarding domain.
    echo "https://${CODESPACE_NAME}-${cs_port}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  else
    detected_ip=$(detectIP)
    echo "http://${app_name}.${detected_ip}.${MAGIC_DOMAIN}"
  fi
}

registerApp() {
  # Registers an app in the app registry and creates an Ingress resource.
  # Usage: registerApp <app-name> <namespace> <service-name> <service-port> [extra-annotations]
  # extra-annotations: optional, newline-separated "key: value" pairs for nginx annotations
  local app_name="$1"
  local namespace="$2"
  local service_name="$3"
  local service_port="$4"
  local extra_annotations="$5"

  if [[ -z "$app_name" || -z "$namespace" || -z "$service_name" || -z "$service_port" ]]; then
    printError "registerApp: requires <app-name> <namespace> <service-name> <service-port>"
    return 1
  fi

  local detected_ip detected_hostname
  detected_ip=$(detectIP)
  detected_hostname=$(detectHostname)

  # Create the Ingress resource — two hosts so the app is reachable via:
  #   1. magic-DNS public IP    (e.g. todoapp.18.134.158.252.sslip.io)
  #   2. server hostname        (e.g. todoapp.autonomous-enablements)
  # The hostname route lets parallel workers test their own k3d-hosted app
  # via Host-header curl on localhost without depending on public DNS.
  local ingress_host="${app_name}.${detected_ip}.${MAGIC_DOMAIN}"
  local hostname_host="${app_name}.${detected_hostname}"

  printInfo "Creating Ingress for $app_name → $service_name:$service_port"
  printInfo "  host (ip):       $ingress_host"
  printInfo "  host (hostname): $hostname_host"

  # Build annotations block
  local annotations="    nginx.ingress.kubernetes.io/proxy-read-timeout: \"3600\"
    nginx.ingress.kubernetes.io/proxy-send-timeout: \"3600\"
    nginx.ingress.kubernetes.io/proxy-buffer-size: \"16k\""

  if [[ -n "$extra_annotations" ]]; then
    annotations="${annotations}
${extra_annotations}"
  fi

  kubectl apply -f - <<INGRESSEOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${app_name}-ingress
  namespace: ${namespace}
  annotations:
${annotations}
spec:
  ingressClassName: nginx
  rules:
  - host: ${ingress_host}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: ${service_name}
            port:
              number: ${service_port}
  - host: ${hostname_host}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: ${service_name}
            port:
              number: ${service_port}
  - http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: ${service_name}
            port:
              number: ${service_port}
INGRESSEOF

  # cs_port stays empty — Codespaces uses port 80 via the catch-all ingress rule
  local cs_port=""

  # Compute the Orbital wildcard subdomain (empty when not in Orbital or no ORBITAL_JOB_ID)
  local orbital_subdomain=""
  if [[ "$(detectRunEnvironment)" == "orbital" ]] && [[ -n "${ORBITAL_JOB_ID:-}" ]]; then
    orbital_subdomain=$(computeOrbitalSubdomain "$app_name")
  fi

  # Write to app registry — field 7 is the Orbital subdomain label (empty outside Orbital)
  mkdir -p "$(dirname "$APP_REGISTRY")"
  echo "${app_name}|${namespace}|${service_name}|${service_port}|${ingress_host}|${cs_port}|${orbital_subdomain}" >> "$APP_REGISTRY"

  local app_url
  app_url=$(getAppURL "$app_name" "$cs_port")
  printInfo "$app_name registered and accessible at: $app_url"
}

registerAstroshopIngress() {
  # Creates a custom Ingress for the Astroshop with multi-path routing:
  # - /v1/traces, /v1/metrics, /v1/logs → otel-collector:4318
  # - / (everything else) → frontend-proxy:8080
  local namespace="${1:-astroshop}"
  local detected_ip detected_hostname
  detected_ip=$(detectIP)
  detected_hostname=$(detectHostname)
  local ingress_host="astroshop.${detected_ip}.${MAGIC_DOMAIN}"
  local hostname_host="astroshop.${detected_hostname}"

  printInfo "Creating Astroshop Ingress with otel-collector routes"
  printInfo "  host (ip):       $ingress_host"
  printInfo "  host (hostname): $hostname_host"

  # Path block reused for both hosts.
  local astro_paths='      - path: /v1/traces
        pathType: ImplementationSpecific
        backend:
          service:
            name: otel-collector
            port:
              number: 4318
      - path: /v1/metrics
        pathType: ImplementationSpecific
        backend:
          service:
            name: otel-collector
            port:
              number: 4318
      - path: /v1/logs
        pathType: ImplementationSpecific
        backend:
          service:
            name: otel-collector
            port:
              number: 4318
      - path: /
        pathType: Prefix
        backend:
          service:
            name: frontend-proxy
            port:
              number: 8080'

  kubectl apply -f - <<ASTROINGRESSEOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: astroshop-ingress
  namespace: ${namespace}
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "false"
    nginx.ingress.kubernetes.io/use-regex: "true"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"
spec:
  ingressClassName: nginx
  rules:
  - host: ${ingress_host}
    http:
      paths:
${astro_paths}
  - host: ${hostname_host}
    http:
      paths:
${astro_paths}
  - http:
      paths:
${astro_paths}
ASTROINGRESSEOF

  # cs_port stays empty — Codespaces uses port 80 via catch-all ingress rule
  local cs_port=""

  # Compute the Orbital wildcard subdomain (empty when not in Orbital)
  local orbital_subdomain=""
  if [[ "$(detectRunEnvironment)" == "orbital" ]] && [[ -n "${ORBITAL_JOB_ID:-}" ]]; then
    orbital_subdomain=$(computeOrbitalSubdomain "astroshop")
  fi

  mkdir -p "$(dirname "$APP_REGISTRY")"
  # Remove old entry if exists
  grep -v "^astroshop|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null || true
  mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null || true
  echo "astroshop|${namespace}|frontend-proxy|8080|${ingress_host}|${cs_port}|${orbital_subdomain}" >> "$APP_REGISTRY"

  local app_url
  app_url=$(getAppURL "astroshop" "$cs_port")
  printInfo "Astroshop registered and accessible at: $app_url"
}

unregisterApp() {
  # Removes an app from the registry and deletes its Ingress resource.
  # Usage: unregisterApp <app-name> <namespace>
  local app_name="$1"
  local namespace="$2"

  kubectl delete ingress "${app_name}-ingress" -n "$namespace" 2>/dev/null

  if [[ -f "$APP_REGISTRY" ]]; then
    grep -v "^${app_name}|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null
    mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null
  fi

  printInfo "$app_name unregistered"
}

listApps() {
  # Lists all registered apps with their URLs.
  if [[ ! -f "$APP_REGISTRY" ]] || [[ ! -s "$APP_REGISTRY" ]]; then
    printInfo "No applications registered. Type 'deployApp' to see available apps."
    return 0
  fi

  printInfoSection "Registered Applications"
  # Read ALL 7 fields. With only 6 names, `read` stuffs the remainder into the
  # last one, so cs_port became the literal "|" for a row with an empty
  # orbital_subdomain — which then leaked into the Codespaces URL.
  while IFS='|' read -r app_name namespace service_name service_port ingress_host cs_port orbital_subdomain; do
    local url
    url=$(getAppURL "$app_name" "$cs_port")
    printInfo "  ${app_name} (ns: ${namespace}) → ${url}"
  done < "$APP_REGISTRY"
}

registerMkdocs() {
  # Registers mkdocs as a non-K8s app routed through ingress.
  # Creates a K8s Service + Endpoints pointing to the host, then an Ingress resource.
  local detected_ip
  detected_ip=$(detectIP)
  local mkdocs_host="docs.${detected_ip}.${MAGIC_DOMAIN}"

  printInfo "Registering mkdocs via ingress (host: $mkdocs_host)"

  # Get the host IP from inside Kind (the docker bridge gateway)
  local host_ip
  host_ip=$(docker exec kind-control-plane sh -c "ip route | grep default | awk '{print \$3}'" 2>/dev/null)
  if [[ -z "$host_ip" ]]; then
    host_ip="172.17.0.1"  # Docker default gateway
  fi

  # Create a Service + Endpoints in default namespace pointing to host mkdocs
  kubectl apply -f - <<MKDOCSEOF
apiVersion: v1
kind: Service
metadata:
  name: mkdocs-external
  namespace: default
spec:
  ports:
  - port: 8000
    targetPort: 8000
---
apiVersion: v1
kind: Endpoints
metadata:
  name: mkdocs-external
  namespace: default
subsets:
- addresses:
  - ip: ${host_ip}
  ports:
  - port: 8000
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: mkdocs-ingress
  namespace: default
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
spec:
  ingressClassName: nginx
  rules:
  - host: ${mkdocs_host}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: mkdocs-external
            port:
              number: 8000
MKDOCSEOF

  # Reached through the ingress on port 80 like any other app, so cs_port is
  # empty — the Codespaces path never gets here (see exposeMkdocs).
  _registerMkdocsRow "$mkdocs_host" ""

  printInfo "Mkdocs registered and accessible at: $(getAppURL "docs" "")"
}

_registerMkdocsRow() {
  # Writes the "docs" row of the app registry. Usage: _registerMkdocsRow <ingress_host> <cs_port>
  # 7 fields, same shape as registerApp — greeting.sh and listApps both read 7.
  local ingress_host="$1" cs_port="$2"
  mkdir -p "$(dirname "$APP_REGISTRY")"
  # Remove old entry if exists
  grep -v "^docs|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null || true
  mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null || true
  echo "docs|default|mkdocs-external|8000|${ingress_host}|${cs_port}|" >> "$APP_REGISTRY"
}

deployAITravelAdvisorApp(){
  [ -z "$FRAMEWORK_APPS_PATH" ] && { echo "❌ source_framework.sh not loaded — run 'source .devcontainer/util/source_framework.sh' first"; return 1; }

  printInfoSection "Deploying AI Travel Advisor App & it's LLM"
  
  if [ -z "$DT_LLM_TOKEN" ]; then
    printError "DT_LLM_TOKEN token is missing"
  fi
  
  printInfo "Evaluating credentials"

  dynatraceEvalReadSaveCredentials

  kubectl apply -f $FRAMEWORK_APPS_PATH/ai-travel-advisor/k8s/namespace.yaml

  kubectl -n ai-travel-advisor create secret generic dynatrace --from-literal="token=$DT_LLM_TOKEN" --from-literal="endpoint=$DT_TENANT/api/v2/otlp"
  
  # Start OLLAMA
  printInfo "Deploying our LLM => Ollama"
  kubectl apply -f $FRAMEWORK_APPS_PATH/ai-travel-advisor/k8s/ollama.yaml
  waitForPod ai-travel-advisor ollama
  printInfo "Waiting for Ollama to get ready"
  kubectl -n ai-travel-advisor wait --for=condition=Ready pod --all --timeout=10m
  printInfo "Ollama is ready"

  # Start Weaviate
  printInfo "Deploying our VectorDB => Weaviate"
  kubectl apply -f $FRAMEWORK_APPS_PATH/ai-travel-advisor/k8s/weaviate.yaml

  waitForPod ai-travel-advisor weaviate
  printInfo "Waiting for Weaviate to get ready"
  kubectl -n ai-travel-advisor wait --for=condition=Ready pod --all --timeout=10m
  printInfo "Weaviate is ready"

  # Start AI Travel Advisor
  printInfo "Deploying AI App => AI Travel Advisor"
  kubectl apply -f $FRAMEWORK_APPS_PATH/ai-travel-advisor/k8s/ai-travel-advisor.yaml
  
  waitForPod ai-travel-advisor ai-travel-advisor
  printInfo "Waiting for AI Travel Advisor to get ready"

  kubectl -n ai-travel-advisor wait --for=condition=Ready pod --all --timeout=10m
  printInfo "AI Travel Advisor is ready"

  registerApp "ai-travel-advisor" "ai-travel-advisor" "ai-travel-advisor" 80
}

deployTodoApp(){

  printInfoSection "Deploying Todo App"

  kubectl create ns todoapp 2>/dev/null || true

  # Create deployment of todoApp
  kubectl -n todoapp create deploy todoapp --image=shinojosa/todoapp:1.0.1

  kubectl -n todoapp expose deployment todoapp --type=ClusterIP --name=todoapp --port=8080 --target-port=8080 2>/dev/null || true
  waitForAllReadyPods todoapp
  registerApp "todoapp" "todoapp" "todoapp" 8080
}

deployAstroshop(){
  [ -z "$FRAMEWORK_APPS_PATH" ] && { echo "❌ source_framework.sh not loaded — run 'source .devcontainer/util/source_framework.sh' first"; return 1; }

  ASTROSHOPDIR="astroshop"

  printInfoSection "Deploying Demo.Live Astroshop"
  if [[ "$ARCH" != "x86_64" ]]; then
    printWarn "This version of the Astroshop only supports AMD/x86 architectures and not ARM, exiting deployment..."
    return 1
  fi

  NAMESPACE="astroshop"

  dynatraceEvalReadSaveCredentials

  if [[ -z "${DT_INGEST_TOKEN}" || -z "${DT_OTEL_ENDPOINT}" ]]; then
    printWarn "DT_INGEST_TOKEN and/or DT_OTEL_ENDPOINT are not set. DT_OTEL_ENDPOINT is calculated with the function 'dynatraceEvalReadSaveCredentials' and the env var DT_ENVIRONMENT"
  else
    printInfo "OTEL Configuration URL $DT_OTEL_ENDPOINT and Ingest Token ${DT_INGEST_TOKEN:0:14}xxx..."
  fi

  kubectl apply -n $NAMESPACE -f $FRAMEWORK_APPS_PATH/$ASTROSHOPDIR/yaml/astroshop-deployment.yaml

  kubectl -n $NAMESPACE create secret generic dt-credentials --from-literal="DT_API_TOKEN=$DT_INGEST_TOKEN" --from-literal="DT_ENDPOINT=$DT_OTEL_ENDPOINT" 2>/dev/null || true

  printInfo "Waiting for pods of $NAMESPACE to be scheduled (this can take a while)"

  # Astroshop needs custom ingress: otel-collector paths + frontend-proxy catch-all
  waitForAllReadyPods "$NAMESPACE"
  registerAstroshopIngress "$NAMESPACE"
}

deployBugZapperApp(){
  [ -z "$FRAMEWORK_APPS_PATH" ] && { echo "❌ source_framework.sh not loaded — run 'source .devcontainer/util/source_framework.sh' first"; return 1; }

  printInfoSection "Deploying BugZapper App"

  kubectl create ns bugzapper 2>/dev/null || true
  kubectl -n bugzapper create deploy bugzapper --image=jhendrick/bugzapper-game:latest

  kubectl -n bugzapper expose deployment bugzapper --type=ClusterIP --name=bugzapper --port=3000 --target-port=3000 2>/dev/null || true
  waitForAllReadyPods bugzapper
  registerApp "bugzapper" "bugzapper" "bugzapper" 3000
}

# deploy easytrade from manifests
deployEasyTrade() {
  [ -z "$FRAMEWORK_APPS_PATH" ] && { echo "❌ source_framework.sh not loaded — run 'source .devcontainer/util/source_framework.sh' first"; return 1; }

  printInfoSection "Deploying EasyTrade"
  
  if [[ "$ARCH" != "x86_64" ]]; then
    printWarn "This version of the EasyTrade only supports AMD/x86 architectures and not ARM, exiting deployment..."
    return 1
  fi

  kubectl create namespace easytrade 2>/dev/null || true

  printInfo "Deploying easytrade manifests"
  kubectl apply -f $FRAMEWORK_APPS_PATH/easytrade/manifests -n easytrade

  printInfo "Waiting for all pods to start"
  waitForAllPods easytrade

  registerApp "easytrade" "easytrade" "frontendreverseproxy-easytrade" 80
}

# deploy hipstershop from manifests
deployHipsterShop() {
  [ -z "$FRAMEWORK_APPS_PATH" ] && { echo "❌ source_framework.sh not loaded — run 'source .devcontainer/util/source_framework.sh' first"; return 1; }
  
  printInfoSection "Deploying HipsterShop"
  
  if [[ "$ARCH" != "x86_64" ]]; then
    printWarn "This version of the Hipstershop only supports AMD/x86 architectures and not ARM, exiting deployment..."
    return 1
  fi

  kubectl create namespace hipstershop 2>/dev/null || true

  printInfo "Deploying hipstershop manifests"
  kubectl apply -f $FRAMEWORK_APPS_PATH/hipstershop/manifests -n hipstershop

  printInfo "Waiting for all pods to start"
  waitForAllPods hipstershop

  registerApp "hipstershop" "hipstershop" "frontend-external" 80
}

deployUnguard(){

  printInfoSection "Deploying Unguard"

  if [[ "$ARCH" != "x86_64" ]]; then
    printWarn "This version of the Unguard only supports AMD/x86 architectures and not ARM, exiting deployment..."
    return 1
  fi

  printInfo "Unguard repository https://github.com/dynatrace-oss/unguard/"

  printInfo "Adding bitnami chart ..."
  helm repo add bitnami https://charts.bitnami.com/bitnami

  printInfo "Installing unguard-mariadb ..."
  helm install unguard-mariadb bitnami/mariadb \
  --version 11.5.7 \
  --set primary.persistence.enabled=false \
  --set image.repository=bitnamilegacy/mariadb \
  --namespace unguard --create-namespace

  printInfo "Waiting for mariadb to come online..."
  waitForAllReadyPods unguard

  printInfo "Installing Unguard"
  helm install unguard oci://ghcr.io/dynatrace-oss/unguard/chart/unguard --version 0.12.0 --namespace unguard

  registerApp "unguard" "unguard" "unguard-envoy-proxy" 8080
}

undeployUnguard() {

  printInfoSection "Undeploying Unguard"
  helm uninstall unguard -n unguard
  helm uninstall unguard-mariadb -n unguard
  kubectl delete ns unguard --force
}

deployOpentelemetryDemo(){
  # Deploys the CNCF OpenTelemetry Demo (upstream, community-maintained)
  # https://opentelemetry.io/docs/demo/kubernetes-deployment/
  local NAMESPACE="opentelemetry-demo"

  printInfoSection "Deploying CNCF OpenTelemetry Demo in NS='$NAMESPACE'"
  printInfo "Source: https://opentelemetry.io/docs/demo/kubernetes-deployment/"

  helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>/dev/null || true
  helm repo update open-telemetry

  helm install opentelemetry-demo open-telemetry/opentelemetry-demo \
    --namespace "$NAMESPACE" --create-namespace

  printWarn "OpenTelemetry Demo is heavy — pods may take a while to schedule"

  # Same multi-path ingress pattern as astroshop — otel-collector + frontend-proxy
  waitForAllReadyPods "$NAMESPACE"
  registerOpentelemetryDemoIngress "$NAMESPACE"
}

registerOpentelemetryDemoIngress() {
  # Creates an Ingress for the CNCF OpenTelemetry Demo with multi-path routing:
  # - /v1/traces, /v1/metrics, /v1/logs → otel-collector:4318
  # - / → frontend-proxy:8080
  local namespace="${1:-opentelemetry-demo}"
  local detected_ip detected_hostname
  detected_ip=$(detectIP)
  detected_hostname=$(detectHostname)
  local ingress_host="otel-demo.${detected_ip}.${MAGIC_DOMAIN}"
  local hostname_host="otel-demo.${detected_hostname}"

  printInfo "Creating OpenTelemetry Demo Ingress"
  printInfo "  host (ip):       $ingress_host"
  printInfo "  host (hostname): $hostname_host"

  local otel_paths='      - path: /v1/traces
        pathType: ImplementationSpecific
        backend:
          service:
            name: otel-collector
            port:
              number: 4318
      - path: /v1/metrics
        pathType: ImplementationSpecific
        backend:
          service:
            name: otel-collector
            port:
              number: 4318
      - path: /v1/logs
        pathType: ImplementationSpecific
        backend:
          service:
            name: otel-collector
            port:
              number: 4318
      - path: /
        pathType: Prefix
        backend:
          service:
            name: frontend-proxy
            port:
              number: 8080'

  kubectl apply -f - <<OTELINGRESSEOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: otel-demo-ingress
  namespace: ${namespace}
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "false"
    nginx.ingress.kubernetes.io/use-regex: "true"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"
spec:
  ingressClassName: nginx
  rules:
  - host: ${ingress_host}
    http:
      paths:
${otel_paths}
  - host: ${hostname_host}
    http:
      paths:
${otel_paths}
  - http:
      paths:
${otel_paths}
OTELINGRESSEOF

  # Register in app registry (same 7-field layout as registerApp).
  # cs_port stays empty — Codespaces uses port 80 via the catch-all ingress rule.
  local cs_port=""
  local orbital_subdomain=""
  if [[ "$(detectRunEnvironment)" == "orbital" ]] && [[ -n "${ORBITAL_JOB_ID:-}" ]]; then
    orbital_subdomain=$(computeOrbitalSubdomain "otel-demo")
  fi
  mkdir -p "$(dirname "$APP_REGISTRY")"
  grep -v "^otel-demo|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null || true
  mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null || true
  echo "otel-demo|${namespace}|frontend-proxy|8080|${ingress_host}|${cs_port}|${orbital_subdomain}" >> "$APP_REGISTRY"

  local app_url
  app_url=$(getAppURL "otel-demo")
  printInfo "OpenTelemetry Demo registered and accessible at: $app_url"
}

undeployOpentelemetryDemo(){
  printInfoSection "Undeploying OpenTelemetry Demo"
  unregisterApp "otel-demo" "opentelemetry-demo"
  helm uninstall opentelemetry-demo --namespace opentelemetry-demo 2>/dev/null
  kubectl delete namespace opentelemetry-demo --force 2>/dev/null
}

# ======================================================================
#  OpenTelemetry Collector in the cluster (deployOtelCollector)
#
#  A standalone collector that exports OTLP to the tenant: the Dynatrace
#  distribution, one Deployment, receiving OTLP gRPC (4317) and HTTP (4318)
#  from any app, enriching with Kubernetes metadata and exporting traces,
#  metrics (as delta) and logs to $DT_OTEL_ENDPOINT with $DT_INGEST_TOKEN --
#  the same contract as astroshop's gateway collector. Independent of the
#  Dynatrace Operator: works with or without a DynaKube. The token travels
#  on stdin into a Secret; it is never printed and never in argv.
#
#  Overrides (all optional; unset = the defaults above, unchanged since 1.17.0).
#  Set them before the call, e.g. in the repo's my_functions.sh:
#
#    OTEL_COLLECTOR_MODE=daemonset     deployment (default) | daemonset — one
#                                      collector per node instead of one replica
#    OTEL_COLLECTOR_POD_LOGS=true      mount the node's /var/log/pods read-only
#                                      and run as root so a filelog receiver can
#                                      read container logs. daemonset only.
#    OTEL_COLLECTOR_CONFIG_FILE=path   replace the whole collector config with
#                                      this file. It must keep the contract the
#                                      manifests rely on:
#                                        - extension health_check on
#                                          ${env:MY_POD_IP}:13133 (the probes)
#                                        - receivers on 4317 / 4318
#                                        - export with ${env:DT_ENDPOINT} and
#                                          "Api-Token ${env:DT_API_TOKEN}"
#                                      Also available in the pod: ${env:K8S_NODE_NAME}.
#                                      Start from `_otelCollectorConfig` (prints
#                                      the default) and add what the training needs.
#
#  Example — per-node collector that also ships pod logs:
#    OTEL_COLLECTOR_MODE=daemonset OTEL_COLLECTOR_POD_LOGS=true \
#    OTEL_COLLECTOR_CONFIG_FILE="$REPO_PATH/.devcontainer/yaml/otel-collector.yaml" \
#      deployOtelCollector
# ======================================================================

_otelCollectorConfig() {
  cat <<'CFGEOF'
extensions:
  health_check:
    endpoint: ${env:MY_POD_IP}:13133
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: ${env:MY_POD_IP}:4317
      http:
        endpoint: ${env:MY_POD_IP}:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_percentage: 80
    spike_limit_percentage: 25
  k8sattributes:
    extract:
      metadata:
        - k8s.pod.name
        - k8s.pod.uid
        - k8s.deployment.name
        - k8s.statefulset.name
        - k8s.daemonset.name
        - k8s.namespace.name
        - k8s.node.name
        - k8s.cluster.uid
        - k8s.container.name
    pod_association:
      - sources:
          - from: resource_attribute
            name: k8s.pod.ip
      - sources:
          - from: resource_attribute
            name: k8s.pod.uid
      - sources:
          - from: connection
  cumulativetodelta: {}
  batch: {}
exporters:
  otlp_http:
    endpoint: ${env:DT_ENDPOINT}
    headers:
      Authorization: "Api-Token ${env:DT_API_TOKEN}"
service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, batch]
      exporters: [otlp_http]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, cumulativetodelta, batch]
      exporters: [otlp_http]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, batch]
      exporters: [otlp_http]
CFGEOF
}

_otelCollectorManifests() {
  # $1 namespace, $2 config sha (rolls the pod when the config changes),
  # $3 kind (Deployment | DaemonSet), $4 pod logs (true = mount /var/log/pods).
  local ns="$1" sha="$2" kind="${3:-Deployment}" podlogs="${4:-false}"
  local replicas="" podsec="" logmount="" logvol="" nodeenv=""
  if [ "$kind" = "Deployment" ]; then
    replicas="
  replicas: 1"
  else
    nodeenv="
            - name: K8S_NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName"
  fi
  if [ "$podlogs" = "true" ]; then
    # Container log files on the node are root-owned 0640.
    podsec="
      securityContext:
        runAsUser: 0
        runAsGroup: 0"
    logmount="
            - {name: varlogpods, mountPath: /var/log/pods, readOnly: true}"
    logvol="
        - name: varlogpods
          hostPath:
            path: /var/log/pods"
  fi
  local image="ghcr.io/dynatrace/dynatrace-otel-collector/dynatrace-otel-collector:${OTEL_COLLECTOR_VERSION}@${OTEL_COLLECTOR_DIGEST}"
  cat <<MANEOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: otel-collector
  namespace: $ns
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: otel-collector-k8sattributes
rules:
  - apiGroups: [""]
    resources: [pods, namespaces, nodes]
    verbs: [get, watch, list]
  - apiGroups: [apps]
    resources: [replicasets]
    verbs: [get, list, watch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: otel-collector-k8sattributes
subjects:
  - kind: ServiceAccount
    name: otel-collector
    namespace: $ns
roleRef:
  kind: ClusterRole
  name: otel-collector-k8sattributes
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: apps/v1
kind: $kind
metadata:
  name: otel-collector
  namespace: $ns
  labels:
    app.kubernetes.io/name: otel-collector
spec:${replicas}
  selector:
    matchLabels:
      app.kubernetes.io/name: otel-collector
  template:
    metadata:
      labels:
        app.kubernetes.io/name: otel-collector
      annotations:
        enablement.dynatrace.com/config-sha: "$sha"
    spec:
      serviceAccountName: otel-collector${podsec}
      containers:
        - name: collector
          image: $image
          args: ["--config=/conf/config.yaml"]
          env:
            - name: MY_POD_IP
              valueFrom:
                fieldRef:
                  fieldPath: status.podIP${nodeenv}
            - name: DT_ENDPOINT
              valueFrom:
                secretKeyRef:
                  name: dynatrace-otelcol-dt-api-credentials
                  key: DT_ENDPOINT
            - name: DT_API_TOKEN
              valueFrom:
                secretKeyRef:
                  name: dynatrace-otelcol-dt-api-credentials
                  key: DT_API_TOKEN
          ports:
            - {name: otlp-grpc, containerPort: 4317}
            - {name: otlp-http, containerPort: 4318}
          readinessProbe:
            httpGet: {path: /, port: 13133}
          livenessProbe:
            httpGet: {path: /, port: 13133}
          resources:
            requests: {cpu: 50m, memory: 128Mi}
            limits: {memory: 512Mi}
          volumeMounts:
            - {name: conf, mountPath: /conf}${logmount}
      volumes:
        - name: conf
          configMap:
            name: otel-collector-config${logvol}
---
apiVersion: v1
kind: Service
metadata:
  name: otel-collector
  namespace: $ns
spec:
  selector:
    app.kubernetes.io/name: otel-collector
  ports:
    - {name: otlp-grpc, port: 4317, targetPort: 4317}
    - {name: otlp-http, port: 4318, targetPort: 4318}
MANEOF
}

deployOtelCollector() {
  # deployOtelCollector — the Dynatrace OpenTelemetry Collector in the cluster,
  # exporting to the tenant. Apps send OTLP to
  #   http://otel-collector.<ns>.svc.cluster.local:4318 (HTTP) or :4317 (gRPC).
  # Idempotent: a re-run updates the credentials and the config in place.
  # Overrides: OTEL_COLLECTOR_MODE, OTEL_COLLECTOR_POD_LOGS, OTEL_COLLECTOR_CONFIG_FILE
  # (documented in the section header above).
  local ns="$OTEL_COLLECTOR_NAMESPACE" cfg sha kind other workload
  local mode="${OTEL_COLLECTOR_MODE:-deployment}" podlogs="${OTEL_COLLECTOR_POD_LOGS:-false}"
  case "$mode" in
    deployment) kind=Deployment other=daemonset workload=deploy/otel-collector ;;
    daemonset)  kind=DaemonSet other=deployment workload=ds/otel-collector ;;
    *) printError "deployOtelCollector: OTEL_COLLECTOR_MODE must be deployment or daemonset (got '$mode')"; return 1 ;;
  esac
  if [ "$podlogs" = "true" ] && [ "$mode" != "daemonset" ]; then
    printError "deployOtelCollector: OTEL_COLLECTOR_POD_LOGS=true needs OTEL_COLLECTOR_MODE=daemonset (one collector per node reads that node's logs)"
    return 1
  fi
  if [ -n "${OTEL_COLLECTOR_CONFIG_FILE:-}" ] && [ ! -r "$OTEL_COLLECTOR_CONFIG_FILE" ]; then
    printError "deployOtelCollector: OTEL_COLLECTOR_CONFIG_FILE '$OTEL_COLLECTOR_CONFIG_FILE' is not a readable file"
    return 1
  fi
  if ! kubectl cluster-info >/dev/null 2>&1; then
    printError "deployOtelCollector: no Kubernetes cluster reachable — run startCluster first"
    return 1
  fi
  if [ -z "$DT_OTEL_ENDPOINT" ] || [ -z "$DT_INGEST_TOKEN" ]; then
    dynatraceEvalReadSaveCredentials >/dev/null 2>&1
  fi
  if [ -z "$DT_OTEL_ENDPOINT" ] || [ -z "$DT_INGEST_TOKEN" ]; then
    printError "deployOtelCollector: DT_OTEL_ENDPOINT and DT_INGEST_TOKEN are needed (set DT_ENVIRONMENT and DT_INGEST_TOKEN)"
    return 1
  fi
  printInfoSection "Deploying the Dynatrace OpenTelemetry Collector ${OTEL_COLLECTOR_VERSION} (namespace $ns)"
  printInfo "Exporting to $DT_OTEL_ENDPOINT (ingest token not printed)"

  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null || return 1
  printf 'DT_ENDPOINT=%s\nDT_API_TOKEN=%s\n' "$DT_OTEL_ENDPOINT" "$DT_INGEST_TOKEN" \
    | kubectl -n "$ns" create secret generic dynatrace-otelcol-dt-api-credentials --from-env-file=/dev/stdin \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null \
    || { printError "Could not create the collector's credentials secret"; return 1; }

  if [ -n "${OTEL_COLLECTOR_CONFIG_FILE:-}" ]; then
    printInfo "Collector config from $OTEL_COLLECTOR_CONFIG_FILE"
    cfg=$(cat "$OTEL_COLLECTOR_CONFIG_FILE")
  else
    cfg=$(_otelCollectorConfig)
  fi
  # The pod-logs flag joins the sha only when on: the default sha (and pod) stay as in 1.17.0.
  sha=$(printf '%s%s%s' "$cfg" "$DT_OTEL_ENDPOINT" "${podlogs#false}" | sha256sum | cut -c1-16)
  kubectl -n "$ns" create cm otel-collector-config --from-literal=config.yaml="$cfg" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null || return 1
  # Switching mode: the other kind would share the Service selector, so remove it.
  kubectl -n "$ns" delete "$other" otel-collector --ignore-not-found >/dev/null 2>&1
  _otelCollectorManifests "$ns" "$sha" "$kind" "$podlogs" >/dev/null || { printError "Could not apply the collector manifests"; return 1; }

  if ! kubectl -n "$ns" rollout status "$workload" --timeout="${OTEL_COLLECTOR_READY_TIMEOUT:-300}s" >/dev/null; then
    printError "The OpenTelemetry Collector did not become ready"
    printError "Inspect with: kubectl -n $ns logs $workload --tail=50"
    return 1
  fi
  printInfo "OpenTelemetry Collector is ready. Send OTLP to:"
  printInfo "  http://otel-collector.$ns.svc.cluster.local:4318  (HTTP)"
  printInfo "  otel-collector.$ns.svc.cluster.local:4317         (gRPC)"
}

undeployOtelCollector() {
  local ns="$OTEL_COLLECTOR_NAMESPACE"
  printInfoSection "Undeploying the OpenTelemetry Collector (namespace $ns)"
  kubectl delete clusterrolebinding otel-collector-k8sattributes --ignore-not-found >/dev/null
  kubectl delete clusterrole otel-collector-k8sattributes --ignore-not-found >/dev/null
  kubectl delete namespace "$ns" --ignore-not-found >/dev/null \
    || { printError "Could not delete namespace $ns"; return 1; }
}

# ======================================================================
#  GitLab in the cluster (deployGitlab) — Perform developer sessions
#
#  GitLab Omnibus CE as ONE Deployment (one PVC, one Service) plus a
#  Runner from its own chart (Kubernetes executor), registered by itself.
#  Spike 92 measured Omnibus at 2.31 GiB steady vs 3.85 GiB for the
#  trimmed cloud-native chart, with the same ~3 min start.
#
#  Secrets: the root password is generated INTO a k8s secret and never
#  printed; the API token (PAT) is minted in-cluster into a secret; git
#  gets it through GIT_ASKPASS, never on a command line or in a URL.
#
#  Written for bash AND zsh (the learner's shell).
# ======================================================================

_gitlabNs() { echo "${GITLAB_NAMESPACE:-gitlab}"; }

_gitlabExternalUrl() {
  # The address the LEARNER sees: Orbital's https subdomain, Codespaces'
  # forwarded port, or the sslip host. GITLAB_EXTERNAL_URL overrides.
  if [ -n "${GITLAB_EXTERNAL_URL:-}" ]; then
    echo "${GITLAB_EXTERNAL_URL%/}"
  else
    getAppURL gitlab
  fi
}

_gitlabOmnibusConfig() {
  # _gitlabOmnibusConfig <external-url> — the gitlab.rb handed to Omnibus.
  # GitLab's "memory-constrained environment" settings, as measured in spike 92.
  # TLS ends in front of the cluster (Orbital's nginx), so Omnibus' own nginx
  # listens on plain 80. For an https external URL it forces the forwarded
  # scheme itself: Rails only issues its Secure session cookie when it
  # believes the request was https, and no proxy in front has to send it.
  local url="$1" cfg host
  host="${url#*://}"; host="${host%%/*}"
  cfg="external_url '${url}'
nginx['listen_port'] = 80
nginx['listen_https'] = false
nginx['client_max_body_size'] = '512m'
letsencrypt['enable'] = false
puma['worker_processes'] = 0
sidekiq['concurrency'] = 10
prometheus_monitoring['enable'] = false
gitlab_rails['env'] = { 'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000' }
gitaly['env'] = { 'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000' }
gitlab_rails['usage_ping_enabled'] = false
registry['enable'] = false
gitlab_kas['enable'] = false
gitlab_pages['enable'] = false
mattermost['enable'] = false"
  # Host + X-Forwarded-Host: Rails builds redirects from them, so both are
  # pinned to the external host here. On Orbital the k3d ingress now passes the
  # public window host through (use-forwarded-headers, values-k3d.yaml), so the
  # pin agrees with what arrives -- it stays because not every path in sends
  # it: a request straight to the sslip host (Orbital's IDE leg, a VPC caller,
  # Kind/Codespaces with the default ingress, an Orbital older than D10) still
  # carries another name, which would send the browser away from the address
  # it is on.
  case "$url" in
    https://*) cfg="${cfg}
nginx['proxy_set_headers'] = { 'Host' => '${host}', 'X-Forwarded-Host' => '${host}', 'X-Forwarded-Proto' => 'https', 'X-Forwarded-Ssl' => 'on' }" ;;
    *) cfg="${cfg}
nginx['proxy_set_headers'] = { 'Host' => '${host}', 'X-Forwarded-Host' => '${host}' }" ;;
  esac
  printf '%s\n' "$cfg"
}

_gitlabGenPassword() {
  # 24 alphanumerics. Printed ONLY into a pipe that kubectl reads as a file.
  LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom 2>/dev/null | head -c 24
}

_gitlabSecretValue() {
  # _gitlabSecretValue <secret> <key> — prints a secret's value for capture
  # into a variable by the caller. Never call it bare.
  kubectl -n "$(_gitlabNs)" get secret "$1" -o "jsonpath={.data.$2}" 2>/dev/null | base64 -d 2>/dev/null
}

_gitlabApi() {
  # _gitlabApi <METHOD> <path> [curl data args...] — GitLab API call made
  # INSIDE the GitLab pod (no route, DNS or ingress involved). The token goes
  # in as a header on stdin (curl -H @-), so it is in no process' arguments.
  # Prints the body, then a last line "HTTP <code>" ("HTTP 000" when GitLab
  # did not answer within GITLAB_API_TIMEOUT seconds).
  local method="$1" api_path="$2" tok
  shift 2
  tok=$(_gitlabSecretValue gitlab-pat token)
  [ -n "$tok" ] || { echo "HTTP 000"; return 1; }
  printf 'PRIVATE-TOKEN: %s\n' "$tok" | timeout "$(( ${GITLAB_API_TIMEOUT:-30} + 15 ))" \
    kubectl -n "$(_gitlabNs)" exec -i deploy/gitlab -c gitlab -- \
    curl -s --max-time "${GITLAB_API_TIMEOUT:-30}" -X "$method" -H @- -w '\nHTTP %{http_code}\n' "$@" \
    "http://127.0.0.1/api/v4${api_path}"
}

_gitlabHttpCode() {
  # Last line of _gitlabApi output -> the status code.
  tail -n 1 | sed -n 's/^HTTP //p'
}

_gitlabApplyManifests() {
  # _gitlabApplyManifests <config-sha> — PVC, Service, Deployment. Re-applying
  # is a no-op unless the config changed (the sha annotation rolls the pod).
  local ns sha="$1"
  ns=$(_gitlabNs)
  kubectl apply -f - <<GITLABEOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gitlab-data
  namespace: ${ns}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 20Gi
---
apiVersion: v1
kind: Service
metadata:
  name: gitlab
  namespace: ${ns}
  labels:
    app: gitlab
spec:
  selector:
    app: gitlab
  ports:
  - name: http
    port: 80
    targetPort: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gitlab
  namespace: ${ns}
  labels:
    app: gitlab
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: gitlab
  template:
    metadata:
      labels:
        app: gitlab
      annotations:
        enablement-framework/config-sha: "${sha}"
    spec:
      containers:
      - name: gitlab
        image: gitlab/gitlab-ce:${GITLAB_VERSION:-18.4.0-ce.0}
        ports:
        - name: http
          containerPort: 80
        env:
        - name: GITLAB_OMNIBUS_CONFIG
          valueFrom:
            configMapKeyRef:
              name: gitlab-omnibus
              key: gitlab.rb
        - name: GITLAB_ROOT_PASSWORD
          valueFrom:
            secretKeyRef:
              name: gitlab-root
              key: password
        readinessProbe:
          httpGet:
            path: /users/sign_in
            port: 80
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        resources:
          requests:
            cpu: 500m
            memory: 2Gi
        volumeMounts:
        - name: data
          mountPath: /etc/gitlab
          subPath: config
        - name: data
          mountPath: /var/opt/gitlab
          subPath: data
        - name: data
          mountPath: /var/log/gitlab
          subPath: logs
        - name: shm
          mountPath: /dev/shm
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: gitlab-data
      - name: shm
        emptyDir:
          medium: Memory
          sizeLimit: 256Mi
GITLABEOF
}

_gitlabEnsureToken() {
  # An API token (PAT, scopes api + repository) in secret gitlab-pat. Kept when
  # it still authenticates; otherwise minted in-cluster with gitlab-rails and
  # piped straight into the secret.
  local ns tok code
  ns=$(_gitlabNs)
  if kubectl -n "$ns" get secret gitlab-pat >/dev/null 2>&1; then
    code=$(_gitlabApi GET /user 2>/dev/null | _gitlabHttpCode)
    case "$code" in
      200)
        printInfo "GitLab API token present and valid (secret $ns/gitlab-pat)"
        return 0 ;;
      401)
        printWarn "GitLab API token in $ns/gitlab-pat is rejected (HTTP 401) — minting a new one" ;;
      *)
        # Not an answer about the token (timeout, 5xx while GitLab restarts):
        # replacing a token that may be fine would be wrong. Fail, re-run later.
        printError "GitLab API did not answer usably (HTTP ${code:-none}) — not replacing the token; re-run deployGitlab when GitLab is ready"
        return 1 ;;
    esac
  fi
  # The mint also revokes every earlier framework token, so exactly one is
  # live — the one in the secret.
  printInfo "Minting a GitLab API token in-cluster (gitlab-rails, ~1 min)"
  tok=$(timeout "${GITLAB_RAILS_TIMEOUT:-300}" kubectl -n "$ns" exec deploy/gitlab -c gitlab -- gitlab-rails runner \
    "u = User.find_by_username('root'); u.personal_access_tokens.active.where('name LIKE ?', 'enablement-framework-%').each(&:revoke!); t = u.personal_access_tokens.create!(name: \"enablement-framework-#{Time.now.to_i}\", scopes: %w[api read_repository write_repository], expires_at: 364.days.from_now); ApplicationSetting.current.update!(signup_enabled: false); puts; puts t.token" \
    2>/dev/null | grep -E '^glpat-' | tail -n 1)
  if [ -z "$tok" ]; then
    printError "Could not mint a GitLab API token (gitlab-rails runner returned none)"
    return 1
  fi
  printf '%s' "$tok" | kubectl -n "$ns" create secret generic gitlab-pat --from-file=token=/dev/stdin \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null || { printError "Could not store the GitLab API token"; return 1; }
  tok=""
  printInfo "GitLab API token stored in secret $ns/gitlab-pat"
}

_gitlabEnsureRunner() {
  # Registers an instance runner through the API (runner authentication token,
  # GitLab >= 16 flow) into secret gitlab-runner-secret, then installs the
  # gitlab-runner chart with the Kubernetes executor. Re-run: the runner token
  # is kept and must still verify; one that does not is an error to look at,
  # not a reason to register a second runner. The chart is upgraded in place.
  local ns out rtok code
  ns=$(_gitlabNs)
  rtok=$(_gitlabSecretValue gitlab-runner-secret runner-token)
  if [ -n "$rtok" ]; then
    # token@- : curl reads the value from stdin, so it is in no argv, here
    # or inside the pod.
    code=$(printf '%s' "$rtok" | timeout "$(( ${GITLAB_API_TIMEOUT:-30} + 15 ))" \
      kubectl -n "$ns" exec -i deploy/gitlab -c gitlab -- \
      curl -s --max-time "${GITLAB_API_TIMEOUT:-30}" -o /dev/null -w '%{http_code}' -X POST \
      --data-urlencode token@- http://127.0.0.1/api/v4/runners/verify 2>/dev/null)
    if [ "$code" != "200" ]; then
      printError "The runner token in $ns/gitlab-runner-secret does not verify (HTTP ${code:-none})"
      printError "If the runner was deleted in GitLab: kubectl -n $ns delete secret gitlab-runner-secret, then re-run deployGitlab"
      return 1
    fi
    printInfo "GitLab Runner token present and valid (secret $ns/gitlab-runner-secret)"
  else
    printInfo "Registering a GitLab instance runner"
    out=$(_gitlabApi POST /user/runners --data 'runner_type=instance_type' --data 'run_untagged=true' \
      --data 'description=enablement-framework-k8s')
    if [ "$(printf '%s\n' "$out" | _gitlabHttpCode)" != "201" ]; then
      printError "Runner registration failed (HTTP $(printf '%s\n' "$out" | _gitlabHttpCode))"
      return 1
    fi
    rtok=$(printf '%s\n' "$out" | grep -o '"token":"glrt-[^"]*"' | head -n 1 | sed 's/.*:"//; s/"$//')
    out=""
    [ -n "$rtok" ] || { printError "Runner registration returned no token"; return 1; }
    printf '%s' "$rtok" | kubectl -n "$ns" create secret generic gitlab-runner-secret \
      --from-literal=runner-registration-token= --from-file=runner-token=/dev/stdin \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null || { printError "Could not store the runner token"; return 1; }
  fi
  rtok=""

  # Fence gate: the egress policy is enforced ~1-2 s after a pod starts —
  # long enough for a job's script or a `services:` entrypoint to reach this
  # container. An init container (runs before both) waits until the kubelet
  # port on the node — inside the fenced range, reachable without the fence —
  # stops answering. A cluster that never enforces the policy fails the job
  # (fail closed); GITLAB_CI_FENCE=0 drops the gate.
  local ep gate=""
  if [ "${GITLAB_CI_FENCE:-1}" != "0" ]; then
    if ep=$(_gitlabApiEndpoint); then
      gate="
      [runners.feature_flags]
        FF_USE_ADVANCED_POD_SPEC_CONFIGURATION = true
      [[runners.kubernetes.pod_spec]]
        name = \"wait-for-egress-fence\"
        patch_type = \"strategic\"
        patch = '''
          initContainers:
          - name: wait-for-egress-fence
            image: alpine:3.20
            command: [\"sh\", \"-c\", \"i=0; while nc -z -w 1 ${ep% *} 10250; do i=\$((i+1)); if [ \$i -ge 60 ]; then echo 'egress fence not enforced (NetworkPolicy ci-job-egress), refusing to run the job'; exit 1; fi; sleep 1; done\"]
        '''"
    else
      printWarn "Could not find the node address — CI jobs run without the egress-fence gate"
    fi
  fi

  printInfo "Installing GitLab Runner (chart ${GITLAB_RUNNER_CHART_VERSION:-0.81.0}, Kubernetes executor)"
  if ! helm upgrade --install gitlab-runner gitlab-runner \
      --repo https://charts.gitlab.io \
      --version "${GITLAB_RUNNER_CHART_VERSION:-0.81.0}" \
      --namespace "$ns" \
      --set gitlabUrl="http://gitlab.${ns}.svc" \
      --set runners.secret=gitlab-runner-secret \
      --set rbac.create=true \
      --set concurrent=4 \
      --set resources.requests.memory=64Mi \
      --values - >/dev/null <<RUNNEREOF
runners:
  config: |
    [[runners]]
      clone_url = "http://gitlab.${ns}.svc"
      [runners.kubernetes]
        namespace = "${ns}"
        image = "alpine:3.20"
        poll_timeout = 300
        [runners.kubernetes.pod_labels]
          "enablement-framework/ci-job" = "true"${gate}
RUNNEREOF
  then
    printError "helm install gitlab-runner failed"
    return 1
  fi
  if ! kubectl -n "$ns" rollout status deploy/gitlab-runner --timeout="${GITLAB_RUNNER_READY_TIMEOUT:-300}s" >/dev/null; then
    printError "GitLab Runner did not become ready"
    return 1
  fi
  printInfo "GitLab Runner ready"
}

_isIPv4() {
  printf '%s\n' "$1" | grep -Eqx '((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])'
}

_gitlabApiEndpoint() {
  # "<ip> <port>" of the Kubernetes API (on k3s: the server node's address).
  local ip port
  ip=$(kubectl get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].addresses[0].ip}' 2>/dev/null)
  port=$(kubectl get endpoints kubernetes -n default -o 'jsonpath={.subsets[0].ports[0].port}' 2>/dev/null)
  case "$port" in ''|*[!0-9]*) port=6443 ;; esac
  _isIPv4 "$ip" || return 1
  echo "$ip $port"
}

_gitlabApplyJobPolicy() {
  # CI jobs run whatever a pipeline says. Their pods (label
  # enablement-framework/ci-job, set by the runner config; a job cannot change
  # it) get an egress NetworkPolicy: any pod in the cluster, the Kubernetes API,
  # and the internet — but no private, link-local or loopback address outside
  # the cluster. That keeps them off this container's ports (code-server binds
  # 0.0.0.0 under Orbital and is reached through the k3d network gateway, which
  # is not a pod, so no ingress policy can guard it) and off the cloud metadata
  # endpoint. k3s enforces NetworkPolicy with its embedded controller — about
  # a second AFTER a pod starts, so the runner config also gives every job pod
  # an init container that waits until the fence holds (_gitlabEnsureRunner).
  local ns ep api_ip api_port api_rule=""
  ns=$(_gitlabNs)
  if ep=$(_gitlabApiEndpoint); then
    api_ip=${ep% *}; api_port=${ep#* }
    api_rule="  - to:
    - ipBlock:
        cidr: ${api_ip}/32
    ports:
    - protocol: TCP
      port: ${api_port}"
  else
    printWarn "Could not find the Kubernetes API endpoint — CI jobs will not reach the API"
  fi
  kubectl apply -f - >/dev/null <<POLICYEOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ci-job-egress
  namespace: ${ns}
spec:
  podSelector:
    matchLabels:
      enablement-framework/ci-job: "true"
  policyTypes: [Egress]
  egress:
  - to:
    - namespaceSelector: {}
${api_rule}
  - to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, 127.0.0.0/8]
POLICYEOF
}

deployGitlab() {
  # deployGitlab — GitLab CE + Runner in the cluster, exposed like every app
  # (registerApp gitlab). Idempotent: a re-run keeps the password, the data,
  # a still-valid API token and runner, and only rolls GitLab when its config
  # (e.g. the external URL) changed.
  local ns url cfg sha
  ns=$(_gitlabNs)
  if ! kubectl cluster-info >/dev/null 2>&1; then
    printError "deployGitlab: no Kubernetes cluster reachable — run startCluster first"
    return 1
  fi
  printInfoSection "Deploying GitLab CE ${GITLAB_VERSION:-18.4.0-ce.0} + Runner (namespace $ns)"

  url=$(_gitlabExternalUrl)
  [ -n "$url" ] || { printError "deployGitlab: could not determine GitLab's external URL"; return 1; }
  printInfo "GitLab external URL: $url"

  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null || return 1

  if ! kubectl -n "$ns" get secret gitlab-root >/dev/null 2>&1; then
    _gitlabGenPassword | kubectl -n "$ns" create secret generic gitlab-root --from-file=password=/dev/stdin >/dev/null \
      || { printError "Could not create the GitLab root password secret"; return 1; }
    printInfo "Root password generated into secret $ns/gitlab-root (not printed)"
  fi

  cfg=$(_gitlabOmnibusConfig "$url")
  sha=$(printf '%s' "$cfg" | sha256sum | cut -c1-16)
  kubectl -n "$ns" create cm gitlab-omnibus --from-literal=gitlab.rb="$cfg" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null || return 1
  _gitlabApplyManifests "$sha" >/dev/null || { printError "Could not apply the GitLab manifests"; return 1; }
  _gitlabApplyJobPolicy || { printError "Could not apply the CI job NetworkPolicy"; return 1; }

  # A re-run must not leave two registry rows for one app.
  if [ -f "$APP_REGISTRY" ]; then
    grep -v '^gitlab|' "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null
    mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY"
  fi
  registerApp gitlab "$ns" gitlab 80 '    nginx.ingress.kubernetes.io/proxy-body-size: "512m"
    nginx.ingress.kubernetes.io/proxy-request-buffering: "off"' || return 1

  waitForGitlabReady || return 1
  _gitlabEnsureToken || return 1
  _gitlabEnsureRunner || return 1

  printInfoSection "GitLab is ready"
  printInfo "URL:  $url"
  gitlabCredentials
}

waitForGitlabReady() {
  # waitForGitlabReady [timeout-seconds] — waits for the GitLab pod to pass its
  # readiness probe (sign-in page 200). First boot runs DB migrations: ~3 min
  # warm, plus the image pull (3.8 GB) on a cold host.
  local t="${1:-${GITLAB_READY_TIMEOUT:-1200}}"
  printInfo "Waiting for GitLab to be ready (up to ${t}s; first boot runs migrations)"
  if ! kubectl -n "$(_gitlabNs)" rollout status deploy/gitlab --timeout="${t}s" >/dev/null; then
    printError "GitLab did not become ready within ${t}s"
    printError "Inspect with: kubectl -n $(_gitlabNs) logs deploy/gitlab --tail=50"
    return 1
  fi
  printInfo "GitLab is ready"
}

gitlabCredentials() {
  # Says how to sign in. Prints the command that reveals the password, never
  # the password itself — this output lands in setup logs Orbital stores.
  printInfo "User: root"
  printInfo "Password: kubectl -n $(_gitlabNs) get secret gitlab-root -o jsonpath='{.data.password}' | base64 -d; echo"
}

gitlabSeedRepo() {
  # gitlabSeedRepo <name> <dir> — creates project root/<name> (private) if it
  # does not exist and pushes <dir>'s files to its main branch, as one commit
  # on top of what is there. Re-running with unchanged files pushes nothing.
  # Files: what git tracks when <dir> is a git work tree (nothing gitignored),
  # everything otherwise — and never .git or a .env* file, tracked or not.
  # Git reaches GitLab through the cluster ingress on this host (the same
  # route the browser uses, minus Orbital), authenticated via GIT_ASKPASS.
  local name="$1" src="$2" ns code host url work ap rc gt="${GITLAB_GIT_TIMEOUT:-600}"
  if [ -z "$name" ] || [ -z "$src" ]; then
    printError "gitlabSeedRepo: usage: gitlabSeedRepo <name> <dir>"
    return 1
  fi
  case "$name" in
    *[!a-zA-Z0-9._-]*|[._-]*) printError "gitlabSeedRepo: invalid project name '$name'"; return 1 ;;
  esac
  if [ ! -d "$src" ]; then
    printError "gitlabSeedRepo: '$src' is not a directory"
    return 1
  fi
  ns=$(_gitlabNs)

  code=$(_gitlabApi GET "/projects/root%2F${name}" -o /dev/null | _gitlabHttpCode)
  if [ "$code" = "404" ]; then
    code=$(_gitlabApi POST /projects --data "name=${name}" --data "path=${name}" \
      --data 'visibility=private' -o /dev/null | _gitlabHttpCode)
    [ "$code" = "201" ] || { printError "gitlabSeedRepo: could not create project $name (HTTP ${code:-none})"; return 1; }
    printInfo "Created GitLab project root/$name"
  elif [ "$code" != "200" ]; then
    printError "gitlabSeedRepo: GitLab API not usable (HTTP ${code:-none}) — is GitLab deployed and ready?"
    return 1
  fi

  host=$(awk -F'|' '$1 == "gitlab" { print $5 }' "$APP_REGISTRY" 2>/dev/null | tail -n 1)
  [ -n "$host" ] || { printError "gitlabSeedRepo: gitlab is not in the app registry — run deployGitlab"; return 1; }
  url="${GITLAB_GIT_URL:-http://${host}}/root/${name}.git"

  work=$(mktemp -d) || return 1
  ap="$work/askpass"
  printf '#!/bin/sh\ncase "$1" in Username*) echo root ;; *) printf "%%s\\n" "$GITLAB_ASKPASS_TOKEN" ;; esac\n' > "$ap"
  chmod 700 "$ap"
  (
    export GIT_ASKPASS="$ap" GIT_TERMINAL_PROMPT=0
    GITLAB_ASKPASS_TOKEN=$(_gitlabSecretValue gitlab-pat token)
    export GITLAB_ASKPASS_TOKEN
    timeout "$gt" git -c "http.curloptResolve=${host}:80:127.0.0.1" -c credential.helper= \
      clone -q "$url" "$work/repo" 2>/dev/null || { echo "clone failed" >&2; exit 1; }
    if git -C "$src" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      # Tracked files that exist (a deleted, not yet committed file is skipped).
      git -C "$src" ls-files -z | ( cd "$src" && xargs -0 -r sh -c \
        'for f; do if [ -e "$f" ] || [ -L "$f" ]; then printf "%s\0" "$f"; fi; done' sh )
    else
      ( cd "$src" && find . -mindepth 1 \( -name .git -prune \) -o \( ! -type d -print0 \) )
    fi | grep -zEv '(^|/)\.git(/|$)|(^|/)\.env[^/]*$' \
      | tar -C "$src" --null --no-recursion -T - -cf - | tar -C "$work/repo" -xf - || exit 1
    cd "$work/repo" || exit 1
    git checkout -q -B main
    git add -A
    if git diff --cached --quiet; then
      echo "nothing to push" >&2; exit 0
    fi
    git -c user.name="Enablement Framework" -c user.email="framework@enablement.local" \
      commit -q -m "Seed from $(basename "$src")" || exit 1
    timeout "$gt" git -c "http.curloptResolve=${host}:80:127.0.0.1" -c credential.helper= \
      push -q origin main > "$work/push.log" 2>&1
    pushed=$?
    sed 's/glpat-[A-Za-z0-9_.-]*/<token>/g' "$work/push.log" >&2
    exit "$pushed"
  )
  rc=$?
  rm -rf "$work"
  if [ "$rc" -ne 0 ]; then
    printError "gitlabSeedRepo: push of $src to root/$name failed"
    return 1
  fi
  printInfo "Seeded root/$name from $src"
}

checkGitlabRunning() {
  [ -n "${LAB_WAIT:-}" ] && ( waitForGitlabReady )
  if kubectl -n "$(_gitlabNs)" get deploy gitlab -o jsonpath='{.status.readyReplicas}' 2>/dev/null | grep -qx 1; then
    printInfo "GitLab is running and ready"; return 0
  fi
  printError "GitLab is not ready yet — the environment may still be starting"; return 1
}
waitForGitlabRunning() { LAB_WAIT=1 checkGitlabRunning; }

uninstallGitlab() {
  # Removes the runner, the GitLab ingress + registry row and the namespace
  # (which deletes the PVC: the repositories and the database go with it).
  local ns
  ns=$(_gitlabNs)
  printInfoSection "Uninstalling GitLab (namespace $ns)"
  helm uninstall gitlab-runner -n "$ns" >/dev/null 2>&1 || true   # absent release is fine
  unregisterApp gitlab "$ns"
  if ! kubectl delete namespace "$ns" --ignore-not-found --wait=true --timeout=300s >/dev/null 2>&1; then
    printError "uninstallGitlab: deleting namespace $ns failed or timed out — check: kubectl get ns $ns"
    return 1
  fi
  printInfo "GitLab removed"
}

# ======================================================================
#  code-server — VS Code in the browser, inside the learner container
#
#  Runs as the learner (vscode) in the same container as the browser
#  terminal: same files, kubectl context, tools and zsh greeting.
#  Auth: code-server's own password whenever PASSWORD (its native env
#  var) is set — Orbital sets it and logs in server-side, because a
#  slot's ports on the worker are reachable from every OTHER learner's
#  container on that host. Under Orbital it refuses to start without
#  one. Elsewhere, unset PASSWORD means --auth none on 127.0.0.1.
# ======================================================================

_codeServerHome() { echo "${CODE_SERVER_HOME:-$HOME/.local/lib/code-server}"; }
_codeServerBin() { echo "$(_codeServerHome)/bin/code-server"; }
_codeServerPidFile() { echo "${XDG_CACHE_HOME:-$HOME/.cache}/code-server/pid"; }

_codeServerInstalledVersion() {
  local bin
  bin=$(_codeServerBin)
  [ -x "$bin" ] || return 1
  # The first run also prints a log line ("[timestamp] Wrote default config…").
  "$bin" --version 2>/dev/null | awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+$/ { print $1; exit }'
}

_codeServerSettings() {
  # User settings, written once (a learner's later edits are kept):
  # no workspace-trust modal, no chat/agent panel, zsh terminal (the same
  # shell + greeting as the browser terminal), no telemetry or update checks,
  # dark theme, and the terminal in MesloLGS NF: the prompt's git branch glyph
  # is a Nerd Font private-use glyph (the browser needs the font; Orbital
  # serves it to the IDE window same-origin: orbital #168).
  local dir="${XDG_DATA_HOME:-$HOME/.local/share}/code-server/User"
  [ -f "$dir/settings.json" ] && return 0
  mkdir -p "$dir" || return 1
  cat > "$dir/settings.json" <<'CSEOF'
{
  "security.workspace.trust.enabled": false,
  "terminal.integrated.defaultProfile.linux": "zsh",
  "chat.disableAIFeatures": true,
  "chat.commandCenter.enabled": false,
  "workbench.startupEditor": "none",
  "telemetry.telemetryLevel": "off",
  "update.mode": "none",
  "extensions.autoCheckUpdates": false,
  "workbench.colorTheme": "Default Dark Modern",
  "terminal.integrated.fontFamily": "'MesloLGS NF', monospace"
}
CSEOF
}

installCodeServer() {
  # installCodeServer [version] — code-server from the release tarball (its
  # install.sh reads $ARCH, which Orbital sets to x86_64, and refuses). The
  # framework's pinned version is verified against the reviewed sha256; any
  # other version needs CODE_SERVER_SHA256, or it is refused.
  local version="${1:-${CODE_SERVER_VERSION:-${CODE_SERVER_PIN_VERSION:-}}}" arch have sha tmp home
  version="${version#v}"
  _isVersion "$version" || { printError "installCodeServer: invalid version '$version'"; return 1; }
  arch=$(_releaseArch) || { printError "installCodeServer: unsupported architecture $(uname -m)"; return 1; }

  have=$(_codeServerInstalledVersion)
  if [ "$have" = "$version" ]; then
    printInfo "code-server $version already installed"
    _codeServerSettings
    _codeServerRegistryRow || return 1
    return 0
  fi

  if [ "$version" = "${CODE_SERVER_PIN_VERSION:-}" ]; then
    if [ "$arch" = "amd64" ]; then sha="${CODE_SERVER_PIN_SHA256_AMD64:-}"; else sha="${CODE_SERVER_PIN_SHA256_ARM64:-}"; fi
  else
    sha="${CODE_SERVER_SHA256:-}"
  fi
  if [ -z "$sha" ]; then
    printError "installCodeServer: no reviewed sha256 for code-server $version ($arch) — set CODE_SERVER_SHA256 or use the pinned ${CODE_SERVER_PIN_VERSION:-version}. Refusing."
    return 1
  fi

  printInfoSection "Installing code-server $version ($arch)"
  tmp=$(mktemp -d) || return 1
  if ! _fetch "https://github.com/coder/code-server/releases/download/v${version}/code-server-${version}-linux-${arch}.tar.gz" \
      "$tmp/cs.tgz" 600; then
    printError "installCodeServer: download failed"
    rm -rf "$tmp"; return 1
  fi
  if ! verifySha256 "$tmp/cs.tgz" "$sha"; then
    rm -rf "$tmp"; return 1
  fi
  home=$(_codeServerHome)
  mkdir -p "$(dirname "$home")" "$HOME/.local/bin" || { rm -rf "$tmp"; return 1; }
  if ! tar -C "$tmp" -xzf "$tmp/cs.tgz"; then
    printError "installCodeServer: could not unpack the tarball"
    rm -rf "$tmp"; return 1
  fi
  rm -rf "$home"
  mv "$tmp/code-server-${version}-linux-${arch}" "$home" || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  ln -sfn "$home/bin/code-server" "$HOME/.local/bin/code-server"
  _codeServerSettings
  _codeServerRegistryRow || return 1
  printInfo "code-server $(_codeServerInstalledVersion) installed in $home"
}

_codeServerRegistryRow() {
  # Writes the `ide` app-registry row at install time, so Orbital knows this
  # workspace HAS an IDE before anyone opens it: the row means "installed",
  # not "running". Same seven fields registerApp writes; one row (upsert).
  # Only the row — no Service/Ingress (post-create must not need the cluster)
  # and no password: startCodeServer -> registerCodeServer creates those and
  # rewrites the same row. Gated like registerCodeServer: Orbital only, or
  # CODE_SERVER_REGISTER=1.
  local ns="${CODE_SERVER_NAMESPACE:-ide}" port="${CODE_SERVER_PORT:-8443}" sub=""
  case "$(detectRunEnvironment)" in
    orbital|orbital_codespaces) ;;
    *) [ "${CODE_SERVER_REGISTER:-}" = "1" ] || return 0 ;;
  esac
  if [ "$(detectRunEnvironment)" = "orbital" ] && [ -n "${ORBITAL_JOB_ID:-}" ]; then
    sub=$(computeOrbitalSubdomain ide)
  fi
  mkdir -p "$(dirname "$APP_REGISTRY")" || { printError "VS Code (ide) NOT registered: cannot create $(dirname "$APP_REGISTRY")"; return 1; }
  if [ -f "$APP_REGISTRY" ]; then
    # grep -v exits 1 when every row was an ide row: that is not a failure.
    { grep -v '^ide|' "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" || [ $? -eq 1 ]; } 2>/dev/null \
      && mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" \
      || { rm -f "${APP_REGISTRY}.tmp" 2>/dev/null; printError "VS Code (ide) NOT registered: could not rewrite $APP_REGISTRY (left as it was)"; return 1; }
  fi
  if ! echo "ide|${ns}|ide|${port}|ide.$(detectIP).${MAGIC_DOMAIN}||${sub}" >> "$APP_REGISTRY" 2>/dev/null; then
    printError "VS Code (ide) NOT registered: could not write $APP_REGISTRY"
    return 1
  fi
  printInfo "VS Code (ide) registered in the app registry"
}

_codeServerOwnsPort() {
  # True when the listener on CODE_SERVER_PORT belongs to OUR code-server: a
  # process in the session whose leader is in the pid file (code-server's
  # HTTP server is a child of the process started). Reads /proc only.
  local pid port hex inodes p sid i
  port="${CODE_SERVER_PORT:-8443}"
  pid=$(cat "$(_codeServerPidFile)" 2>/dev/null)
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  [ -d "/proc/$pid" ] || return 1
  hex=$(printf '%04X' "$port")
  # state 0A = LISTEN; field 10 = socket inode
  inodes=$(awk -v h=":$hex" '$4 == "0A" && substr($2, length($2) - 4) == h { print $10 }' \
    /proc/net/tcp /proc/net/tcp6 2>/dev/null)
  [ -n "$inodes" ] || return 1
  for p in /proc/[0-9]*; do
    # session id: 4th field after the ")" that closes the command name
    sid=$(sed 's/^.*) //' "$p/stat" 2>/dev/null | cut -d' ' -f4)
    [ "$sid" = "$pid" ] || continue
    for i in $inodes; do
      ls -l "$p/fd" 2>/dev/null | grep -qF "socket:[$i]" && return 0
    done
  done
  return 1
}

_codeServerHealthy() {
  # /healthz answers 200 AND the answering listener is ours — another process
  # on the port must not pass for code-server.
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${CODE_SERVER_PORT:-8443}/healthz" 2>/dev/null)" = "200" ] \
    && _codeServerOwnsPort
}

startCodeServer() {
  # startCodeServer [dir] — starts code-server on CODE_SERVER_PORT (8443) in
  # its own session, opening <dir> (default: the repo). Already healthy -> no-op.
  # Bind: 0.0.0.0 under Orbital (the slot publishes the port to the master
  # only), 127.0.0.1 anywhere else. CODE_SERVER_BIND overrides.
  local dir="${1:-${REPO_PATH:-$PWD}}" port="${CODE_SERVER_PORT:-8443}" bind pidf bin i auth
  if [ ! -d "$dir" ]; then
    printError "startCodeServer: '$dir' is not a directory"
    return 1
  fi
  case "$port" in ''|*[!0-9]*) printError "startCodeServer: invalid CODE_SERVER_PORT '$port'"; return 1 ;; esac
  if _codeServerHealthy; then
    printInfo "code-server already running on port $port"
    registerCodeServer
    return
  fi
  [ -n "$(_codeServerInstalledVersion)" ] || installCodeServer || return 1
  bin=$(_codeServerBin)
  case "$(detectRunEnvironment)" in
    orbital|orbital_codespaces) bind="${CODE_SERVER_BIND:-0.0.0.0}" ;;
    *) bind="${CODE_SERVER_BIND:-127.0.0.1}" ;;
  esac
  if [ -n "${PASSWORD:-}" ] || [ -n "${HASHED_PASSWORD:-}" ]; then
    auth=password
  else
    case "$(detectRunEnvironment)" in
      orbital|orbital_codespaces)
        printError "startCodeServer: PASSWORD is not set — under Orbital another learner's container can reach this port, so code-server will not run without auth. Refusing."
        return 1 ;;
    esac
    # No password is only acceptable when nothing but this container can
    # connect: a non-loopback bind would hand a shell to the network.
    case "$bind" in
      127.*|localhost|::1|'[::1]') ;;
      *) printError "startCodeServer: CODE_SERVER_BIND=$bind without PASSWORD would serve an unauthenticated IDE beyond this container. Set PASSWORD or bind 127.0.0.1. Refusing."
         return 1 ;;
    esac
    auth=none
  fi
  pidf=$(_codeServerPidFile)
  mkdir -p "$(dirname "$pidf")" || return 1
  # setsid: own session, so stopCodeServer can end it with every extension
  # host and terminal it spawned. --reconnection-grace-time bounds what an
  # abandoned browser tab keeps alive (spike 92: six closed tabs held 2.2 GiB).
  # The session leader writes its own pid: $! would name setsid itself when
  # an interactive shell made the job a group leader and setsid had to fork.
  rm -f "$pidf"
  ( setsid sh -c 'echo $$ > "$1"; shift; exec "$@"' sh "$pidf" \
      "$bin" --bind-addr "${bind}:${port}" --auth "$auth" \
      --disable-telemetry --disable-update-check --disable-workspace-trust \
      --reconnection-grace-time "${CODE_SERVER_RECONNECT_GRACE:-600}" \
      "$dir" > "$(dirname "$pidf")/code-server.log" 2>&1 < /dev/null & )
  i=0
  while [ "$i" -lt 60 ]; do
    _codeServerHealthy && break
    i=$((i + 1)); sleep 0.5
  done
  if ! _codeServerHealthy; then
    printError "code-server did not answer /healthz within 30s — see $(dirname "$pidf")/code-server.log"
    return 1
  fi
  printInfo "code-server running on ${bind}:${port} (auth $auth, pid $(cat "$pidf" 2>/dev/null)), workspace $dir"
  registerCodeServer
}

_codeServerGateway() {
  # The address pods (and the ingress controller) reach this container on: the
  # gateway of the k3d node's docker network. Under Orbital the learner
  # container shares the slot's network namespace, which owns that gateway.
  local node="${CODE_SERVER_K3D_NODE:-k3d-${K3D_CLUSTER_NAME:-enablement}-server-0}"
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' "$node" 2>/dev/null | awk 'NF { print; exit }'
}

registerCodeServer() {
  # registerCodeServer — exposes code-server like every other app: app "ide" in
  # the app registry, an Ingress, and a selector-less Service whose endpoint is
  # this container (code-server is not a pod). Orbital reads the registry, so
  # the IDE is listed with the workspace's apps like any other. Only under
  # Orbital by default, and never without a password (an unauthenticated
  # IDE on the ingress is a shell for anyone who reaches it).
  # CODE_SERVER_REGISTER=1 forces it outside Orbital.
  local ns="${CODE_SERVER_NAMESPACE:-ide}" port="${CODE_SERVER_PORT:-8443}" gw
  case "$(detectRunEnvironment)" in
    orbital|orbital_codespaces) ;;
    *) [ "${CODE_SERVER_REGISTER:-}" = "1" ] || { printInfo "code-server not registered as an app outside Orbital (set CODE_SERVER_REGISTER=1 to force)"; return 0; } ;;
  esac
  if [ -z "${PASSWORD:-}" ] && [ -z "${HASHED_PASSWORD:-}" ]; then
    printWarn "registerCodeServer: code-server has no password — not exposing it through the ingress"
    return 0
  fi
  gw=$(_codeServerGateway)
  if ! _isIPv4 "$gw"; then
    printError "registerCodeServer: could not find the k3d network gateway — is the cluster running?"
    return 1
  fi
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null || return 1
  kubectl apply -f - >/dev/null <<IDEEOF || { printError "registerCodeServer: could not create the ide Service"; return 1; }
apiVersion: v1
kind: Service
metadata:
  name: ide
  namespace: ${ns}
spec:
  ports:
  - name: http
    port: ${port}
    targetPort: ${port}
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: ide-learner-container
  namespace: ${ns}
  labels:
    kubernetes.io/service-name: ide
addressType: IPv4
ports:
- name: http
  port: ${port}
  protocol: TCP
endpoints:
- addresses: ["${gw}"]
IDEEOF
  if [ -f "$APP_REGISTRY" ]; then
    grep -v '^ide|' "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null
    mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY"
  fi
  registerApp ide "$ns" ide "$port"
}

stopCodeServer() {
  # Ends code-server's whole session (server, extension hosts, terminals). The
  # `ide` registry row STAYS: it means "installed", not "running". Orbital
  # stops code-server after a failed sign-in, and dropping the row there would
  # hide the VS Code button for the rest of the session; the next click starts
  # it again. Only uninstallCodeServer removes the row. The Ingress registerApp
  # made IS deleted, so the host answers 404 (no route), never a 502 from an
  # ingress pointing at a dead listener; registerCodeServer re-creates it on
  # the next start. The Service stays (harmless without a route).
  local pidf pid
  pidf=$(_codeServerPidFile)
  pid=$(cat "$pidf" 2>/dev/null)
  if [ -n "$pid" ] && grep -q code-server "/proc/$pid/cmdline" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    printInfo "code-server stopped (pid $pid)"
  else
    printInfo "code-server is not running"
  fi
  rm -f "$pidf"
  kubectl delete ingress ide-ingress -n "${CODE_SERVER_NAMESPACE:-ide}" --ignore-not-found >/dev/null 2>&1
}

uninstallCodeServer() {
  # Stops code-server, removes its app registration and the install. User
  # settings and extensions stay.
  stopCodeServer
  unregisterApp ide "${CODE_SERVER_NAMESPACE:-ide}" >/dev/null 2>&1
  kubectl delete namespace "${CODE_SERVER_NAMESPACE:-ide}" --ignore-not-found >/dev/null 2>&1
  rm -rf "$(_codeServerHome)"
  rm -f "$HOME/.local/bin/code-server"
  printInfo "code-server removed"
}

checkCodeServerRunning() {
  if _codeServerHealthy; then
    printInfo "code-server is running on port ${CODE_SERVER_PORT:-8443}"; return 0
  fi
  printError "code-server is not running on port ${CODE_SERVER_PORT:-8443}"; return 1
}

deployApp(){
  
  if [ "$#" -eq 0 ]; then
    showDeployAppUsage
    return 0
  elif [ "$#" -eq 1 ]; then
    local input="$1"
  elif [ "$#" -eq 2 ]; then
    local input="$1"
    if [[ "$2" == "-d" ]]; then
      local delete=true
    else
      printWarn "Unexpected 2nd argument"
      showDeployAppUsage
      return 1
    fi
  else
    printWarn "Unexpected number of arguments"
    showDeployAppUsage
    return 1
  fi

  case "$input" in
    1 | a | ai-travel-advisor)
      if [[ $delete ]]; then
        printInfoSection "Undeploying ai-travel-advisor..."
        unregisterApp "ai-travel-advisor" "ai-travel-advisor"
        kubectl delete ns ai-travel-advisor --force
      else
        deployAITravelAdvisorApp
      fi
      ;;

    2 | b | astroshop)
      if [[ $delete ]]; then
        printInfoSection "Undeploying astroshop..."
        unregisterApp "astroshop" "astroshop"
        kubectl delete ns astroshop --force
      else
        deployAstroshop
      fi
      ;;

    3 | c | bugzapper)
       if [[ $delete ]]; then
        printInfo "Undeploying bugzapper..."
        unregisterApp "bugzapper" "bugzapper"
        kubectl delete ns bugzapper --force
      else
        deployBugZapperApp
      fi
      ;;

    4 | d | easytrade)
       if [[ $delete ]]; then
        printInfo "Undeploying easytrade..."
        unregisterApp "easytrade" "easytrade"
        kubectl delete ns easytrade --force
      else
        deployEasyTrade
      fi
      ;;

    5 | e | hipstershop)
       if [[ $delete ]]; then
        printInfo "Undeploying hipstershop..."
        unregisterApp "hipstershop" "hipstershop"
        kubectl delete ns hipstershop --force
      else
        deployHipsterShop
      fi
      ;;

    6 | f | todoapp)
       if [[ $delete ]]; then
        printInfo "Undeploying todoapp..."
        unregisterApp "todoapp" "todoapp"
        kubectl delete ns todoapp --force
      else
        deployTodoApp
      fi
      ;;

    7 | g | unguard)
       if [[ $delete ]]; then
        printInfo "Undeploying unguard..."
        unregisterApp "unguard" "unguard"
        undeployUnguard
      else
        deployUnguard
      fi
      ;;

    8 | h | opentelemetry-demo | otel-demo)
       if [[ $delete ]]; then
        printInfo "Undeploying opentelemetry-demo..."
        undeployOpentelemetryDemo
      else
        deployOpentelemetryDemo
      fi
      ;;

    9 | i | gitlab)
       if [[ $delete ]]; then
        uninstallGitlab
      else
        deployGitlab
      fi
      ;;

    *)
      printWarn "Invalid selection: '$input'. Please choose a valid app identifier."
      showDeployAppUsage
      return 1
      ;;
  esac
  return 0
}

showDeployAppUsage(){
  printInfoSection "   Un/Deploy an Application to your Kubernetes Cluster      "
  printInfo "                ${PACKAGE} Application repository  ${PACKAGE}                               "
  printInfo "                                                                            "
  printInfo "For deploying one of the following apps, type the number, character or name "
  printInfo "associated e.g. for astroshop type deployApp '2', 'b' or 'astroshop'        "
  printInfo "                                                                            "
  printInfo "For undeploying an app, type -d as an extra argument                        "
  printInfo "----------------------------------------------------------------------------"
  printInfo "[#]  [c]  [ name ]             AMD     ARM                                  "
  printInfo "[1]   a   ai-travel-advisor     +       +                                   "
  printInfo "[2]   b   astroshop             +       -                                   "
  printInfo "[3]   c   bugzapper             +       +                                   "
  printInfo "[4]   d   easytrade             +       -                                   "
  printInfo "[5]   e   hipstershop           +       -                                   "
  printInfo "[6]   f   todoapp               +       +                                   "
  printInfo "[7]   g   unguard               +       -                                   "
  printInfo "[8]   h   opentelemetry-demo    +       +    (CNCF upstream)                "
  printInfo "[9]   i   gitlab                +       +    (GitLab CE + Runner)          "
  printInfo "----------------------------------------------------------------------------"
  printInfo "Astroshop = Dynatrace-curated demo | OpenTelemetry Demo = CNCF upstream    "
}

deleteCache(){
  local container_cache="${REPO_PATH}/.devcontainer/.cache"
  local host_cache="${HOME}/.cache/dt-framework"

  if [ -d "$container_cache" ]; then
    rm -rf "$container_cache"
    printInfo "Container cache deleted: $container_cache"
  else
    printInfo "No container cache found"
  fi

  if [ -d "$host_cache" ]; then
    rm -rf "$host_cache"
    printInfo "Host cache deleted: $host_cache"
  else
    printInfo "No host cache found"
  fi

  printInfoSection "Cache cleared. Run 'source .devcontainer/util/source_framework.sh' to re-pull."
}

deleteCodespace(){
  printWarn "Warning! Codespace $CODESPACE_NAME will be deleted, the connection will be lost in a sec... "
  gh codespace delete --codespace "$CODESPACE_NAME" --force
}


showOpenPorts(){
  sudo netstat -tulnp
  # another alternative is 
  # sudo ss -tulnp
}

deployGhdocs(){
  mkdocs gh-deploy
}

getRunningDockerContainernameByImagePattern(){
  pattern=$1

  containername=$(docker ps --filter "status=running" --format "{{.Names}} {{.Image}}" | grep $pattern | awk '{print $1}')

  echo $containername

}

# Back-compat: the function was called verifyCodespaceCreation until 1.10.0. It
# verifies a *container* creation regardless of whether that container is a
# Codespace, an Orbital slot or a local dev container, hence the rename. Older
# docs and muscle memory still reach for the old name, so keep it working.
verifyCodespaceCreation(){
  verifyContainerCreation "$@"
}

_creationNoiseFilter() {
  # Reads log lines on stdin, prints only those that are real errors: the
  # grep for 'error|failed' in verifyContainerCreation is intentionally broad,
  # so known noise is dropped here. Patterns are EXTENDED regexes — escape ( ).
  grep -v -i -E \
      -e 'no errors detected' \
      -e 'errors detected in the creation' \
      -e 'There has been.*error' \
      -e 'configmap.*dtcredentials' \
      -e 'Verify Codespace creation' \
      -e 'error_count' \
      -e 'ERROR_COUNT' \
      -e 'npm warn' \
      -e 'npm WARN' \
      -e 'WARN.*not set' \
      -e 'warning:' \
      -e 'ErrorPolicy' \
      -e 'error-page' \
      -e 'error\.html' \
      -e 'error_reporting' \
      -e 'errorHandler' \
      -e 'error-handling' \
      -e 'stderr' \
      -e 'printError' \
      -e 'on-error' \
      -e 'if-error' \
      -e 'onerror' \
      -e 'error\.log' \
      -e 'errors=' \
      -e 'error_' \
      -e 'IfNotPresent.*failed' \
      -e 'failedScheduling' \
      -e 'Failed to check' \
      -e 'FAILED_PRECONDITION' \
      -e 'devtools \(non-fatal\)'
}

verifyContainerCreation(){
  printInfoSection "Verify container creation"
  calculateTime

  # Collect raw logs based on instantiation type
  local raw_errors=""
  if [[ $INSTANTIATION_TYPE == "github-codespaces" ]]; then
    if [ -f "$CODESPACE_PSHARE_FOLDER/creation.log" ]; then
      raw_errors=$(grep -i -E 'error|failed' "$CODESPACE_PSHARE_FOLDER/creation.log" 2>/dev/null || true)
    fi
  elif [[ $INSTANTIATION_TYPE == "remote-container" ]] || [[ $INSTANTIATION_TYPE == "github-workflow" ]]; then
    local containername
    containername=$(getRunningDockerContainernameByImagePattern "vsc")
    if [ -n "$containername" ]; then
      raw_errors=$(docker logs "$containername" 2>&1 | grep -i -E 'error|failed' || true)
    fi
  elif [[ $INSTANTIATION_TYPE == "local-docker-container" ]]; then
    local containername
    containername=$(getRunningDockerContainernameByImagePattern "dt-enablement")
    if [ -n "$containername" ]; then
      raw_errors=$(docker logs "$containername" 2>&1 | grep -i -E 'error|failed' || true)
    fi
  elif [[ $INSTANTIATION_TYPE == "orbital" ]]; then
    # Inside Orbital Sysbox; executor streams logs to Redis — no extra collection needed.
    true
  else
    printWarn "Unknown instantiation type: $INSTANTIATION_TYPE"
  fi

  # Filter out known noise patterns that are not real errors.
  # The grep for 'error|failed' is intentionally broad so we catch real issues,
  # but it also catches the framework's own log output, informational messages,
  # and compound words. We filter those out here.
  CODESPACE_ERRORS=""
  if [ -n "$raw_errors" ]; then
    CODESPACE_ERRORS=$(printf "%s" "$raw_errors" | _creationNoiseFilter || true)
  fi

  if [ -n "$CODESPACE_ERRORS" ]; then
    # wc -l counts newlines, not lines — add a trailing newline so the last line is counted
    ERROR_COUNT=$(printf "%s\n" "$CODESPACE_ERRORS" | grep -c .)
  else
    ERROR_COUNT=0
  fi

  if [ "$ERROR_COUNT" -gt 0 ]; then
    printWarn "$ERROR_COUNT issues detected in the creation of the codespace:"
    printWarn "$CODESPACE_ERRORS"
  else
    printInfo "No errors detected in the creation of the codespace"
  fi

  export CODESPACE_ERRORS
  export ERROR_COUNT
  updateEnvVariable ERROR_COUNT
}

calculateTime(){
  # Read from file
  if [ -e "$COUNT_FILE" ]; then
    source $COUNT_FILE
  fi
  # if equal 0 then set duration and update file
  if [ "$DURATION" -eq 0 ]; then 
    DURATION="$SECONDS"
    updateEnvVariable DURATION
  fi
  printInfo "It took $(($DURATION / 60)) minutes and $(($DURATION % 60)) seconds the finalizePostCreation-creation of the codespace."
}

updateEnvVariable(){
  local variable="$1"
  # Checking the process name (zsh/bash)
  if [[ "$(ps -p $$ -o comm=)" == "zsh" ]]; then
    #printInfo "ZSH"
    #printInfo "update [$variable:${(P)variable}]"
    # indirect variable expansion in ZSH
    # shellcheck disable=SC2296
    sed "s|^$variable=.*|$variable=${(P)variable}|" $COUNT_FILE > $COUNT_FILE.tmp
    mv $COUNT_FILE.tmp $COUNT_FILE
  else
    #printInfo "BASH"
    #printInfo "update [$variable:${!variable}]"
    # indirect variable expansion in BASH
    sed "s|^$variable=.*|$variable=${!variable}|" $COUNT_FILE  > $COUNT_FILE.tmp
    mv $COUNT_FILE.tmp $COUNT_FILE
  fi
  
  export $variable
}

finalizePostCreation(){
  # e2e testing
  # If the codespace is created (eg. via a Dynatrace workflow)
  # and hardcoded to have a name starting with dttest-bash b
  # Then run the e2e test harness
  # Otherwise, send the startup ping
  # dtctl + Monaco in every environment; FRAMEWORK_DEVTOOLS=off skips it.
  # Before verifyContainerCreation so its time counts in the creation time.
  installDevTools

  if [[ "$CODESPACE_NAME" == dttest-* ]]; then
      # Set default repository for gh CLI
      gh repo set-default "$GITHUB_REPOSITORY"

      # Set up a label, used if / when the e2e test fails
      # This may already be set, so catch error and always return true
      gh label create "e2e test failed" --force || true

      # Install required Python packages
      pip install -r "$REPO_PATH/.devcontainer/testing/requirements.txt" --break-system-packages

      # Run the test harness script
      python "$REPO_PATH/.devcontainer/testing/testharness.py"

      # Testing finished. Destroy the codespace
      gh codespace delete --codespace "$CODESPACE_NAME" --force
  else
      
      verifyContainerCreation
      postCodespaceTracker
  fi
}


runIntegrationTests(){
  #this function will trigger the integration Tests for this repo.
  bash "${REPO_PATH}/.devcontainer/test/integration.sh"
}

calculateReadingTime(){
  
  printInfoSection "Calculating the reading time of the Documentation"
  DOCS_DIR="/docs"
  WORDS_PER_MIN=200
  total_words=0
  total_mins=0

  printInfo "Section \t\t| Words \t| Estimated Reading Time (min)"
  printInfo "--------\t\t|-------\t|-----------------------------"
  find "$REPO_PATH/$DOCS_DIR" -type f -name "*.md" | while read -r file; do
      section=$(basename "$file")
      words=$(wc -w < "$file")
      # Calculate reading time, rounding up
      mins=$(( (words + WORDS_PER_MIN - 1) / WORDS_PER_MIN ))
      total_words=$((total_words + words))
      total_mins=$((total_mins + mins))

      printInfo "$section \t\t| $words \t| $mins min"
  done
  
  printInfo "---------------------------------------------"
  printInfo "TOTAL     | $total_words | $total_mins min"

}

checkHost(){

  printInfoSection "Verifying Host requirements"
  make_available=false
  docker_available=false
  docker_accessible=false
  node_available=false
  npm_available=false
  #TODO: Check that the files can be modified, needed for the docker user to write in the volume mount, test @ignacio.goldman setup.

  # Check if host is Ubuntu
  if grep -qi ubuntu /etc/os-release; then
    printInfo "✅ Ubuntu detected"
  else
    printWarn "⚠️ Not Ubuntu, we can't guarantee proper functioning"
  fi

  # Check if make is installed
  if command -v make >/dev/null; then
    printInfo "✅ make is installed (version: $(make --version))"
    make_available=true
  else
    printWarn "❌ make is NOT installed"
    make_available=false
  fi

  # Check if docker is installed
  if command -v docker >/dev/null; then
    printInfo "✅ docker is installed (version: $(docker --version))"
    docker_available=true
  else
    printWarn "❌ docker is NOT installed"
    docker_available=false
  fi

  # Check if user has access to docker
  if docker info >/dev/null 2>&1; then
    printInfo "✅ Docker is accessible"
    docker_accessible=true
  else
    printWarn "❌ No access to Docker"
    docker_accessible=false
  fi

  # Check if node is installed
  if command -v node >/dev/null; then
    printInfo "✅ node is installed (version: $(node --version))"
    node_available=true
  else
    printWarn "❌ node is NOT installed (needed for Dynatrace MCP Server)"
    node_available=false
  fi

  # Check if npm is installed
  if command -v npm >/dev/null; then
    printInfo "✅ npm is installed (version: $(npm --version)) "
    npm_available=true
  else
    printWarn "❌ npm is NOT installed (needed for MCP Server)"
    npm_available=false
  fi

  # Prompt if any requirement is missing
  if [ "$make_available" = false ] || [ "$docker_available" = false ] || [ "$docker_accessible" = false ] || [ "$node_available" = false ] || [ "$npm_available" = false ]; then
    printWarn "One or more requirements are missing or not accessible"
    printWarn "Would you like to attempt to correct them now? (y/n) 'yes' to run the commands for you, 'n' we only print how to resolve the issue"
    read -r answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
      # Install make if missing
      if [ "$make_available" = false ]; then
        printInfo "Installing make..."
        sudo apt-get update && sudo apt-get install -y make
      fi
      # Install docker if missing
      if [ "$docker_available" = false ]; then
        printInfo "Installing docker..."
        sudo apt-get update && sudo apt-get install -y docker.io
        sudo systemctl enable --now docker
      fi
      # Add user to docker group if docker not accessible
      if [ "$docker_accessible" = false ]; then
        printInfo "Adding user $USER to docker group and restarting docker..."
        sudo usermod -aG docker $USER
        sudo systemctl restart docker
        printWarn "You may need to log out and log back in for group changes to take effect."
      fi
      # Install node if missing
      if [ "$node_available" = false ]; then
        printInfo "Installing nodejs..."
        curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - \
          && sudo apt-get install -y nodejs 
      fi
      # Install npm if missing
      if [ "$npm_available" = false ]; then
        printInfo "Installing npm..."
        sudo npm install -g npm@latest  && sudo rm -rf /var/lib/apt/lists/*
      fi
      printInfo "Auto-fix attempted. Please re-run this function or open a new shell."
    else
      printWarn "Host setup not corrected. Some features may not work as expected."
      if [ "$make_available" = false ]; then
        printInfo "To install make: sudo apt-get update && sudo apt-get install -y make"
      fi
      if [ "$docker_available" = false ]; then
        printInfo "To install docker: sudo apt-get update && sudo apt-get install -y docker.io && sudo systemctl enable --now docker"
      fi
      if [ "$docker_accessible" = false ]; then
        printInfo "To enable Docker access: sudo usermod -aG docker $USER && sudo systemctl restart docker (then log out and back in)"
      fi
      if [ "$node_available" = false ]; then
        printInfo "To install nodejs: sudo apt-get update && sudo apt-get install -y nodejs"
      fi
      if [ "$npm_available" = false ]; then
        printInfo "To install npm: sudo apt-get update && sudo apt-get install -y npm"
      fi
    fi
  else
    printInfo "✅ All requirements are met for running the enablement-framework. Navigate to the .devcontainer/ folder then 'make start' to start your enablement jouney 🚀"
  fi

}


freeUpSpace(){

  printInfoSection "Freeing up disk space"
  printInfo "Disk usage before cleanup:"
  df -h / | tail -1 | awk '{printInfo "  Used: "$3" / "$2" ("$5" full) — Free: "$4}'
  df -h /

  # APT cache cleanup
  if command -v apt-get >/dev/null 2>&1; then
    printInfo "Cleaning APT cache and removing unused packages..."
    sudo apt-get autoremove -y 2>/dev/null
    sudo apt-get autoclean -y 2>/dev/null
    sudo apt-get clean 2>/dev/null
  else
    printWarn "apt-get not found, skipping APT cleanup"
  fi

  # Systemd journal logs
  if command -v journalctl >/dev/null 2>&1; then
    printInfo "Vacuuming journal logs (keeping last 7 days, max 200M)..."
    sudo journalctl --vacuum-time=7d 2>/dev/null || true
    sudo journalctl --vacuum-size=200M 2>/dev/null || true
  else
    printWarn "journalctl not found, skipping journal cleanup"
  fi

  # Python pip cache
  if command -v pip >/dev/null 2>&1; then
    printInfo "Purging pip cache..."
    pip cache purge 2>/dev/null || true
  elif command -v pip3 >/dev/null 2>&1; then
    printInfo "Purging pip3 cache..."
    pip3 cache purge 2>/dev/null || true
  else
    printWarn "pip not found, skipping Python cache cleanup"
  fi

  # Node package manager caches
  if command -v npm >/dev/null 2>&1; then
    printInfo "Cleaning npm cache..."
    npm cache clean --force 2>/dev/null || true
  fi
  if command -v yarn >/dev/null 2>&1; then
    printInfo "Cleaning yarn cache..."
    yarn cache clean 2>/dev/null || true
  fi
  if command -v pnpm >/dev/null 2>&1; then
    printInfo "Pruning pnpm store..."
    pnpm store prune 2>/dev/null || true
  fi

  # Docker cleanup
  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      printInfo "Pruning unused Docker resources (images, containers, volumes, build cache)..."
      docker system prune -af --volumes 2>/dev/null || true
      docker builder prune -af 2>/dev/null || true
    else
      printWarn "Docker is installed but not accessible, skipping Docker cleanup"
    fi
  else
    printWarn "Docker not found, skipping Docker cleanup"
  fi

  # Temp files older than 7 days
  printInfo "Removing temp files older than 7 days..."
  sudo find /tmp -mindepth 1 -mtime +7 -delete 2>/dev/null || true

  # User cache directories older than 30 days
  printInfo "Cleaning stale user cache directories (older than 30 days)..."
  find ~/.cache -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} + 2>/dev/null || true

  printInfo "Disk usage after cleanup:"
  df -h /

  printInfo "✅ Disk space cleanup complete"
}

# ======================================================================
#   Lab step checks and solution helpers
# ----------------------------------------------------------------------
#   A lesson's shell-verification block calls a check:
#
#     command: "source .devcontainer/util/source_framework.sh >/dev/null 2>&1 && checkNodeReady"
#     expect:
#       operator: exit-zero
#
#   A learner's click probes ONCE and answers at once (return 0 = pass,
#   return 1 = fail with a hint) — never a spinner while the check retries.
#
#   Automation (the training test, resume, "Run solution", the editor's
#   Test step) exports LAB_WAIT=1 first: the check then waits for the
#   expected state before probing, so verifying a step right after its
#   solution does not race the rollout.
#
#   The waits run in a subshell: waitForPod / waitForAllReadyPods `exit 1`
#   on timeout, which must end the wait, never the learner's shell. The
#   probe after the wait decides pass or fail.
#
#   A repo's my_functions.sh is sourced after this file, so a repo that
#   still defines one of these functions keeps its own copy until it
#   removes it.
#
#   The waitFor* names are LAB_WAIT wrappers kept for lab content written
#   against them (enablement-kubernetes-101).
# ======================================================================

# Cluster node is Ready.
checkNodeReady() {
  if [ -n "${LAB_WAIT:-}" ]; then
    local i=0
    while [ "$i" -lt 18 ]; do
      [ "$(kubectl get nodes --no-headers 2>/dev/null | grep -c ' Ready')" -gt 0 ] && break
      i=$((i + 1)); printInfo "node not Ready yet ($i/18), waiting 5s"; sleep 5
    done
  fi
  if [ "$(kubectl get nodes --no-headers 2>/dev/null | grep -c ' Ready')" -gt 0 ]; then
    printInfo "Cluster node is Ready"; return 0
  fi
  printError "Cluster node is not Ready yet"; return 1
}
waitForNodeReady() { LAB_WAIT=1 checkNodeReady; }

# TODO app pods are Running in the todoapp namespace.
checkTodoAppRunning() {
  [ -n "${LAB_WAIT:-}" ] && ( waitForAllReadyPods todoapp )
  if [ "$(kubectl get pods -n todoapp --no-headers 2>/dev/null | grep -c Running)" -gt 0 ]; then
    printInfo "todoapp pods are Running"; return 0
  fi
  printError "todoapp pods are not Running yet — the environment may still be starting"; return 1
}
waitForTodoAppRunning() { LAB_WAIT=1 checkTodoAppRunning; }

# Dynatrace Operator pod is Running.
checkOperatorReady() {
  [ -n "${LAB_WAIT:-}" ] && ( waitForPod dynatrace operator )
  if kubectl get pods -n dynatrace --no-headers 2>/dev/null | grep -E 'operator' | grep -q Running; then
    printInfo "Dynatrace Operator pod is Running"; return 0
  fi
  printError "Dynatrace Operator is not running — install the operator, then check again"; return 1
}
waitForOperatorReady() { LAB_WAIT=1 checkOperatorReady; }

# A DynaKube custom resource exists in the dynatrace namespace.
checkDynakube() {
  if [ -n "${LAB_WAIT:-}" ]; then
    local i=0
    while [ "$i" -lt 30 ]; do
      kubectl get dynakube -n dynatrace --no-headers 2>/dev/null | grep -q . && break
      i=$((i + 1)); printInfo "no DynaKube yet ($i/30), waiting 5s"; sleep 5
    done
  fi
  if kubectl get dynakube -n dynatrace --no-headers 2>/dev/null | grep -q .; then
    printInfo "DynaKube custom resource is present"; return 0
  fi
  printError "No DynaKube found in the dynatrace namespace — apply the DynaKube, then check again"; return 1
}
waitForDynakube() { LAB_WAIT=1 checkDynakube; }

# ActiveGate pod is Running in the dynatrace namespace.
checkActiveGateReady() {
  [ -n "${LAB_WAIT:-}" ] && ( waitForPod dynatrace activegate )
  if kubectl get pods -n dynatrace --no-headers 2>/dev/null | grep -i activegate | grep -q Running; then
    printInfo "ActiveGate pod is Running"; return 0
  fi
  printError "ActiveGate pod is not Running yet — it can take a minute or two after the DynaKube is applied; check again shortly"; return 1
}
waitForActiveGateReady() { LAB_WAIT=1 checkActiveGateReady; }

# OneAgent injection annotation is present on the pods of a namespace
# (default todoapp) — true only for pods (re)started after the DynaKube.
# Usage: checkOneAgentInjected [namespace]
checkOneAgentInjected() {
  local ns="${1:-todoapp}"
  local jp='{.items[*].metadata.annotations.oneagent\.dynatrace\.com/injected}'
  if [ -n "${LAB_WAIT:-}" ]; then
    local i=0
    while [ "$i" -lt 24 ]; do
      kubectl get pods -n "$ns" -o jsonpath="$jp" 2>/dev/null | tr ' ' '\n' | grep -q true && break
      i=$((i + 1)); printInfo "not injected yet ($i/24), waiting 10s"; sleep 10
    done
  fi
  if kubectl get pods -n "$ns" -o jsonpath="$jp" 2>/dev/null | tr ' ' '\n' | grep -q true; then
    printInfo "OneAgent is injected into the $ns pods"; return 0
  fi
  printError "OneAgent is not injected into the $ns pods — restart the deployment, wait for the rollout, then check again"; return 1
}
waitForOneAgentInjected() { LAB_WAIT=1 checkOneAgentInjected "$@"; }

# Dynatrace log module pod is Running. Container logs are captured by the
# log monitoring DaemonSet (pods named <dynakube>-logmonitoring), which the
# operator rolls out from the DynaKube's `logMonitoring` section. It tails
# container stdout — no pod restart and no code injection are involved.
checkLogModuleReady() {
  [ -n "${LAB_WAIT:-}" ] && ( waitForPod dynatrace logmonitoring )
  if kubectl get pods -n dynatrace --no-headers 2>/dev/null | grep -i logmonitoring | grep -q Running; then
    printInfo "Dynatrace log module is Running — your cluster's container logs are being captured"; return 0
  fi
  printError "Log module pod is not Running yet — it starts shortly after the DynaKube is applied; check again in a moment"; return 1
}
waitForLogModuleReady() { LAB_WAIT=1 checkLogModuleReady; }

# Solution helper: restart the todoapp so the Dynatrace webhook injects
# OneAgent into the new pods, and wait for the rollout.
restartTodoApp() {
  printInfoSection "Restarting todoapp so OneAgent gets injected"
  kubectl rollout restart deployment -n todoapp || return 1
  kubectl rollout status deployment -n todoapp --timeout=180s
}

# Solution helper: create a TODO through the app's HTTP API so fresh todoapp
# log lines and a POST /todos trace reach Grail. The learner may do this by
# hand in the app UI; this is the automation equivalent for LAB_SOLUTION
# commands and the training test, which cannot click a web page.
#
# The app logs "Adding a new todo: TodoRecord{title='...'}"; match that text in
# a DQL check, not the title. Usage: generateTodoTraffic [title]
#
# It always waits for the endpoint first (TODO_TRAFFIC_WAIT seconds, default
# 60; 150 with LAB_WAIT=1): a finished `kubectl rollout status` only means the
# container started, and the app answers HTTP some seconds later. Without the
# wait, `restartTodoApp && generateTodoTraffic` raced the rollout.
generateTodoTraffic() {
  local title="${1:-Dynatrace enablement}"
  local url="http://localhost:${K3D_LB_HTTP_PORT:-80}"
  local host="todoapp.$(detectHostname)"
  local wait_s="${TODO_TRAFFIC_WAIT:-60}"
  [ -n "${LAB_WAIT:-}" ] && [ -z "${TODO_TRAFFIC_WAIT:-}" ] && wait_s=150
  local step=5 waited=0
  printInfoSection "Creating a TODO so logs and traces reach Grail"
  printInfo "title: $title  | endpoint: $url (Host: $host)"

  until curl -sf -o /dev/null --max-time 5 -H "Host: $host" "$url/todos"; do
    if [ "$waited" -ge "$wait_s" ]; then
      printError "todoapp HTTP endpoint did not answer within ${wait_s}s — make sure the app is running (checkTodoAppRunning), then try again"
      return 1
    fi
    printInfo "app endpoint not answering yet (${waited}s/${wait_s}s), waiting ${step}s"
    sleep "$step"; waited=$((waited + step))
  done

  local resp
  resp=$(curl -s --max-time 10 -H "Host: $host" -X POST "$url/todos" -H "Content-Type: application/json" \
    -d "{\"title\":\"$title\",\"completed\":false}")
  if echo "$resp" | grep -q '"status":"ok"'; then
    printInfo "Created TODO \"$title\" — its log and its POST /todos trace should appear in Grail within ~2 min"
    return 0
  fi
  printError "Failed to create the TODO. Response: $resp"
  return 1
}

# Custom functions for each repo can be added in my_functions.sh
# Guarded: repos without one (or a shell opened outside the repo) must not error.
if [ -f "$REPO_PATH/.devcontainer/util/my_functions.sh" ]; then
  source "$REPO_PATH/.devcontainer/util/my_functions.sh"
fi

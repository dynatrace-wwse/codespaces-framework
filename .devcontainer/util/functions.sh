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
  echo -e "${GREEN}[$LOGNAME| ${YELLOW}WARN${GREEN} |$(timestamp) ${LILA}| ${RESET}$1${LILA}  |"
}

printError() {
  if [ "$2" = "false" ]; then
    return 0
  fi
  echo -e "${GREEN}[$LOGNAME| ${RED}ERROR${GREEN} |$(timestamp) ${LILA}| ${RESET}$1${LILA}  |"
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
    sleep 10
  done

  if [[ $RETRY == $RETRY_MAX ]]; then
    printError "Following pods are not still not running. Please check their events. Exiting installation..."
    kubectl get pods --field-selector=status.phase!=Running -A
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
    kubectl get pods --field-selector=status.phase!=Running -A
    exit 1
  fi
}

waitAppCanHandleRequests(){
  # Function to verify app can handle requests on a given port
  # First parameter: PORT (default: 30100)
  # Second parameter: RETRY_MAX (default: 5)
  # Usage examples:
  #   waitAppCanHandleRequests          - uses default port 30100 and 5 retries
  #   waitAppCanHandleRequests 8080     - uses port 8080 and 5 retries
  #   waitAppCanHandleRequests 8080 10  - uses port 8080 and 10 retries
  if [[ $# -eq 0 ]]; then
    PORT="30100"
    RETRY_MAX=5
  elif [[ $# -eq 1 ]]; then
    PORT="$1"
    RETRY_MAX=5
  elif [[ $# -eq 2 ]]; then
    PORT="$1"
    RETRY_MAX="$2"
  else
    PORT="30100"
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


setUpTerminal(){
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
  # Install ingress controller if not using legacy ports
  if [[ "$USE_LEGACY_PORTS" != "true" ]]; then
    installIngressController
  fi

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
    local status
    status=$(k3d cluster list -o json 2>/dev/null | python3 -c "
import sys, json
clusters = json.load(sys.stdin)
for c in clusters:
    if 'enablement' in c.get('name',''):
        nodes = c.get('nodes',[])
        running = sum(1 for n in nodes if n.get('state',{}).get('running',False))
        print('running' if running > 0 else 'stopped')
        break
" 2>/dev/null)

    if [[ "$status" == "running" ]]; then
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

  # Install ingress controller
  if [[ "$USE_LEGACY_PORTS" != "true" ]]; then
    installIngressController
  fi

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
  # Installs K3d if not already present
  if command -v k3d &>/dev/null; then
    return 0
  fi
  printInfo "Installing K3d..."
  curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
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
  #   K3D_NODEPORT_BASE     first NodePort exposed    (default: 30100)
  #
  # On the ops server (where 80/443 are nginx's), set:
  #   export K3D_LB_HTTP_PORT=30080 K3D_LB_HTTPS_PORT=30443 K3D_API_PORT=6444
  : "${K3D_CLUSTER_NAME:=enablement}"
  : "${K3D_LB_HTTP_PORT:=80}"
  : "${K3D_LB_HTTPS_PORT:=443}"
  : "${K3D_API_PORT:=6443}"
  : "${K3D_NODEPORT_BASE:=30100}"

  printInfoSection "Creating K3d cluster ($K3D_CLUSTER_NAME)"
  printInfo "Ports — http:$K3D_LB_HTTP_PORT  https:$K3D_LB_HTTPS_PORT  api:$K3D_API_PORT  nodeports:$K3D_NODEPORT_BASE..+200"

  installK3d

  local NP1=$K3D_NODEPORT_BASE
  local NP2=$((K3D_NODEPORT_BASE + 100))
  local NP3=$((K3D_NODEPORT_BASE + 200))

  k3d cluster create "$K3D_CLUSTER_NAME" \
    --api-port "$K3D_API_PORT" \
    -p "${K3D_LB_HTTP_PORT}:80@loadbalancer" \
    -p "${K3D_LB_HTTPS_PORT}:443@loadbalancer" \
    -p "${NP1}:${NP1}@server:0" \
    -p "${NP2}:${NP2}@server:0" \
    -p "${NP3}:${NP3}@server:0" \
    --k3s-arg "--disable=traefik@server:0" \
    --wait

  if k3d cluster list 2>/dev/null | grep -q "^${K3D_CLUSTER_NAME} "; then
    printInfo "K3d cluster '$K3D_CLUSTER_NAME' created — reach ingress at http://localhost:${K3D_LB_HTTP_PORT}/"
    attachK3dCluster
  else
    printError "K3d cluster '$K3D_CLUSTER_NAME' failed to start"
    return 1
  fi
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
  else
    printWarn "Could not connect to K3d cluster '$K3D_CLUSTER_NAME'"
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
  # - DT_*TOKEN vars are validated for Dynatrace token format (dt0c01.* or dt0s01.*)
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
      if [[ "$var_value" == dt0c01.* || "$var_value" == dt0s01.* ]] && [ ${#var_value} -gt 60 ]; then
        printInfo "$var_name: valid Dynatrace token format (${var_value:0:14}xxx...)"
      else
        printError "$var_name: invalid token format. Expected dt0c01.* or dt0s01.* with min 60 chars"
        printError "  Got: ${var_value:0:20}... (length: ${#var_value})"
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

  # Check if it's a token
  if [[ "$secret" == dt0c01.* || "$secret" == dt0s01.* ]] && [ ${#secret} -gt 60 ]; then
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
  #   mode: cloudnative (default) | apponly | k8s-only
  local mode="${1:-cloudnative}"
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

  # Create the secret for Dynakube to use
  kubectl -n dynatrace create secret generic "$RepositoryName" \
    --from-literal="apiToken=$DT_OPERATOR_TOKEN" \
    --from-literal="dataIngestToken=$DT_INGEST_TOKEN" \
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

generateDynakube() {
  # Generates a Dynakube YAML from config into .devcontainer/yaml/gen/dynakube.yaml.
  # Usage: generateDynakube [mode]
  #   mode overrides the config file's mode setting
  local mode_override="$1"

  printInfoSection "Generating Dynakube"

  # Load config
  loadDynakubeConfig

  local mode="${mode_override:-${DK_MODE:-cloudnative}}"
  local api_version="${DK_DYNAKUBE_API_VERSION:-v1beta6}"
  local cluster_name="${RepositoryName:-$(hostname)}"
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
  networkZone: ${cluster_name}
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
    cat >> "$gen_file" <<AOEOF
  oneAgent:
    applicationMonitoring: {}
AOEOF
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
  # If mkdocs.yaml uses INHERIT and mkdocs-base.yaml is missing, fetch it from the framework
  if grep -q '^INHERIT:' "${REPO_PATH}/mkdocs.yaml" 2>/dev/null && [ ! -f "${REPO_PATH}/mkdocs-base.yaml" ]; then
    printInfo "Fetching mkdocs-base.yaml from framework v${FRAMEWORK_VERSION}..."
    curl -fsSL "https://raw.githubusercontent.com/dynatrace-wwse/codespaces-framework/${FRAMEWORK_VERSION}/mkdocs-base.yaml" -o "${REPO_PATH}/mkdocs-base.yaml"
  fi
}

exposeMkdocs(){
  printInfo "Exposing Mkdocs in your dev.container in port 8000 & running in the background, type 'jobs' to show the process."
  nohup mkdocs serve --dev-addr=0.0.0.0:8000 --watch-theme --dirtyreload --livereload > /dev/null 2>&1 &

  # Register mkdocs with ingress if available and not using legacy ports
  if [[ "$USE_LEGACY_PORTS" != "true" ]] && kubectl get ns ingress-nginx &>/dev/null; then
    registerMkdocs
  else
    local url
    if [[ "$CODESPACES" == true ]]; then
      url="https://${CODESPACE_NAME}-8000.app.github.dev"
    else
      url="http://localhost:8000"
    fi
    printInfo "Mkdocs available at: $url"
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

installIngressController() {
  # Installs the nginx ingress controller in the Kind cluster.
  # Installs the nginx ingress controller.
  # Uses Kind-specific or baremetal manifest depending on CLUSTER_ENGINE.
  printInfoSection "Installing nginx ingress controller"

  # Check if already installed
  if kubectl get ns ingress-nginx &>/dev/null && \
     kubectl get pod -n ingress-nginx -l app.kubernetes.io/component=controller --no-headers 2>/dev/null | grep -q "Running"; then
    printInfo "Ingress controller already running"
    return 0
  fi

  # Choose the right manifest based on cluster engine
  # K3d uses provider/cloud (has built-in load balancer proxy on port 80/443)
  # Kind uses provider/kind (has hostPort binding via extraPortMappings)
  local ingress_provider="kind"
  if [[ "$CLUSTER_ENGINE" != "kind" ]]; then
    ingress_provider="cloud"
  fi

  printInfo "Deploying ingress-nginx (provider: $ingress_provider)..."
  kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v${INGRESS_NGINX_VERSION}/deploy/static/provider/${ingress_provider}/deploy.yaml"

  printInfo "Waiting for ingress controller to be ready..."
  kubectl wait --namespace ingress-nginx \
    --for=condition=ready pod \
    --selector=app.kubernetes.io/component=controller \
    --timeout=120s

  printInfo "Ingress controller installed and ready"
}

getAppURL() {
  # Returns the user-facing URL for an app based on environment type.
  # Usage: getAppURL <app-name> [port]
  local app_name="$1"
  local cs_port="$2"
  local detected_ip

  if [[ "$CODESPACES" == true ]]; then
    if [[ -n "$cs_port" ]]; then
      echo "https://${CODESPACE_NAME}-${cs_port}.app.github.dev"
    else
      echo "https://${CODESPACE_NAME}-80.app.github.dev"
    fi
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
INGRESSEOF

  # For Codespaces: set up port forwarding for this app
  local cs_port=""
  if [[ "$CODESPACES" == true ]]; then
    cs_port=$(getNextCodespacesPort)
    printInfo "Setting up Codespaces port forward on port $cs_port for $app_name"
    # Use kubectl port-forward in background
    kubectl port-forward -n "$namespace" "svc/$service_name" "${cs_port}:${service_port}" &>/dev/null &
  fi

  # Write to app registry
  mkdir -p "$(dirname "$APP_REGISTRY")"
  echo "${app_name}|${namespace}|${service_name}|${service_port}|${ingress_host}|${cs_port}" >> "$APP_REGISTRY"

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
ASTROINGRESSEOF

  # Register in app registry for greeting/listApps
  local cs_port=""
  if [[ "$CODESPACES" == true ]]; then
    cs_port=$(getNextCodespacesPort)
    kubectl port-forward -n "$namespace" "svc/frontend-proxy" "${cs_port}:8080" &>/dev/null &
  fi
  mkdir -p "$(dirname "$APP_REGISTRY")"
  # Remove old entry if exists
  grep -v "^astroshop|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null || true
  mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null || true
  echo "astroshop|${namespace}|frontend-proxy|8080|${ingress_host}|${cs_port}" >> "$APP_REGISTRY"

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

  # Kill any port-forward for this app
  if [[ -f "$APP_REGISTRY" ]]; then
    local cs_port
    cs_port=$(grep "^${app_name}|" "$APP_REGISTRY" | cut -d'|' -f6)
    if [[ -n "$cs_port" ]]; then
      # Kill the port-forward process
      pkill -f "port-forward.*${cs_port}:" 2>/dev/null || true
    fi
    # Remove from registry
    grep -v "^${app_name}|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null
    mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null
  fi

  printInfo "$app_name unregistered"
}

getNextCodespacesPort() {
  # Returns the next available port for Codespaces port-forwarding.
  # Starts at INGRESS_CS_PORT_START (8080) and increments.
  local port=$INGRESS_CS_PORT_START
  if [[ -f "$APP_REGISTRY" ]]; then
    local max_port
    max_port=$(awk -F'|' '{print $6}' "$APP_REGISTRY" | grep -v '^$' | sort -n | tail -1)
    if [[ -n "$max_port" ]]; then
      port=$((max_port + 1))
    fi
  fi
  echo "$port"
}

listApps() {
  # Lists all registered apps with their URLs.
  if [[ ! -f "$APP_REGISTRY" ]] || [[ ! -s "$APP_REGISTRY" ]]; then
    printInfo "No applications registered. Type 'deployApp' to see available apps."
    return 0
  fi

  printInfoSection "Registered Applications"
  while IFS='|' read -r app_name namespace service_name service_port ingress_host cs_port; do
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

  # Register in app registry
  local cs_port=""
  if [[ "$CODESPACES" == true ]]; then
    cs_port=8000  # mkdocs already listens on 8000
  fi
  mkdir -p "$(dirname "$APP_REGISTRY")"
  # Remove old entry if exists
  grep -v "^docs|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null || true
  mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null || true
  echo "docs|default|mkdocs-external|8000|${mkdocs_host}|${cs_port}" >> "$APP_REGISTRY"

  local url
  url=$(getAppURL "docs" "$cs_port")
  printInfo "Mkdocs registered and accessible at: $url"
}

getNextFreeAppPort() {
  # When print_log == true, then log is printed out but the 
  # variable is not echoed out, this way is not printed in the log. If print_log =0 false, then the variable is echoed out 
  # so the value can be catched as return vaue and stored.
  local print_log="$1"
  if [ -z "$print_log" ]; then
    # As default no log is printed out. 
    print_log=false
  fi

  printInfo "Iterating over NODE_PORTS: $NODE_PORTS" $print_log

  # Reconstruct array (portable for Bash and Zsh)
  PORT_ARRAY=()
  for port in $(echo "$NODE_PORTS"); do
    PORT_ARRAY+=("$port")
  done

  for port in "${PORT_ARRAY[@]}"; do
    printInfo "Verifying if $port is free in Kubernetes Cluster..." $print_log

    # Searching for services attached to a NodePort
    allocated_app=$(kubectl get svc --all-namespaces -o wide | grep "$port")
    
    if [[ "$?" == '0' ]]; then
      printWarn "Port $port is allocated by: $allocated_app" $print_log
      app_deployed=true
    else
      printInfo "Port $port is free, allocating to app" $print_log
      if [[ $app_deployed ]]; then
        printWarn "You already have applications deployed, be careful with the sizing of your Kubernetes Cluster ;)" $print_log
      fi 
      # Use echo to return the value (functions can't use `return` for strings/numbers reliably)
      echo "$port"
      return 0
    fi
  done
  printWarn "No NodePort is free for deploying apps in your container, please delete some apps before deploying more." $print_log
  return 1
}


deployAITravelAdvisorApp(){
  [ -z "$FRAMEWORK_APPS_PATH" ] && { echo "❌ source_framework.sh not loaded — run 'source .devcontainer/util/source_framework.sh' first"; return 1; }

  printInfoSection "Deploying AI Travel Advisor App & it's LLM"
  
  if [ -z "$DT_LLM_TOKEN" ]; then
    printError "DT_LLM_TOKEN token is missing"
  fi
  
  printInfo "Evaluating credentials"

  dynatraceEvalReadSaveCredentials

  local PORT=""
  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed"
      return 1
    fi
  fi

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

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    kubectl patch service ai-travel-advisor --namespace=ai-travel-advisor --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"
    waitAppCanHandleRequests $PORT 20
    printInfo "AI Travel Advisor is available via NodePort=$PORT"
  else
    registerApp "ai-travel-advisor" "ai-travel-advisor" "ai-travel-advisor" 8080
  fi
}

deployTodoApp(){

  printInfoSection "Deploying Todo App"

  kubectl create ns todoapp 2>/dev/null || true

  # Create deployment of todoApp
  kubectl -n todoapp create deploy todoapp --image=shinojosa/todoapp:1.0.1

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    # Legacy NodePort mode
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed"
      return 1
    fi
    kubectl -n todoapp expose deployment todoapp --type=NodePort --name=todoapp --port=8080 --target-port=8080
    kubectl patch service todoapp --namespace=todoapp --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"
    waitForAllReadyPods todoapp
    waitAppCanHandleRequests $PORT
    printInfoSection "TodoApp is available via NodePort=$PORT"
  else
    # Ingress mode
    kubectl -n todoapp expose deployment todoapp --type=ClusterIP --name=todoapp --port=8080 --target-port=8080 2>/dev/null || true
    waitForAllReadyPods todoapp
    registerApp "todoapp" "todoapp" "todoapp" 8080
  fi
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

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed"
      return 1
    fi
    kubectl patch service frontend-proxy --namespace=$NAMESPACE --patch='{"spec": {"type": "NodePort"}}'
    kubectl patch service frontend-proxy --namespace=$NAMESPACE --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"
    waitAppCanHandleRequests $PORT 60
    printInfo "Astroshop deployed and available via NodePort=$PORT"
  else
    # Astroshop needs custom ingress: otel-collector paths + frontend-proxy catch-all
    registerAstroshopIngress "$NAMESPACE"
  fi
}

deployBugZapperApp(){
  [ -z "$FRAMEWORK_APPS_PATH" ] && { echo "❌ source_framework.sh not loaded — run 'source .devcontainer/util/source_framework.sh' first"; return 1; }

  printInfoSection "Deploying BugZapper App"

  kubectl create ns bugzapper 2>/dev/null || true
  kubectl -n bugzapper create deploy bugzapper --image=jhendrick/bugzapper-game:latest

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed"
      return 1
    fi
    kubectl -n bugzapper expose deployment bugzapper --type=NodePort --name=bugzapper --port=3000 --target-port=3000
    kubectl patch service bugzapper --namespace=bugzapper --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"
    waitForAllReadyPods bugzapper
    waitAppCanHandleRequests $PORT
    printInfoSection "Bugzapper is available via NodePort=$PORT"
  else
    kubectl -n bugzapper expose deployment bugzapper --type=ClusterIP --name=bugzapper --port=3000 --target-port=3000 2>/dev/null || true
    waitForAllReadyPods bugzapper
    registerApp "bugzapper" "bugzapper" "bugzapper" 3000
  fi
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

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed"
      return 1
    fi
    kubectl patch service frontendreverseproxy-easytrade --namespace=easytrade --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"
    waitAppCanHandleRequests $PORT
    printInfo "EasyTrade is available via NodePort=$PORT"
  else
    registerApp "easytrade" "easytrade" "frontendreverseproxy-easytrade" 80
  fi
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

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed"
      return 1
    fi
    kubectl patch service frontend-external --namespace=hipstershop --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"
    waitAppCanHandleRequests $PORT
    printInfo "HipsterShop is available via NodePort=$PORT"
  else
    registerApp "hipstershop" "hipstershop" "frontend-external" 80
  fi
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

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed, all NodePorts are busy"
      return 1
    fi
    kubectl patch service unguard-envoy-proxy --namespace=unguard --patch="{\"spec\": {\"type\": \"NodePort\", \"ports\": [{\"port\": 8080, \"nodePort\": $PORT }]}}"
  else
    registerApp "unguard" "unguard" "unguard-envoy-proxy" 8080
  fi
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

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    getNextFreeAppPort true
    PORT=$(getNextFreeAppPort)
    if [[ $? -ne 0 ]]; then
      printWarn "Application can't be deployed"
      return 1
    fi
    kubectl patch service frontend-proxy --namespace="$NAMESPACE" --patch='{"spec": {"type": "NodePort"}}'
    kubectl patch service frontend-proxy --namespace="$NAMESPACE" --type='json' --patch="[{\"op\": \"replace\", \"path\": \"/spec/ports/0/nodePort\", \"value\":$PORT}]"
    printInfo "OpenTelemetry Demo available via NodePort=$PORT"
  else
    # Same multi-path ingress pattern as astroshop — otel-collector + frontend-proxy
    registerOpentelemetryDemoIngress "$NAMESPACE"
  fi
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
            name: opentelemetry-demo-otelcol
            port:
              number: 4318
      - path: /v1/metrics
        pathType: ImplementationSpecific
        backend:
          service:
            name: opentelemetry-demo-otelcol
            port:
              number: 4318
      - path: /v1/logs
        pathType: ImplementationSpecific
        backend:
          service:
            name: opentelemetry-demo-otelcol
            port:
              number: 4318
      - path: /
        pathType: Prefix
        backend:
          service:
            name: opentelemetry-demo-frontendproxy
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
OTELINGRESSEOF

  # Register in app registry
  local cs_port=""
  if [[ "$CODESPACES" == true ]]; then
    cs_port=$(getNextCodespacesPort)
    kubectl port-forward -n "$namespace" "svc/opentelemetry-demo-frontendproxy" "${cs_port}:8080" &>/dev/null &
  fi
  mkdir -p "$(dirname "$APP_REGISTRY")"
  grep -v "^otel-demo|" "$APP_REGISTRY" > "${APP_REGISTRY}.tmp" 2>/dev/null || true
  mv "${APP_REGISTRY}.tmp" "$APP_REGISTRY" 2>/dev/null || true
  echo "otel-demo|${namespace}|opentelemetry-demo-frontendproxy|8080|${ingress_host}|${cs_port}" >> "$APP_REGISTRY"

  local app_url
  app_url=$(getAppURL "otel-demo" "$cs_port")
  printInfo "OpenTelemetry Demo registered and accessible at: $app_url"
}

undeployOpentelemetryDemo(){
  printInfoSection "Undeploying OpenTelemetry Demo"
  unregisterApp "otel-demo" "opentelemetry-demo"
  helm uninstall opentelemetry-demo --namespace opentelemetry-demo 2>/dev/null
  kubectl delete namespace opentelemetry-demo --force 2>/dev/null
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

verifyCodespaceCreation(){
  printInfoSection "Verify Codespace creation"
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
  else
    printWarn "Unknown instantiation type: $INSTANTIATION_TYPE"
  fi

  # Filter out known noise patterns that are not real errors.
  # The grep for 'error|failed' is intentionally broad so we catch real issues,
  # but it also catches the framework's own log output, informational messages,
  # and compound words. We filter those out here.
  CODESPACE_ERRORS=""
  if [ -n "$raw_errors" ]; then
    CODESPACE_ERRORS=$(printf "%s" "$raw_errors" | grep -v -i -E \
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
    || true)
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
      
      verifyCodespaceCreation
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

# Custom functions for each repo can be added in my_functions.sh
source $REPO_PATH/.devcontainer/util/my_functions.sh

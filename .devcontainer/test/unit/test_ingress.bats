#!/usr/bin/env bats
# Tests for Phase 2 Task 2: App Exposure via Ingress
# Covers: detectIP, getAppURL, registerApp, unregisterApp, getNextCodespacesPort, listApps

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  mkdir -p "$FAKE_REPO/.devcontainer/test"
  mkdir -p "$FAKE_REPO/.vscode"

  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env"
  export APP_REGISTRY="$TEST_DIR/app-registry"
  export INGRESS_CS_PORT_START=8080
  export USE_LEGACY_PORTS="false"

  # Create minimal stubs
  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN=""
BLUE=""
CYAN=""
YELLOW=""
ORANGE=""
RED=""
LILA=""
NORMAL=""
RESET=""
thickline=""
halfline=""
thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"
export ENV_FILE
COUNT_FILE="$REPO_PATH/.devcontainer/util/.count"
export COUNT_FILE
INSTANTIATION_TYPE="local-docker-container"
export USE_LEGACY_PORTS="${USE_LEGACY_PORTS:-false}"
export APP_REGISTRY="${APP_REGISTRY:-${HOME}/.cache/dt-framework/app-registry}"
export INGRESS_CS_PORT_START="${INGRESS_CS_PORT_START:-8080}"
export INGRESS_NGINX_VERSION="4.12.1"
VARSEOF

  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"

  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" \
     "$FAKE_REPO/.devcontainer/util/functions.sh"

  # Mock kubectl
  kubectl() { return 1; }
  export -f kubectl
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
}

# ============================================================
# detectIP tests
# ============================================================

@test "detectIP: uses EXTERNAL_IP when set" {
  source_functions
  export EXTERNAL_IP="10.0.0.42"

  result=$(detectIP)
  [ "$result" = "10.0.0.42" ]
}

@test "detectIP: returns 127.0.0.1 in Codespaces" {
  source_functions
  unset EXTERNAL_IP
  export CODESPACES=true

  result=$(detectIP)
  [ "$result" = "127.0.0.1" ]
}

@test "detectIP: tries ifconfig.me when not Codespaces" {
  source_functions
  unset EXTERNAL_IP
  unset CODESPACES

  # Mock curl to return a known IP
  curl() { echo "203.0.113.5"; }
  export -f curl

  result=$(detectIP)
  [ "$result" = "203.0.113.5" ]
}

@test "detectIP: falls back to hostname -I when curl fails" {
  source_functions
  unset EXTERNAL_IP
  unset CODESPACES

  # Mock curl to fail
  curl() { return 1; }
  export -f curl

  # Mock hostname to return a known IP
  hostname() { echo "192.168.1.100 172.17.0.1"; }
  export -f hostname

  result=$(detectIP)
  [ "$result" = "192.168.1.100" ]
}

# ============================================================
# getAppURL tests
# ============================================================

@test "getAppURL: returns nip.io URL for local environments" {
  source_functions
  unset CODESPACES
  export EXTERNAL_IP="10.0.0.1"

  result=$(getAppURL "todoapp")
  [ "$result" = "http://todoapp.10.0.0.1.nip.io" ]
}

@test "getAppURL: returns Codespaces URL with port" {
  source_functions
  export CODESPACES=true
  export CODESPACE_NAME="myspace"

  result=$(getAppURL "todoapp" "8080")
  [ "$result" = "https://myspace-8080.app.github.dev" ]
}

@test "getAppURL: returns Codespaces port 80 URL when no port given" {
  source_functions
  export CODESPACES=true
  export CODESPACE_NAME="myspace"

  result=$(getAppURL "todoapp")
  [ "$result" = "https://myspace-80.app.github.dev" ]
}

# ============================================================
# App registry tests
# ============================================================

@test "getNextCodespacesPort: returns start port when registry is empty" {
  source_functions

  result=$(getNextCodespacesPort)
  [ "$result" = "8080" ]
}

@test "getNextCodespacesPort: increments from last used port" {
  source_functions
  mkdir -p "$(dirname "$APP_REGISTRY")"
  echo "app1|ns1|svc1|80|host1|8080" > "$APP_REGISTRY"
  echo "app2|ns2|svc2|80|host2|8081" >> "$APP_REGISTRY"

  result=$(getNextCodespacesPort)
  [ "$result" = "8082" ]
}

@test "registerApp: creates registry entry" {
  source_functions
  export EXTERNAL_IP="10.0.0.1"

  # Mock kubectl to succeed for apply
  kubectl() {
    if [[ "$1" == "apply" ]]; then return 0; fi
    if [[ "$1" == "port-forward" ]]; then return 0; fi
    return 1
  }
  export -f kubectl

  registerApp "todoapp" "todoapp" "todoapp" 8080

  [ -f "$APP_REGISTRY" ]
  run cat "$APP_REGISTRY"
  [[ "$output" == *"todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.nip.io|"* ]]
}

@test "registerApp: fails with missing arguments" {
  source_functions

  run registerApp "todoapp"
  [ "$status" -eq 1 ]
}

@test "unregisterApp: removes entry from registry" {
  source_functions
  mkdir -p "$(dirname "$APP_REGISTRY")"
  echo "todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.nip.io|" > "$APP_REGISTRY"
  echo "astroshop|astroshop|frontend-proxy|8080|astroshop.10.0.0.1.nip.io|" >> "$APP_REGISTRY"

  # Mock kubectl
  kubectl() { return 0; }
  export -f kubectl

  unregisterApp "todoapp" "todoapp"

  run cat "$APP_REGISTRY"
  [[ "$output" != *"todoapp|"* ]]
  [[ "$output" == *"astroshop|"* ]]
}

@test "listApps: shows registered apps" {
  source_functions
  mkdir -p "$(dirname "$APP_REGISTRY")"
  export EXTERNAL_IP="10.0.0.1"
  echo "todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.nip.io|" > "$APP_REGISTRY"

  run listApps
  [ "$status" -eq 0 ]
  [[ "$output" == *"todoapp"* ]]
}

@test "listApps: handles empty registry" {
  source_functions

  run listApps
  [ "$status" -eq 0 ]
  [[ "$output" == *"No applications registered"* ]]
}

# ============================================================
# Deploy function ingress mode tests
# ============================================================

@test "deployTodoApp: uses registerApp in ingress mode" {
  source_functions
  export USE_LEGACY_PORTS="false"
  export EXTERNAL_IP="10.0.0.1"

  # Mock kubectl for deployment
  kubectl() {
    case "$1" in
      create|apply|expose|wait) return 0 ;;
      get)
        if [[ "$*" == *"--all-namespaces"* ]]; then
          echo "todoapp   todoapp   ClusterIP   10.96.0.1   8080/TCP"
        elif [[ "$*" == *"pod"* ]]; then
          echo "NAME       READY   STATUS    RESTARTS   AGE"
          echo "todoapp-x  1/1     Running   0          1m"
        fi
        return 0 ;;
      *) return 0 ;;
    esac
  }
  export -f kubectl

  # Mock waitForAllReadyPods to skip
  waitForAllReadyPods() { return 0; }
  export -f waitForAllReadyPods

  deployTodoApp

  [ -f "$APP_REGISTRY" ]
  run cat "$APP_REGISTRY"
  [[ "$output" == *"todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.nip.io"* ]]
}

@test "deployTodoApp: legacy mode uses getNextFreeAppPort (not registerApp)" {
  source_functions
  export USE_LEGACY_PORTS="true"
  export NODE_PORTS="30100 30200 30300"

  # In legacy mode, getNextFreeAppPort is called. We verify the code path
  # by testing that registerApp is NOT called (no registry file created).
  # We mock getNextFreeAppPort directly to avoid kubectl dependency.
  getNextFreeAppPort() {
    if [[ "$1" == "true" ]]; then return 0; fi
    echo "30100"
    return 0
  }
  export -f getNextFreeAppPort

  kubectl() { return 0; }
  export -f kubectl

  waitForAllReadyPods() { return 0; }
  export -f waitForAllReadyPods

  waitAppCanHandleRequests() { return 0; }
  export -f waitAppCanHandleRequests

  deployTodoApp

  # Registry should NOT exist in legacy mode
  [ ! -f "$APP_REGISTRY" ] || [ ! -s "$APP_REGISTRY" ]
}

# ============================================================
# Backward compatibility
# ============================================================

@test "USE_LEGACY_PORTS defaults to false" {
  source_functions
  [ "$USE_LEGACY_PORTS" = "false" ]
}

@test "kind-cluster.yml has port 80 mapping" {
  run cat "$BATS_TEST_DIRNAME/../../yaml/kind/kind-cluster.yml"
  [[ "$output" == *"hostPort: 80"* ]]
  [[ "$output" == *"containerPort: 80"* ]]
}

@test "kind-cluster.yml still has legacy port 30100" {
  run cat "$BATS_TEST_DIRNAME/../../yaml/kind/kind-cluster.yml"
  [[ "$output" == *"hostPort: 30100"* ]]
}

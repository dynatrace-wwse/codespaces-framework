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
  export MAGIC_DOMAIN="sslip.io"

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
export APP_REGISTRY="${APP_REGISTRY:-${HOME}/.cache/dt-framework/app-registry}"
export INGRESS_NGINX_VERSION="1.12.1"
export MAGIC_DOMAIN="${MAGIC_DOMAIN:-sslip.io}"
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

@test "getAppURL: returns sslip.io URL for local environments" {
  source_functions
  unset CODESPACES
  export EXTERNAL_IP="10.0.0.1"

  result=$(getAppURL "todoapp")
  [ "$result" = "http://todoapp.10.0.0.1.sslip.io" ]
}

@test "getAppURL: Codespaces honours an explicit port (mkdocs on 8000)" {
  source_functions
  export CODESPACES=true
  export CODESPACE_NAME="myspace"

  # Services that bypass the ingress get their own forwarded port.
  result=$(getAppURL "docs" "8000")
  [ "$result" = "https://myspace-8000.app.github.dev" ]
}

@test "getAppURL: returns Codespaces port 80 URL when no port given" {
  source_functions
  export CODESPACES=true
  export CODESPACE_NAME="myspace"

  result=$(getAppURL "todoapp")
  [ "$result" = "https://myspace-80.app.github.dev" ]
}

@test "getAppURL: empty port arg falls back to 80 (registerApp stores an empty cs_port)" {
  source_functions
  export CODESPACES=true
  export CODESPACE_NAME="myspace"

  result=$(getAppURL "todoapp" "")
  [ "$result" = "https://myspace-80.app.github.dev" ]
}

@test "getAppURL: honours a non-default port-forwarding domain" {
  source_functions
  export CODESPACES=true
  export CODESPACE_NAME="myspace"
  export GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="preview.app.github.dev"

  result=$(getAppURL "todoapp")
  [ "$result" = "https://myspace-80.preview.app.github.dev" ]
}

@test "getAppURL: returns Orbital wildcard subdomain URL" {
  source_functions
  unset CODESPACES
  export ORBITAL_ENVIRONMENT=true
  export ORBITAL_JOB_ID="mk3p9aqz-7f3a"

  result=$(getAppURL "todoapp")
  [[ "$result" == "https://todoapp--"*".autonomous-enablements.whydevslovedynatrace.com" ]]
}

# ============================================================
# computeOrbitalSubdomain tests (canonical Orbital exposure path)
# ============================================================

@test "computeOrbitalSubdomain: builds {app}--{job_id} verbatim" {
  source_functions
  # Canonical short id (base36 ts + hex) used as-is — no worker-prefix stripping.
  export ORBITAL_JOB_ID="mk3p9aqz-7f3a"

  result=$(computeOrbitalSubdomain "todoapp")
  [[ "$result" == "todoapp--mk3p9aqz-7f3a" ]]
}

@test "computeOrbitalSubdomain: preserves hyphenated app name before the -- separator" {
  source_functions
  export ORBITAL_JOB_ID="mk3p9aqz-7f3a"

  result=$(computeOrbitalSubdomain "otel-demo")
  [[ "$result" == "otel-demo--mk3p9aqz-7f3a" ]]
}

@test "computeOrbitalSubdomain: empty when ORBITAL_JOB_ID unset" {
  source_functions
  unset ORBITAL_JOB_ID

  result=$(computeOrbitalSubdomain "todoapp")
  [ -z "$result" ]
}

@test "computeOrbitalSubdomain: label stays within 63-char DNS limit" {
  source_functions
  export ORBITAL_JOB_ID="$(printf 'x%.0s' {1..120})"

  result=$(computeOrbitalSubdomain "astroshop")
  [ "${#result}" -le 63 ]
}

# ============================================================
# App registry tests
# ============================================================

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
  [[ "$output" == *"todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.sslip.io|"* ]]
}

@test "registerApp: fails with missing arguments" {
  source_functions

  run registerApp "todoapp"
  [ "$status" -eq 1 ]
}

@test "unregisterApp: removes entry from registry" {
  source_functions
  mkdir -p "$(dirname "$APP_REGISTRY")"
  echo "todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.sslip.io|" > "$APP_REGISTRY"
  echo "astroshop|astroshop|frontend-proxy|8080|astroshop.10.0.0.1.sslip.io|" >> "$APP_REGISTRY"

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
  echo "todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.sslip.io|" > "$APP_REGISTRY"

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

@test "listApps: 7-field row does not leak a delimiter into the Codespaces port" {
  source_functions
  mkdir -p "$(dirname "$APP_REGISTRY")"
  export CODESPACES=true
  export CODESPACE_NAME="myspace"
  # 7 fields with an empty cs_port AND an empty orbital_subdomain — reading
  # only 6 names used to leave cs_port as the literal "|".
  echo "todoapp|todoapp|todoapp|8080|todoapp.127.0.0.1.sslip.io||" > "$APP_REGISTRY"

  run listApps
  [ "$status" -eq 0 ]
  [[ "$output" == *"https://myspace-80.app.github.dev"* ]]
  [[ "$output" != *"myspace-|"* ]]
}

@test "listApps: mkdocs row keeps its own forwarded port" {
  source_functions
  mkdir -p "$(dirname "$APP_REGISTRY")"
  export CODESPACES=true
  export CODESPACE_NAME="myspace"
  echo "docs|default|mkdocs-external|8000||8000|" > "$APP_REGISTRY"

  run listApps
  [ "$status" -eq 0 ]
  [[ "$output" == *"https://myspace-8000.app.github.dev"* ]]
}

# ============================================================
# Deploy function ingress mode tests
# ============================================================

@test "deployTodoApp: uses registerApp in ingress mode" {
  source_functions
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
  [[ "$output" == *"todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.sslip.io"* ]]
}

# ============================================================
# Ingress-only exposure
# ============================================================

@test "kind-cluster.yml has port 80 mapping" {
  run cat "$BATS_TEST_DIRNAME/../../yaml/kind/kind-cluster.yml"
  [[ "$output" == *"hostPort: 80"* ]]
  [[ "$output" == *"containerPort: 80"* ]]
}

@test "kind-cluster.yml has no legacy NodePort mappings (30100-30300)" {
  run cat "$BATS_TEST_DIRNAME/../../yaml/kind/kind-cluster.yml"
  [[ "$output" != *"30100"* ]]
  [[ "$output" != *"30200"* ]]
  [[ "$output" != *"30300"* ]]
}

@test "functions.sh has no NodePort helpers (getNextFreeAppPort / getNextCodespacesPort)" {
  run cat "$BATS_TEST_DIRNAME/../../util/functions.sh"
  [[ "$output" != *"getNextFreeAppPort"* ]]
  [[ "$output" != *"getNextCodespacesPort"* ]]
}

@test "functions.sh deploy functions contain no NodePort logic" {
  run cat "$BATS_TEST_DIRNAME/../../util/functions.sh"
  [[ "$output" != *"USE_LEGACY_PORTS"* ]]
  [[ "$output" != *"--type=NodePort"* ]]
  [[ "$output" != *"nodePort"* ]]
}

# ============================================================
# installIngressController
# ============================================================
# Two regressions are pinned here, both from the Codespaces failure:
#   1. it used to wait 120s and DISCARD the result, printing "installed and
#      ready" regardless — so the run continued with no controller and the next
#      registerApp died with "no endpoints available for service
#      ingress-nginx-controller-admission".
#   2. it used to apply the static provider manifest, which always ships the
#      admission webhook: two certgen Jobs must write a secret the controller
#      mounts with optional:false before it can start at all (~150s of a ~250s
#      startup on a 4-core Codespace).

setup_ingress_mocks() {
  export CLUSTER_ENGINE=k3d
  mkdir -p "$FAKE_REPO/.devcontainer/yaml/ingress"
  echo "controller: {admissionWebhooks: {enabled: false}}" \
    > "$FAKE_REPO/.devcontainer/yaml/ingress/values-k3d.yaml"
  echo "controller: {admissionWebhooks: {enabled: false}}" \
    > "$FAKE_REPO/.devcontainer/yaml/ingress/values-kind.yaml"
  export FRAMEWORK_CACHE=""
}

@test "installIngressController: installs via Helm with the admission webhook disabled" {
  source_functions
  setup_ingress_mocks

  kubectl() { case "$1 $2" in "get ns") return 1 ;; *) return 0 ;; esac; }
  helm() { echo "HELMARGS: $*"; return 0; }
  export -f kubectl helm

  run installIngressController
  [ "$status" -eq 0 ]
  [[ "$output" == *"HELMARGS:"* ]]
  [[ "$output" == *"--repo https://kubernetes.github.io/ingress-nginx"* ]]
  [[ "$output" == *"values-k3d.yaml"* ]]
  # the static manifest path must be gone
  [[ "$output" != *"deploy/static/provider"* ]]
  [[ "$output" == *"Ingress controller installed and ready"* ]]
}

@test "installIngressController: derives the chart version from INGRESS_NGINX_VERSION" {
  source_functions
  setup_ingress_mocks
  export INGRESS_NGINX_VERSION=1.12.1

  kubectl() { case "$1 $2" in "get ns") return 1 ;; *) return 0 ;; esac; }
  helm() { echo "HELMARGS: $*"; return 0; }
  export -f kubectl helm

  run installIngressController
  [ "$status" -eq 0 ]
  [[ "$output" == *"--version 4.12.1"* ]]
}

@test "installIngressController: kind uses its own values file" {
  source_functions
  setup_ingress_mocks
  export CLUSTER_ENGINE=kind

  kubectl() { case "$1 $2" in "get ns") return 1 ;; *) return 0 ;; esac; }
  helm() { echo "HELMARGS: $*"; return 0; }
  export -f kubectl helm

  run installIngressController
  [ "$status" -eq 0 ]
  [[ "$output" == *"values-kind.yaml"* ]]
}

@test "installIngressController: returns non-zero and does not claim success when the wait fails" {
  source_functions
  setup_ingress_mocks
  export INGRESS_READY_TIMEOUT=1

  kubectl() {
    case "$1 $2" in
      "get ns")           return 1 ;;
      "wait --namespace") return 1 ;;   # controller never becomes ready
      *)                  return 0 ;;
    esac
  }
  helm() { return 0; }
  export -f kubectl helm

  run installIngressController
  [ "$status" -ne 0 ]
  [[ "$output" != *"Ingress controller installed and ready"* ]]
  [[ "$output" == *"did not become ready"* ]]
}

@test "installIngressController: returns non-zero when the helm install fails" {
  source_functions
  setup_ingress_mocks

  kubectl() { case "$1 $2" in "get ns") return 1 ;; *) return 0 ;; esac; }
  helm() { return 1; }
  export -f kubectl helm

  run installIngressController
  [ "$status" -ne 0 ]
  [[ "$output" == *"helm install of ingress-nginx"* ]]
  [[ "$output" != *"Ingress controller installed and ready"* ]]
}

@test "installIngressController: fails clearly when the values file is missing" {
  source_functions
  export CLUSTER_ENGINE=k3d FRAMEWORK_CACHE=""

  kubectl() { case "$1 $2" in "get ns") return 1 ;; *) return 0 ;; esac; }
  helm() { echo "HELM SHOULD NOT RUN"; return 0; }
  export -f kubectl helm

  run installIngressController
  [ "$status" -ne 0 ]
  [[ "$output" == *"Missing ingress values file"* ]]
  [[ "$output" != *"HELM SHOULD NOT RUN"* ]]
}

@test "installIngressController: no static-manifest apply and no 120s timeout remain" {
  run cat "$BATS_TEST_DIRNAME/../../util/functions.sh"
  # The apply of the upstream static manifest is what dragged the admission
  # webhook in. Match the fetch itself, not the words — the function comment
  # legitimately names the manifests it replaced.
  [[ "$output" != *"raw.githubusercontent.com/kubernetes/ingress-nginx"* ]]
  [[ "$output" != *"--timeout=120s"* ]]
  [[ "$output" == *'INGRESS_READY_TIMEOUT:-600'* ]]
}

@test "ingress values: both engines disable the admission webhook" {
  run cat "$BATS_TEST_DIRNAME/../../yaml/ingress/values-k3d.yaml"
  [ "$status" -eq 0 ]
  [[ "$output" == *"admissionWebhooks:"* ]]
  [[ "$output" == *"enabled: false"* ]]

  run cat "$BATS_TEST_DIRNAME/../../yaml/ingress/values-kind.yaml"
  [ "$status" -eq 0 ]
  [[ "$output" == *"admissionWebhooks:"* ]]
  [[ "$output" == *"enabled: false"* ]]
}

@test "ingress values: kind reproduces the static kind provider settings" {
  run cat "$BATS_TEST_DIRNAME/../../yaml/ingress/values-kind.yaml"
  [[ "$output" == *"hostPort:"* ]]
  [[ "$output" == *"type: NodePort"* ]]
  [[ "$output" == *'ingress-ready: "true"'* ]]
  [[ "$output" == *"watchIngressWithoutClass: true"* ]]
}

#!/usr/bin/env bats
# Tests for cluster engine routing and run environment detection.
# Covers: detectRunEnvironment, startCluster/stopCluster/deleteCluster dispatch,
#         deployCloudNative Kind guard, deployCloudNative Orbital warn.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  mkdir -p "$FAKE_REPO/.devcontainer/test"
  mkdir -p "$FAKE_REPO/.devcontainer/yaml/gen"
  mkdir -p "$FAKE_REPO/.vscode"

  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env"
  export APP_REGISTRY="$TEST_DIR/app-registry"
  export FRAMEWORK_CACHE=""
  export ARCH="x86_64"

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
export USE_LEGACY_PORTS="false"
export MAGIC_DOMAIN="sslip.io"
VARSEOF

  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" \
     "$FAKE_REPO/.devcontainer/util/functions.sh"

  kubectl() { return 0; }
  export -f kubectl
  helm() { return 0; }
  export -f helm
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
}

# ============================================================
# detectRunEnvironment
# ============================================================

@test "detectRunEnvironment: returns orbital when ORBITAL_ENVIRONMENT=true" {
  source_functions
  unset CODESPACE_NAME K3D_CLUSTER_NAME
  export ORBITAL_ENVIRONMENT=true
  run detectRunEnvironment
  [ "$status" -eq 0 ]
  [ "$output" = "orbital" ]
}

@test "detectRunEnvironment: returns orbital when K3D_CLUSTER_NAME=master-*" {
  source_functions
  unset CODESPACE_NAME ORBITAL_ENVIRONMENT
  export K3D_CLUSTER_NAME="master-test"
  run detectRunEnvironment
  [ "$status" -eq 0 ]
  [ "$output" = "orbital" ]
}

@test "detectRunEnvironment: returns codespaces when CODESPACE_NAME set" {
  source_functions
  unset ORBITAL_ENVIRONMENT K3D_CLUSTER_NAME
  export CODESPACE_NAME="my-codespace-123"
  run detectRunEnvironment
  [ "$status" -eq 0 ]
  [ "$output" = "codespaces" ]
}

@test "detectRunEnvironment: returns local as fallback" {
  source_functions
  unset ORBITAL_ENVIRONMENT K3D_CLUSTER_NAME CODESPACE_NAME
  run detectRunEnvironment
  [ "$status" -eq 0 ]
  [ "$output" = "local" ]
}

@test "detectRunEnvironment: ORBITAL_ENVIRONMENT takes priority over CODESPACE_NAME" {
  source_functions
  export ORBITAL_ENVIRONMENT=true
  export CODESPACE_NAME="my-codespace-123"
  run detectRunEnvironment
  [ "$status" -eq 0 ]
  [ "$output" = "orbital" ]
}

@test "detectRunEnvironment: K3D_CLUSTER_NAME without master- prefix is not orbital" {
  source_functions
  unset ORBITAL_ENVIRONMENT CODESPACE_NAME
  export K3D_CLUSTER_NAME="k3s-default"
  run detectRunEnvironment
  [ "$status" -eq 0 ]
  [ "$output" = "local" ]
}

# ============================================================
# startCluster / stopCluster / deleteCluster engine routing
# ============================================================

@test "startCluster routes to startK3dCluster when CLUSTER_ENGINE=k3d" {
  source_functions
  export CLUSTER_ENGINE=k3d
  startK3dCluster() { echo "called=k3d"; }
  export -f startK3dCluster
  result=$(startCluster)
  [[ "$result" == *"called=k3d"* ]]
}

@test "startCluster routes to startKindCluster when CLUSTER_ENGINE=kind" {
  source_functions
  export CLUSTER_ENGINE=kind
  startKindCluster() { echo "called=kind"; }
  export -f startKindCluster
  result=$(startCluster)
  [[ "$result" == *"called=kind"* ]]
}

@test "stopCluster routes to stopK3dCluster when CLUSTER_ENGINE=k3d" {
  source_functions
  export CLUSTER_ENGINE=k3d
  stopK3dCluster() { echo "stopped=k3d"; }
  export -f stopK3dCluster
  result=$(stopCluster)
  [[ "$result" == *"stopped=k3d"* ]]
}

@test "stopCluster routes to stopKindCluster when CLUSTER_ENGINE=kind" {
  source_functions
  export CLUSTER_ENGINE=kind
  stopKindCluster() { echo "stopped=kind"; }
  export -f stopKindCluster
  result=$(stopCluster)
  [[ "$result" == *"stopped=kind"* ]]
}

@test "deleteCluster routes to deleteK3dCluster when CLUSTER_ENGINE=k3d" {
  source_functions
  export CLUSTER_ENGINE=k3d
  deleteK3dCluster() { echo "deleted=k3d"; }
  export -f deleteK3dCluster
  result=$(deleteCluster)
  [[ "$result" == *"deleted=k3d"* ]]
}

@test "deleteCluster routes to deleteKindCluster when CLUSTER_ENGINE=kind" {
  source_functions
  export CLUSTER_ENGINE=kind
  deleteKindCluster() { echo "deleted=kind"; }
  export -f deleteKindCluster
  result=$(deleteCluster)
  [[ "$result" == *"deleted=kind"* ]]
}

# ============================================================
# deployCloudNative — k3d guard warning
# ============================================================

@test "deployCloudNative warns when CLUSTER_ENGINE=k3d (default)" {
  source_functions
  unset CLUSTER_ENGINE
  warned=0
  printWarn() { warned=1; }
  export -f printWarn
  deployDynatrace() { :; }
  export -f deployDynatrace
  deployCloudNative
  [ "$warned" -eq 1 ]
}

@test "deployCloudNative does NOT warn when CLUSTER_ENGINE=kind (non-Orbital)" {
  source_functions
  export CLUSTER_ENGINE=kind
  unset ORBITAL_ENVIRONMENT CODESPACE_NAME K3D_CLUSTER_NAME
  warned=0
  printWarn() { warned=1; }
  export -f printWarn
  deployDynatrace() { :; }
  export -f deployDynatrace
  deployCloudNative
  [ "$warned" -eq 0 ]
}

@test "deployCloudNative warns on Orbital even when CLUSTER_ENGINE=kind" {
  source_functions
  export CLUSTER_ENGINE=kind
  export ORBITAL_ENVIRONMENT=true
  warned=0
  printWarn() { warned=1; }
  export -f printWarn
  deployDynatrace() { :; }
  export -f deployDynatrace
  deployCloudNative
  [ "$warned" -eq 1 ]
}

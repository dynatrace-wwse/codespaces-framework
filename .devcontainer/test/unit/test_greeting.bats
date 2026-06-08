#!/usr/bin/env bats
# Tests for printGreeting / printRunningApplications app-exposure output.
# greeting.sh runs as a standalone bash process (printGreeting -> `bash greeting.sh`)
# that sources only variables.sh — NOT functions.sh. These tests guard that the
# per-environment app URL is printed correctly (orbital / codespaces / local)
# without depending on functions.sh being loaded.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  export REPO_PATH="$FAKE_REPO"
  export APP_REGISTRY="$TEST_DIR/app-registry"

  # Minimal variables.sh stub (greeting.sh sources this). No colors needed —
  # unset color vars expand to empty since greeting.sh does not use `set -u`.
  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
RepositoryName="test-enablement"
INSTANTIATION_TYPE="local-docker-container"
export APP_REGISTRY="${APP_REGISTRY:-${HOME}/.cache/dt-framework/app-registry}"
VARSEOF

  # Use the real greeting.sh under test
  cp "$BATS_TEST_DIRNAME/../../util/greeting.sh" \
     "$FAKE_REPO/.devcontainer/util/greeting.sh"

  # Cluster appears running so printApplications calls printRunningApplications
  export CLUSTER_STATUS="running"
  export CLUSTER_TYPE="K3d (K3s)"

  # Registry: 7 fields incl. orbital subdomain
  echo "todoapp|todoapp|todoapp|8080|todoapp.10.0.0.1.sslip.io|todoapp--34ea2d-k8s-101|todoapp--34ea2d-k8s-101" > "$APP_REGISTRY"

  # Mock kubectl (printKubernetesInformation calls `kubectl version`)
  kubectl() { return 0; }
  export -f kubectl
}

teardown() {
  rm -rf "$TEST_DIR"
}

run_greeting() {
  run bash "$FAKE_REPO/.devcontainer/util/greeting.sh"
}

@test "greeting: orbital prints wildcard subdomain URL" {
  export ORBITAL_ENVIRONMENT=true
  unset CODESPACE_NAME CODESPACES
  run_greeting
  [[ "$output" == *"https://todoapp--34ea2d-k8s-101.autonomous-enablements.whydevslovedynatrace.com"* ]]
}

@test "greeting: orbital detected via K3D_CLUSTER_NAME=master-* prefix" {
  unset ORBITAL_ENVIRONMENT CODESPACE_NAME CODESPACES
  export K3D_CLUSTER_NAME="master-k8s-101-abc"
  run_greeting
  [[ "$output" == *"https://todoapp--34ea2d-k8s-101.autonomous-enablements.whydevslovedynatrace.com"* ]]
}

@test "greeting: codespaces prints port-80 github.dev URL" {
  unset ORBITAL_ENVIRONMENT K3D_CLUSTER_NAME
  export CODESPACE_NAME="myspace" CODESPACES=true
  run_greeting
  [[ "$output" == *"https://myspace-80.app.github.dev"* ]]
}

@test "greeting: local prints sslip.io magic-DNS URL" {
  unset ORBITAL_ENVIRONMENT CODESPACE_NAME CODESPACES K3D_CLUSTER_NAME
  run_greeting
  [[ "$output" == *"http://todoapp.10.0.0.1.sslip.io"* ]]
}

@test "greeting: does not depend on detectRunEnvironment (no command-not-found)" {
  export ORBITAL_ENVIRONMENT=true
  run_greeting
  [[ "$output" != *"detectRunEnvironment: command not found"* ]]
  [[ "$output" != *"command not found"* ]]
}

@test "greeting: empty registry shows the deployApp hint, no URLs" {
  : > "$APP_REGISTRY"
  unset ORBITAL_ENVIRONMENT CODESPACE_NAME CODESPACES K3D_CLUSTER_NAME
  run_greeting
  [[ "$output" == *"No applications are running"* ]]
  [[ "$output" != *"is reachable under"* ]]
}

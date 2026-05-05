#!/usr/bin/env bats
# Tests for Phase 2 Task 3: Config-driven Dynakube generation
# Covers: loadDynakubeConfig, generateDynakube, deployDynatrace wrappers

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

  # DT credentials for generation
  export DT_TENANT="https://abc123.live.dynatrace.com"
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  export DT_OPERATOR_TOKEN="dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"
  export DT_INGEST_TOKEN="dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"

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
DT_OPERATOR_VERSION="1.8.1"
AG_IMAGE="public.ecr.aws/dynatrace/dynatrace-activegate:1.327.28"
OA_IMAGE="public.ecr.aws/dynatrace/dynatrace-oneagent:1.325.66"
export USE_LEGACY_PORTS="false"
export MAGIC_DOMAIN="sslip.io"
VARSEOF

  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"

  # Copy the defaults config
  cp "$BATS_TEST_DIRNAME/../../yaml/dynakube-defaults.yaml" \
     "$FAKE_REPO/.devcontainer/yaml/dynakube-defaults.yaml"

  # Copy functions.sh
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" \
     "$FAKE_REPO/.devcontainer/util/functions.sh"

  # Mock kubectl and helm
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
# loadDynakubeConfig tests
# ============================================================

@test "loadDynakubeConfig: loads defaults when no repo config" {
  source_functions

  loadDynakubeConfig

  [ "$DK_MODE" = "apponly" ]
  # Operator version should be set (whatever is in dynakube-defaults.yaml)
  [[ -n "$DK_OPERATOR_VERSION" ]]
  [[ "$DK_DYNAKUBE_API_VERSION" == *"v1beta6"* ]]
  [ "$DK_AG_REPLICAS" = "1" ]
}

@test "loadDynakubeConfig: repo config overrides defaults" {
  source_functions

  # Create repo-level config
  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
operator_version: "1.9.0"
mode: apponly
kspm: true
ag_memory_request: "1Gi"
EOF

  loadDynakubeConfig

  [ "$DK_OPERATOR_VERSION" = "1.9.0" ]
  [ "$DK_MODE" = "apponly" ]
  [ "$DK_KSPM" = "true" ]
  [ "$DK_AG_MEMORY_REQUEST" = "1Gi" ]
}

# ============================================================
# generateDynakube tests
# ============================================================

@test "generateDynakube: creates dynakube.yaml in gen/" {
  source_functions

  generateDynakube cloudnative

  [ -f "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml" ]
}

@test "generateDynakube: cloudnative mode includes cloudNativeFullStack" {
  source_functions

  generateDynakube cloudnative

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"cloudNativeFullStack"* ]]
  [[ "$output" == *"kubernetes-monitoring"* ]]
  [[ "$output" == *"routing"* ]]
}

@test "generateDynakube: apponly mode includes applicationMonitoring" {
  source_functions

  generateDynakube apponly

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"applicationMonitoring"* ]]
  [[ "$output" != *"cloudNativeFullStack"* ]]
}

@test "generateDynakube: k8s-only mode has no oneAgent section" {
  source_functions

  generateDynakube k8s-only

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"kubernetes-monitoring"* ]]
  [[ "$output" != *"cloudNativeFullStack"* ]]
  [[ "$output" != *"applicationMonitoring"* ]]
  # routing/debugging/dynatrace-api are independent toggles — present if enabled in config
}

@test "generateDynakube: uses v1beta6 API version" {
  source_functions

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"dynatrace.com/v1beta6"* ]]
}

@test "generateDynakube: sets correct apiUrl from DT_TENANT" {
  source_functions

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"apiUrl: https://abc123.live.dynatrace.com/api"* ]]
}

@test "generateDynakube: uses RepositoryName as cluster name" {
  source_functions

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"name: test-enablement"* ]]
  [[ "$output" == *"networkZone: test-enablement"* ]]
}

@test "generateDynakube: Kind-optimized resources" {
  source_functions

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"cpu: 100m"* ]]
  [[ "$output" == *"memory: 512Mi"* ]]
  [[ "$output" == *"replicas: 1"* ]]
}

@test "generateDynakube: includes Secret with encoded tokens" {
  source_functions

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"kind: Secret"* ]]
  [[ "$output" == *"apiToken:"* ]]
  [[ "$output" == *"dataIngestToken:"* ]]
}

@test "generateDynakube: log_monitoring enabled by default" {
  source_functions

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"logMonitoring"* ]]
}

@test "generateDynakube: kspm disabled by default" {
  source_functions

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" != *"kspm"* ]]
}

@test "generateDynakube: kspm enabled via config" {
  source_functions

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
mode: cloudnative
kspm: true
EOF

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"kspm"* ]]
  [[ "$output" == *"/boot"* ]]
}

@test "generateDynakube: sensitive_data adds ClusterRole" {
  source_functions

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
mode: cloudnative
sensitive_data: true
EOF

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"ClusterRole"* ]]
  [[ "$output" == *"configmaps"* ]]
  [[ "$output" == *"secrets"* ]]
}

@test "generateDynakube: telemetry_ingest adds protocols" {
  source_functions

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
mode: cloudnative
telemetry_ingest: true
EOF

  generateDynakube

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"telemetryIngest"* ]]
  [[ "$output" == *"otlp"* ]]
}

@test "generateDynakube: ARM architecture sets AG and OA images" {
  source_functions
  export ARCH="aarch64"

  generateDynakube cloudnative

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"dynatrace-activegate"* ]]
  [[ "$output" == *"dynatrace-oneagent"* ]]
}

@test "generateDynakube: AMD architecture does not set explicit images" {
  source_functions
  export ARCH="x86_64"

  generateDynakube cloudnative

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" != *"dynatrace-activegate"* ]]
  [[ "$output" != *"dynatrace-oneagent"* ]]
}

# ============================================================
# Wrapper function tests
# ============================================================

@test "deployCloudNative calls deployDynatrace with cloudnative" {
  source_functions

  # Mock deployDynatrace to capture the mode
  deployDynatrace() { echo "MODE=$1"; }
  export -f deployDynatrace

  result=$(deployCloudNative)
  [[ "$result" == "MODE=cloudnative" ]]
}

@test "deployApplicationMonitoring calls deployDynatrace with apponly" {
  source_functions

  deployDynatrace() { echo "MODE=$1"; }
  export -f deployDynatrace

  result=$(deployApplicationMonitoring)
  [[ "$result" == "MODE=apponly" ]]
}

# ============================================================
# Error payload tests
# ============================================================

@test "postCodespaceTracker: includes error_detail and app_id" {
  source_functions

  export ERROR_COUNT=2
  export CODESPACE_ERRORS="Error: pod crashed"
  export DURATION=45
  export FRAMEWORK_VERSION="1.2.7"

  # Mock curl to capture the payload
  curl() {
    # Find the -d argument
    local payload=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "-d" ]]; then
        payload="$2"
        break
      fi
      shift
    done
    echo "$payload"
  }
  export -f curl

  result=$(postCodespaceTracker)
  [[ "$result" == *"errors_detail"* ]]
  [[ "$result" == *"pod crashed"* ]]
  [[ "$result" == *"app_id"* ]]
  [[ "$result" == *"dynatrace-wwse-test-enablement"* ]]
  [[ "$result" == *"framework.version"* ]]
}

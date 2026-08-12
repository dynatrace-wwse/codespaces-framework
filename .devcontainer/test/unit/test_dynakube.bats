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
  # Session-identity inputs must not leak in from the host environment
  unset DT_HOSTGROUP GITHUB_USER
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
# Per-user session identity tests (getDtSessionId)
# ============================================================

@test "getDtSessionId: empty when no DT_HOSTGROUP or GITHUB_USER" {
  source_functions

  run getDtSessionId
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "getDtSessionId: DT_HOSTGROUP takes priority and is sanitized" {
  source_functions
  export DT_HOSTGROUP="Sergio.Hinojosa-20260714"
  export GITHUB_USER="ignored"

  run getDtSessionId
  [ "$output" = "sergio-hinojosa-20260714" ]
}

@test "getDtSessionId: derives from GITHUB_USER plus date" {
  source_functions
  export GITHUB_USER="TestUser"

  run getDtSessionId
  [ "$output" = "testuser-$(date +%Y%m%d)" ]
}

@test "generateDynakube: DT_HOSTGROUP makes name and hostGroup unique, networkZone stays repo-scoped" {
  source_functions
  export DT_HOSTGROUP="alice-20260714"

  generateDynakube cloudnative

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"name: test-enablement-alice-20260714"* ]]
  [[ "$output" == *"tokens: test-enablement-alice-20260714"* ]]
  [[ "$output" == *"hostGroup: test-enablement-alice-20260714"* ]]
  [[ "$output" == *"networkZone: test-enablement"$'\n'* ]]
}

@test "generateDynakube: long repo name truncated to keep session id, max 38 chars" {
  source_functions
  export RepositoryName="enablement-kubernetes-opentelemetry-openpipeline"
  export DT_HOSTGROUP="bob-20260714"

  generateDynakube cloudnative

  name_line=$(grep -m1 '^  name: ' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml")
  name="${name_line#  name: }"
  [ "${#name}" -le 38 ]
  [[ "$name" == *"-bob-20260714" ]]
  [[ "$name" != *"--"* ]] || true
  [[ "$name" != *"-" ]]
}

# Operator >= 1.10 tightens the DynaKube name limit per enabled feature:
# telemetryIngest -> 37 (-otel-collector suffix), KSPM -> 35, extensions -> 31.

@test "generateDynakube: telemetry_ingest caps name at 37 (operator 1.10 -otel-collector suffix)" {
  source_functions
  export RepositoryName="enablement-kubernetes-101"
  export DT_HOSTGROUP="training-test-1102-20260731"

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
telemetry_ingest: true
EOF

  generateDynakube apponly

  name_line=$(grep -m1 '^  name: ' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml")
  name="${name_line#  name: }"
  [ "${#name}" -le 37 ]
  [[ "$name" == *"-training-test-1102-20260731" ]]
}

@test "generateDynakube: telemetry_ingest off keeps the 38 cap" {
  source_functions
  export RepositoryName="enablement-kubernetes-101"
  export DT_HOSTGROUP="training-test-1102-20260731"

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
telemetry_ingest: false
EOF

  generateDynakube apponly

  name_line=$(grep -m1 '^  name: ' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml")
  name="${name_line#  name: }"
  [ "${#name}" -le 38 ]
  [ "${#name}" -ge 38 ]
}

@test "generateDynakube: kspm and extensions tighten the cap further (35 / 31)" {
  source_functions
  export RepositoryName="enablement-kubernetes-opentelemetry-openpipeline"
  export DT_HOSTGROUP="bob-20260714"

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
mode: cloudnative
telemetry_ingest: true
kspm: true
extensions: true
EOF

  generateDynakube cloudnative

  name_line=$(grep -m1 '^  name: ' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml")
  name="${name_line#  name: }"
  [ "${#name}" -le 31 ]
  [[ "$name" == *"-bob-20260714" ]]
}

@test "generateDynakube: no session id also respects the feature cap (long repo names)" {
  source_functions
  export RepositoryName="enablement-kubernetes-opentelemetry-openpipeline"

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
telemetry_ingest: true
EOF

  generateDynakube apponly

  name_line=$(grep -m1 '^  name: ' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml")
  name="${name_line#  name: }"
  [ "${#name}" -le 37 ]
  [[ "$name" != *"-" ]]
}

# The contract every lab depends on: `endsWith(k8s.cluster.name, "{{DT_SESSION_ID}}")`.
# The composed name used to be truncated at the TAIL, which cut the session id off
# and made that filter return zero records while the data was fine (SPEC-005).

@test "generateDynakube: name ends with the WHOLE session id at every feature cap" {
  source_functions
  export RepositoryName="enablement-kubernetes-opentelemetry-openpipeline"
  # 26 chars — the longest id Orbital can now produce (17 local + '-' + date).
  export DT_HOSTGROUP="abcdefghijklmnopq-20260812"

  for features in "" "telemetry_ingest: true" "kspm: true" "extensions: true"; do
    printf '%s\n' "$features" > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml"
    generateDynakube apponly
    name_line=$(grep -m1 '^  name: ' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml")
    name="${name_line#  name: }"
    [[ "$name" == *"abcdefghijklmnopq-20260812" ]]
    [ "${#name}" -le 38 ]
  done
}

@test "generateDynakube: the constant 'enablement-' prefix is dropped from the name only" {
  source_functions
  export RepositoryName="enablement-kubernetes-101"
  export DT_HOSTGROUP="tt-n-mk3p9aqz-20260812"

  cat > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml" <<'EOF'
telemetry_ingest: true
EOF

  generateDynakube apponly

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  # readable repo half + intact session id, instead of the old "e-tt-n-…"
  [[ "$output" == *"name: kubernetes-101-tt-n-mk3p9aqz-20260812"* ]]
  [[ "$output" == *"hostGroup: kubernetes-101-tt-n-mk3p9aqz-20260812"* ]] || true
  # networkZone keeps the full repo name — repo-scoped identity, unchanged
  [[ "$output" == *"networkZone: enablement-kubernetes-101"$'\n'* ]]
}

@test "generateDynakube: an over-long session id warns loudly instead of being cut" {
  source_functions
  export RepositoryName="enablement-kubernetes-101"
  # 45 chars — the shape the old training-test identity produced
  export DT_HOSTGROUP="training-test-manual-ingest-probe-1786501397-20260812"

  run generateDynakube apponly
  [[ "$output" == *"does not fit"* ]]
  [[ "$output" == *"will NOT match"* ]]

  name_line=$(grep -m1 '^  name: ' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml")
  name="${name_line#  name: }"
  # falls back to the repo-scoped name; never ships a half-truncated session id
  [[ "$name" != *"training-test-manual-ingest"* ]]
  [ "${#name}" -le 38 ]
}

@test "generateDynakube: no session id keeps pre-1.9 repo-scoped identity" {
  source_functions

  generateDynakube cloudnative

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"name: test-enablement"$'\n'* ]]
  [[ "$output" == *"hostGroup: test-enablement"$'\n'* ]]
}

# ============================================================
# Wrapper function tests
# ============================================================

@test "deployCloudNative calls deployDynatrace with cloudnative" {
  source_functions

  # Suppress warning banners (printWarn → stdout) so we can match MODE= cleanly
  printWarn() { :; }
  export -f printWarn
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
  export FRAMEWORK_VERSION="1.3.0"

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

# ============================================================
# Sprint ActiveGate image workaround
# ============================================================

@test "isSprintTenant: true for sprint apps + api URLs, false for prod/sro" {
  source_functions

  run isSprintTenant "https://ydi9582h.sprint.apps.dynatracelabs.com"
  [ "$status" -eq 0 ]
  run isSprintTenant "https://ydi9582h.sprint.dynatracelabs.com/api"
  [ "$status" -eq 0 ]

  run isSprintTenant "https://geu80787.apps.dynatrace.com"
  [ "$status" -ne 0 ]
  run isSprintTenant "https://sro97894.apps.dynatrace.com"
  [ "$status" -ne 0 ]
}

@test "isSprintTenant: falls back to DT_ENVIRONMENT when no arg" {
  source_functions

  DT_ENVIRONMENT="https://ydi9582h.sprint.apps.dynatracelabs.com" run isSprintTenant
  [ "$status" -eq 0 ]
  DT_ENVIRONMENT="https://geu80787.apps.dynatrace.com" run isSprintTenant
  [ "$status" -ne 0 ]
}

@test "_pickLatestActiveGateTag: picks newest clean version, ignores sig/att/fips/raw" {
  source_functions

  output="$(printf "%s\n" \
    "1.341.34.20260703-181150" \
    "1.343.52.20260727-092518" \
    "1.339.39.20260605-153224" \
    "1.343.52.20260727-092518-fips" \
    "sha256-deadbeef.sig" \
    "1.341.31-raw" \
    "sha256-cafe.att" | _pickLatestActiveGateTag)"
  [ "$output" = "1.343.52.20260727-092518" ]
}

@test "_pickLatestActiveGateTag: empty when no clean version tags" {
  source_functions

  output="$(printf "%s\n" "latest" "sha256-x.sig" "1.341.31-raw" | _pickLatestActiveGateTag)"
  [ -z "$output" ]
}

@test "fixSprintActiveGateImage: no-op (returns 0, no kubectl) on non-sprint tenant" {
  source_functions
  # kubectl stub that would fail the test if called
  kubectl() { echo "KUBECTL_SHOULD_NOT_RUN" >&2; return 1; }
  export -f kubectl

  DT_ENVIRONMENT="https://geu80787.apps.dynatrace.com" run fixSprintActiveGateImage
  [ "$status" -eq 0 ]
  [[ "$output" != *"KUBECTL_SHOULD_NOT_RUN"* ]]
  [[ "$output" != *"downgrading to the latest available"* ]]
}

@test "generateDynakube: pins the public ActiveGate image on a sprint tenant (x86)" {
  source_functions
  latestPublicActiveGateImage() { echo "public.ecr.aws/dynatrace/dynatrace-activegate:9.9.9.9-9"; }
  getLatestEcrTag() { echo "1.2.3.4-5"; }

  DT_ENVIRONMENT="https://ydi9582h.sprint.apps.dynatracelabs.com" generateDynakube apponly

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *'image: "public.ecr.aws/dynatrace/dynatrace-activegate:9.9.9.9-9"'* ]]
}

@test "generateDynakube: no ActiveGate image on a non-sprint tenant (x86)" {
  source_functions
  # would fail the test if the sprint branch ran
  latestPublicActiveGateImage() { echo "public.ecr.aws/dynatrace/dynatrace-activegate:SHOULD_NOT_RUN"; }
  getLatestEcrTag() { echo "1.2.3.4-5"; }

  generateDynakube apponly   # setup() exports a prod DT_ENVIRONMENT

  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" != *"SHOULD_NOT_RUN"* ]]
  [[ "$output" != *"dynatrace-activegate:"* ]]
}

@test "generateDynakube: sprint + ARM keeps the ARM image, no double pin" {
  source_functions
  latestPublicActiveGateImage() { echo "public.ecr.aws/dynatrace/dynatrace-activegate:SHOULD_NOT_RUN"; }
  getLatestEcrTag() { echo "1.2.3.4-5"; }

  ARCH="aarch64" DT_ENVIRONMENT="https://ydi9582h.sprint.apps.dynatracelabs.com" \
    generateDynakube apponly

  run grep -c '^    image:' "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [ "$output" = "1" ]
  run cat "$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" == *"dynatrace-activegate:1.2.3.4-5"* ]]
  [[ "$output" != *"SHOULD_NOT_RUN"* ]]
}

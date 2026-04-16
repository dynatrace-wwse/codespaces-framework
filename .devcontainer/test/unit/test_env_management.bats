#!/usr/bin/env bats
# Tests for Phase 2 Task 1: Environment Variable Management
# Covers: parseDynatraceEnvironment, variablesNeeded, enableMCP, disableMCP,
#         validateSaveCredentials (refactored), helper.sh improvements

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  # Create a fake repo directory matching the framework structure
  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  mkdir -p "$FAKE_REPO/.devcontainer/test"
  mkdir -p "$FAKE_REPO/.devcontainer/runlocal"
  mkdir -p "$FAKE_REPO/.vscode"

  # Set required variables before sourcing
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env"

  # Create minimal variables.sh stub
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
if [ -e "$ENV_FILE" ]; then
  source "$ENV_FILE"
fi
COUNT_FILE="$REPO_PATH/.devcontainer/util/.count"
export COUNT_FILE
INSTANTIATION_TYPE="local-docker-container"
VARSEOF

  # Create minimal test_functions.sh stub
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"

  # Create minimal my_functions.sh stub (sourced at the end of functions.sh)
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"

  # Copy the real functions.sh
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" \
     "$FAKE_REPO/.devcontainer/util/functions.sh"

  # Copy the real helper.sh
  cp "$BATS_TEST_DIRNAME/../../runlocal/helper.sh" \
     "$FAKE_REPO/.devcontainer/runlocal/helper.sh"

  # Source functions in a clean environment
  # We override kubectl to avoid real cluster calls
  kubectl() { return 1; }
  export -f kubectl
}

teardown() {
  rm -rf "$TEST_DIR"
}

# Helper to source functions.sh in a subshell with our stubs
source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
}

# ============================================================
# parseDynatraceEnvironment tests
# ============================================================

@test "parseDynatraceEnvironment: prod URL transforms .apps. to .live." {
  source_functions

  parseDynatraceEnvironment "https://abc123.apps.dynatrace.com"

  [ "$DT_TENANT" = "https://abc123.live.dynatrace.com" ]
  [ "$DT_ENV_TYPE" = "prod" ]
  [ "$DT_OTEL_ENDPOINT" = "https://abc123.live.dynatrace.com/api/v2/otlp" ]
  [ "$DT_ENVIRONMENT" = "https://abc123.apps.dynatrace.com" ]
}

@test "parseDynatraceEnvironment: sprint URL removes .apps." {
  source_functions

  parseDynatraceEnvironment "https://abc123.sprint.apps.dynatracelabs.com"

  [ "$DT_TENANT" = "https://abc123.sprint.dynatracelabs.com" ]
  [ "$DT_ENV_TYPE" = "sprint" ]
}

@test "parseDynatraceEnvironment: dev URL removes .apps." {
  source_functions

  parseDynatraceEnvironment "https://abc123.dev.apps.dynatracelabs.com"

  [ "$DT_TENANT" = "https://abc123.dev.dynatracelabs.com" ]
  [ "$DT_ENV_TYPE" = "dev" ]
}

@test "parseDynatraceEnvironment: strips trailing path after .com" {
  source_functions

  parseDynatraceEnvironment "https://abc123.apps.dynatrace.com/ui/apps"

  [ "$DT_TENANT" = "https://abc123.live.dynatrace.com" ]
}

@test "parseDynatraceEnvironment: generic labs URL" {
  source_functions

  parseDynatraceEnvironment "https://xyz789.apps.dynatracelabs.com"

  [ "$DT_TENANT" = "https://xyz789.dynatracelabs.com" ]
  [ "$DT_ENV_TYPE" = "labs" ]
}

@test "parseDynatraceEnvironment: fails on invalid URL (no https)" {
  source_functions

  run parseDynatraceEnvironment "http://abc123.apps.dynatrace.com"
  [ "$status" -eq 1 ]
}

@test "parseDynatraceEnvironment: fails on invalid URL (no dynatrace domain)" {
  source_functions

  run parseDynatraceEnvironment "https://example.com"
  [ "$status" -eq 1 ]
}

@test "parseDynatraceEnvironment: fails when no argument and no env var" {
  source_functions
  unset DT_ENVIRONMENT

  run parseDynatraceEnvironment
  [ "$status" -eq 1 ]
}

@test "parseDynatraceEnvironment: reads from DT_ENVIRONMENT when no arg" {
  source_functions
  export DT_ENVIRONMENT="https://geu80787.apps.dynatrace.com"

  parseDynatraceEnvironment

  [ "$DT_TENANT" = "https://geu80787.live.dynatrace.com" ]
  [ "$DT_ENV_TYPE" = "prod" ]
}

# ============================================================
# variablesNeeded tests
# ============================================================

@test "variablesNeeded: passes when all required vars are set" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  export DT_OPERATOR_TOKEN="dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"
  export DT_INGEST_TOKEN="dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ"

  run variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:true
  [ "$status" -eq 0 ]
}

@test "variablesNeeded: fails when required var is missing" {
  source_functions
  unset DT_ENVIRONMENT
  export DT_OPERATOR_TOKEN="dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"

  run variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true
  [ "$status" -eq 1 ]
}

@test "variablesNeeded: warns but passes when optional var is missing" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  unset DT_INGEST_TOKEN

  run variablesNeeded DT_ENVIRONMENT:true DT_INGEST_TOKEN:false
  [ "$status" -eq 0 ]
  [[ "$output" == *"not set (optional)"* ]]
}

@test "variablesNeeded: validates DT token format (valid dt0c01)" {
  source_functions
  export DT_OPERATOR_TOKEN="dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"

  run variablesNeeded DT_OPERATOR_TOKEN:true
  [ "$status" -eq 0 ]
  [[ "$output" == *"valid Dynatrace token format"* ]]
}

@test "variablesNeeded: validates DT token format (valid dt0s01)" {
  source_functions
  export DT_OPERATOR_TOKEN="dt0s01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"

  run variablesNeeded DT_OPERATOR_TOKEN:true
  [ "$status" -eq 0 ]
  [[ "$output" == *"valid Dynatrace token format"* ]]
}

@test "variablesNeeded: rejects invalid token format" {
  source_functions
  export DT_OPERATOR_TOKEN="invalid-token-format"

  run variablesNeeded DT_OPERATOR_TOKEN:true
  [ "$status" -eq 1 ]
  [[ "$output" == *"invalid token format"* ]]
}

@test "variablesNeeded: rejects short token" {
  source_functions
  export DT_OPERATOR_TOKEN="dt0c01.short"

  run variablesNeeded DT_OPERATOR_TOKEN:true
  [ "$status" -eq 1 ]
  [[ "$output" == *"invalid token format"* ]]
}

@test "variablesNeeded: parses DT_ENVIRONMENT and sets derived vars" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"

  variablesNeeded DT_ENVIRONMENT:true

  [ "$DT_TENANT" = "https://abc123.live.dynatrace.com" ]
  [ "$DT_OTEL_ENDPOINT" = "https://abc123.live.dynatrace.com/api/v2/otlp" ]
}

@test "variablesNeeded: multiple missing required vars all reported" {
  source_functions
  unset DT_ENVIRONMENT DT_OPERATOR_TOKEN DT_INGEST_TOKEN

  run variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:true
  [ "$status" -eq 1 ]
  [[ "$output" == *"DT_ENVIRONMENT is required but not set"* ]]
  [[ "$output" == *"DT_OPERATOR_TOKEN is required but not set"* ]]
  [[ "$output" == *"DT_INGEST_TOKEN is required but not set"* ]]
}

@test "variablesNeeded: generic non-DT variable passes when set" {
  source_functions
  export MY_CUSTOM_VAR="hello"

  run variablesNeeded MY_CUSTOM_VAR:true
  [ "$status" -eq 0 ]
  [[ "$output" == *"MY_CUSTOM_VAR is set"* ]]
}

# ============================================================
# enableMCP / disableMCP tests
# ============================================================

@test "enableMCP: creates mcp.json when DT_ENVIRONMENT is set" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  touch "$ENV_FILE"

  enableMCP

  [ -f "$FAKE_REPO/.vscode/mcp.json" ]
  # Verify content
  run cat "$FAKE_REPO/.vscode/mcp.json"
  [[ "$output" == *"dynatrace-mcp-server"* ]]
  [[ "$output" == *"@dynatrace-oss/dynatrace-mcp-server@latest"* ]]
}

@test "enableMCP: writes DT_ENVIRONMENT to .env if not present" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  touch "$ENV_FILE"

  enableMCP

  run cat "$ENV_FILE"
  [[ "$output" == *"DT_ENVIRONMENT=https://abc123.apps.dynatrace.com"* ]]
}

@test "enableMCP: reads DT_ENVIRONMENT from .env if not in env" {
  source_functions
  unset DT_ENVIRONMENT
  echo "DT_ENVIRONMENT=https://xyz789.apps.dynatrace.com" > "$ENV_FILE"

  enableMCP

  [ -f "$FAKE_REPO/.vscode/mcp.json" ]
}

@test "disableMCP: removes mcp.json" {
  source_functions
  mkdir -p "$FAKE_REPO/.vscode"
  echo '{"servers":{}}' > "$FAKE_REPO/.vscode/mcp.json"

  disableMCP

  [ ! -f "$FAKE_REPO/.vscode/mcp.json" ]
}

@test "disableMCP: no error when mcp.json doesn't exist" {
  source_functions

  run disableMCP
  [ "$status" -eq 0 ]
  [[ "$output" == *"not enabled"* ]]
}

@test "enableMCP: round-trip enable → disable → enable" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  touch "$ENV_FILE"

  enableMCP
  [ -f "$FAKE_REPO/.vscode/mcp.json" ]

  disableMCP
  [ ! -f "$FAKE_REPO/.vscode/mcp.json" ]

  enableMCP
  [ -f "$FAKE_REPO/.vscode/mcp.json" ]
  run cat "$FAKE_REPO/.vscode/mcp.json"
  [[ "$output" == *"dynatrace-mcp-server"* ]]
}

@test "enableMCP: updates mcp.json when called with different environment" {
  source_functions
  touch "$ENV_FILE"

  # First enable with env A
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  enableMCP
  [ -f "$FAKE_REPO/.vscode/mcp.json" ]

  # Change environment and re-enable
  export DT_ENVIRONMENT="https://xyz789.apps.dynatrace.com"
  # Update .env too
  sed -i 's/abc123/xyz789/' "$ENV_FILE"
  enableMCP

  # .env should reference the new environment
  run cat "$ENV_FILE"
  [[ "$output" == *"xyz789"* ]]
}

@test "enableMCP: does not duplicate DT_ENVIRONMENT in .env" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  echo "DT_ENVIRONMENT=https://abc123.apps.dynatrace.com" > "$ENV_FILE"

  enableMCP

  # Should have exactly one DT_ENVIRONMENT line
  local count
  count=$(grep -c "DT_ENVIRONMENT=" "$ENV_FILE")
  [ "$count" -eq 1 ]
}

@test "enableMCP: mcp.json has correct structure" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  touch "$ENV_FILE"

  enableMCP

  # Validate JSON structure
  run cat "$FAKE_REPO/.vscode/mcp.json"
  [[ "$output" == *'"type": "stdio"'* ]]
  [[ "$output" == *'"command": "npx"'* ]]
  [[ "$output" == *'${workspaceFolder}'* ]]
  [[ "$output" == *'.devcontainer/.env'* ]]
}

@test "setupMCPServer: deprecated wrapper calls enableMCP" {
  source_functions
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  touch "$ENV_FILE"

  run setupMCPServer
  [ "$status" -eq 0 ]
  [[ "$output" == *"deprecated"* ]]
  [ -f "$FAKE_REPO/.vscode/mcp.json" ]
}

# ============================================================
# helper.sh tests
# ============================================================

@test "helper.sh: detectNeededVariables parses post-create.sh" {
  # Set _MAKEFILE_DIR for helper.sh
  export _MAKEFILE_DIR="$FAKE_REPO/.devcontainer"

  # Create a post-create.sh with variablesNeeded
  cat > "$FAKE_REPO/.devcontainer/post-create.sh" <<'EOF'
#!/bin/bash
source .devcontainer/util/source_framework.sh
variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:false
setUpTerminal
EOF

  source "$FAKE_REPO/.devcontainer/runlocal/helper.sh"

  result=$(detectNeededVariables)
  [[ "$result" == *"DT_ENVIRONMENT:true"* ]]
  [[ "$result" == *"DT_OPERATOR_TOKEN:true"* ]]
  [[ "$result" == *"DT_INGEST_TOKEN:false"* ]]
}

@test "helper.sh: generateEnvExample creates .env.example" {
  export _MAKEFILE_DIR="$FAKE_REPO/.devcontainer"

  cat > "$FAKE_REPO/.devcontainer/post-create.sh" <<'EOF'
#!/bin/bash
variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:false
EOF

  source "$FAKE_REPO/.devcontainer/runlocal/helper.sh"

  generateEnvExample

  [ -f "$FAKE_REPO/.devcontainer/.env.example" ]
  run cat "$FAKE_REPO/.devcontainer/.env.example"
  [[ "$output" == *"DT_ENVIRONMENT="* ]]
  [[ "$output" == *"DT_OPERATOR_TOKEN="* ]]
  [[ "$output" == *"(required)"* ]]
  [[ "$output" == *"(optional)"* ]]
}

@test "helper.sh: getDockerEnvsFromEnvFile fails with info when .env missing" {
  export _MAKEFILE_DIR="$FAKE_REPO/.devcontainer"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env"

  cat > "$FAKE_REPO/.devcontainer/post-create.sh" <<'EOF'
#!/bin/bash
variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:false
EOF

  source "$FAKE_REPO/.devcontainer/runlocal/helper.sh"

  # .env does not exist — should exit 1 with helpful output
  run getDockerEnvsFromEnvFile
  [ "$status" -eq 1 ]
  [[ "$output" == *".env file not found"* ]]
  [[ "$output" == *"REQUIRED"* ]]
  [[ "$output" == *"DT_ENVIRONMENT"* ]]
}

@test "helper.sh: getDockerEnvsFromEnvFile loads vars from .env" {
  export _MAKEFILE_DIR="$FAKE_REPO/.devcontainer"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env"

  echo "DT_ENVIRONMENT=https://abc123.apps.dynatrace.com" > "$ENV_FILE"

  source "$FAKE_REPO/.devcontainer/runlocal/helper.sh"

  getDockerEnvsFromEnvFile

  [[ "$DOCKER_ENVS" == *"-e DT_ENVIRONMENT=https://abc123.apps.dynatrace.com"* ]]
}

@test "helper.sh: getDockerEnvsFromEnvFile warns on empty required vars" {
  export _MAKEFILE_DIR="$FAKE_REPO/.devcontainer"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env"

  cat > "$FAKE_REPO/.devcontainer/post-create.sh" <<'EOF'
#!/bin/bash
variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true
EOF

  # .env exists but DT_OPERATOR_TOKEN is missing
  echo "DT_ENVIRONMENT=https://abc123.apps.dynatrace.com" > "$ENV_FILE"

  source "$FAKE_REPO/.devcontainer/runlocal/helper.sh"

  run getDockerEnvsFromEnvFile
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARNING: Required variable DT_OPERATOR_TOKEN"* ]]
}

# ============================================================
# verifyParseSecret backward compatibility tests
# ============================================================

@test "verifyParseSecret: parses prod tenant URL (backward compat)" {
  source_functions

  result=$(verifyParseSecret "https://abc123.apps.dynatrace.com" false)
  [[ "$result" == "https://abc123.live.dynatrace.com" ]]
}

@test "verifyParseSecret: validates token format (backward compat)" {
  source_functions

  result=$(verifyParseSecret "dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY" false)
  [[ "$result" == "dt0c01.XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX.YYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY" ]]
}

@test "verifyParseSecret: rejects invalid secret (backward compat)" {
  source_functions

  run verifyParseSecret "invalid-secret" false
  [ "$status" -eq 1 ]
}

# ============================================================
# Configmap removal verification
# ============================================================

@test "validateSaveCredentials: no configmap references in function" {
  # Verify configmap code was fully removed
  run grep -c "configmap\|dtcredentials" "$FAKE_REPO/.devcontainer/util/functions.sh"
  # Only the filter line in verifyCodespaceCreation should reference configmap/dtcredentials
  # (as a noise filter pattern)
  local count="$output"
  # Should be minimal — only the noise filter in verifyCodespaceCreation
  [[ "$count" -le 2 ]]
}

@test "dynatraceEvalReadSaveCredentials: no configmap fallback" {
  source_functions
  unset DT_ENVIRONMENT DT_OPERATOR_TOKEN DT_INGEST_TOKEN

  # With no env vars and no configmap, should return 1 (warn, not error)
  run dynatraceEvalReadSaveCredentials
  [ "$status" -eq 1 ]
  # Should NOT mention configmap
  [[ "$output" != *"configmap"* ]]
  [[ "$output" != *"ConfigMap"* ]]
  # Should suggest env vars or .env file
  [[ "$output" == *"environment variables"* ]]
}

# ============================================================
# verifyCodespaceCreation tests
# ============================================================

@test "verifyCodespaceCreation: filters out configmap noise from error count" {
  source_functions

  # Use codespaces mode with a fake creation.log — avoids docker mocking issues
  export INSTANTIATION_TYPE="github-codespaces"
  export SECONDS=10
  export DURATION=0
  mkdir -p "$(dirname "$COUNT_FILE")"
  echo "DURATION=0" > "$COUNT_FILE"
  echo "ERROR_COUNT=0" >> "$COUNT_FILE"

  # Create a fake creation.log with mixed output
  export CODESPACE_PSHARE_FOLDER="$TEST_DIR/codespaces"
  mkdir -p "$CODESPACE_PSHARE_FOLDER"
  cat > "$CODESPACE_PSHARE_FOLDER/creation.log" <<'LOGEOF'
INFO: Starting Kind cluster
ERROR: configmap default/dtcredentials already exists
configmap dtcredentials not found
npm WARN deprecated package@1.0
WARNING: error_reporting is disabled
Real error: pod crashloopbackoff
INFO: Deployment complete
LOGEOF

  verifyCodespaceCreation

  # Only "Real error: pod crashloopbackoff" should be counted
  [ "$ERROR_COUNT" -eq 1 ]
  [[ "$CODESPACE_ERRORS" == *"crashloopbackoff"* ]]
  [[ "$CODESPACE_ERRORS" != *"configmap"* ]]
}

@test "verifyCodespaceCreation: zero errors on clean creation" {
  source_functions

  export INSTANTIATION_TYPE="github-codespaces"
  export SECONDS=30
  export DURATION=0
  mkdir -p "$(dirname "$COUNT_FILE")"
  echo "DURATION=0" > "$COUNT_FILE"
  echo "ERROR_COUNT=0" >> "$COUNT_FILE"

  # Create a clean creation.log
  export CODESPACE_PSHARE_FOLDER="$TEST_DIR/codespaces"
  mkdir -p "$CODESPACE_PSHARE_FOLDER"
  cat > "$CODESPACE_PSHARE_FOLDER/creation.log" <<'LOGEOF'
INFO: Starting application
INFO: Deployment complete
INFO: All pods running
LOGEOF

  verifyCodespaceCreation

  [ "$ERROR_COUNT" -eq 0 ]
  [ -z "$CODESPACE_ERRORS" ]
}

@test "verifyCodespaceCreation: counts real errors correctly" {
  source_functions

  export INSTANTIATION_TYPE="github-codespaces"
  export SECONDS=20
  export DURATION=0
  mkdir -p "$(dirname "$COUNT_FILE")"
  echo "DURATION=0" > "$COUNT_FILE"
  echo "ERROR_COUNT=0" >> "$COUNT_FILE"

  export CODESPACE_PSHARE_FOLDER="$TEST_DIR/codespaces"
  mkdir -p "$CODESPACE_PSHARE_FOLDER"
  cat > "$CODESPACE_PSHARE_FOLDER/creation.log" <<'LOGEOF'
ERROR: failed to pull image nginx:latest
ERROR: pod todoapp-xyz failed to start
npm WARN optional dep failed
LOGEOF

  verifyCodespaceCreation

  # Two real errors, npm warn is filtered
  [ "$ERROR_COUNT" -eq 2 ]
}

@test "verifyCodespaceCreation: filters own output from subsequent runs" {
  source_functions

  export INSTANTIATION_TYPE="github-codespaces"
  export SECONDS=15
  export DURATION=0
  mkdir -p "$(dirname "$COUNT_FILE")"
  echo "DURATION=0" > "$COUNT_FILE"
  echo "ERROR_COUNT=0" >> "$COUNT_FILE"

  # Simulate logs that include the framework's own previous output
  # This is what happens when you run verifyCodespaceCreation a second time
  export CODESPACE_PSHARE_FOLDER="$TEST_DIR/codespaces"
  mkdir -p "$CODESPACE_PSHARE_FOLDER"
  cat > "$CODESPACE_PSHARE_FOLDER/creation.log" <<'LOGEOF'
INFO: Starting Kind cluster
INFO: Deployment complete
[dynatrace.enablement| INFO | No errors detected in the creation of the codespace
There has been no errors detected in the creation of the codespace.
There has been 5 errors detected in the creation of the codespace, type verifyCodespaceCreation to understand more.
[dynatrace.enablement| WARN | 2 issues detected in the creation of the codespace:
[dynatrace.enablement| INFO | ERROR_COUNT=0
LOGEOF

  verifyCodespaceCreation

  # All lines are framework's own output — should be zero real errors
  [ "$ERROR_COUNT" -eq 0 ]
}

@test "verifyCodespaceCreation: filters framework log format noise" {
  source_functions

  export INSTANTIATION_TYPE="github-codespaces"
  export SECONDS=10
  export DURATION=0
  mkdir -p "$(dirname "$COUNT_FILE")"
  echo "DURATION=0" > "$COUNT_FILE"
  echo "ERROR_COUNT=0" >> "$COUNT_FILE"

  # Common framework/tool output that contains 'error' but isn't a real error
  export CODESPACE_PSHARE_FOLDER="$TEST_DIR/codespaces"
  mkdir -p "$CODESPACE_PSHARE_FOLDER"
  cat > "$CODESPACE_PSHARE_FOLDER/creation.log" <<'LOGEOF'
npm WARN deprecated package@1.0
npm warn peer dep failed optional
warning: LF will be replaced by CRLF
error_reporting = E_ALL
errorHandler is registered
error-page configured for nginx
stderr redirected to /dev/null
printError is a function
on-error callback registered
errors=0 in final check
LOGEOF

  verifyCodespaceCreation

  # All lines are noise — should be zero real errors
  [ "$ERROR_COUNT" -eq 0 ]
}

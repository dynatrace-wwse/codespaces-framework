#!/usr/bin/env bats
# Tests for $FRAMEWORK_APPS_PATH guard in deploy functions
# Verifies that deploy functions fail with clear error when source_framework.sh not loaded

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"
  export REPO_PATH="$TEST_DIR/workspaces/test-repo"
  mkdir -p "$REPO_PATH/.devcontainer/util"

  # Unset FRAMEWORK_APPS_PATH to simulate not having sourced source_framework.sh
  unset FRAMEWORK_APPS_PATH
  unset FRAMEWORK_CACHE

  # Create minimal stubs so functions.sh can be sourced without errors
  # (we only need the function definitions, not the full framework)
  echo '# variables stub' > "$REPO_PATH/.devcontainer/util/variables.sh"
  mkdir -p "$REPO_PATH/.devcontainer/test"
  echo '# test stub' > "$REPO_PATH/.devcontainer/test/test_functions.sh"
}

teardown() {
  rm -rf "$TEST_DIR"
}

# Helper: source functions.sh with minimal environment
source_functions_only() {
  # Source functions.sh without source_framework.sh
  # This gives us the function definitions without setting FRAMEWORK_APPS_PATH
  cd "$REPO_PATH"
  source "$BATS_TEST_DIRNAME/../../util/functions.sh" 2>/dev/null || true
}

@test "deployAstroshop: fails without FRAMEWORK_APPS_PATH" {
  source_functions_only
  run deployAstroshop
  [ "$status" -eq 1 ]
  [[ "$output" == *"source_framework.sh not loaded"* ]]
}

@test "deployAITravelAdvisorApp: fails without FRAMEWORK_APPS_PATH" {
  source_functions_only
  run deployAITravelAdvisorApp
  [ "$status" -eq 1 ]
  [[ "$output" == *"source_framework.sh not loaded"* ]]
}

@test "deployEasyTrade: fails without FRAMEWORK_APPS_PATH" {
  source_functions_only
  run deployEasyTrade
  [ "$status" -eq 1 ]
  [[ "$output" == *"source_framework.sh not loaded"* ]]
}

@test "deployHipsterShop: fails without FRAMEWORK_APPS_PATH" {
  source_functions_only
  run deployHipsterShop
  [ "$status" -eq 1 ]
  [[ "$output" == *"source_framework.sh not loaded"* ]]
}

@test "deployBugZapperApp: fails without FRAMEWORK_APPS_PATH" {
  source_functions_only
  run deployBugZapperApp
  [ "$status" -eq 1 ]
  [[ "$output" == *"source_framework.sh not loaded"* ]]
}

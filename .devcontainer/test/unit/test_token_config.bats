#!/usr/bin/env bats
# Tests for loadTokenConfig / _parseTokenScalars (framework 1.11.0)
#
# Covers: framework default, repo override precedence, absent field, malformed value.
# Mirrors the setup idiom from test_dynakube.bats.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  mkdir -p "$FAKE_REPO/.devcontainer/test"
  mkdir -p "$FAKE_REPO/.devcontainer/yaml"

  export REPO_PATH="$FAKE_REPO"
  export FRAMEWORK_CACHE=""
  unset TK_MIGRATION_STATUS

  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN="" BLUE="" CYAN="" YELLOW="" ORANGE="" RED="" LILA="" NORMAL="" RESET=""
thickline="" halfline="" thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"
export ENV_FILE
COUNT_FILE="$REPO_PATH/.devcontainer/util/.count"
export COUNT_FILE
INSTANTIATION_TYPE="local-docker-container"
VARSEOF

  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"

  # Copy the framework dt-tokens.yaml (the default)
  cp "$BATS_TEST_DIRNAME/../../yaml/dt-tokens.yaml" \
     "$FAKE_REPO/.devcontainer/yaml/dt-tokens.yaml"

  # Copy functions.sh
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" \
     "$FAKE_REPO/.devcontainer/util/functions.sh"
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source .devcontainer/util/variables.sh
  source .devcontainer/util/functions.sh
}

# ── 1. Framework default ──────────────────────────────────────────────────────

@test "no repo file → framework default migrationStatus exported as pending" {
  source_functions

  # No FRAMEWORK_CACHE set, so defaults_file == config_file (repo path).
  # Set FRAMEWORK_CACHE to point at the framework copy we put in FAKE_REPO.
  export FRAMEWORK_CACHE="$FAKE_REPO"

  # Remove the repo-level yaml so there is no override
  rm "$FAKE_REPO/.devcontainer/yaml/dt-tokens.yaml"

  # Put the framework yaml only in FRAMEWORK_CACHE path
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/yaml"
  cat > "$FRAMEWORK_CACHE/.devcontainer/yaml/dt-tokens.yaml" <<'YAML'
migrationStatus: pending
tokens:
  - name_suffix: operator
    env_var: DT_OPERATOR_TOKEN
YAML

  # Now make REPO_PATH a different dir with no yaml
  export ANOTHER_REPO="$TEST_DIR/workspaces/other-repo"
  mkdir -p "$ANOTHER_REPO/.devcontainer/yaml"
  export REPO_PATH="$ANOTHER_REPO"

  loadTokenConfig
  [[ "$TK_MIGRATION_STATUS" == "pending" ]]
}

@test "framework yaml with migrationStatus pending sets TK_MIGRATION_STATUS=pending" {
  source_functions

  # Both framework cache and repo file point at the same fake repo yaml (no override)
  export FRAMEWORK_CACHE="$FAKE_REPO"

  loadTokenConfig
  [[ "$TK_MIGRATION_STATUS" == "pending" ]]
}

# ── 2. Repo override wins ─────────────────────────────────────────────────────

@test "repo-specific file overrides framework migrationStatus" {
  source_functions

  # Framework cache has pending
  export FRAMEWORK_CACHE="$TEST_DIR/fw"
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/yaml"
  cat > "$FRAMEWORK_CACHE/.devcontainer/yaml/dt-tokens.yaml" <<'YAML'
migrationStatus: pending
tokens: []
YAML

  # Repo has migrated
  cat > "$FAKE_REPO/.devcontainer/yaml/dt-tokens.yaml" <<'YAML'
migrationStatus: migrated
YAML

  loadTokenConfig
  [[ "$TK_MIGRATION_STATUS" == "migrated" ]]
}

@test "repo override does not affect framework cache file" {
  source_functions

  export FRAMEWORK_CACHE="$TEST_DIR/fw"
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/yaml"
  cat > "$FRAMEWORK_CACHE/.devcontainer/yaml/dt-tokens.yaml" <<'YAML'
migrationStatus: pending
YAML

  cat > "$FAKE_REPO/.devcontainer/yaml/dt-tokens.yaml" <<'YAML'
migrationStatus: migrated
YAML

  loadTokenConfig
  # Verify the framework file is unchanged
  grep -q "migrationStatus: pending" "$FRAMEWORK_CACHE/.devcontainer/yaml/dt-tokens.yaml"
}

# ── 3. Absent migrationStatus field ──────────────────────────────────────────

@test "absent migrationStatus in framework file → TK_MIGRATION_STATUS unset" {
  source_functions

  export FRAMEWORK_CACHE="$TEST_DIR/fw"
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/yaml"
  cat > "$FRAMEWORK_CACHE/.devcontainer/yaml/dt-tokens.yaml" <<'YAML'
tokens:
  - name_suffix: operator
    env_var: DT_OPERATOR_TOKEN
YAML

  # No repo override
  export ANOTHER_REPO="$TEST_DIR/other"
  mkdir -p "$ANOTHER_REPO/.devcontainer/yaml"
  export REPO_PATH="$ANOTHER_REPO"

  unset TK_MIGRATION_STATUS
  loadTokenConfig
  [[ -z "${TK_MIGRATION_STATUS:-}" ]]
}

# ── 4. Token list does not leak into TK_* variables ──────────────────────────

@test "token list entries do not create spurious TK_* variables" {
  source_functions

  export FRAMEWORK_CACHE="$FAKE_REPO"

  loadTokenConfig

  # These keys exist in the tokens: block and must NOT appear as TK_* vars
  [[ -z "${TK_NAME_SUFFIX:-}" ]]
  [[ -z "${TK_ENV_VAR:-}" ]]
  [[ -z "${TK_SCOPES:-}" ]]
}

# ── 5. Malformed / unrecognised value is exported as-is ──────────────────────

@test "unrecognised migrationStatus value is exported unchanged" {
  source_functions

  export FRAMEWORK_CACHE="$TEST_DIR/fw"
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/yaml"
  cat > "$FRAMEWORK_CACHE/.devcontainer/yaml/dt-tokens.yaml" <<'YAML'
migrationStatus: not-a-valid-status
YAML
  export ANOTHER_REPO="$TEST_DIR/other"
  mkdir -p "$ANOTHER_REPO/.devcontainer/yaml"
  export REPO_PATH="$ANOTHER_REPO"

  loadTokenConfig
  # The bash function exports whatever it finds; validation is Orbital's job
  [[ "$TK_MIGRATION_STATUS" == "not-a-valid-status" ]]
}

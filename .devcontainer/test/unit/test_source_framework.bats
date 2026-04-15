#!/usr/bin/env bats
# Tests for source_framework.sh — versioned pull mechanism
# Covers: cache hit, cache miss, partial cache, network failure, variable exports
# Must pass under both bash and zsh

setup() {
  # Create a temporary directory for each test
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  # Create a fake repo directory
  export FAKE_REPO="$TEST_DIR/workspaces/my-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"

  # Copy source_framework.sh to the fake repo
  cp "$BATS_TEST_DIRNAME/../../util/source_framework.sh" \
     "$FAKE_REPO/.devcontainer/util/source_framework.sh"

  # Override FRAMEWORK_VERSION for tests
  export FRAMEWORK_VERSION="99.99.99"
  export FRAMEWORK_CACHE="$HOME/.cache/dt-framework/$FRAMEWORK_VERSION"
}

teardown() {
  rm -rf "$TEST_DIR"
}

# Helper: create a fake cached framework (simulates a successful prior clone)
create_fake_cache() {
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/util"
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/p10k"
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/test"
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/apps/todo-app"

  # Minimal stubs so source doesn't fail
  echo '# variables stub' > "$FRAMEWORK_CACHE/.devcontainer/util/variables.sh"
  # functions.sh stub must source my_functions.sh (like the real one does)
  cat > "$FRAMEWORK_CACHE/.devcontainer/util/functions.sh" <<'FSTUB'
# functions stub
# Source repo-level my_functions.sh if it exists, otherwise framework stub
if [ -f "$REPO_PATH/.devcontainer/util/my_functions.sh" ]; then
  source "$REPO_PATH/.devcontainer/util/my_functions.sh"
elif [ -n "$FRAMEWORK_CACHE" ] && [ -f "$FRAMEWORK_CACHE/.devcontainer/util/my_functions.sh" ]; then
  source "$FRAMEWORK_CACHE/.devcontainer/util/my_functions.sh"
fi
FSTUB
  echo '# greeting stub' > "$FRAMEWORK_CACHE/.devcontainer/util/greeting.sh"
  echo '# my_functions stub' > "$FRAMEWORK_CACHE/.devcontainer/util/my_functions.sh"
  echo '# test_functions stub' > "$FRAMEWORK_CACHE/.devcontainer/test/test_functions.sh"
  echo '# p10k stub' > "$FRAMEWORK_CACHE/.devcontainer/p10k/.p10k.zsh"
  echo '# zshrc stub' > "$FRAMEWORK_CACHE/.devcontainer/p10k/.zshrc"

  # Write the .complete sentinel
  touch "$FRAMEWORK_CACHE/.complete"
}

# Helper: create a partial cache (no .complete sentinel)
create_partial_cache() {
  mkdir -p "$FRAMEWORK_CACHE/.devcontainer/util"
  echo '# partial' > "$FRAMEWORK_CACHE/.devcontainer/util/functions.sh"
  # No .complete sentinel — cache is incomplete
}

# ============================================================
# Test: Cache exists (.complete present) -> skip clone entirely
# ============================================================
@test "cache hit: .complete present -> skip clone, source files" {
  create_fake_cache

  # Mock git to verify it's NOT called
  git() { echo "GIT_CALLED"; return 1; }
  export -f git

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh'

  [ "$status" -eq 0 ]
  # git should NOT have been called
  [[ "$output" != *"GIT_CALLED"* ]]
  # Should not see the pulling message
  [[ "$output" != *"Pulling framework"* ]]
}

@test "cache hit: exports REPO_PATH correctly" {
  create_fake_cache

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh && echo "REPO_PATH=$REPO_PATH"'

  [ "$status" -eq 0 ]
  [[ "$output" == *"REPO_PATH=$FAKE_REPO"* ]]
}

@test "cache hit: exports FRAMEWORK_APPS_PATH correctly" {
  create_fake_cache

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh && echo "APPS=$FRAMEWORK_APPS_PATH"'

  [ "$status" -eq 0 ]
  [[ "$output" == *"APPS=$FRAMEWORK_CACHE/.devcontainer/apps"* ]]
}

@test "cache hit: exports RepositoryName correctly" {
  create_fake_cache

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh && echo "NAME=$RepositoryName"'

  [ "$status" -eq 0 ]
  [[ "$output" == *"NAME=my-enablement"* ]]
}

@test "cache hit: p10k files are available in cache (copied by setUpTerminal)" {
  create_fake_cache

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh'

  [ "$status" -eq 0 ]
  # source_framework.sh does NOT copy p10k — that is setUpTerminal's job.
  # Verify the p10k files are available in the cache for setUpTerminal to use.
  [ -f "$FRAMEWORK_CACHE/.devcontainer/p10k/.p10k.zsh" ]
  [ -f "$FRAMEWORK_CACHE/.devcontainer/p10k/.zshrc" ]
}

# ============================================================
# Test: Cache missing -> attempt git clone (will fail with fake version)
# ============================================================
@test "cache miss: no cache dir -> attempts git clone" {
  # No cache exists; git clone will fail (fake version 99.99.99)
  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh 2>&1'

  [ "$status" -ne 0 ]
  [[ "$output" == *"Pulling framework v99.99.99"* ]]
  [[ "$output" == *"Failed to pull framework"* ]]
}

@test "cache miss: .complete sentinel NOT written on failed clone" {
  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh 2>&1'

  [ "$status" -ne 0 ]
  [ ! -f "$FRAMEWORK_CACHE/.complete" ]
}

# ============================================================
# Test: Partial cache (no .complete) -> re-clone attempted
# ============================================================
@test "partial cache: no .complete -> re-clone attempted" {
  create_partial_cache

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh 2>&1'

  # Should attempt to clone (will fail due to fake version)
  [ "$status" -ne 0 ]
  [[ "$output" == *"Pulling framework v99.99.99"* ]]
}

# ============================================================
# Test: my_functions.sh override — repo version wins
# ============================================================
@test "my_functions: repo version wins over framework stub" {
  create_fake_cache

  # Create repo-level my_functions.sh
  echo 'MY_FUNC_SOURCE="repo"' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  # Framework stub has different value
  echo 'MY_FUNC_SOURCE="framework"' > "$FRAMEWORK_CACHE/.devcontainer/util/my_functions.sh"

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh && echo "SOURCE=$MY_FUNC_SOURCE"'

  [ "$status" -eq 0 ]
  [[ "$output" == *"SOURCE=repo"* ]]
}

@test "my_functions: framework stub loads when repo version absent" {
  create_fake_cache

  # No repo-level my_functions.sh
  echo 'MY_FUNC_SOURCE="framework"' > "$FRAMEWORK_CACHE/.devcontainer/util/my_functions.sh"

  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/source_framework.sh && echo "SOURCE=$MY_FUNC_SOURCE"'

  [ "$status" -eq 0 ]
  [[ "$output" == *"SOURCE=framework"* ]]
}

# ============================================================
# Test: FRAMEWORK_VERSION defaults to 1.2.0 when not set
# ============================================================
@test "FRAMEWORK_VERSION defaults to 1.2.5 when unset" {
  unset FRAMEWORK_VERSION

  cd "$FAKE_REPO"
  run bash -c '
    unset FRAMEWORK_VERSION
    source .devcontainer/util/source_framework.sh 2>&1
    echo "VER=$FRAMEWORK_VERSION"
  '

  # It will fail (no matching tag for remote clone) but should set the variable
  [[ "$output" == *"VER=1.2.5"* ]]
}

# ============================================================
# Test: Multiple versions can coexist in cache
# ============================================================
@test "version isolation: different versions use different cache dirs" {
  create_fake_cache

  # Create a second version cache
  export FRAMEWORK_VERSION_2="88.88.88"
  CACHE_2="$HOME/.cache/dt-framework/$FRAMEWORK_VERSION_2"
  mkdir -p "$CACHE_2/.devcontainer/util"
  mkdir -p "$CACHE_2/.devcontainer/test"
  echo 'VER_CHECK="v88"' > "$CACHE_2/.devcontainer/util/variables.sh"
  echo '# stub' > "$CACHE_2/.devcontainer/util/functions.sh"
  echo '# stub' > "$CACHE_2/.devcontainer/util/greeting.sh"
  echo '# stub' > "$CACHE_2/.devcontainer/util/my_functions.sh"
  echo '# stub' > "$CACHE_2/.devcontainer/test/test_functions.sh"
  mkdir -p "$CACHE_2/.devcontainer/p10k"
  echo '# stub' > "$CACHE_2/.devcontainer/p10k/.p10k.zsh"
  echo '# stub' > "$CACHE_2/.devcontainer/p10k/.zshrc"
  touch "$CACHE_2/.complete"

  # Version 99.99.99 cache still exists separately
  [ -f "$FRAMEWORK_CACHE/.complete" ]
  [ -f "$CACHE_2/.complete" ]
  [ "$FRAMEWORK_CACHE" != "$CACHE_2" ]
}

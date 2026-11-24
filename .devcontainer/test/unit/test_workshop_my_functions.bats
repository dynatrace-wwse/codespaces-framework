#!/usr/bin/env bats
# ENH-186: a training's shared infrastructure owns .devcontainer/util/workshop-my-functions.sh.
# functions.sh sources it ONLY when Orbital marked the box as the workshop shared machine
# (ORBITAL_SHARED_ROLE=shared, written by orbital shared/shared_seat.py env_lines), after the
# repo's my_functions.sh. A learner/trainer seat or a Codespace never loads it.
#
# Each case sources the REAL functions.sh in a fresh bash, so the wiring is what is tested,
# not a copy of the condition.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"; mkdir -p "$HOME"
  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/variables.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"
  cat > "$FAKE_REPO/.devcontainer/util/my_functions.sh" <<'MF'
LOADED="${LOADED:-} my"
whoOwns() { echo repo; }
MF
  cat > "$FAKE_REPO/.devcontainer/util/workshop-my-functions.sh" <<'WMF'
LOADED="${LOADED:-} workshop"
whoOwns() { echo shared; }
sharedOnly() { echo yes; }
WMF
}

teardown() {
  rm -rf "$TEST_DIR"
}

# Source functions.sh in a clean bash with only the given role, print what loaded.
_load() {
  local role="$1"
  env -i HOME="$HOME" PATH="$PATH" REPO_PATH="$FAKE_REPO" RepositoryName=test-enablement \
    ARCH=x86_64 FRAMEWORK_CACHE="" ${role:+ORBITAL_SHARED_ROLE="$role"} \
    bash -c 'cd "$REPO_PATH" && source .devcontainer/util/functions.sh >/dev/null 2>&1
             echo "loaded:${LOADED}|owner:$(whoOwns)|shared:$(type -t sharedOnly || echo none)"'
}

@test "shared machine (ORBITAL_SHARED_ROLE=shared): workshop-my-functions.sh is sourced after my_functions.sh" {
  run _load shared
  [ "$status" -eq 0 ]
  [ "$output" = "loaded: my workshop|owner:shared|shared:function" ]
}

@test "seat or Codespace (role unset): workshop-my-functions.sh is NOT sourced" {
  run _load ""
  [ "$status" -eq 0 ]
  [ "$output" = "loaded: my|owner:repo|shared:none" ]
}

@test "malformed role values never load it: learner, true, 1, SHARED, 'shared '" {
  for role in learner true 1 SHARED "shared "; do
    run _load "$role"
    [ "$status" -eq 0 ]
    [ "$output" = "loaded: my|owner:repo|shared:none" ] || { echo "role=[$role] -> $output"; false; }
  done
}

@test "shared machine without the file: sourcing functions.sh still succeeds" {
  rm "$FAKE_REPO/.devcontainer/util/workshop-my-functions.sh"
  run _load shared
  [ "$status" -eq 0 ]
  [ "$output" = "loaded: my|owner:repo|shared:none" ]
}

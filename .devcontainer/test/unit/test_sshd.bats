#!/usr/bin/env bats
# Tests for the sshd helper folded into the framework (functions.sh).
# sshd installs/readies an OpenSSH server so `gh codespace ssh` (the Enablement
# App in-app terminal relay) can attach. These tests guard that the function is
# framework-provided and always lays down host keys + the privilege-separation
# dir, without performing a real apt install (sudo is mocked to a log).

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  mkdir -p "$FAKE_REPO/.devcontainer/test"
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"

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
VARSEOF

  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" \
     "$FAKE_REPO/.devcontainer/util/functions.sh"

  # Mock sudo so no real apt-get / ssh-keygen / mkdir runs; record the args.
  export SUDO_LOG="$TEST_DIR/sudo.log"
  sudo() { echo "$*" >> "$SUDO_LOG"; return 0; }
  export -f sudo
  apt-get() { return 0; }
  export -f apt-get
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
}

@test "sshd: function is defined by the framework" {
  source_functions
  run declare -F sshd
  [ "$status" -eq 0 ]
}

@test "sshd: runs cleanly with no command-not-found" {
  source_functions
  run sshd
  [ "$status" -eq 0 ]
  [[ "$output" == *"Enabling OpenSSH server"* ]]
  [[ "$output" != *"command not found"* ]]
}

@test "sshd: always generates host keys and the privilege-separation dir" {
  source_functions
  run sshd
  [ "$status" -eq 0 ]
  grep -q "ssh-keygen -A" "$SUDO_LOG"
  grep -q "mkdir -p /run/sshd" "$SUDO_LOG"
}

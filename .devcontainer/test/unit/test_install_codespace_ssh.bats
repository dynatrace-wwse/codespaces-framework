#!/usr/bin/env bats
# Tests for installCodespaceSSH (functions.sh).
# installCodespaceSSH idempotently installs/readies an OpenSSH server so the
# Enablement App can relay a terminal into an Orbital-orchestrated Codespace via
# `gh codespace ssh`. setUpTerminal calls it only when
# INSTANTIATION_TYPE == "orbital_codespaces". These tests guard that the function
# is framework-provided and performs a privileged install/setup step, without
# running a real apt-get (sudo is mocked to a log).

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
  : > "$SUDO_LOG"
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

@test "installCodespaceSSH: function is defined by the framework" {
  source_functions
  run declare -F installCodespaceSSH
  [ "$status" -eq 0 ]
}

@test "installCodespaceSSH: runs cleanly with no command-not-found" {
  source_functions
  run installCodespaceSSH
  [ "$status" -eq 0 ]
  [[ "$output" == *"Installing SSH server for Orbital Codespaces relay"* ]]
  [[ "$output" != *"command not found"* ]]
}

@test "installCodespaceSSH: performs a privileged install/setup step" {
  # Host-agnostic: when /usr/sbin/sshd is absent it apt-get installs; when present
  # it generates host keys. Either path issues a sudo command we can observe.
  source_functions
  run installCodespaceSSH
  [ "$status" -eq 0 ]
  grep -qE "apt-get|ssh-keygen" "$SUDO_LOG"
}

@test "installCodespaceSSH: setUpTerminal wires it only for orbital_codespaces" {
  # Guard the gating contract without executing setUpTerminal: the call site must
  # sit inside an INSTANTIATION_TYPE == "orbital_codespaces" check.
  run grep -B2 "    installCodespaceSSH" "$FAKE_REPO/.devcontainer/util/functions.sh"
  [[ "$output" == *'INSTANTIATION_TYPE'* ]]
  [[ "$output" == *'orbital_codespaces'* ]]
}

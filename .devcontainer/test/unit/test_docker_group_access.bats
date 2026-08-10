#!/usr/bin/env bats
# Tests for ensureDockerGroupAccess / _dockerAccessVerdict (functions.sh).
#
# Codespaces runs postCreateCommand as its own `docker exec`, fired ~4s after the
# container starts — while /entrypoint.sh is still running `groupmod -g <sock gid>
# docker` + `usermod -aG docker $USER` (measured 3-5s). dockerd freezes an exec's
# supplementary groups at creation, so when the CLI wins that race post-create never
# holds the docker GID and every docker call in it gets EACCES, while shells opened
# afterwards work fine. `make start` and the VS Code dev container run post-create as
# the container's CMD, which the entrypoint's own `exec sg docker` already covers.
#
# The decision lives in the side-effect-free _dockerAccessVerdict so every branch —
# including the re-exec — is testable without replacing the process.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test"
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export GITHUB_REPOSITORY="dynatrace-wwse/test-enablement"

  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN=""; BLUE=""; CYAN=""; YELLOW=""; ORANGE=""; RED=""; LILA=""; NORMAL=""; RESET=""
thickline=""; halfline=""; thinline=""
VARSEOF
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  # Socket stand-in, so no test depends on the host having /var/run/docker.sock.
  export DOCKER_SOCKET="$TEST_DIR/docker.sock"
  python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])" "$DOCKER_SOCKET"

  export SCRIPT="$FAKE_REPO/post-create.sh"
  echo '#!/bin/bash' > "$SCRIPT"

  sleep() { :; }          # never wait out the 30s window
  export -f sleep
  unset DT_DOCKER_REGROUP
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
}

stub_docker() {                      # 1 = daemon answers, 0 = EACCES
  export DOCKER_OK="${1:-0}"
  docker() { [ "$DOCKER_OK" = "1" ] && return 0; return 1; }
  export -f docker
}

stub_id() {                          # 1 = group DB lists vscode in docker
  export IN_GROUP="${1:-1}"
  id() {
    case "$*" in
      "-un")       echo "vscode" ;;
      "-nG vscode") [ "$IN_GROUP" = "1" ] && echo "vscode docker" || echo "vscode" ;;
      *)           echo "vscode" ;;
    esac
  }
  export -f id
}

@test "verdict ok: docker answers (plain docker, dev container, Orbital, healthy Codespace)" {
  stub_docker 1; stub_id 1
  source_functions
  [ "$(_dockerAccessVerdict "$SCRIPT")" = "ok" ]
}

@test "verdict no-socket: nothing mounted at the socket path" {
  stub_docker 0; stub_id 1
  export DOCKER_SOCKET="$TEST_DIR/definitely-not-here.sock"
  source_functions
  [ "$(_dockerAccessVerdict "$SCRIPT")" = "no-socket" ]
}

@test "verdict no-membership: EACCES and the group DB does not list the user yet" {
  stub_docker 0; stub_id 0
  source_functions
  [ "$(_dockerAccessVerdict "$SCRIPT")" = "no-membership" ]
}

@test "verdict regroup: EACCES but the group DB already lists the user — the race" {
  stub_docker 0; stub_id 1
  source_functions
  [ "$(_dockerAccessVerdict "$SCRIPT")" = "regroup" ]
}

@test "verdict already-regrouped: never re-exec twice" {
  stub_docker 0; stub_id 1
  export DT_DOCKER_REGROUP=1
  source_functions
  [ "$(_dockerAccessVerdict "$SCRIPT")" = "already-regrouped" ]
}

@test "verdict not-a-script: sourced into a shell, re-exec would kill the terminal" {
  stub_docker 0; stub_id 1
  source_functions
  [ "$(_dockerAccessVerdict "/not/a/real/script")" = "not-a-script" ]
}

@test "ensureDockerGroupAccess returns 0 and stays silent when docker is healthy" {
  stub_docker 1; stub_id 1
  source_functions
  run ensureDockerGroupAccess "$SCRIPT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "ensureDockerGroupAccess returns 0 and stays silent when there is no socket" {
  stub_docker 0; stub_id 1
  export DOCKER_SOCKET="$TEST_DIR/definitely-not-here.sock"
  source_functions
  run ensureDockerGroupAccess "$SCRIPT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "ensureDockerGroupAccess fails open when the membership never appears" {
  stub_docker 0; stub_id 0
  source_functions
  run ensureDockerGroupAccess "$SCRIPT"
  [ "$status" -eq 1 ]
  [[ "$output" == *"still not a member of the docker group"* ]]
}

@test "ensureDockerGroupAccess returns 0 if docker recovers while waiting" {
  # Membership missing on the first look, docker healthy on the next.
  stub_id 0
  export FLAG="$TEST_DIR/flag"
  docker() { [ -f "$FLAG" ] && return 0; touch "$FLAG"; return 1; }
  export -f docker
  source_functions
  run ensureDockerGroupAccess "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Docker became reachable"* ]]
}

@test "the regroup branch re-execs via sg, and only after checking sg exists" {
  # Asserted on the source: executing it would replace the bats process.
  run grep -A4 "^    regroup)" "$FAKE_REPO/.devcontainer/util/functions.sh"
  [[ "$output" == *"exec sg docker"* ]]
  run grep -n "command -v sg" "$FAKE_REPO/.devcontainer/util/functions.sh"
  [ "$status" -eq 0 ]
}

@test "setUpTerminal calls it before anything else" {
  run grep -A3 "^setUpTerminal(){" "$FAKE_REPO/.devcontainer/util/functions.sh"
  [[ "$output" == *"ensureDockerGroupAccess"* ]]
}

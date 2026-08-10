#!/usr/bin/env bats
# Tests for INSTANTIATION_TYPE detection (variables.sh).
#
# The tricky case is telling an Orbital-orchestrated Codespace apart from one the
# learner opened by hand on the same repo. ORBITAL_ENVIRONMENT is a repo-scoped
# USER Codespaces secret, so it survives the session that set it and cannot, on its
# own, answer that question. variables.sh therefore treats it as a hint and
# confirms it against Orbital's GET /api/codespace/orbital/{name}.
#
# curl is stubbed throughout — no test here touches the network.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"

  export VARS="$BATS_TEST_DIRNAME/../../util/variables.sh"

  # Pre-set so variables.sh skips its `git remote get-url origin` probe: the fake
  # repo is not a git checkout, and bats runs the body under `set -e`.
  export GITHUB_REPOSITORY="dynatrace-wwse/test-enablement"

  # Point the confirmation at nothing real; the stub below answers instead.
  export ORBITAL_BASE_URL="http://orbital.invalid"
  export CURL_LOG="$TEST_DIR/curl.log"
  : > "$CURL_LOG"

  # Neutral starting env — bats inherits the developer's shell.
  unset CODESPACES ORBITAL_ENVIRONMENT CODESPACE_NAME REMOTE_CONTAINERS \
        GITHUB_WORKFLOW GITHUB_STEP_SUMMARY INSTANTIATION_TYPE
}

teardown() {
  rm -rf "$TEST_DIR"
}

# variables.sh probes the cluster with `docker inspect` / `kubectl cluster-info`
# in plain command substitutions. Those pre-date this file and are not strict-mode
# safe, and bats runs every test body under `set -e` — so source with errexit off.
# The detection logic under test is asserted on the exported result, not on $?.
source_vars() {
  set +e
  source "$VARS"
  set -e
  return 0
}

# Stub curl: records the URL it was asked for, prints $CURL_BODY, exits $CURL_RC.
stub_curl() {
  export CURL_BODY="${1-}"
  export CURL_RC="${2-0}"
  curl() {
    echo "$*" >> "$CURL_LOG"
    [ -n "$CURL_BODY" ] && printf '%s' "$CURL_BODY"
    return "$CURL_RC"
  }
  export -f curl
}

@test "plain Codespace (no marker) is github-codespaces and never calls Orbital" {
  stub_curl '{"orbital":false}' 0
  export CODESPACES=true CODESPACE_NAME="fluffy-space-guacamole"
  source_vars
  [ "$INSTANTIATION_TYPE" = "github-codespaces" ]
  [ ! -s "$CURL_LOG" ]
}

@test "Orbital Codespace: marker confirmed → orbital_codespaces" {
  stub_curl '{"orbital":true}' 0
  export CODESPACES=true ORBITAL_ENVIRONMENT=true CODESPACE_NAME="fluffy-space-guacamole"
  source_vars
  [ "$INSTANTIATION_TYPE" = "orbital_codespaces" ]
  grep -q "fluffy-space-guacamole" "$CURL_LOG"
}

@test "stale marker: Orbital says not mine → downgraded to github-codespaces" {
  # The regression this whole change exists for: a hand-opened Codespace on a repo
  # that Orbital used once before must not claim to be Orbital-orchestrated.
  stub_curl '{"orbital":false}' 0
  export CODESPACES=true ORBITAL_ENVIRONMENT=true CODESPACE_NAME="stale-marker-codespace"
  source_vars
  [ "$INSTANTIATION_TYPE" = "github-codespaces" ]
}

@test "ops server unreachable → keeps orbital_codespaces (fail-safe direction)" {
  # Wrongly downgrading a real Orbital session breaks the in-app terminal for a
  # whole training; wrongly keeping the marker costs one small apt-get.
  stub_curl '' 7
  export CODESPACES=true ORBITAL_ENVIRONMENT=true CODESPACE_NAME="unreachable-codespace"
  source_vars
  [ "$INSTANTIATION_TYPE" = "orbital_codespaces" ]
}

@test "verdict is cached per Codespace — variables.sh is sourced on every terminal open" {
  stub_curl '{"orbital":false}' 0
  export CODESPACES=true ORBITAL_ENVIRONMENT=true CODESPACE_NAME="cached-codespace"
  source_vars
  [ "$INSTANTIATION_TYPE" = "github-codespaces" ]
  [ "$(wc -l < "$CURL_LOG")" -eq 1 ]

  unset INSTANTIATION_TYPE
  source_vars
  [ "$INSTANTIATION_TYPE" = "github-codespaces" ]
  [ "$(wc -l < "$CURL_LOG")" -eq 1 ]   # answered from cache, no second 5s call
}

@test "Sysbox/Orbital job (marker, not a Codespace) stays orbital and skips the probe" {
  stub_curl '{"orbital":false}' 0
  export ORBITAL_ENVIRONMENT=true
  source_vars
  [ "$INSTANTIATION_TYPE" = "orbital" ]
  [ ! -s "$CURL_LOG" ]
}

@test "local container: no marker, no Codespaces" {
  stub_curl '{"orbital":false}' 0
  source_vars
  [ "$INSTANTIATION_TYPE" = "local-docker-container" ]
  [ ! -s "$CURL_LOG" ]
}

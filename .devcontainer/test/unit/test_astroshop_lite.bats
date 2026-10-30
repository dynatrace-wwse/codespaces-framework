#!/usr/bin/env bats
# Tests deployAstroshopLite / deployApp astroshop-lite: which OTLP endpoint the
# chart is installed with, and that the vendored chart and values exist.
#
# Nothing touches a cluster: helm and kubectl are stubs on PATH that log their
# arguments; deployOtelCollector, waitForAllReadyPods and registerApp are
# overridden after functions.sh is sourced so each test sees exactly which were
# called.
#
# Every negated assertion ends in "|| false": bats runs tests under set -e,
# which ignores a "! cmd" that fails — only "|| false" makes it count.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"
  unset ASTROSHOP_LITE_OTLP_ENDPOINT
  export REPO_PATH="$TEST_DIR/workspaces/test-repo"
  mkdir -p "$REPO_PATH/.devcontainer/util" "$REPO_PATH/.devcontainer/test"
  echo '# variables stub' > "$REPO_PATH/.devcontainer/util/variables.sh"
  echo '# test stub' > "$REPO_PATH/.devcontainer/test/test_functions.sh"
  export FRAMEWORK_APPS_PATH="$BATS_TEST_DIRNAME/../../apps"
  export OTEL_COLLECTOR_NAMESPACE="otel-collector"

  export BIN="$TEST_DIR/bin" CALLS="$TEST_DIR/calls.log"
  mkdir -p "$BIN"; : > "$CALLS"
  for t in helm kubectl; do
    printf '#!/bin/bash\necho "%s $*" >> "%s"\nexit 0\n' "$t" "$CALLS" > "$BIN/$t"
    chmod +x "$BIN/$t"
  done
  export PATH="$BIN:$PATH"
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$REPO_PATH"
  source "$BATS_TEST_DIRNAME/../../util/functions.sh" 2>/dev/null || true
  printInfoSection() { :; }; printInfo() { :; }; printError() { echo "ERR $*"; }
  deployOtelCollector() { echo "deployOtelCollector" >> "$CALLS"; return "${COLLECTOR_RC:-0}"; }
  waitForAllReadyPods() { echo "waitForAllReadyPods $*" >> "$CALLS"; }
  registerApp() { echo "registerApp $*" >> "$CALLS"; }
}

@test "astroshop-lite: vendored chart and framework values are present" {
  [ -f "$FRAMEWORK_APPS_PATH/astroshop-lite/chart/Chart.yaml" ]
  [ -f "$FRAMEWORK_APPS_PATH/astroshop-lite/values.yaml" ]
}

@test "astroshop-lite: every image in values.yaml is pinned by digest" {
  run grep -E '^\s*(image|tag):' "$FRAMEWORK_APPS_PATH/astroshop-lite/values.yaml"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 8 ]
  for l in "${lines[@]}"; do [[ "$l" == *"@sha256:"* ]] || { echo "unpinned: $l"; false; }; done
}

@test "astroshop-lite: the chart's own debug-only collector stays off" {
  grep -qE '^collector:' "$FRAMEWORK_APPS_PATH/astroshop-lite/values.yaml"
  run awk '/^collector:/{f=1;next} f&&/^[^ ]/{f=0} f&&/enabled:/{print $2}' "$FRAMEWORK_APPS_PATH/astroshop-lite/values.yaml"
  [ "$output" = "false" ]
}

@test "deployAstroshopLite: default deploys the framework collector and points the chart at it" {
  source_functions
  run deployAstroshopLite
  [ "$status" -eq 0 ]
  grep -qx "deployOtelCollector" "$CALLS"
  grep -q "helm upgrade --install astroshop-lite .*/astroshop-lite/chart --namespace astroshop-lite --create-namespace -f .*/astroshop-lite/values.yaml --set-string otlp.endpoint=http://otel-collector.otel-collector.svc.cluster.local:4317" "$CALLS"
  grep -q "registerApp astroshop-lite astroshop-lite astroshop-lite-frontend 8080" "$CALLS"
}

@test "deployAstroshopLite: ASTROSHOP_LITE_OTLP_ENDPOINT wins and no collector is deployed" {
  source_functions
  export ASTROSHOP_LITE_OTLP_ENDPOINT="http://gw.agents.svc.cluster.local:4317"
  run deployAstroshopLite
  [ "$status" -eq 0 ]
  ! grep -qx "deployOtelCollector" "$CALLS" || false
  grep -q -- "--set-string otlp.endpoint=http://gw.agents.svc.cluster.local:4317" "$CALLS"
}

@test "deployAstroshopLite: a failed collector stops the deploy before helm" {
  source_functions
  export COLLECTOR_RC=1
  run deployAstroshopLite
  [ "$status" -eq 1 ]
  [[ "$output" == *"needs a collector"* ]]
  ! grep -q "^helm " "$CALLS" || false
}

@test "deployApp: 10, j and astroshop-lite all route to deployAstroshopLite" {
  source_functions
  deployAstroshopLite() { echo "deployAstroshopLite" >> "$CALLS"; }
  for k in 10 j astroshop-lite; do
    : > "$CALLS"; deployApp "$k"
    grep -qx "deployAstroshopLite" "$CALLS"
  done
}

@test "deployApp astroshop-lite -d undeploys" {
  source_functions
  undeployAstroshopLite() { echo "undeployAstroshopLite" >> "$CALLS"; }
  deployApp astroshop-lite -d
  grep -qx "undeployAstroshopLite" "$CALLS"
}

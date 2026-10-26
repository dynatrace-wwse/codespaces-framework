#!/usr/bin/env bats
# BUG-158: when waitForAllReadyPods gives up, its output must name the pods it
# gave up on -- including a pod that is Running but never Ready (a readiness
# probe that never passes). It printed `kubectl get pods
# --field-selector=status.phase!=Running`, which hides exactly that pod, so a
# staging seat failed with "No resources found" and no way to tell which pod.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"; mkdir -p "$HOME"
  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test"
  export REPO_PATH="$FAKE_REPO" RepositoryName="test-enablement"
  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN=""; BLUE=""; CYAN=""; YELLOW=""; ORANGE=""; RED=""; LILA=""; NORMAL=""; RESET=""
thickline=""; halfline=""; thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"; export ENV_FILE
COUNT_FILE="$REPO_PATH/.devcontainer/util/.count"; export COUNT_FILE
INSTANTIATION_TYPE="local-docker-container"
VARSEOF
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  # A cluster where the operator is fine and the ActiveGate is Running but
  # never Ready. The field selector behaves as the real kubectl does: a
  # Running pod is filtered out whatever its readiness.
  kubectl() {
    case "$*" in
      *"--field-selector=status.phase!=Running"*) echo "No resources found" ;;
      "describe pod -n dynatrace ag-activegate-0")
        printf '    State:          Running\n    Ready:          False\n    Readiness:      http-get https://:https/rest/health delay=90s\nEvents:\n  Warning  Unhealthy  1m  kubelet  Readiness probe failed: HTTP probe failed with statuscode: 503\n' ;;
      "get pods -n dynatrace"*|"get pods -n dynatrace -o wide"*)
        printf 'NAME                       READY   STATUS    RESTARTS   AGE\ndynatrace-operator-abc     1/1     Running   0          5m\nag-activegate-0            0/1     Running   0          5m\n' ;;
      *) return 0 ;;
    esac
  }
  sleep() { :; }
  export -f kubectl sleep
}

teardown() { rm -rf "$TEST_DIR"; }

@test "waitForAllReadyPods: a Running-but-not-Ready pod is named when the wait gives up" {
  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/functions.sh >/dev/null 2>&1; waitForAllReadyPods dynatrace'
  [ "$status" -eq 1 ]
  [[ "$output" == *"ag-activegate-0"* ]]
  [[ "$output" == *"Readiness probe failed"* ]]
}

@test "waitForAllReadyPods: the failure runs the same diagnostics as waitForAllPods" {
  cd "$FAKE_REPO"
  run bash -c 'source .devcontainer/util/functions.sh >/dev/null 2>&1; waitForAllReadyPods dynatrace'
  [ "$status" -eq 1 ]
  [[ "$output" == *"Pod diagnostics"* ]]
}

@test "waitForAllReadyPods: all pods Ready returns 0 and dumps nothing" {
  cd "$FAKE_REPO"
  kubectl() { printf 'NAME   READY   STATUS\nop     1/1     Running\n'; }
  export -f kubectl
  run bash -c 'source .devcontainer/util/functions.sh >/dev/null 2>&1; waitForAllReadyPods dynatrace'
  [ "$status" -eq 0 ]
  [[ "$output" != *"Pod diagnostics"* ]]
}

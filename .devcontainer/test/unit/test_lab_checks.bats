#!/usr/bin/env bats
# Tests for the lab step checks and solution helpers in functions.sh
# (checkNodeReady … generateTodoTraffic). kubectl, curl and sleep are mocked:
# each mock reads its canned answer from a file under $TEST_DIR, and records
# its arguments, so a test pins both what the check saw and what it asked.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test"
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export APP_REGISTRY="$TEST_DIR/app-registry"
  export FRAMEWORK_CACHE=""
  export EXTERNAL_HOSTNAME="labhost"
  unset LAB_WAIT TODO_TRAFFIC_WAIT K3D_LB_HTTP_PORT

  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
GREEN=""; BLUE=""; CYAN=""; YELLOW=""; ORANGE=""; RED=""; LILA=""; NORMAL=""; RESET=""
thickline=""; halfline=""; thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"
INSTANTIATION_TYPE="local-docker-container"
VARSEOF
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  # Canned kubectl answers, one file per query.
  : > "$TEST_DIR/nodes"; : > "$TEST_DIR/pods"; : > "$TEST_DIR/dynakube"; : > "$TEST_DIR/inject"
  echo 0 > "$TEST_DIR/rollout_rc"
  : > "$TEST_DIR/kubectl.log"; : > "$TEST_DIR/curl.log"; : > "$TEST_DIR/sleep.log"
  kubectl() {
    echo "$*" >> "$TEST_DIR/kubectl.log"
    case "$*" in
      "get nodes"*)          cat "$TEST_DIR/nodes" ;;
      "get dynakube"*)       cat "$TEST_DIR/dynakube" ;;
      "get pods"*jsonpath*)  cat "$TEST_DIR/inject" ;;
      "get pods"*)           cat "$TEST_DIR/pods" ;;
      "rollout"*)            return "$(cat "$TEST_DIR/rollout_rc")" ;;
    esac
  }
  # curl: GET probes fail until the Nth call ($TEST_DIR/curl_ok_after, 0 = never).
  echo 1 > "$TEST_DIR/curl_ok_after"; echo 0 > "$TEST_DIR/curl_n"
  curl() {
    echo "$*" >> "$TEST_DIR/curl.log"
    case "$*" in
      *"-X POST"*) echo '{"status":"ok"}'; return 0 ;;
    esac
    local n; n=$(( $(cat "$TEST_DIR/curl_n") + 1 )); echo "$n" > "$TEST_DIR/curl_n"
    local ok; ok=$(cat "$TEST_DIR/curl_ok_after")
    [ "$ok" -ne 0 ] && [ "$n" -ge "$ok" ]
  }
  sleep() { echo "$*" >> "$TEST_DIR/sleep.log"; }
  export -f kubectl curl sleep
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
}

# ============================================================
# Every check: a single probe passes or fails at once
# ============================================================

@test "checks: all nine helpers are defined by functions.sh" {
  source_functions
  for f in checkNodeReady checkOperatorReady checkDynakube checkActiveGateReady \
           checkOneAgentInjected checkLogModuleReady checkTodoAppRunning \
           restartTodoApp generateTodoTraffic; do
    declare -F "$f" >/dev/null || { echo "missing: $f"; return 1; }
  done
}

@test "checkNodeReady: Ready node passes, NotReady fails" {
  source_functions
  echo "k3d-server-0   Ready    control-plane   1m   v1.30" > "$TEST_DIR/nodes"
  run checkNodeReady
  [ "$status" -eq 0 ]; [[ "$output" == *"Cluster node is Ready"* ]]
  echo "k3d-server-0   NotReady control-plane   1m   v1.30" > "$TEST_DIR/nodes"
  run checkNodeReady
  [ "$status" -eq 1 ]; [[ "$output" == *"not Ready yet"* ]]
}

@test "checkOperatorReady: Running operator passes, absent fails with a generic hint" {
  source_functions
  echo "dynatrace-operator-abc   1/1   Running   0   1m" > "$TEST_DIR/pods"
  run checkOperatorReady
  [ "$status" -eq 0 ]
  : > "$TEST_DIR/pods"
  run checkOperatorReady
  [ "$status" -eq 1 ]
  [[ "$output" == *"install the operator, then check again"* ]]
  [[ "$output" != *"Helm"* ]]
}

@test "checkDynakube: present passes, absent fails" {
  source_functions
  echo "my-dynakube   https://x/api   Running   1m" > "$TEST_DIR/dynakube"
  run checkDynakube
  [ "$status" -eq 0 ]
  : > "$TEST_DIR/dynakube"
  run checkDynakube
  [ "$status" -eq 1 ]; [[ "$output" == *"No DynaKube found"* ]]
}

@test "checkActiveGateReady: Running ActiveGate passes, Pending fails" {
  source_functions
  echo "my-dynakube-activegate-0   1/1   Running   0   1m" > "$TEST_DIR/pods"
  run checkActiveGateReady
  [ "$status" -eq 0 ]
  echo "my-dynakube-activegate-0   0/1   Pending   0   1m" > "$TEST_DIR/pods"
  run checkActiveGateReady
  [ "$status" -eq 1 ]
}

@test "checkLogModuleReady: Running logmonitoring passes, absent fails" {
  source_functions
  echo "my-dynakube-logmonitoring-x1   1/1   Running   0   1m" > "$TEST_DIR/pods"
  run checkLogModuleReady
  [ "$status" -eq 0 ]
  : > "$TEST_DIR/pods"
  run checkLogModuleReady
  [ "$status" -eq 1 ]
}

@test "checkTodoAppRunning: Running pod passes, ContainerCreating fails" {
  source_functions
  echo "todoapp-1   1/1   Running   0   1m" > "$TEST_DIR/pods"
  run checkTodoAppRunning
  [ "$status" -eq 0 ]
  echo "todoapp-1   0/1   ContainerCreating   0   1m" > "$TEST_DIR/pods"
  run checkTodoAppRunning
  [ "$status" -eq 1 ]
}

@test "checkOneAgentInjected: defaults to todoapp, takes a namespace" {
  source_functions
  echo "true" > "$TEST_DIR/inject"
  run checkOneAgentInjected
  [ "$status" -eq 0 ]; [[ "$output" == *"todoapp pods"* ]]
  grep -q -- "-n todoapp" "$TEST_DIR/kubectl.log"
  run checkOneAgentInjected astroshop
  [ "$status" -eq 0 ]; [[ "$output" == *"astroshop pods"* ]]
  grep -q -- "-n astroshop" "$TEST_DIR/kubectl.log"
  : > "$TEST_DIR/inject"
  run checkOneAgentInjected astroshop
  [ "$status" -eq 1 ]; [[ "$output" == *"not injected into the astroshop pods"* ]]
}

# ============================================================
# Click vs LAB_WAIT
# ============================================================

@test "learner click probes once: no waiting without LAB_WAIT" {
  source_functions
  run checkDynakube
  [ "$status" -eq 1 ]
  [ ! -s "$TEST_DIR/sleep.log" ]
  [ "$(grep -c 'get dynakube' "$TEST_DIR/kubectl.log")" -eq 1 ]
}

@test "LAB_WAIT=1 retries until the state appears (bounded)" {
  source_functions
  LAB_WAIT=1 run checkDynakube
  [ "$status" -eq 1 ]
  [ "$(wc -l < "$TEST_DIR/sleep.log")" -eq 30 ]
}

@test "waitFor* wrappers turn LAB_WAIT on and forward arguments" {
  source_functions
  run waitForOneAgentInjected astroshop
  [ "$status" -eq 1 ]
  [ "$(wc -l < "$TEST_DIR/sleep.log")" -eq 24 ]
  grep -q -- "-n astroshop" "$TEST_DIR/kubectl.log"
}

@test "LAB_WAIT wait timing out does not kill the calling shell" {
  # waitForPod exits 1 after 60 tries; the check must still return its own
  # verdict and the caller must keep running.
  source_functions
  run bash -c 'cd "$FAKE_REPO"; source .devcontainer/util/functions.sh; LAB_WAIT=1 checkOperatorReady; echo "rc=$? shell-alive"'
  [[ "$output" == *"rc=1 shell-alive"* ]]
}

# ============================================================
# Solution helpers
# ============================================================

@test "restartTodoApp: restarts then waits for the rollout" {
  source_functions
  run restartTodoApp
  [ "$status" -eq 0 ]
  [ "$(sed -n 1p "$TEST_DIR/kubectl.log")" = "rollout restart deployment -n todoapp" ]
  [[ "$(sed -n 2p "$TEST_DIR/kubectl.log")" == "rollout status deployment -n todoapp"* ]]
  echo 1 > "$TEST_DIR/rollout_rc"
  run restartTodoApp
  [ "$status" -eq 1 ]
}

@test "generateTodoTraffic: waits for the endpoint after a restart (F-3 race)" {
  source_functions
  echo 4 > "$TEST_DIR/curl_ok_after"     # endpoint answers on the 4th probe
  run generateTodoTraffic
  [ "$status" -eq 0 ]
  [[ "$output" == *"Created TODO"* ]]
  [ "$(wc -l < "$TEST_DIR/sleep.log")" -eq 3 ]
  grep -q -- "-X POST" "$TEST_DIR/curl.log"
  grep -q "Host: todoapp.labhost" "$TEST_DIR/curl.log"
}

@test "generateTodoTraffic: endpoint never answers -> bounded, clear failure, no POST" {
  source_functions
  echo 0 > "$TEST_DIR/curl_ok_after"
  run generateTodoTraffic
  [ "$status" -eq 1 ]
  [[ "$output" == *"did not answer within 60s"* ]]
  [ "$(wc -l < "$TEST_DIR/sleep.log")" -eq 12 ]
  ! grep -q -- "-X POST" "$TEST_DIR/curl.log"
}

@test "generateTodoTraffic: LAB_WAIT waits longer, TODO_TRAFFIC_WAIT overrides" {
  source_functions
  echo 0 > "$TEST_DIR/curl_ok_after"
  LAB_WAIT=1 run generateTodoTraffic
  [[ "$output" == *"within 150s"* ]]
  : > "$TEST_DIR/sleep.log"
  TODO_TRAFFIC_WAIT=10 run generateTodoTraffic
  [[ "$output" == *"within 10s"* ]]
  [ "$(wc -l < "$TEST_DIR/sleep.log")" -eq 2 ]
}

@test "generateTodoTraffic: title argument is sent" {
  source_functions
  run generateTodoTraffic "Kubernetes 101"
  [ "$status" -eq 0 ]
  grep -q '"title":"Kubernetes 101"' "$TEST_DIR/curl.log"
}

# ============================================================
# Repo override
# ============================================================

@test "a repo's my_functions.sh copy still overrides the framework helper" {
  cat > "$FAKE_REPO/.devcontainer/util/my_functions.sh" <<'MYEOF'
checkNodeReady() { echo "repo copy"; return 0; }
MYEOF
  source_functions
  run checkNodeReady
  [ "$output" = "repo copy" ]
}

#!/usr/bin/env bats
# BUG-181 (ENH-146 A6): a re-apply with NEW tokens must move every token consumer onto them.
# Covers: dynakubeTokenFingerprint, refreshDynatraceTokenConsumers, and the hook in deployDynatrace.
# kubectl is a stateful fake: "apply" swaps the live Secret for the manifest's.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"; mkdir -p "$HOME"
  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test" "$FAKE_REPO/.devcontainer/yaml/gen"
  export REPO_PATH="$FAKE_REPO" RepositoryName="test-enablement" ARCH="x86_64"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env" APP_REGISTRY="$TEST_DIR/app-registry" FRAMEWORK_CACHE=""
  export DT_TENANT="https://abc123.live.dynatrace.com"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/variables.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../yaml/dynakube-defaults.yaml" "$FAKE_REPO/.devcontainer/yaml/"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  # Live state the fake kubectl reads and writes. Token values are recognisable so a leak shows.
  export LIVE_SECRET="$TEST_DIR/live-secret" NEXT_SECRET="$TEST_DIR/next-secret" CALLS="$TEST_DIR/calls"
  : > "$CALLS"
  cat > "$TEST_DIR/workloads.json" <<'EOF'
{"items":[
 {"kind":"StatefulSet","metadata":{"namespace":"dynatrace","name":"dk-otel-collector"},"spec":{"template":{"spec":{"containers":[{"env":[{"name":"T","valueFrom":{"secretKeyRef":{"name":"dk","key":"dataIngestToken"}}}]}]}}}},
 {"kind":"Deployment","metadata":{"namespace":"app","name":"envfrom"},"spec":{"template":{"spec":{"initContainers":[{"envFrom":[{"secretRef":{"name":"dk"}}]}],"containers":[{}]}}}},
 {"kind":"DaemonSet","metadata":{"namespace":"app","name":"vol"},"spec":{"template":{"spec":{"containers":[{}],"volumes":[{"secret":{"secretName":"dk"}}]}}}},
 {"kind":"Deployment","metadata":{"namespace":"todoapp","name":"todoapp"},"spec":{"template":{"spec":{"containers":[{"env":[{"name":"X","value":"dk"},{"name":"Y","valueFrom":{"secretKeyRef":{"name":"other","key":"k"}}}]}]}}}},
 {"kind":"Deployment","metadata":{"namespace":"dynatrace","name":"dynatrace-operator"},"spec":{"template":{"spec":{"containers":[{}]}}}}
]}
EOF
  export WORKLOADS="$TEST_DIR/workloads.json"
  kubectl() {
    local args="$*"
    case "$args" in
      *"apply -f"*) echo "apply" >> "$CALLS"; [[ -f "$NEXT_SECRET" ]] && cp "$NEXT_SECRET" "$LIVE_SECRET"; return 0 ;;
      *"get -f"*'.data}'*) [[ -f "$LIVE_SECRET" ]] && printf 'dynatrace/dk=%s\n' "$(cat "$LIVE_SECRET")"; return 0 ;;
      *"get -f"*'kind=="Secret"'*) [[ -f "$LIVE_SECRET" ]] && printf 'dk '; return 0 ;;
      *"get -f"*'kind=="DynaKube"'*) printf 'dynatrace/dk '; return 0 ;;
      *"get deployments,statefulsets,daemonsets -A -o json"*) cat "$WORKLOADS"; return 0 ;;
      *"rollout restart"*|*"annotate dynakube"*) echo "$args" >> "$CALLS"; return 0 ;;
      *"rollout status"*) echo "$args" >> "$CALLS"
        [[ -n "${STUCK:-}" && "$args" == *"$STUCK"* ]] && return 1; return 0 ;;
      *) return 0 ;;
    esac
  }
  export -f kubectl
}

teardown() { rm -rf "$TEST_DIR"; }

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
  dynatraceEvalReadSaveCredentials() { :; }
  generateDynakube() { touch "$REPO_PATH/.devcontainer/yaml/gen/dynakube.yaml"; }
  fixSprintActiveGateImage() { :; }
  fixSprintCodeModulesImage() { :; }
  waitForPod() { :; }
  waitForAllReadyPods() { :; }
  sleep() { :; }
}

restarts() { grep -c "rollout restart" "$CALLS" || true; }

@test "refresh: a changed token Secret restarts every workload that reads it, and only those" {
  source_functions
  echo '{"apiToken":"T0LDVALUE","dataIngestToken":"I0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE","dataIngestToken":"INEWVALUE"}' > "$NEXT_SECRET"

  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  grep -q -- "-n dynatrace rollout restart statefulset/dk-otel-collector" "$CALLS"
  grep -q -- "-n app rollout restart deployment/envfrom" "$CALLS"
  grep -q -- "-n app rollout restart daemonset/vol" "$CALLS"
  [ "$(restarts)" -eq 3 ]
  ! grep -q "todoapp\|dynatrace-operator" "$CALLS"
}

@test "refresh: the DynaKube is annotated so the operator reconciles now" {
  source_functions
  echo '{"apiToken":"T0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE"}' > "$NEXT_SECRET"

  run deployDynatrace apponly
  grep -q -- "-n dynatrace annotate dynakube dk dynatrace.enablement/tokens-refreshed-at=20[0-9-]*T[0-9:]*Z --overwrite" "$CALLS"
}

@test "refresh: the restarts happen AFTER the apply (they must read the new Secret)" {
  source_functions
  echo '{"apiToken":"T0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE"}' > "$NEXT_SECRET"

  run deployDynatrace apponly
  [ "$(head -1 "$CALLS")" = "apply" ]
}

@test "refresh: an unchanged Secret (an ordinary restart) restarts and annotates nothing" {
  source_functions
  echo '{"apiToken":"TSAMEVALUE"}' > "$LIVE_SECRET"
  cp "$LIVE_SECRET" "$NEXT_SECRET"

  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  [ "$(restarts)" -eq 0 ]
  ! grep -q "annotate" "$CALLS"
}

@test "refresh: a first deploy (no Secret before) restarts and annotates nothing" {
  source_functions
  rm -f "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE"}' > "$NEXT_SECRET"

  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  [ "$(restarts)" -eq 0 ]
  ! grep -q "annotate" "$CALLS"
}

@test "refresh: no token value reaches the output; the fingerprint is a bare hash" {
  source_functions
  echo '{"apiToken":"T0LDVALUE","dataIngestToken":"I0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE","dataIngestToken":"INEWVALUE"}' > "$NEXT_SECRET"

  run dynakubeTokenFingerprint "$REPO_PATH/.devcontainer/yaml/gen/dynakube.yaml"
  [[ "$output" =~ ^[0-9a-f]{64}$ ]]

  run deployDynatrace apponly
  [[ "$output" == *"Restarting statefulset/dk-otel-collector"* ]]   # the output is captured at all
  [[ "$output" != *"VALUE"* ]]
  ! grep -q "VALUE" "$CALLS"
}

@test "refresh: without jq it says the consumers keep the OLD tokens, and still annotates" {
  source_functions
  echo '{"apiToken":"T0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE"}' > "$NEXT_SECRET"
  mkdir -p "$TEST_DIR/bin"
  for t in sha256sum cut date cat cp grep head rm mkdir touch; do ln -s "$(command -v $t)" "$TEST_DIR/bin/$t"; done

  PATH="$TEST_DIR/bin" run deployDynatrace apponly
  [[ "$output" == *"jq not found"*"OLD tokens"* ]]
  [ "$(restarts)" -eq 0 ]
  grep -q "annotate dynakube dk" "$CALLS"
}

@test "refresh: the DynaKube is annotated BEFORE the restarts (readers start after its timestamp)" {
  source_functions
  echo '{"apiToken":"T0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE"}' > "$NEXT_SECRET"

  run deployDynatrace apponly
  [ "$(grep -n 'annotate' "$CALLS" | cut -d: -f1)" -lt "$(grep -n 'rollout restart' "$CALLS" | head -1 | cut -d: -f1)" ]
}

statuses() { grep -c "rollout status" "$CALLS" || true; }

@test "refresh: it waits for every restarted rollout to finish, after its restart" {
  source_functions
  echo '{"apiToken":"T0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE"}' > "$NEXT_SECRET"

  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  [ "$(statuses)" -eq 3 ]
  for w in "dynatrace rollout status statefulset/dk-otel-collector" "app rollout status deployment/envfrom" \
           "app rollout status daemonset/vol"; do
    grep -q -- "-n $w --timeout=120s" "$CALLS"
  done
  # every wait comes after the last restart: nothing is declared done while another is pending
  [ "$(grep -n 'rollout restart' "$CALLS" | tail -1 | cut -d: -f1)" -lt "$(grep -n 'rollout status' "$CALLS" | head -1 | cut -d: -f1)" ]
  [[ "$output" == *"statefulset/dk-otel-collector in dynatrace is on the new tokens"* ]]
}

@test "refresh: a rollout that does not finish fails the deploy, loudly" {
  source_functions
  echo '{"apiToken":"T0LDVALUE"}' > "$LIVE_SECRET"
  echo '{"apiToken":"TNEWVALUE"}' > "$NEXT_SECRET"
  export STUCK="statefulset/dk-otel-collector"

  run deployDynatrace apponly
  [ "$status" -ne 0 ]
  [[ "$output" == *"TOKEN REFRESH INCOMPLETE: statefulset/dk-otel-collector"* ]]
  [ "$(statuses)" -eq 3 ]           # the others are still waited for
}

@test "refresh: an unchanged Secret waits for nothing" {
  source_functions
  echo '{"apiToken":"TSAMEVALUE"}' > "$LIVE_SECRET"
  cp "$LIVE_SECRET" "$NEXT_SECRET"

  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  [ "$(statuses)" -eq 0 ]
}

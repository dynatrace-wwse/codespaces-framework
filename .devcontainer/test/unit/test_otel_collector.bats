#!/usr/bin/env bats
# Tests deployOtelCollector / undeployOtelCollector (177): a standalone
# Dynatrace OpenTelemetry Collector in the cluster that exports to the tenant.
#
# Nothing touches a cluster or the network: kubectl is a stub on PATH that logs
# its arguments and keeps what it is fed on stdin. The ingest token is a
# sentinel, and the tests grep every output and every logged argument list for
# it: a token on a command line or in a log is a failure.
#
# Every negated assertion ends in "|| false": bats runs tests under set -e,
# which ignores a "! cmd" that fails — only "|| false" makes it count.

TOKEN_SENTINEL="dt0c01.SENTINELPUBLIC0123456789.SENTINELSECRET0123456789"

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"
  unset DT_EVAL_SECRETS DT_ENVIRONMENT DT_OPERATOR_TOKEN OTEL_COLLECTOR_VERSION \
        OTEL_COLLECTOR_DIGEST OTEL_COLLECTOR_NAMESPACE OTEL_COLLECTOR_READY_TIMEOUT

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test"
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export FRAMEWORK_CACHE=""
  export APP_REGISTRY="$TEST_DIR/app-registry"

  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN="" BLUE="" CYAN="" YELLOW="" ORANGE="" RED="" LILA="" NORMAL="" RESET=""
thickline="" halfline="" thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"
export ENV_FILE
VARSEOF
  # The real pins, nothing else.
  grep -E '^(OTEL_COLLECTOR_|export OTEL_COLLECTOR_)' \
    "$BATS_TEST_DIRNAME/../../util/variables.sh" >> "$FAKE_REPO/.devcontainer/util/variables.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  export BIN="$TEST_DIR/bin" STATE="$TEST_DIR/state" ARGS_LOG="$TEST_DIR/args.log"
  mkdir -p "$BIN" "$STATE"
  : > "$ARGS_LOG"
  export PATH="$BIN:$PATH"
  make_kubectl_stub

  export DT_OTEL_ENDPOINT="https://abc12345.live.dynatrace.com/api/v2/otlp"
  export DT_INGEST_TOKEN="$TOKEN_SENTINEL"
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source .devcontainer/util/variables.sh
  source .devcontainer/util/functions.sh
}

make_kubectl_stub() {
  # Logs argv. `create secret ... --from-env-file=/dev/stdin` keeps stdin in
  # $STATE/secret; `apply -f -` appends stdin to $STATE/applied.yaml;
  # `rollout status` answers $STUB_ROLLOUT_RC.
  cat > "$BIN/kubectl" <<'EOF'
#!/bin/bash
echo "kubectl $*" >> "$ARGS_LOG"
[ -n "${KUBECTL_NO_CLUSTER:-}" ] && [ "$1" = "cluster-info" ] && exit 1
args="$*"
case "$args" in
  *"create secret generic"*"--from-env-file=/dev/stdin"*)
    cat > "$STATE/secret"; echo "kind: Secret"; exit 0 ;;
  *"create cm otel-collector-config"*)
    for a in "$@"; do case "$a" in --from-literal=config.yaml=*) printf '%s' "${a#--from-literal=config.yaml=}" > "$STATE/config.yaml" ;; esac; done
    echo "kind: ConfigMap"; exit 0 ;;
  *"apply -f -"*) cat >> "$STATE/applied.yaml"; echo "---" >> "$STATE/applied.yaml"; exit 0 ;;
  *"rollout status"*) exit "${STUB_ROLLOUT_RC:-0}" ;;
esac
[[ "$args" == *"--dry-run=client"* ]] && echo "apiVersion: v1"
exit 0
EOF
  chmod +x "$BIN/kubectl"
}

no_token_anywhere() {
  [[ "$1" != *"SENTINELSECRET"* ]]
  ! grep -q "SENTINELSECRET" "$ARGS_LOG" || false
  ! grep -q "SENTINELSECRET" "$STATE/applied.yaml" 2>/dev/null || false
}

@test "variables.sh: the collector is pinned by tag and index digest" {
  source_functions
  [[ "$OTEL_COLLECTOR_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
  [[ "$OTEL_COLLECTOR_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
  [ "$OTEL_COLLECTOR_NAMESPACE" = "otel-collector" ]
}

@test "deployOtelCollector: refuses without a cluster, applies nothing" {
  export KUBECTL_NO_CLUSTER=1
  source_functions
  run deployOtelCollector
  [ "$status" -eq 1 ]
  [[ "$output" == *"no Kubernetes cluster"* ]]
  ! grep -q "apply" "$ARGS_LOG" || false
}

@test "deployOtelCollector: refuses without tenant credentials, creates nothing" {
  unset DT_OTEL_ENDPOINT DT_INGEST_TOKEN
  source_functions
  run deployOtelCollector
  [ "$status" -eq 1 ]
  [[ "$output" == *"DT_OTEL_ENDPOINT and DT_INGEST_TOKEN are needed"* ]]
  ! grep -q "create namespace" "$ARGS_LOG" || false
  [ ! -e "$STATE/secret" ]
}

@test "deployOtelCollector: token reaches the Secret through stdin only, never argv or output" {
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  grep -qx "DT_API_TOKEN=$TOKEN_SENTINEL" "$STATE/secret"
  grep -qx "DT_ENDPOINT=$DT_OTEL_ENDPOINT" "$STATE/secret"
  grep -q "create secret generic dynatrace-otelcol-dt-api-credentials --from-env-file=/dev/stdin --dry-run=client" "$ARGS_LOG"
  [[ "$output" == *"ingest token not printed"* ]]
  no_token_anywhere "$output"
}

@test "deployOtelCollector: pinned multi-arch image by digest, OTLP 4317 + 4318 Service, waits for rollout" {
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  grep -q "image: ghcr.io/dynatrace/dynatrace-otel-collector/dynatrace-otel-collector:${OTEL_COLLECTOR_VERSION}@${OTEL_COLLECTOR_DIGEST}" "$STATE/applied.yaml"
  grep -q "port: 4317" "$STATE/applied.yaml"
  grep -q "port: 4318" "$STATE/applied.yaml"
  grep -q "secretKeyRef:" "$STATE/applied.yaml"
  grep -q "kubectl -n otel-collector rollout status deploy/otel-collector --timeout=300s" "$ARGS_LOG"
  [[ "$output" == *"http://otel-collector.otel-collector.svc.cluster.local:4318"* ]]
}

@test "deployOtelCollector: config exports traces, metrics and logs to the tenant with the Api-Token header" {
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  python3 - "$STATE/config.yaml" <<'PY'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1]))
exp = c["exporters"]["otlp_http"]
assert exp["endpoint"] == "${env:DT_ENDPOINT}", exp
assert exp["headers"]["Authorization"] == "Api-Token ${env:DT_API_TOKEN}", exp
p = c["service"]["pipelines"]
for sig in ("traces", "metrics", "logs"):
    assert p[sig]["receivers"] == ["otlp"], sig
    assert p[sig]["exporters"] == ["otlp_http"], sig
assert "cumulativetodelta" in p["metrics"]["processors"]   # Dynatrace ingests delta only
assert set(c["receivers"]["otlp"]["protocols"]) == {"grpc", "http"}
PY
}

@test "deployOtelCollector: a collector that never gets ready fails, rc 1" {
  export STUB_ROLLOUT_RC=1
  source_functions
  run deployOtelCollector
  [ "$status" -eq 1 ]
  [[ "$output" == *"did not become ready"* ]]
  no_token_anywhere "$output"
}

@test "deployOtelCollector: re-run is idempotent (apply, never a bare create)" {
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  run deployOtelCollector
  [ "$status" -eq 0 ]
  # every create is a --dry-run=client render piped into apply (the configmap's
  # literal spans log lines, so count rather than match per line)
  [ "$(grep -c "^kubectl .*create " "$ARGS_LOG")" -eq 6 ]
  [ "$(grep -c -- "--dry-run=client" "$ARGS_LOG")" -eq 6 ]
}

@test "deployOtelCollector: needs no Dynatrace Operator (no dynakube, no helm)" {
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  ! grep -q -i "dynakube\|helm" "$ARGS_LOG" || false
}

@test "deployOtelCollector: returns, never exits the caller's shell" {
  export KUBECTL_NO_CLUSTER=1
  source_functions
  deployOtelCollector || true
  echo "still here"
}

@test "undeployOtelCollector: removes the namespace and the cluster-wide RBAC" {
  source_functions
  run undeployOtelCollector
  [ "$status" -eq 0 ]
  grep -q "kubectl delete namespace otel-collector --ignore-not-found" "$ARGS_LOG"
  grep -q "kubectl delete clusterrole otel-collector-k8sattributes --ignore-not-found" "$ARGS_LOG"
  grep -q "kubectl delete clusterrolebinding otel-collector-k8sattributes --ignore-not-found" "$ARGS_LOG"
}

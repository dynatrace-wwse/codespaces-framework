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
        OTEL_COLLECTOR_DIGEST OTEL_COLLECTOR_NAMESPACE OTEL_COLLECTOR_READY_TIMEOUT \
        OTEL_COLLECTOR_MODE OTEL_COLLECTOR_POD_LOGS OTEL_COLLECTOR_CONFIG_FILE

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

# ── Overrides (179): OTEL_COLLECTOR_MODE / _POD_LOGS / _CONFIG_FILE ──────────

# Runs deployOtelCollector from a given functions.sh with no override set and
# keeps what it applied, the config it shipped and the rollout it waited on.
render_with() {
  # $1 functions.sh to use, $2 output dir
  cp "$1" "$FAKE_REPO/.devcontainer/util/functions.sh"
  : > "$ARGS_LOG"; rm -f "$STATE/applied.yaml" "$STATE/config.yaml"
  ( source_functions; deployOtelCollector >/dev/null )
  mkdir -p "$2"
  cp "$STATE/applied.yaml" "$2/applied.yaml"
  cp "$STATE/config.yaml" "$2/config.yaml"
  grep "rollout status" "$ARGS_LOG" > "$2/rollout"
}

@test "overrides: unset, the collector is byte-identical to 1.17.0 (control)" {
  git -C "$BATS_TEST_DIRNAME" show 1.17.0:.devcontainer/util/functions.sh > "$TEST_DIR/functions-1.17.0.sh" \
    || { [ -z "${GITHUB_ACTIONS:-}" ] || { echo "tag 1.17.0 missing in CI — the control cannot run"; false; }; skip "tag 1.17.0 not in this clone"; }
  render_with "$TEST_DIR/functions-1.17.0.sh" "$TEST_DIR/old"
  render_with "$BATS_TEST_DIRNAME/../../util/functions.sh" "$TEST_DIR/new"
  grep -q 'config-sha' "$TEST_DIR/new/applied.yaml"   # the sha (pod roll trigger) is compared too
  diff "$TEST_DIR/old/applied.yaml" "$TEST_DIR/new/applied.yaml"
  diff "$TEST_DIR/old/config.yaml" "$TEST_DIR/new/config.yaml"
  diff "$TEST_DIR/old/rollout" "$TEST_DIR/new/rollout"
}

@test "overrides: default is a Deployment with no hostPath, no root, no node env" {
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  grep -q "^kind: Deployment" "$STATE/applied.yaml"
  ! grep -q "^kind: DaemonSet" "$STATE/applied.yaml" || false
  ! grep -q "hostPath\|runAsUser\|K8S_NODE_NAME" "$STATE/applied.yaml" || false
}

@test "overrides: OTEL_COLLECTOR_MODE=daemonset renders a DaemonSet and waits on ds/" {
  export OTEL_COLLECTOR_MODE=daemonset
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  grep -q "^kind: DaemonSet" "$STATE/applied.yaml"
  ! grep -q "^kind: Deployment" "$STATE/applied.yaml" || false
  ! grep -q "replicas:" "$STATE/applied.yaml" || false
  grep -q "K8S_NODE_NAME" "$STATE/applied.yaml"
  ! grep -q "hostPath" "$STATE/applied.yaml" || false
  grep -q "kubectl -n otel-collector rollout status ds/otel-collector" "$ARGS_LOG"
  grep -q "kubectl -n otel-collector delete deployment otel-collector --ignore-not-found" "$ARGS_LOG"
  python3 -c "import sys,yaml; list(yaml.safe_load_all(open(sys.argv[1])))" "$STATE/applied.yaml"
}

@test "overrides: OTEL_COLLECTOR_POD_LOGS=true mounts /var/log/pods read-only as root" {
  export OTEL_COLLECTOR_MODE=daemonset OTEL_COLLECTOR_POD_LOGS=true
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  python3 - "$STATE/applied.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if isinstance(d, dict) and "kind" in d]
ds = [d for d in docs if d["kind"] == "DaemonSet"][0]
pod = ds["spec"]["template"]["spec"]
assert pod["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}, pod.get("securityContext")
vols = {v["name"]: v for v in pod["volumes"]}
assert vols["varlogpods"]["hostPath"]["path"] == "/var/log/pods"
m = {v["name"]: v for v in pod["containers"][0]["volumeMounts"]}
assert m["varlogpods"] == {"name": "varlogpods", "mountPath": "/var/log/pods", "readOnly": True}
PY
  no_token_anywhere "$output"
}

@test "overrides: OTEL_COLLECTOR_POD_LOGS=true without daemonset is refused, nothing applied" {
  export OTEL_COLLECTOR_POD_LOGS=true
  source_functions
  run deployOtelCollector
  [ "$status" -eq 1 ]
  [[ "$output" == *"needs OTEL_COLLECTOR_MODE=daemonset"* ]]
  ! grep -q "apply" "$ARGS_LOG" || false
}

@test "overrides: an unknown OTEL_COLLECTOR_MODE is refused, nothing applied" {
  export OTEL_COLLECTOR_MODE=statefulset
  source_functions
  run deployOtelCollector
  [ "$status" -eq 1 ]
  [[ "$output" == *"must be deployment or daemonset"* ]]
  ! grep -q "apply" "$ARGS_LOG" || false
}

@test "overrides: OTEL_COLLECTOR_CONFIG_FILE replaces the config verbatim" {
  printf 'receivers:\n  filelog:\n    include: [/var/log/pods/*/*/*.log]\n# marker-179\n' > "$TEST_DIR/custom.yaml"
  export OTEL_COLLECTOR_CONFIG_FILE="$TEST_DIR/custom.yaml"
  source_functions
  run deployOtelCollector
  [ "$status" -eq 0 ]
  # $(cat) drops the trailing newline, as it does for the default config
  [ "$(cat "$TEST_DIR/custom.yaml")" = "$(cat "$STATE/config.yaml")" ]
  grep -q "marker-179" "$STATE/config.yaml"
}

@test "overrides: a missing OTEL_COLLECTOR_CONFIG_FILE is refused before anything is created" {
  export OTEL_COLLECTOR_CONFIG_FILE="$TEST_DIR/nope.yaml"
  source_functions
  run deployOtelCollector
  [ "$status" -eq 1 ]
  [[ "$output" == *"is not a readable file"* ]]
  ! grep -q "create namespace" "$ARGS_LOG" || false
}

@test "overrides: a config change rolls the pod (sha differs)" {
  source_functions
  run deployOtelCollector
  a=$(grep -o 'config-sha: "[0-9a-f]*"' "$STATE/applied.yaml" | tail -1)
  printf 'x: 1\n' > "$TEST_DIR/c.yaml"
  export OTEL_COLLECTOR_CONFIG_FILE="$TEST_DIR/c.yaml"
  run deployOtelCollector
  b=$(grep -o 'config-sha: "[0-9a-f]*"' "$STATE/applied.yaml" | tail -1)
  [ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ]
}

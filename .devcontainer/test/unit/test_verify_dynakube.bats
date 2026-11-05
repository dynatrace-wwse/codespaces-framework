#!/usr/bin/env bats
# BUG-167: a half-written or corrupt gen/dynakube.yaml applied "successfully" and the log
# module silently never came up.
# Covers: verifyDynakube (good files per mode + negative controls), atomic generateDynakube
# (concurrent writers, a reader during generation), and deployDynatrace propagating a
# failed verification / generation / kubectl apply.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"; mkdir -p "$HOME"
  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test" "$FAKE_REPO/.devcontainer/yaml/gen"
  export REPO_PATH="$FAKE_REPO" RepositoryName="test-enablement" ARCH="x86_64"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env" APP_REGISTRY="$TEST_DIR/app-registry" FRAMEWORK_CACHE=""
  unset DT_HOSTGROUP GITHUB_USER
  export DT_TENANT="https://abc123.live.dynatrace.com"
  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  export DT_OPERATOR_TOKEN="dt0c01.OPERATORTOKENVALUE" DT_INGEST_TOKEN="dt0c01.INGESTTOKENVALUE"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/variables.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../yaml/dynakube-defaults.yaml" "$FAKE_REPO/.devcontainer/yaml/"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"
  export GEN="$FAKE_REPO/.devcontainer/yaml/gen/dynakube.yaml"
  export CALLS="$TEST_DIR/calls"; : > "$CALLS"
  kubectl() {
    case "$*" in
      *"apply -f"*) echo "apply" >> "$CALLS"; return "${APPLY_RC:-0}" ;;
      *) return 0 ;;
    esac
  }
  export -f kubectl
}

teardown() { rm -rf "$TEST_DIR"; }

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
  # No network: image tags are fixed. ECR_DELAY stands in for the seconds the real
  # lookups take, which is the window BUG-167's reader fell into.
  getLatestEcrTag() { [[ -n "${ECR_DELAY:-}" ]] && command sleep "$ECR_DELAY"; echo "1.2.3"; }
  isSprintTenant() { return 1; }
}

config() { printf '%s\n' "$@" > "$FAKE_REPO/.devcontainer/yaml/dynakube-config.yaml"; }

# ---------------------------------------------------------------- good files pass

@test "verifyDynakube: a generated apponly file passes (defaults: logs + telemetry)" {
  source_functions
  generateDynakube apponly
  run verifyDynakube "$GEN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"OK (mode apponly)"* ]]
}

@test "verifyDynakube: a generated cloudnative file passes" {
  source_functions
  generateDynakube cloudnative
  run verifyDynakube "$GEN" cloudnative
  [ "$status" -eq 0 ]
}

@test "verifyDynakube: a generated k8s-only file passes" {
  source_functions
  generateDynakube k8s-only
  run verifyDynakube "$GEN" k8s-only
  [ "$status" -eq 0 ]
}

@test "verifyDynakube: every feature on (kspm, extensions, sensitive data) passes" {
  config "kspm: true" "extensions: true" "telemetry_ingest: true" "sensitive_data: true"
  source_functions
  generateDynakube apponly
  run verifyDynakube "$GEN"
  [ "$status" -eq 0 ]
}

@test "verifyDynakube: log_monitoring false in the config — a file without the log module passes" {
  config "log_monitoring: false"
  source_functions
  generateDynakube apponly
  ! grep -q 'logMonitoring' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -eq 0 ]
}

@test "verifyDynakube: mode defaults to the one in the file's header" {
  source_functions
  generateDynakube cloudnative
  run verifyDynakube "$GEN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"mode cloudnative"* ]]
}

@test "verifyDynakube: no file argument checks gen/dynakube.yaml" {
  source_functions
  generateDynakube apponly
  run verifyDynakube
  [ "$status" -eq 0 ]
  [[ "$output" == *"$GEN OK"* ]]
}

# ---------------------------------------------------------------- negative controls

@test "verifyDynakube: a truncated file fails" {
  source_functions
  generateDynakube apponly
  # Cut mid-way through the DynaKube: what a reader saw while the generator was still writing.
  head -n "$(( $(wc -l < "$GEN") / 2 ))" "$GEN" > "$TEST_DIR/t" && mv "$TEST_DIR/t" "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"FAILED"* ]]
}

@test "verifyDynakube: a file cut before logMonitoring was appended fails (the BUG-167 window)" {
  source_functions
  generateDynakube apponly
  # Everything up to the oneAgent section: valid YAML, valid DynaKube, no log module.
  sed -n '1,/applicationMonitoring/p' "$GEN" > "$TEST_DIR/t" && mv "$TEST_DIR/t" "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"spec.logMonitoring is missing"* ]]
}

@test "verifyDynakube: missing logMonitoring fails" {
  source_functions
  generateDynakube apponly
  sed -i '/^  logMonitoring: {}/d' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"spec.logMonitoring is missing but the config asks for it"* ]]
}

@test "verifyDynakube: logMonitoring without its template image fails" {
  source_functions
  generateDynakube apponly
  python3 - "$GEN" <<'EOF'
import sys, re
s = open(sys.argv[1]).read()
s = re.sub(r"    logMonitoring:\n      imageRef:\n        repository: [^\n]*\n        tag: [^\n]*\n", "", s)
open(sys.argv[1], "w").write(s)
EOF
  grep -q '^  logMonitoring: {}' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"templates.logMonitoring.imageRef"* ]]
}

@test "verifyDynakube: wrong apiVersion fails" {
  source_functions
  generateDynakube apponly
  sed -i 's#dynatrace.com/v1beta6#dynatrace.com/v1beta1#' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"apiVersion is 'dynatrace.com/v1beta1'"* ]]
}

@test "verifyDynakube: wrong kind fails" {
  source_functions
  generateDynakube apponly
  sed -i 's/^kind: DynaKube/kind: DynaKub/' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"expected exactly one DynaKube, found 0"* ]]
}

@test "verifyDynakube: missing apiUrl fails" {
  source_functions
  generateDynakube apponly
  sed -i '/^  apiUrl:/d' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"spec.apiUrl is ''"* ]]
}

@test "verifyDynakube: an apiUrl without a tenant (DT_TENANT unset at generation) fails" {
  source_functions
  generateDynakube apponly
  sed -i 's#^  apiUrl: .*#  apiUrl: /api#' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"spec.apiUrl is '/api'"* ]]
}

@test "verifyDynakube: a token Secret that is not in the file fails" {
  source_functions
  generateDynakube apponly
  sed -i 's/^  tokens: .*/  tokens: some-other-secret/' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"token Secret 'some-other-secret' named by spec.tokens is not in the file"* ]]
}

@test "verifyDynakube: an empty apiToken fails, and no token value is printed" {
  source_functions
  generateDynakube apponly
  sed -i 's/^  apiToken: .*/  apiToken: ""/' "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"has no apiToken"* ]]
  [[ "$output" != *"$(printf '%s' "$DT_INGEST_TOKEN" | base64 -w0)"* ]]
}

@test "verifyDynakube: the wrong oneAgent section for the mode fails" {
  source_functions
  generateDynakube apponly
  run verifyDynakube "$GEN" cloudnative
  [ "$status" -ne 0 ]
  [[ "$output" == *"cloudNativeFullStack is missing"* ]]
  run verifyDynakube "$GEN" k8s-only
  [ "$status" -ne 0 ]
  [[ "$output" == *"spec.oneAgent must be absent"* ]]
}

@test "verifyDynakube: a module the config did not ask for fails" {
  source_functions
  generateDynakube apponly
  config "log_monitoring: false"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"spec.logMonitoring is present but the config does not ask for it"* ]]
}

@test "verifyDynakube: two interleaved generations (duplicate keys) fail" {
  source_functions
  generateDynakube apponly
  # What 202 measured with two concurrent in-place writers: a second spec tail appended.
  sed -n '/^  oneAgent:/,$p' "$GEN" >> "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"does not parse"* ]]
}

@test "verifyDynakube: a missing or empty file fails" {
  source_functions
  run verifyDynakube "$TEST_DIR/nope.yaml"
  [ "$status" -ne 0 ]
  : > "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------- atomic generation

@test "generateDynakube: 20 concurrent generations leave a complete file and no temp files" {
  source_functions
  export ECR_DELAY=0.2
  local i
  for i in $(seq 1 20); do generateDynakube apponly >/dev/null 2>&1 & done
  wait
  run verifyDynakube "$GEN"
  [ "$status" -eq 0 ]
  [ "$(find "$FAKE_REPO/.devcontainer/yaml/gen" -name '.dynakube.yaml.*' | wc -l)" -eq 0 ]
}

@test "generateDynakube: a reader during regeneration never sees a partial file" {
  source_functions
  generateDynakube apponly >/dev/null 2>&1
  export ECR_DELAY=1
  ( for i in 1 2 3; do generateDynakube apponly >/dev/null 2>&1; done ) &
  local writer=$! reads=0 bad=0
  while kill -0 "$writer" 2>/dev/null; do
    cp "$GEN" "$TEST_DIR/snap" 2>/dev/null || continue
    reads=$((reads + 1))
    grep -q '^  logMonitoring: {}' "$TEST_DIR/snap" || bad=$((bad + 1))
  done
  wait "$writer"
  echo "reads=$reads bad=$bad"
  [ "$reads" -gt 20 ]
  [ "$bad" -eq 0 ]
}

@test "generateDynakube: an unwritable gen dir fails and leaves the previous file" {
  [ "$(id -u)" -ne 0 ] || skip "root writes anywhere"
  source_functions
  generateDynakube apponly >/dev/null 2>&1
  local before; before="$(sha256sum < "$GEN")"
  chmod a-w "$FAKE_REPO/.devcontainer/yaml/gen"
  run generateDynakube cloudnative
  chmod u+w "$FAKE_REPO/.devcontainer/yaml/gen"
  [ "$status" -ne 0 ]
  [ "$(sha256sum < "$GEN")" = "$before" ]
}

# ---------------------------------------------------------------- deployDynatrace fails loudly

deploy_stubs() {
  dynatraceEvalReadSaveCredentials() { :; }
  fixSprintActiveGateImage() { :; }
  fixSprintCodeModulesImage() { :; }
  waitForPod() { :; }
  waitForAllReadyPods() { :; }
  sleep() { :; }
}

@test "deployDynatrace: a good file is applied and returns 0" {
  source_functions; deploy_stubs
  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  [ "$(grep -c apply "$CALLS")" -eq 1 ]
}

@test "deployDynatrace: a failed kubectl apply returns non-zero" {
  source_functions; deploy_stubs
  APPLY_RC=1 run deployDynatrace apponly
  [ "$status" -ne 0 ]
  [[ "$output" == *"kubectl apply of $GEN FAILED (rc=1)"* ]]
}

@test "deployDynatrace: a truncated generated file is NOT applied and returns non-zero" {
  source_functions; deploy_stubs
  generateDynakube() {
    printf -- '---\napiVersion: dynatrace.com/v1beta6\nkind: DynaKube\nmetadata:\n  name: x\n  namespace: dynatrace\nspec:\n  apiUrl: https://abc123.live.dynatrace.com/api\n  oneAgent:\n    applicationMoni' > "$GEN"
  }
  run deployDynatrace apponly
  [ "$status" -ne 0 ]
  [[ "$output" == *"Dynakube NOT applied"* ]]
  [ "$(grep -c apply "$CALLS")" -eq 0 ]
}

@test "deployDynatrace: a failed generation returns non-zero and does not apply the previous file" {
  source_functions; deploy_stubs
  generateDynakube apponly >/dev/null 2>&1   # a stale, valid file from an earlier run
  generateDynakube() { return 1; }
  run deployDynatrace apponly
  [ "$status" -ne 0 ]
  [ "$(grep -c apply "$CALLS")" -eq 0 ]
}

@test "deployDynatrace: a failure returns, it does not exit the sourcing shell" {
  source_functions; deploy_stubs
  APPLY_RC=1 deployDynatrace apponly >/dev/null 2>&1 || true
  # Still here: an `exit` would have ended this test before the next line.
  [ "$(grep -c apply "$CALLS")" -eq 1 ]
}

# ---------------------------------------------------------------- zsh, file mode, no PyYAML

@test "generateDynakube: the published file keeps the umask mode (0644 under umask 022)" {
  source_functions
  umask 022
  generateDynakube apponly >/dev/null 2>&1
  [ "$(stat -c %a "$GEN")" = "644" ]
}

@test "generate + verify under zsh (integration tests run in zsh)" {
  command -v zsh >/dev/null || skip "zsh not installed"
  run zsh -c '
    umask 022; cd "$FAKE_REPO"; source .devcontainer/util/functions.sh >/dev/null 2>&1
    getLatestEcrTag() { echo 1.2.3; }; isSprintTenant() { return 1; }
    generateDynakube apponly >/dev/null 2>&1 || { echo GEN-FAILED; exit 1; }
    stat -c MODE=%a "$GEN"
    verifyDynakube'
  echo "$output"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MODE=644"* ]]
  [[ "$output" == *"OK (mode apponly)"* ]]
}

# Without PyYAML (the training image has none) verifyDynakube parses with
# `kubectl create --dry-run=client -o json`, which prints one JSON object per
# resource, back to back, and may warn on stderr. A yaml.py that raises
# ImportError ahead of site-packages hides PyYAML from python3.
no_pyyaml() {
  mkdir -p "$TEST_DIR/noyaml"
  echo 'raise ImportError("hidden by test")' > "$TEST_DIR/noyaml/yaml.py"
  export PYTHONPATH="$TEST_DIR/noyaml"
  ! python3 -c 'import yaml' 2>/dev/null
}
kubectl_dry_run_from() {
  # Emits what kubectl would: each document of $1 as its own JSON object (via real
  # PyYAML, outside the hidden path), plus a warning on stderr. Fails like kubectl
  # when the YAML does not parse.
  export KDR_SRC="$1"
  kubectl() {
    case "$*" in
      *"create --dry-run=client -o json -f"*)
        echo "Warning: resource is missing an annotation" >&2
        PYTHONPATH= python3 -c '
import sys, json, yaml
try:
    for d in yaml.safe_load_all(open(sys.argv[1])):
        if d is not None: print(json.dumps(d, indent=2))
except yaml.YAMLError as e:
    sys.stderr.write("error: error parsing %s: %s\n" % (sys.argv[1], e)); sys.exit(1)
' "$KDR_SRC" ;;
      *) return 0 ;;
    esac
  }
  export -f kubectl
}

@test "verifyDynakube without PyYAML: a good file passes via kubectl's parser" {
  source_functions
  generateDynakube apponly >/dev/null 2>&1
  no_pyyaml
  kubectl_dry_run_from "$GEN"
  run verifyDynakube "$GEN"
  echo "$output"
  [ "$status" -eq 0 ]
}

@test "verifyDynakube without PyYAML: missing logMonitoring still fails" {
  source_functions
  generateDynakube apponly >/dev/null 2>&1
  sed -i '/^  logMonitoring: {}/d' "$GEN"
  no_pyyaml
  kubectl_dry_run_from "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"spec.logMonitoring is missing"* ]]
}

@test "verifyDynakube without PyYAML: a file kubectl cannot parse fails" {
  source_functions
  generateDynakube apponly >/dev/null 2>&1
  printf '  bad: [unclosed\n' >> "$GEN"
  no_pyyaml
  kubectl_dry_run_from "$GEN"
  run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"does not parse"* ]]
}

@test "verifyDynakube with neither PyYAML nor kubectl fails, never passes" {
  source_functions
  generateDynakube apponly >/dev/null 2>&1
  no_pyyaml
  unset -f kubectl
  mkdir -p "$TEST_DIR/bin"; ln -s "$(command -v python3)" "$TEST_DIR/bin/python3"
  for t in sed head mktemp cat rm; do ln -s "$(command -v $t)" "$TEST_DIR/bin/$t"; done
  PATH="$TEST_DIR/bin" run verifyDynakube "$GEN"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no YAML parser available"* ]]
}

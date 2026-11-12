#!/usr/bin/env bats
# BUG-199 (e) / ENH-163 F13: the framework registers the Kubernetes cluster itself
# (builtin:cloud.kubernetes by kube-system UID) instead of relying on the operator's optional,
# silent create; verifies it; and removes exactly what it recorded at teardown.
# curl and kubectl are fakes driven by files: the tenant's objects live in $OBJS (one JSON
# object per line), every request is logged to $CALLS.

UID_A="11111111-2222-3333-4444-555555555555"
UID_B="99999999-8888-7777-6666-555555555555"
TOKEN="dt0c01.LEAKCHECKAAAAAAAAAAAAAAAAAA.LEAKCHECKBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"; mkdir -p "$HOME"
  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test" "$FAKE_REPO/.devcontainer/yaml/gen"
  export REPO_PATH="$FAKE_REPO" RepositoryName="test-enablement" ARCH="x86_64"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env" APP_REGISTRY="$TEST_DIR/app-registry" FRAMEWORK_CACHE=""
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/variables.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../yaml/dynakube-defaults.yaml" "$FAKE_REPO/.devcontainer/yaml/"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  export DT_ENVIRONMENT="https://abc123.apps.dynatrace.com"
  export DT_TENANT="https://abc123.live.dynatrace.com"
  export DT_OPERATOR_TOKEN="$TOKEN"
  unset ORBITAL_SHARED_ROLE DT_K8S_REGISTRATION DT_K8S_REGISTRATION_STRICT
  export DT_K8S_REGISTRATION_WAIT=20
  export CALLS="$TEST_DIR/calls" OBJS="$TEST_DIR/objs" KUID="$UID_A" DK="byoa-shop-ws-1"
  export STATUS_MEID_FILE="$TEST_DIR/status-meid"
  : > "$CALLS"; : > "$OBJS"
  # Scenario knobs: VALIDATE_CODE, CREATE_CODE, LIST_CODE, DELETE_CODE, GET_CODE
  # (empty = the natural answer). STATUS_MEID: "auto" = the DynaKube reports the object's MEID.
  export STATUS_MEID="auto"

  curl() {
    local method=GET url="" outf="" data="" a
    while (( $# )); do
      case "$1" in
        -X) method="$2"; shift ;;
        -o) outf="$2"; shift ;;
        -w|-H|--max-time) shift ;;
        --data-binary) data=$(cat "${2#@}"); shift ;;
        http*) url="$1" ;;
      esac
      shift
    done
    echo "$method $url" >> "$CALLS"
    local code=200 body='{}'
    case "$method $url" in
      "GET "*"/settings/objects?schemaIds="*|"GET "*"/settings/objects?nextPageKey="*)
        code="${LIST_CODE:-200}"
        body=$(jq -sc '{items: .}' "$OBJS") ;;
      "POST "*"validateOnly=true")
        code="${VALIDATE_CODE:-200}"; body='[{"code":200}]'
        [[ "$code" != 200 ]] && body='{"error":{"code":'"$code"',"message":"Token is missing required scope settings.write"}}'
        # BUG-216: the real cap refusal (COE 2026-11-12) carries the reason in constraintViolations.
        [[ "$code" == 400 && -n "${VALIDATE_CAP:-}" ]] && body='{"error":{"code":400,"message":"Constraints violated.","constraintViolations":[{"path":"","message":"Validation failed for 1 Validators.\n\t- builtin:cloud.kubernetes: Maximum number of monitored Kubernetes clusters reached. Please disable monitoring of some clusters."}]}}' ;;
      "POST "*"/settings/objects")
        code="${CREATE_CODE:-200}"
        if [[ "$code" == 200 ]]; then
          local n; n=$(wc -l < "$OBJS")
          printf '%s' "$data" | jq -c --arg id "OBJ$n" --arg m "KUBERNETES_CLUSTER-NEW$n" \
            '.[0] | {objectId: $id, scope: $m, value: .value}' >> "$OBJS"
          body='[{"code":200,"objectId":"OBJ'"$n"'"}]'
        fi ;;
      "GET "*"/settings/objects/"*)
        local id="${url##*/}"
        body=$(jq -c --arg id "$id" 'select(.objectId == $id)' "$OBJS")
        if [[ -n "${GET_CODE:-}" ]]; then code="$GET_CODE"; elif [[ -z "$body" ]]; then code=404; body='{}'; fi ;;
      "DELETE "*"/settings/objects/"*)
        local id="${url##*/}"
        code="${DELETE_CODE:-204}"
        [[ "$code" == 204 ]] && { jq -c --arg id "$id" 'select(.objectId != $id)' "$OBJS" > "$OBJS.t"; mv "$OBJS.t" "$OBJS"; } ;;
    esac
    printf '%s' "$body" > "$outf"
    printf '%s' "$code"
  }
  kubectl() {
    local args="$*"
    case "$args" in
      *"get namespace kube-system"*) printf '%s' "$KUID" ;;
      *"get dynakube -o jsonpath={.items[0].metadata.name}"*) printf '%s' "$DK" ;;
      *"annotate dynakube"*) echo "kubectl annotate" >> "$CALLS" ;;
      *'MonitoredEntity'*) printf 'True|StatusUpdated' ;;
      *"logs -n dynatrace deploy/dynatrace-operator"*) [[ -n "${OPERATOR_LOG:-}" ]] && cat "$OPERATOR_LOG" ;;
      *'kubernetesClusterMEID'*)
        if [[ "$STATUS_MEID" == auto ]]; then
          jq -r --arg u "$KUID" 'select(.value.clusterId == $u) | .scope' "$OBJS" | head -1
        else printf '%s' "$STATUS_MEID"; fi ;;
      *) return 0 ;;
    esac
  }
  export -f curl kubectl
}

teardown() { rm -rf "$TEST_DIR"; }

load_fw() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
  sleep() { :; }
}

posts() { grep -c "^POST " "$CALLS" || true; }
seed() {  # seed <objectId> <uid> <label> <meid>
  jq -cn --arg id "$1" --arg u "$2" --arg l "$3" --arg m "$4" \
    '{objectId: $id, scope: $m, value: {enabled: true, label: $l, clusterIdEnabled: true, clusterId: $u}}' >> "$OBJS"
}

# ── ensure ────────────────────────────────────────────────────────────────────────────────────

@test "ensure: no object for this cluster → validate, create, verify; recorded for teardown" {
  load_fw
  run dynatraceEnsureK8sRegistration
  [ "$status" -eq 0 ]
  grep -q "POST https://abc123.live.dynatrace.com/api/v2/settings/objects?validateOnly=true" "$CALLS"
  grep -q "POST https://abc123.live.dynatrace.com/api/v2/settings/objects$" "$CALLS"
  [ "$(jq -r 'select(.value.clusterId=="'"$UID_A"'") | .value.label' "$OBJS")" = "byoa-shop-ws-1" ]
  grep -q "^$UID_A OBJ0 byoa-shop-ws-1$" "$FAKE_REPO/.devcontainer/.dt-k8s-registrations"
}

@test "ensure: an object for this UID already exists → no create, adopted, rc 0" {
  load_fw
  seed OBJX "$UID_A" "operator-made" KUBERNETES_CLUSTER-OLD
  run dynatraceEnsureK8sRegistration
  [ "$status" -eq 0 ]
  [ "$(posts)" -eq 0 ]
  grep -q "^$UID_A OBJX operator-made$" "$FAKE_REPO/.devcontainer/.dt-k8s-registrations"
}

@test "ensure: another cluster's object is not mistaken for ours" {
  load_fw
  seed OBJB "$UID_B" "someone-else" KUBERNETES_CLUSTER-B
  run dynatraceEnsureK8sRegistration
  [ "$status" -eq 0 ]
  [ "$(posts)" -eq 2 ]
}

@test "ensure: token without EFFECTIVE settings.write (403 on validate) → fails loudly, creates nothing" {
  load_fw
  export VALIDATE_CODE=403
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [[ "$output" == *"no EFFECTIVE settings.write"* ]]
  [[ "$output" == *"kubernetesClusterMEID is outdated"* ]]
  [ "$(posts)" -eq 1 ]
  [ ! -s "$OBJS" ]
}

@test "ensure: platform token without write (404 'No write access' on validate) → same settings.write message" {
  load_fw
  export VALIDATE_CODE=404
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [ "$(posts)" -eq 1 ]
  [[ "$output" == *"no EFFECTIVE settings.write"*"(HTTP 404)"* ]]
  [ ! -s "$OBJS" ]
}

@test "ensure: rejected token (401) says revoked or expired" {
  load_fw
  export VALIDATE_CODE=401
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [[ "$output" == *"REJECTED (HTTP 401)"* ]]
}

@test "ensure: objects cannot be read → fails, never creates (could-not-tell is not absent)" {
  load_fw
  export LIST_CODE=500
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [ "$(posts)" -eq 0 ]
  [[ "$output" == *"cannot read builtin:cloud.kubernetes (HTTP 500)"* ]]
}

@test "ensure: token without settings.read (403 on the read) → says so, creates nothing" {
  # SRO 2026-11-08: an ingest token failed here with rc 1 and NO message — the error was printed
  # on stdout inside oid=\$(…) and swallowed. The reason must reach the log.
  load_fw
  export LIST_CODE=403
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [ "$(posts)" -eq 0 ]
  [[ "$output" == *"cannot READ builtin:cloud.kubernetes (HTTP 403)"* ]]
  [[ "$output" == *"settings.read"* ]]
}

@test "ensure: rejected token on the read (401) says revoked or expired" {
  load_fw
  export LIST_CODE=401
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [ "$(posts)" -eq 0 ]
  [[ "$output" == *"REJECTED (HTTP 401)"* ]]
}

@test "ensure: object exists but the DynaKube never names its MEID → fails after the wait" {
  load_fw
  export STATUS_MEID="KUBERNETES_CLUSTER-STALE"
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [[ "$output" == *"reports 'KUBERNETES_CLUSTER-STALE'"* ]]
}

@test "ensure: DT_K8S_REGISTRATION=off → no tenant call at all" {
  load_fw
  export DT_K8S_REGISTRATION=off
  run dynatraceEnsureK8sRegistration
  [ "$status" -eq 0 ]
  ! grep -q "settings" "$CALLS"
}

@test "ensure: unreadable kube-system UID → fails before any tenant call" {
  load_fw
  export KUID=""
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  ! grep -q "settings" "$CALLS"
}

@test "ensure: a platform token goes to the platform proxy as Bearer" {
  load_fw
  export DT_OPERATOR_TOKEN="dt0s16.PLATFORMAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
  run dynatraceEnsureK8sRegistration
  [ "$status" -eq 0 ]
  grep -q "GET https://abc123.apps.dynatrace.com/platform/classic/environment-api/v2/settings/objects" "$CALLS"
}

@test "ensure: DT_TENANT unset (a fresh shell) → derived from DT_ENVIRONMENT" {
  load_fw
  unset DT_TENANT
  run dynatraceEnsureK8sRegistration
  [ "$status" -eq 0 ]
  grep -q "GET https://abc123.live.dynatrace.com/api/v2/settings/objects" "$CALLS"
}

@test "ensure: the token never reaches a command line or the output" {
  load_fw
  export VALIDATE_CODE=403
  run dynatraceEnsureK8sRegistration
  ! grep -q "LEAKCHECK" "$CALLS"
  [[ "$output" != *LEAKCHECK* ]]
}

# ── deployDynatrace: fatal only where opted in ────────────────────────────────────────────────

deploy_stubbed() {
  load_fw
  dynatraceEvalReadSaveCredentials() { :; }
  generateDynakube() { touch "$REPO_PATH/.devcontainer/yaml/gen/dynakube.yaml"; }
  verifyDynakube() { :; }
  dynakubeTokenFingerprint() { :; }
  fixSprintActiveGateImage() { :; }
  fixSprintCodeModulesImage() { :; }
  waitForPod() { :; }
  waitForAllReadyPods() { :; }
}

@test "deploy: registration fails on an ordinary training → loud WARN, deploy still succeeds" {
  deploy_stubbed
  export VALIDATE_CODE=403
  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  [[ "$output" == *"Kubernetes cluster NOT registered"* ]]
}

@test "deploy: registration fails on a shared box (ORBITAL_SHARED_ROLE=shared) → deploy fails" {
  deploy_stubbed
  export VALIDATE_CODE=403 ORBITAL_SHARED_ROLE=shared
  run deployDynatrace apponly
  [ "$status" -ne 0 ]
  [[ "$output" == *"failing the deploy"* ]]
}

@test "deploy: DT_K8S_REGISTRATION_STRICT=1 makes it fatal anywhere" {
  deploy_stubbed
  export VALIDATE_CODE=403 DT_K8S_REGISTRATION_STRICT=1
  run deployDynatrace apponly
  [ "$status" -ne 0 ]
}

@test "deploy: opt-out on a shared box → no registration, deploy succeeds" {
  deploy_stubbed
  export VALIDATE_CODE=403 ORBITAL_SHARED_ROLE=shared DT_K8S_REGISTRATION=off
  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  ! grep -q "settings" "$CALLS"
}

@test "deploy: registration succeeds → creates the object as part of the deploy" {
  deploy_stubbed
  run deployDynatrace apponly
  [ "$status" -eq 0 ]
  [ "$(jq -r '.value.clusterId' "$OBJS")" = "$UID_A" ]
}

# ── teardown ──────────────────────────────────────────────────────────────────────────────────

@test "remove: deletes the recorded object and clears the record" {
  load_fw
  dynatraceEnsureK8sRegistration >/dev/null
  run dynatraceRemoveK8sRegistrations
  [ "$status" -eq 0 ]
  grep -q "DELETE .*/settings/objects/OBJ0" "$CALLS"
  [ ! -s "$OBJS" ]
  [ ! -f "$FAKE_REPO/.devcontainer/.dt-k8s-registrations" ]
}

@test "remove: an object whose label changed is NOT deleted" {
  load_fw
  seed OBJX "$UID_A" "someone-renamed-it" KUBERNETES_CLUSTER-X
  echo "$UID_A OBJX byoa-shop-ws-1" > "$FAKE_REPO/.devcontainer/.dt-k8s-registrations"
  run dynatraceRemoveK8sRegistrations
  ! grep -q "^DELETE" "$CALLS"
  [ -s "$OBJS" ]
}

@test "remove: an object that now carries another cluster's UID is NOT deleted" {
  load_fw
  seed OBJX "$UID_B" "byoa-shop-ws-1" KUBERNETES_CLUSTER-X
  echo "$UID_A OBJX byoa-shop-ws-1" > "$FAKE_REPO/.devcontainer/.dt-k8s-registrations"
  run dynatraceRemoveK8sRegistrations
  ! grep -q "^DELETE" "$CALLS"
}

@test "remove: nothing recorded → no tenant call" {
  load_fw
  run dynatraceRemoveK8sRegistrations
  [ "$status" -eq 0 ]
  [ ! -s "$CALLS" ]
}

@test "remove: already gone (404) counts as removed" {
  load_fw
  echo "$UID_A OBJGONE byoa-shop-ws-1" > "$FAKE_REPO/.devcontainer/.dt-k8s-registrations"
  run dynatraceRemoveK8sRegistrations
  [ "$status" -eq 0 ]
  [ ! -f "$FAKE_REPO/.devcontainer/.dt-k8s-registrations" ]
}

@test "remove: a failed delete keeps the record and fails" {
  load_fw
  dynatraceEnsureK8sRegistration >/dev/null
  export DELETE_CODE=403
  run dynatraceRemoveK8sRegistrations
  [ "$status" -ne 0 ]
  grep -q "OBJ0" "$FAKE_REPO/.devcontainer/.dt-k8s-registrations"
}

@test "frameworkTeardown: runs Monaco cleanup even when the registration removal fails" {
  load_fw
  dynatraceEnsureK8sRegistration >/dev/null
  export DELETE_CODE=500
  undeployMonaco() { echo "monaco-ran" >> "$CALLS"; }
  run frameworkTeardown
  [ "$status" -ne 0 ]
  grep -q "monaco-ran" "$CALLS"
}

# ── BUG-216: the tenant's cap of monitored clusters ─────────────────────────────────────────

@test "ensure: tenant at its cluster cap (400 on validate) → names the cap, creates nothing" {
  load_fw
  export VALIDATE_CODE=400 VALIDATE_CAP=1
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [[ "$output" == *"limit of monitored Kubernetes clusters is reached"* ]]
  [[ "$output" == *"BUG-216"* ]]
  [[ "$output" != *"validation of the"* ]]
  [ "$(posts)" -eq 1 ]
  [ ! -s "$OBJS" ]
}

@test "ensure: an ordinary 400 on validate is NOT reported as the cap (negative control)" {
  load_fw
  export VALIDATE_CODE=400
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [[ "$output" == *"validation of the"* ]]
  [[ "$output" != *"limit of monitored Kubernetes clusters"* ]]
}

@test "ensure: MEID never resolves and the operator log shows the cap → names the cap" {
  load_fw
  export STATUS_MEID="" OPERATOR_LOG="$TEST_DIR/oplog"
  echo '{"level":"error","msg":"Reconciler error","error":"create kubernetes setting: HTTP 400: Validation failed for 1 Validators.\n\t- builtin:cloud.kubernetes: Maximum number of monitored Kubernetes clusters reached. Please disable monitoring of some clusters."}' > "$OPERATOR_LOG"
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [[ "$output" == *"the operator reports the tenant's limit of monitored Kubernetes clusters is reached"* ]]
  [[ "$output" == *"no MEID"* ]]
}

@test "ensure: MEID never resolves, operator log clean → no cap message (negative control)" {
  load_fw
  export STATUS_MEID="" OPERATOR_LOG="$TEST_DIR/oplog"
  echo '{"level":"info","msg":"no MEs found, no kubernetesClusterMEID will be set"}' > "$OPERATOR_LOG"
  run dynatraceEnsureK8sRegistration
  [ "$status" -ne 0 ]
  [[ "$output" != *"limit of monitored Kubernetes clusters"* ]]
}

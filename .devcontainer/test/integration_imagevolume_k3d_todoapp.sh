#!/bin/bash
# Integration test: ApplicationMonitoring with code modules delivered as Kubernetes IMAGE VOLUMES
# (code_modules_delivery: image-volume) on K3d + todo-app.
#
# Self-contained: fresh K3d cluster, Operator WITHOUT the CSI driver, apponly DynaKube carrying the
# image-volume annotation, todo-app restarted into injection, then asserts the injection really
# happened -- not just that the pod is Running. Both silent failure modes of image volumes leave the
# pod Running with no agent (k3s 1.31: the API server drops the volume; containerd < 2.2: subPath
# ignored), so a pod phase proves nothing here.
#
# Asserts:
#   - node: kubelet >= 1.35 and containerd >= 2.2 (dt-enablement >= v1.5.0)
#   - no CSIDriver object, no CSI DaemonSet in dynatrace
#   - the DynaKube carries feature.dynatrace.com/mount-code-modules-via-image-volume: "true"
#   - the injected todo-app pod has a volume of type `image` with the codemodules image
#   - the OneAgent code module is LOADED in the app process (liboneagent in /proc/<pid>/maps)
#
# Environment: Orbital (Sysbox + K3d), Codespaces, or local. Arch: AMD64 and ARM64.
# Credentials: DT_ENVIRONMENT, DT_OPERATOR_TOKEN, DT_INGEST_TOKEN (any tenant).
# Run: bash .devcontainer/test/integration_imagevolume_k3d_todoapp.sh
source .devcontainer/util/source_framework.sh

printInfoSection "=== DT image-volume K3d integration test | arch: $ARCH ==="

assertEnvVariable DT_ENVIRONMENT
assertEnvVariable DT_OPERATOR_TOKEN
assertEnvVariable DT_INGEST_TOKEN

# The opt-in lives in the repo's dynakube-config.yaml. Write it for this run only, unless the repo
# already opts in; restore whatever was there on exit.
_iv_cfg="$REPO_PATH/.devcontainer/yaml/dynakube-config.yaml"
_iv_backup=""
if [[ -f "$_iv_cfg" ]] && grep -qE '^code_modules_delivery:[[:space:]]*"?image-volume' "$_iv_cfg"; then
  printInfo "Repo already opts in to image volumes ($_iv_cfg)"
else
  if [[ -f "$_iv_cfg" ]]; then
    _iv_backup="$(mktemp)"
    cp "$_iv_cfg" "$_iv_backup"
  fi
  printf '\ncode_modules_delivery: image-volume\n' >> "$_iv_cfg"
fi

_iv_fail=0
_iv_cleanup() {
  if [[ -n "$_iv_backup" ]]; then
    mv "$_iv_backup" "$_iv_cfg"
  elif [[ -f "$_iv_cfg" ]] && [[ "$(grep -cvE '^[[:space:]]*$|^code_modules_delivery: image-volume$' "$_iv_cfg")" == "0" ]]; then
    rm -f "$_iv_cfg"
  fi
  [[ "${KEEP_CLUSTER:-}" == "true" ]] || deleteK3dCluster
}
trap _iv_cleanup EXIT

_iv_ok()   { printInfo "✅ $*"; }
_iv_fail() { printError "❌ $*"; _iv_fail=1; }

printInfo "Pre-test cleanup: removing any existing K3d clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true

printInfoSection "1/6  Starting K3d cluster"
export CLUSTER_ENGINE=k3d
startK3dCluster

printInfoSection "2/6  Node versions (kubelet >= 1.35, containerd >= 2.2)"
_kubelet="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"
_runtime="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}')"
printInfo "kubelet ${_kubelet} | runtime ${_runtime}"
# version_ge A B: A >= B (dotted numeric)
version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }
_kv="${_kubelet#v}"; _kv="${_kv%%+*}"
_cv="${_runtime#containerd://}"; _cv="${_cv%%-*}"
if [[ "$_runtime" == containerd://* ]] && version_ge "$_cv" 2.2 && version_ge "$_kv" 1.35; then
  _iv_ok "node supports image volumes with subPath"
else
  _iv_fail "node cannot serve image volumes (needs kubelet >= 1.35, containerd >= 2.2) -- STOP"
  exit 1
fi

printInfoSection "3/6  Operator without CSI driver"
dynatraceDeployOperator
if kubectl get csidriver csi.oneagent.dynatrace.com >/dev/null 2>&1; then
  _iv_fail "CSIDriver csi.oneagent.dynatrace.com exists"
else
  _iv_ok "no Dynatrace CSIDriver"
fi
if kubectl -n dynatrace get daemonset -o name 2>/dev/null | grep -q csi; then
  _iv_fail "CSI DaemonSet present in dynatrace"
else
  _iv_ok "no CSI DaemonSet"
fi

printInfoSection "4/6  ApplicationMonitoring DynaKube with the image-volume annotation"
deployApplicationMonitoring || exit 1
_ann="$(kubectl -n dynatrace get dynakube -o jsonpath='{.items[0].metadata.annotations.feature\.dynatrace\.com/mount-code-modules-via-image-volume}')"
if [[ "$_ann" == "true" ]]; then _iv_ok "DynaKube annotation set"; else _iv_fail "DynaKube annotation missing (got '${_ann}')"; fi
_cm="$(kubectl -n dynatrace get dynakube -o jsonpath='{.items[0].spec.oneAgent.applicationMonitoring.codeModulesImage}')"
printInfo "codeModulesImage: ${_cm:-<none>}"
[[ -n "$_cm" ]] && _iv_ok "codeModulesImage pinned" || _iv_fail "codeModulesImage empty"
verifyDynakube || _iv_fail "DynaKube did not become ready"

printInfoSection "5/6  todo-app, injected"
deployTodoApp
# Pods created before the webhook was ready are not injected: restart once the DynaKube is up.
kubectl -n todoapp rollout restart deploy/todoapp
kubectl -n todoapp rollout status deploy/todoapp --timeout=300s
_pod="$(kubectl -n todoapp get pods -l app=todoapp --field-selector=status.phase=Running \
  --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')"
printInfo "pod: ${_pod}"

printInfoSection "6/6  Injection assertions"
_ivols="$(kubectl -n todoapp get pod "$_pod" -o json | python3 -c '
import sys, json
p = json.load(sys.stdin)
for v in p["spec"].get("volumes", []):
    if "image" in v:
        print(v["name"] + " " + v["image"].get("reference", ""))
')"
if [[ -n "$_ivols" ]]; then
  _iv_ok "pod has image volume(s): ${_ivols//$'\n'/; }"
else
  _iv_fail "pod has NO volume of type image"
  kubectl -n todoapp get pod "$_pod" -o jsonpath='{.spec.volumes}'; echo
fi
if kubectl -n todoapp get pod "$_pod" -o json | grep -q '"csi"'; then
  _iv_fail "pod still mounts a CSI volume"
else
  _iv_ok "pod mounts no CSI volume"
fi
# The agent must be loaded, not just mounted: look for liboneagent in any app process's maps.
_loaded=""
for _i in $(seq 1 12); do
  _loaded="$(kubectl -n todoapp exec "$_pod" -c todoapp -- sh -c \
    'grep -l liboneagent /proc/[0-9]*/maps 2>/dev/null | head -3' 2>/dev/null)"
  [[ -n "$_loaded" ]] && break
  sleep 5
done
if [[ -n "$_loaded" ]]; then
  _iv_ok "OneAgent code module loaded (${_loaded//$'\n'/ })"
else
  _iv_fail "OneAgent code module NOT loaded in any process"
  kubectl -n todoapp exec "$_pod" -c todoapp -- sh -c 'ls /opt/dynatrace/oneagent 2>&1 | head' || true
fi

if [[ "$_iv_fail" == "0" ]]; then
  printInfoSection "✅ DT image-volume K3d test PASSED (arch: $ARCH, kubelet ${_kubelet}, ${_runtime})"
else
  printInfoSection "❌ DT image-volume K3d test FAILED (arch: $ARCH)"
  exit 1
fi

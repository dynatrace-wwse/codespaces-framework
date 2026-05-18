#!/bin/bash
# Dual-engine integration test: verifies nginx ingress app exposure works
# identically with both Kind and K3d cluster engines.
#
# Each engine:
#   1. Starts the cluster
#   2. Installs nginx ingress controller
#   3. Deploys a simple test app via registerApp
#   4. Asserts reachability via assertRunningApp
#   5. Deletes the cluster
#
# Run: bash .devcontainer/test/integration_engines.sh
source .devcontainer/util/source_framework.sh

# Wipe any pre-existing clusters so port bindings don't conflict between engines.
# Note: k3d cluster list -o name still emits a header; use JSON output instead.
printInfo "Pre-test cleanup: removing any existing clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true
kind get clusters 2>/dev/null | xargs -r -I{} kind delete cluster --name {} 2>/dev/null || true

# Kind test: AMD64 + not Orbital.
# On Orbital (Sysbox), Kind node pods get stuck in ContainerCreating — the
# container nesting depth (Sysbox→DinD→dt-enablement→kind-node→pod) exceeds
# what Sysbox's userspace kernel can virtualise. k3d works because it uses
# k3s (single binary, no inner containerd) — one fewer nesting level.
# Kind is only valid in real VM environments (GitHub Codespaces, local).
_run_env=$(detectRunEnvironment)
if [[ "$ARCH" != "x86_64" ]]; then
  printWarn "Skipping Kind engine test — not AMD64 (arch: $ARCH)"
elif [[ "$_run_env" == "orbital" ]]; then
  printWarn "Skipping Kind engine test — running on Orbital/Sysbox (pods would stuck ContainerCreating)"
else
  # --- Kind ---
  printInfoSection "Engine test 1/2: Kind"
  export CLUSTER_ENGINE=kind
  startKindCluster
  deployTodoApp
  assertRunningApp todoapp
  deleteKindCluster
fi

# --- K3d ---
printInfoSection "Engine test 2/2: K3d"
export CLUSTER_ENGINE=k3d
startK3dCluster
deployTodoApp
assertRunningApp todoapp
deleteK3dCluster

printInfoSection "✅ Both engines passed: nginx ingress exposure is identical for Kind and K3d"

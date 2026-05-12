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

# --- Kind ---
printInfoSection "Engine test 1/2: Kind"
export CLUSTER_ENGINE=kind
startKindCluster
deployTodoApp
assertRunningApp todoapp
deleteKindCluster

# --- K3d ---
printInfoSection "Engine test 2/2: K3d"
export CLUSTER_ENGINE=k3d
startK3dCluster
deployTodoApp
assertRunningApp todoapp
deleteK3dCluster

printInfoSection "✅ Both engines passed: nginx ingress exposure is identical for Kind and K3d"

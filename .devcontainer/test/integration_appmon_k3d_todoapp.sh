#!/bin/bash
# Integration test: Dynatrace ApplicationMonitoring (apponly) + K3d + todo-app
#
# Self-contained: creates a fresh K3d cluster, deploys DT in ApplicationMonitoring
# mode, deploys todo-app, asserts everything is reachable, then tears down.
#
# Architecture: AMD64 and ARM64 — run independently on each worker arch.
# DT modules (OneAgent code module, CSI driver) differ per CPU arch; this test
# validates the correct image is pulled and injected on both.
#
# Environment: Orbital (Sysbox + K3d), Codespaces, or local. All supported.
# Credentials: DT_ENVIRONMENT, DT_OPERATOR_TOKEN, DT_INGEST_TOKEN from .env
#              pointing to the COE tenant (geu80787.apps.dynatrace.com).
#
# Run: bash .devcontainer/test/integration_appmon_k3d_todoapp.sh
source .devcontainer/util/source_framework.sh

printInfoSection "=== DT AppMon K3d integration test | arch: $ARCH ==="

_run_env=$(detectRunEnvironment)
printInfo "Environment: $_run_env | Arch: $ARCH"

# Credentials check — fail fast before spending time on cluster setup
assertEnvVariable DT_ENVIRONMENT
assertEnvVariable DT_OPERATOR_TOKEN
assertEnvVariable DT_INGEST_TOKEN

# Pre-test cleanup: remove any clusters left from postCreate so we start clean.
printInfo "Pre-test cleanup: removing any existing K3d clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true

# 1. Start K3d cluster
printInfoSection "1/5  Starting K3d cluster"
export CLUSTER_ENGINE=k3d
startK3dCluster

# 2. Deploy Dynatrace Operator via Helm
printInfoSection "2/5  Deploying Dynatrace Operator"
dynatraceDeployOperator

# 3. Deploy ApplicationMonitoring (apponly: ActiveGate + CSI code injection, no OneAgent DaemonSet)
printInfoSection "3/5  Deploying ApplicationMonitoring (apponly)"
deployApplicationMonitoring

# 4. Deploy todo-app (registers ingress via registerApp)
printInfoSection "4/5  Deploying todo-app"
deployTodoApp

# 5. Assertions
printInfoSection "5/5  Assertions"

assertRunningPod dynatrace operator
assertRunningPod dynatrace activegate
assertRunningPod todoapp todoapp
assertRunningApp todoapp

printInfoSection "Verifying dynakube spec: applicationMonitoring"
if kubectl get dynakube -n dynatrace -o yaml 2>/dev/null | grep -q "applicationMonitoring:"; then
  printInfo "✅ Dynakube spec contains applicationMonitoring"
else
  printError "❌ Dynakube spec missing applicationMonitoring"
  kubectl get dynakube -n dynatrace -o yaml
  deleteK3dCluster
  exit 1
fi

# Cleanup
printInfoSection "Cleanup: deleting K3d cluster"
deleteK3dCluster

printInfoSection "✅ DT AppMon K3d test PASSED (arch: $ARCH)"

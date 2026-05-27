#!/bin/bash
# Integration test: Dynatrace CloudNativeFullStack (CNFS) + K3d + todo-app
#
# Self-contained: creates a fresh K3d cluster, deploys DT in CloudNativeFullStack
# mode, deploys todo-app, asserts what can be validated, then tears down.
#
# KNOWN LIMITATION — OneAgent DaemonSet:
#   K3d nodes are Docker containers. OneAgent's host init module requires access
#   to real kernel interfaces (/proc, /sys) that are unavailable inside container
#   nodes. The DaemonSet will be in CrashLoopBackOff. This is expected and the
#   test PASSES despite it.
#   On Orbital/Sysbox: additionally restricted by Sysbox host-syscall boundaries.
#   Future: CNFS with working OneAgent requires real VM nodes. Tracked for later
#   when training environments are spun directly on VMs instead of Sysbox.
#
# What IS validated: Operator running, ActiveGate running, dynakube spec with
# cloudNativeFullStack, todo-app deployed and reachable via ingress.
#
# Architecture: AMD64 and ARM64 — run independently per arch.
# DT code modules (CSI init container) differ per CPU arch; both are tested.
#
# Credentials: DT_ENVIRONMENT, DT_OPERATOR_TOKEN, DT_INGEST_TOKEN from .env
#              pointing to the COE tenant (geu80787.apps.dynatrace.com).
#
# Run: bash .devcontainer/test/integration_cnfs_k3d_todoapp.sh
source .devcontainer/util/source_framework.sh

printInfoSection "=== DT CNFS K3d integration test | arch: $ARCH ==="

_run_env=$(detectRunEnvironment)
printInfo "Environment: $_run_env | Arch: $ARCH"
if [[ "$_run_env" == "orbital" ]]; then
  printWarn "Orbital/Sysbox: OneAgent DaemonSet crash expected (host-syscall restriction + K3d container nodes)."
else
  printWarn "K3d: OneAgent DaemonSet will CrashLoopBackOff (K3d nodes are containers, not VMs)."
fi
printWarn "Assertions: Operator + ActiveGate + dynakube spec + app only. OneAgent running state skipped."

# Credentials check
assertEnvVariable DT_ENVIRONMENT
assertEnvVariable DT_OPERATOR_TOKEN
assertEnvVariable DT_INGEST_TOKEN

# Pre-test cleanup
printInfo "Pre-test cleanup: removing any existing K3d clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true

# 1. Start K3d cluster
printInfoSection "1/5  Starting K3d cluster"
export CLUSTER_ENGINE=k3d
startK3dCluster

# 2. Deploy Dynatrace Operator
printInfoSection "2/5  Deploying Dynatrace Operator"
dynatraceDeployOperator

# 3. Deploy CloudNativeFullStack
# deployCloudNative warns about K3d/Sysbox OneAgent limitations and continues.
# It waits for ActiveGate (critical component) but does NOT block on OneAgent.
printInfoSection "3/5  Deploying CloudNativeFullStack"
deployCloudNative

# 4. Deploy todo-app
printInfoSection "4/5  Deploying todo-app"
deployTodoApp

# 5. Assertions
printInfoSection "5/5  Assertions"

assertRunningPod dynatrace operator
assertRunningPod dynatrace activegate
assertRunningPod todoapp todoapp
assertRunningApp todoapp

printInfoSection "Verifying dynakube spec: cloudNativeFullStack"
if kubectl get dynakube -n dynatrace -o yaml 2>/dev/null | grep -q "cloudNativeFullStack:"; then
  printInfo "✅ Dynakube spec contains cloudNativeFullStack"
else
  printError "❌ Dynakube spec missing cloudNativeFullStack"
  kubectl get dynakube -n dynatrace -o yaml
  deleteK3dCluster
  exit 1
fi

# OneAgent DaemonSet: assert existence only — running state is NOT checked.
# CrashLoopBackOff is expected on K3d container nodes (both Orbital and local).
printInfoSection "OneAgent DaemonSet presence check (running state NOT asserted)"
if kubectl get daemonset -n dynatrace 2>/dev/null | grep -qi "oneagent"; then
  printInfo "✅ OneAgent DaemonSet exists (crash expected on K3d — asserted as known limitation)"
  kubectl get daemonset -n dynatrace
else
  printWarn "⚠️  OneAgent DaemonSet not yet visible — operator may still be reconciling"
  kubectl get all -n dynatrace
fi

# Cleanup
printInfoSection "Cleanup: deleting K3d cluster"
deleteK3dCluster

printInfoSection "✅ DT CNFS K3d test PASSED (arch: $ARCH)"
printInfo "OneAgent running assertion intentionally skipped — K3d container nodes cannot run OneAgent host module."
printInfo "Future: test CNFS on real VM nodes when training environments migrate off Sysbox."

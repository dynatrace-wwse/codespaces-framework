#!/bin/bash
# Integration test: dtwiz (dynatrace-oss/dtwiz) + K3d
#
# Validates the dtwiz-101 bootcamp training path end-to-end, platform-token-only:
# installs the dtwiz CLI, points it at a tenant with a PLATFORM token, and runs
# the same commands a learner runs — status, analyze, install kubernetes — then
# asserts the operator + DynaKube came up in a fresh K3d cluster and tears down.
#
# Why this suite exists: dtwiz uses DT_PLATFORM_TOKEN (dt0s16), NOT the classic
# DT_OPERATOR_TOKEN/DT_INGEST_TOKEN the other DT suites use, and operator >=1.10
# accepts the platform token directly in the DynaKube secret. This is the
# platform-token-native rollout the bootcamp trains on.
#
# Credentials: DT_ENVIRONMENT + DT_PLATFORM_TOKEN (platform token with, at least,
#   installer download + settings read + AG-token create — the profile the
#   Quickstart "auto discovery" token carries).
#
# Run: bash .devcontainer/test/integration_dtwiz_k3d.sh
source .devcontainer/util/source_framework.sh

printInfoSection "=== dtwiz K3d integration test | arch: $ARCH ==="

_run_env=$(detectRunEnvironment)
printInfo "Environment: $_run_env | Arch: $ARCH"

# Credentials check — fail fast before any cluster setup.
assertEnvVariable DT_ENVIRONMENT
assertEnvVariable DT_PLATFORM_TOKEN

# Pre-test cleanup: remove any clusters left from postCreate so we start clean.
printInfo "Pre-test cleanup: removing any existing K3d clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true

# 1. Fresh K3d cluster (dtwiz names the cluster from the kube context)
printInfoSection "1/5  Starting K3d cluster"
export CLUSTER_ENGINE=k3d
startK3dCluster

# 2. Install the dtwiz CLI (official installer)
printInfoSection "2/5  Installing dtwiz CLI"
source <(curl -sSL https://raw.githubusercontent.com/dynatrace-oss/dtwiz/main/scripts/install.sh)
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
if ! command -v dtwiz >/dev/null 2>&1; then
  printError "❌ dtwiz not on PATH after install"
  deleteK3dCluster; exit 1
fi
printInfo "dtwiz version: $(dtwiz --version 2>/dev/null || echo unknown)"

# 3. dtwiz connectivity — token must validate against the tenant
printInfoSection "3/5  dtwiz status (platform-token auth)"
if dtwiz status 2>&1 | tee /tmp/dtwiz_status.log | grep -qiE "Platform Token:.*valid|valid \("; then
  printInfo "✅ dtwiz authenticated to $DT_ENVIRONMENT"
else
  printError "❌ dtwiz status did not confirm a valid platform token"
  cat /tmp/dtwiz_status.log
  deleteK3dCluster; exit 1
fi
dtwiz analyze 2>&1 | head -20 || true

# 4. dtwiz install kubernetes — deploys the operator with the platform token
printInfoSection "4/5  dtwiz install kubernetes"
if ! dtwiz install kubernetes --yes 2>&1 | tail -40; then
  printWarn "dtwiz install kubernetes returned non-zero — checking cluster state anyway"
fi

# 5. Assertions — operator + DynaKube present
printInfoSection "5/5  Assertions"
assertRunningPod dynatrace operator
if kubectl get dynakube -n dynatrace >/dev/null 2>&1; then
  printInfo "✅ DynaKube created by dtwiz:"
  kubectl get dynakube -n dynatrace --no-headers
else
  printError "❌ No DynaKube after dtwiz install kubernetes"
  kubectl -n dynatrace get pods
  deleteK3dCluster; exit 1
fi

# Cleanup
printInfoSection "Cleanup: deleting K3d cluster"
deleteK3dCluster

printInfoSection "✅ dtwiz K3d test PASSED (arch: $ARCH)"

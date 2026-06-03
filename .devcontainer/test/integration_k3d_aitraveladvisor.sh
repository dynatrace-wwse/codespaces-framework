#!/bin/bash
# Integration test: AI Travel Advisor app on K3d
#
# Validates that the AI Travel Advisor stack (Ollama, Weaviate, app) deploys
# successfully on K3d and is reachable through the nginx ingress.
#
# Requires DT_LLM_TOKEN — skips gracefully when absent so CI stays green on
# workers that don't carry this credential.
#
# Architecture: AMD64 only — Ollama image is not published for ARM64.
#
# Run: bash .devcontainer/test/integration_k3d_aitraveladvisor.sh
source .devcontainer/util/source_framework.sh

if [[ "$ARCH" != "x86_64" ]]; then
  printWarn "Skipping AI Travel Advisor test — AMD64 only (arch: $ARCH)"
  exit 0
fi

if [[ -z "$DT_LLM_TOKEN" ]]; then
  printWarn "Skipping AI Travel Advisor test — DT_LLM_TOKEN not set"
  exit 0
fi

printInfoSection "=== AI Travel Advisor K3d integration test | arch: $ARCH ==="

_run_env=$(detectRunEnvironment)
printInfo "Environment: $_run_env | Arch: $ARCH"

# Pre-test cleanup
printInfo "Pre-test cleanup: removing any existing K3d clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true

# 1. Start K3d cluster
printInfoSection "1/4  Starting K3d cluster"
export CLUSTER_ENGINE=k3d
startK3dCluster

# 2. Deploy AI Travel Advisor (Ollama → Weaviate → app)
printInfoSection "2/4  Deploying AI Travel Advisor stack"
deployAITravelAdvisorApp

# 3. Assertions
printInfoSection "3/4  Assertions"

assertRunningPod ai-travel-advisor ollama
assertRunningPod ai-travel-advisor weaviate
assertRunningPod ai-travel-advisor ai-travel-advisor
assertRunningApp ai-travel-advisor

# Verify weaviate PVC uses local-path (the fix this test guards)
printInfoSection "Verifying weaviate PVC storageClassName: local-path"
SC=$(kubectl get pvc weaviate-pvc -n ai-travel-advisor -o jsonpath='{.spec.storageClassName}' 2>/dev/null)
if [[ "$SC" == "local-path" ]]; then
  printInfo "✅ weaviate PVC storageClassName = local-path"
else
  printError "❌ weaviate PVC storageClassName = '${SC}' (expected local-path)"
  deleteK3dCluster
  exit 1
fi

# 4. Cleanup
printInfoSection "4/4  Cleanup: deleting K3d cluster"
deleteK3dCluster

printInfoSection "✅ AI Travel Advisor K3d test PASSED (arch: $ARCH)"

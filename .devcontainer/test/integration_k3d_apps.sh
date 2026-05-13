#!/bin/bash
# K3d multi-app integration test — AMD64 only.
#
# Validates that each app deploys successfully on k3d and is reachable
# through the nginx ingress. Runs sequentially — each app is deployed into
# a fresh k3d cluster, verified, then torn down.
#
# Skip list: apps that only support amd64 already guard themselves, but
# we also skip here because this test file is exclusively for AMD64 CI.
#
# Run: bash .devcontainer/test/integration_k3d_apps.sh
source .devcontainer/util/source_framework.sh

if [[ "$ARCH" != "x86_64" ]]; then
  printWarn "Skipping k3d apps integration test — AMD64 only (arch: $ARCH)"
  exit 0
fi

PASSED=0
FAILED=0

run_app_test() {
  local app_name="$1"
  local deploy_fn="$2"
  local assert_name="${3:-$app_name}"

  printInfoSection "Testing app: $app_name"
  export CLUSTER_ENGINE=k3d
  startK3dCluster

  if "$deploy_fn" 2>&1; then
    if assertRunningApp "$assert_name" 2>&1; then
      printInfo "✅ $app_name — deployed and reachable"
      PASSED=$((PASSED + 1))
    else
      printError "❌ $app_name — deployed but NOT reachable via ingress"
      FAILED=$((FAILED + 1))
    fi
  else
    printError "❌ $app_name — deployment FAILED"
    FAILED=$((FAILED + 1))
  fi

  deleteK3dCluster
}

# ---------------------------------------------------------------------------
# 1. Todo App — lightweight test app, always available
# ---------------------------------------------------------------------------
run_app_test "todoapp" deployTodoApp "todoapp"

# ---------------------------------------------------------------------------
# 2. Astroshop (AMD64 only — guarded inside deployAstroshop)
# ---------------------------------------------------------------------------
run_app_test "astroshop" deployAstroshop "astroshop"

# ---------------------------------------------------------------------------
# 3. Astronomy Shop (OTel demo — used by k8s-otel labs)
# ---------------------------------------------------------------------------
run_app_test "astronomy-shop" deployAstronomyShopOpenTelemetry "astronomy-shop"

# ---------------------------------------------------------------------------
# 4. AI Travel Advisor (gen-ai lab)
# ---------------------------------------------------------------------------
run_app_test "aitraveladvisor" deployAITravelAdvisorApp "aitraveladvisor"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printInfoSection "K3d apps integration test — results"
printInfo "Passed: $PASSED"
if [[ "$FAILED" -gt 0 ]]; then
  printError "Failed: $FAILED"
  exit 1
else
  printInfo "✅ All $PASSED app(s) passed on k3d"
fi

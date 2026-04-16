#!/bin/bash
# Load framework
source .devcontainer/util/source_framework.sh

printInfoSection "Running integration Tests for $RepositoryName"

# --- Kubernetes Cluster ---
assertRunningPod kube-system coredns

# --- Ingress Controller (when not using legacy ports) ---
if [[ "$USE_LEGACY_PORTS" != "true" ]]; then
  printInfoSection "Verifying Ingress Controller"
  assertRunningPod ingress-nginx controller
fi

# --- Dynatrace Components (if deployed) ---
if kubectl get ns dynatrace &>/dev/null; then
  printInfoSection "Verifying Dynatrace Components"
  assertRunningPod dynatrace operator
  assertRunningPod dynatrace activegate
  assertRunningPod dynatrace oneagent
fi

# --- Applications ---
if kubectl get ns todoapp &>/dev/null; then
  printInfoSection "Verifying TodoApp"
  assertRunningPod todoapp todoapp

  if [[ "$USE_LEGACY_PORTS" == "true" ]]; then
    assertRunningApp 30100
  else
    # Verify ingress route exists
    printInfo "Checking TodoApp ingress..."
    kubectl get ingress -n todoapp todoapp-ingress
    if [[ $? -eq 0 ]]; then
      printInfo "✅ TodoApp ingress route exists"
    else
      printError "❌ TodoApp ingress route missing"
      exit 1
    fi

    # Verify app is accessible via ingress (from inside Kind node)
    local detected_ip
    detected_ip=$(detectIP)
    local ingress_host="todoapp.${detected_ip}.${MAGIC_DOMAIN}"
    printInfo "Testing TodoApp via ingress host: $ingress_host"
    assertRunningHttp 80
  fi
fi

# --- App Registry ---
if [[ "$USE_LEGACY_PORTS" != "true" ]] && [[ -f "$APP_REGISTRY" ]]; then
  printInfoSection "Verifying App Registry"
  local app_count
  app_count=$(wc -l < "$APP_REGISTRY")
  printInfo "✅ $app_count app(s) registered in $APP_REGISTRY"
fi

printInfoSection "Integration tests completed for $RepositoryName"

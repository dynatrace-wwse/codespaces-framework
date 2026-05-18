#!/bin/bash
# Kind + Astroshop integration test.
#
# Starts a Kind cluster, deploys Astroshop, and validates that:
#   - The frontend is reachable via nginx ingress
#   - The root path serves valid HTML with expected shop content
#   - Static assets (images, CSS, JS) load with HTTP 200/304
#
# Guards:
#   - AMD64 only: Astroshop images are x86 only
#   - Skipped on Orbital/Sysbox: pod nesting depth exceeds what Sysbox can virtualise
#
# Run: bash .devcontainer/test/integration_kind_astroshop.sh
source .devcontainer/util/source_framework.sh

if [[ "$ARCH" != "x86_64" ]]; then
  printWarn "Skipping — Astroshop is AMD64 only (arch: $ARCH)"
  exit 0
fi

_run_env=$(detectRunEnvironment)
if [[ "$_run_env" == "orbital" ]]; then
  printWarn "Skipping — Kind pods stuck ContainerCreating on Orbital/Sysbox"
  printWarn "Use integration_k3d_apps.sh for Astroshop testing on Orbital"
  exit 0
fi

# Remove any pre-existing kind clusters so port bindings don't conflict
printInfo "Pre-test cleanup: removing existing Kind clusters..."
kind get clusters 2>/dev/null | xargs -r -I{} kind delete cluster --name {} 2>/dev/null || true

# -------------------------------------------------------------------
printInfoSection "1/4  Starting Kind cluster"
export CLUSTER_ENGINE=kind
startKindCluster

# -------------------------------------------------------------------
printInfoSection "2/4  Deploying Astroshop"
deployAstroshop

# -------------------------------------------------------------------
printInfoSection "3/4  Asserting ingress reachability"
assertRunningApp astroshop

# -------------------------------------------------------------------
printInfoSection "4/4  Asserting HTML and static asset content"
assertAstroshopContent

# -------------------------------------------------------------------
printInfoSection "Cleaning up Kind cluster"
deleteKindCluster

printInfoSection "✅  Astroshop on Kind: HTML and assets validated"

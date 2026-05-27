#!/bin/bash
# TEST BRANCH: minimal post-create for wildcard-subdomain astroshop verification.
# Brings up k3d + astroshop only (no DT Operator, no OneAgent).
# This post-create is intentionally stripped down for the fix/wildcard-subdomain-app-routing branch.
export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal
startK3dCluster
installK9s

if [[ "$ARCH" != "x86_64" ]]; then
  printWarn "Astroshop only supports x86_64 — skipping deployment"
  finalizePostCreation
  exit 0
fi

printInfoSection "Deploying Astroshop (wildcard subdomain test — no DT components)"

kubectl create ns astroshop 2>/dev/null || true
kubectl apply -n astroshop -f "$FRAMEWORK_APPS_PATH/astroshop/yaml/astroshop-deployment.yaml"
kubectl -n astroshop create secret generic dt-credentials \
  --from-literal="DT_API_TOKEN=test-only" \
  --from-literal="DT_ENDPOINT=http://localhost:4318" 2>/dev/null || true

waitForAllReadyPods "astroshop"
registerAstroshopIngress "astroshop"

finalizePostCreation
printInfoSection "Astroshop ready — verify images at: $(getAppURL astroshop)images/products/LensCleaningKit.jpg"

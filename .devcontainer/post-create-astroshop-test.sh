#!/bin/bash
# Minimal post-create for wildcard-subdomain testing.
# Brings up only k3d + astroshop (no DT Operator, no OneAgent).
# Verifies that images load correctly via the subdomain URL.
# Usage: triggered automatically when the framework spins a daemon job
#        pointing to this file as the post-create script.

export SECONDS=0
source .devcontainer/util/source_framework.sh

setUpTerminal
startK3dCluster

printInfoSection "Deploying Astroshop (test — no DT components)"

# Astroshop requires x86_64; skip gracefully on ARM
if [[ "$ARCH" != "x86_64" ]]; then
  printWarn "Astroshop only supports x86_64 — skipping"
  finalizePostCreation
  exit 0
fi

kubectl create ns astroshop 2>/dev/null || true
kubectl apply -n astroshop -f "$FRAMEWORK_APPS_PATH/astroshop/yaml/astroshop-deployment.yaml"

# Skip DT credentials — no operator/ingest token needed for this test
kubectl -n astroshop create secret generic dt-credentials \
  --from-literal="DT_API_TOKEN=dummy" \
  --from-literal="DT_ENDPOINT=http://localhost:4318" 2>/dev/null || true

waitForAllReadyPods "astroshop"
registerAstroshopIngress "astroshop"

printInfoSection "Astroshop test deployment complete"
printInfo "App URL: $(getAppURL astroshop)"
printInfo "Verify images: $(getAppURL astroshop)images/products/LensCleaningKit.jpg"

finalizePostCreation

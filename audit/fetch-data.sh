#!/bin/bash
# Fetch audit data for all repos in repos.yaml
set -euo pipefail

ORG="dynatrace-wwse"
OUTPUT_DIR="/home/ubuntu/enablement-framework/codespaces-framework/audit/data"
mkdir -p "$OUTPUT_DIR"

REPOS=(
  enablement-kubernetes-opentelemetry
  enablement-gen-ai-llm-observability
  enablement-business-observability
  enablement-dql-301
  enablement-dynatrace-log-ingest-101
  enablement-browser-dem-biz-observability
  enablement-live-debugger-bug-hunting
  enablement-workflow-essentials
  enablement-azure-webapp-otel
  enablement-codespaces-template
  enablement-openpipeline-segments-iam
  enablement-kubernetes-opentelemetry-openpipeline
  enablement-dql-fundamentals
  workshop-dynatrace-log-analytics
  workshop-destination-automation
  demo-agentic-ai-with-nvidia
  demo-mcp-unguard
  demo-opentelemetry
  demo-astroshop-runtime-optimization
  demo-astroshop-problems
  ace-integration
  bizobs-journey-simulator
  bug-busters
  remote-environment
  codespaces-framework
  dynatrace-wwse.github.io
  codespaces-tracker
)

for REPO in "${REPOS[@]}"; do
  echo "=== Fetching $REPO ==="
  RDIR="$OUTPUT_DIR/$REPO"
  mkdir -p "$RDIR"

  # 1. Repo metadata
  gh api "repos/$ORG/$REPO" --jq '{
    created_at: .created_at,
    fork: .fork,
    default_branch: .default_branch,
    archived: .archived,
    topics: .topics
  }' > "$RDIR/meta.json" 2>/dev/null || echo '{"error":"not found"}' > "$RDIR/meta.json"

  # 2. Contributors count
  CONTRIB_COUNT=$(gh api "repos/$ORG/$REPO/contributors" --jq 'length' 2>/dev/null || echo "0")
  echo "$CONTRIB_COUNT" > "$RDIR/contributors.txt"

  # 3. devcontainer.json - try .devcontainer/devcontainer.json first, then .devcontainer.json
  DEVCONTAINER=""
  RAW=$(gh api "repos/$ORG/$REPO/contents/.devcontainer/devcontainer.json" 2>/dev/null || true)
  if echo "$RAW" | jq -e '.content' >/dev/null 2>&1; then
    DEVCONTAINER=$(echo "$RAW" | jq -r '.content' | tr -d '\n' | base64 -d 2>/dev/null || true)
  fi
  if [ -z "$DEVCONTAINER" ]; then
    RAW=$(gh api "repos/$ORG/$REPO/contents/.devcontainer.json" 2>/dev/null || true)
    if echo "$RAW" | jq -e '.content' >/dev/null 2>&1; then
      DEVCONTAINER=$(echo "$RAW" | jq -r '.content' | tr -d '\n' | base64 -d 2>/dev/null || true)
    fi
  fi
  if [ -n "$DEVCONTAINER" ]; then
    echo "$DEVCONTAINER" > "$RDIR/devcontainer.json"
  else
    echo "" > "$RDIR/devcontainer.json"
  fi

  # 4. myFunctions.sh
  MYFUNC_RAW=$(gh api "repos/$ORG/$REPO/contents/.devcontainer/myFunctions.sh" 2>/dev/null || true)
  if echo "$MYFUNC_RAW" | jq -e '.content' >/dev/null 2>&1; then
    echo "$MYFUNC_RAW" | jq -r '.content' | tr -d '\n' | base64 -d > "$RDIR/myFunctions.sh" 2>/dev/null || echo "" > "$RDIR/myFunctions.sh"
  else
    echo "" > "$RDIR/myFunctions.sh"
  fi

  # 5. README.md
  README_RAW=$(gh api "repos/$ORG/$REPO/contents/README.md" 2>/dev/null || true)
  if echo "$README_RAW" | jq -e '.content' >/dev/null 2>&1; then
    echo "$README_RAW" | jq -r '.content' | tr -d '\n' | base64 -d > "$RDIR/README.md" 2>/dev/null || echo "" > "$RDIR/README.md"
  else
    echo "" > "$RDIR/README.md"
  fi

  # 6. .ramconfig
  RAMCONFIG_RAW=$(gh api "repos/$ORG/$REPO/contents/.ramconfig" 2>/dev/null || true)
  if echo "$RAMCONFIG_RAW" | jq -e '.content' >/dev/null 2>&1; then
    echo "$RAMCONFIG_RAW" | jq -r '.content' | tr -d '\n' | base64 -d > "$RDIR/ramconfig" 2>/dev/null || echo "" > "$RDIR/ramconfig"
  else
    echo "" > "$RDIR/ramconfig"
  fi

  # Also check .ramconfig.json and .ramconfig.yaml
  for EXT in json yaml yml; do
    RC_RAW=$(gh api "repos/$ORG/$REPO/contents/.ramconfig.$EXT" 2>/dev/null || true)
    if echo "$RC_RAW" | jq -e '.content' >/dev/null 2>&1; then
      echo "$RC_RAW" | jq -r '.content' | tr -d '\n' | base64 -d > "$RDIR/ramconfig.$EXT" 2>/dev/null || true
    fi
  done

  echo "Done: $REPO"
done

echo "=== All repos fetched ==="

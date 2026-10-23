#!/bin/bash
# Integration test: standalone OpenTelemetry Collector (deployOtelCollector) + K3d,
# with data proven to ARRIVE in the tenant, not only a pod that runs.
#
# Self-contained: fresh K3d cluster, deployOtelCollector (no Dynatrace Operator —
# the collector must not need one), then a throwaway pod sends one OTLP/HTTP log
# and one span carrying a unique marker to the collector's Service, and Grail is
# asked for that marker. Tears the cluster down at the end.
#
# Architecture: AMD64 and ARM64 (the Dynatrace collector image is multi-arch).
# Environment: Orbital (Sysbox + K3d), Codespaces, or local.
# Credentials: DT_ENVIRONMENT + DT_INGEST_TOKEN (the collector's export), and a
#              platform token to read Grail: DT_PLATFORM_TOKEN if the environment
#              has one, otherwise minted for this run (mint_platform_token.sh) and
#              revoked at exit.
#
# Run: bash .devcontainer/test/integration_k3d_otel_collector.sh
source .devcontainer/util/source_framework.sh
source .devcontainer/test/mint_platform_token.sh

printInfoSection "=== OTel Collector K3d integration test | arch: $ARCH ==="

assertEnvVariable DT_ENVIRONMENT
assertEnvVariable DT_INGEST_TOKEN

trap 'revokeMintedPlatformToken' EXIT INT TERM
mintPlatformTokenForTest storage:logs:read storage:spans:read storage:buckets:read || exit 1

_dql() {
  # _dql <query> — records as JSON lines. The token goes in a header file
  # descriptor, never in argv.
  local body resp token tries=0
  body=$(jq -nc --arg q "$1" '{query: $q, requestTimeoutMilliseconds: 30000}')
  resp=$(curl -sS -m 40 -X POST "${DT_ENVIRONMENT%/}/platform/storage/query/v1/query:execute" \
    -H @<(printf 'Authorization: Bearer %s\n' "$DT_PLATFORM_TOKEN") -H 'Content-Type: application/json' -d "$body")
  while [ "$(printf '%s' "$resp" | jq -r '.state // empty')" = RUNNING ] && [ "$tries" -lt 30 ]; do
    token=$(printf '%s' "$resp" | jq -r '.requestToken')
    tries=$((tries + 1)); sleep 1
    resp=$(curl -sS -m 40 -G "${DT_ENVIRONMENT%/}/platform/storage/query/v1/query:poll" \
      --data-urlencode "request-token=$token" -H @<(printf 'Authorization: Bearer %s\n' "$DT_PLATFORM_TOKEN"))
  done
  printf '%s' "$resp" | jq -c '.result.records[]?'
}

_fail() { printError "❌ $1"; deleteK3dCluster; exit 1; }

printInfo "Pre-test cleanup: removing any existing K3d clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true

printInfoSection "1/4  Starting K3d cluster"
export CLUSTER_ENGINE=k3d
startK3dCluster || { printError "❌ K3d cluster did not come up"; exit 1; }

printInfoSection "2/4  deployOtelCollector"
deployOtelCollector || _fail "deployOtelCollector failed"
assertRunningPod "$OTEL_COLLECTOR_NAMESPACE" otel-collector

printInfoSection "3/4  Sending one log and one span with a unique marker"
MARKER="otelcol-it-$(date +%s)-$RANDOM"
NOW_NS="$(date +%s)000000000"
TRACE_ID=$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')
SPAN_ID=$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')
SVC="http://otel-collector.${OTEL_COLLECTOR_NAMESPACE}.svc.cluster.local:4318"
LOG_JSON=$(jq -nc --arg m "$MARKER" --arg t "$NOW_NS" \
  '{resourceLogs:[{resource:{attributes:[{key:"service.name",value:{stringValue:"otelcol-integration"}}]},
    scopeLogs:[{logRecords:[{timeUnixNano:$t,severityText:"INFO",body:{stringValue:("deployOtelCollector integration " + $m)}}]}]}]}')
SPAN_JSON=$(jq -nc --arg m "$MARKER" --arg t "$NOW_NS" --arg tr "$TRACE_ID" --arg sp "$SPAN_ID" \
  '{resourceSpans:[{resource:{attributes:[{key:"service.name",value:{stringValue:"otelcol-integration"}}]},
    scopeSpans:[{spans:[{traceId:$tr,spanId:$sp,name:$m,kind:1,startTimeUnixNano:$t,endTimeUnixNano:$t}]}]}]}')
kubectl -n "$OTEL_COLLECTOR_NAMESPACE" run otlp-sender --rm -i --restart=Never --image=curlimages/curl:8.11.1 -- \
  sh -c "curl -sf -o /dev/null -w 'logs %{http_code}\n' -H 'Content-Type: application/json' -d '$LOG_JSON' $SVC/v1/logs &&
         curl -sf -o /dev/null -w 'traces %{http_code}\n' -H 'Content-Type: application/json' -d '$SPAN_JSON' $SVC/v1/traces" \
  || _fail "the collector did not accept OTLP/HTTP"

printInfoSection "4/4  Asking Grail for marker $MARKER"
found_log="" found_span=""
for i in $(seq 1 24); do
  [ -z "$found_log" ] && found_log=$(_dql "fetch logs, from:now()-30m | filter contains(content, \"$MARKER\") | limit 1")
  [ -z "$found_span" ] && found_span=$(_dql "fetch spans, from:now()-30m | filter span.name == \"$MARKER\" | limit 1")
  [ -n "$found_log" ] && [ -n "$found_span" ] && break
  sleep 10
done
if kubectl -n "$OTEL_COLLECTOR_NAMESPACE" logs deploy/otel-collector 2>/dev/null | grep -q -i "exporting failed\|permanent error"; then
  kubectl -n "$OTEL_COLLECTOR_NAMESPACE" logs deploy/otel-collector --tail=20 | grep -v -i "authorization"
  _fail "the collector reported export errors"
fi
[ -n "$found_log" ] || _fail "log with marker $MARKER never arrived in Grail"
[ -n "$found_span" ] || _fail "span $MARKER never arrived in Grail"
printInfo "✅ log and span with marker $MARKER are in Grail"

printInfoSection "Cleanup: deleting K3d cluster"
deleteK3dCluster
printInfoSection "✅ OTel Collector K3d test PASSED (arch: $ARCH)"

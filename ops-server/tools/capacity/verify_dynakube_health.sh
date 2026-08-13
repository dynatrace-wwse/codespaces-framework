#!/bin/bash
# Verify each session from the DynaKube's OWN status. jsonpath only — no nested
# python heredoc, which is what mangled the phase extraction on the first pass.
#
# Why not pod counting: logMonitoring is scheduled LATE by design, so its
# absence at any single moment proves nothing, and waitForAllReadyPods only
# waits on pods that already EXIST. The DynaKube reports its phase and raises
# its own errors in .status.conditions — that is the authoritative signal.
set -u
OUT=${1:-/tmp/vdk2}
sudo rm -rf "$OUT"; mkdir -p "$OUT"

for C in $(sudo docker ps --format '{{.Names}}' | grep sb-slot | sort); do
  sudo docker exec "$C" docker exec dt test -d /workspaces/enablement-kubernetes-101 2>/dev/null || continue
(
  phase=$(sudo docker exec "$C" docker exec dt kubectl get dynakube -n dynatrace \
            -o jsonpath='{.items[0].status.phase}' 2>/dev/null)
  # Every condition as "Type=Status"; anything not "=True" is a problem.
  conds=$(sudo docker exec "$C" docker exec dt kubectl get dynakube -n dynatrace \
            -o jsonpath='{range .items[0].status.conditions[*]}{.type}={.status}:{.reason} {end}' 2>/dev/null)
  ncond=$(echo "$conds" | tr ' ' '\n' | grep -c '=')
  bad=$(echo "$conds" | tr ' ' '\n' | grep '=' | grep -v '=True:' | tr '\n' ',')
  pods=$(sudo docker exec "$C" docker exec dt bash -lc \
            'kubectl get pods -n dynatrace --no-headers 2>/dev/null | awk "{print \$1, \$2, \$3}"' 2>/dev/null)
  echo "$pods" > "$OUT/$C.pods"
  echo "$conds" > "$OUT/$C.conds"
  total=$(echo "$pods" | grep -c .)
  ready=$(echo "$pods" | awk '{split($2,r,"/"); if (r[1]==r[2] && r[1]!="0" && ($3=="Running"||$3=="Completed")) n++} END{print n+0}')
  logmon=$(echo "$pods" | grep -c logmonitoring)

  verdict=HEALTHY
  [ "$phase" != "Running" ] && verdict=PHASE_NOT_RUNNING
  [ -n "$bad" ] && verdict=CONDITION_FAILED
  [ "$ready" != "$total" ] && verdict=PODS_NOT_READY
  [ "$ncond" -eq 0 ] && verdict=NO_CONDITIONS
  echo "$C $verdict phase=${phase:-none} conds_ok=$((ncond - $(echo "$bad" | tr ',' '\n' | grep -c '='))) /$ncond ready=${ready}/${total} logmon=${logmon} bad=${bad:--}" >> "$OUT/summary.txt"
) &
done
wait
echo "--- verdict:"; awk '{print $2}' "$OUT/summary.txt" | sort | uniq -c | sort -rn
echo "--- phase:";   grep -oE 'phase=[A-Za-z]+' "$OUT/summary.txt" | sort | uniq -c
echo "--- logMonitoring pod present:"; grep -oE 'logmon=[0-9]+' "$OUT/summary.txt" | sort | uniq -c
echo "--- failing conditions:"; grep -oE 'bad=.*$' "$OUT/summary.txt" | sed 's/bad=//' | sort | uniq -c | sort -rn | head
echo "--- key conditions across all sessions:"
cat "$OUT"/*.conds 2>/dev/null | tr ' ' '\n' | grep -E '^(ActiveGateStatefulSet|OtelStatefulSet|LogMonitoringDaemonSet|LogMonitoringSettings|Tokens)=' \
  | sort | uniq -c | sort -rn

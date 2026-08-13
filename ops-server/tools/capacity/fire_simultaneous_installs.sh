#!/bin/bash
# Fire the k8s-101 operator install in every OCCUPIED slot at the same instant.
#
# Simultaneity is the measurement. The failure mode we are looking for is not
# "the install is slow" — it is that N installs contending for the same disk
# each miss the framework's own 600s waitForAllPods gate, which a learner
# experiences as "Run solution" failing. Staggering the starts would hide
# exactly that, so every slot is launched from one loop with no sleep.
#
# Unlike the 30-slot run, this selects only slots that actually hold a session
# (the repo is present): a warm-but-empty slot has nothing to install and would
# return instantly, inflating the pass rate.
set -u
OUT=${1:-/tmp/labN}
sudo rm -rf "$OUT"; mkdir -p "$OUT"

ALL=$(sudo docker ps --format '{{.Names}}' | grep sb-slot | sort)
CONTAINERS=""
for C in $ALL; do
  if sudo docker exec "$C" docker exec dt test -d /workspaces/enablement-kubernetes-101 2>/dev/null; then
    CONTAINERS="$CONTAINERS $C"
  fi
done
N=$(echo $CONTAINERS | wc -w)
echo "launching operator install in $N occupied slots at $(date +%H:%M:%S)"
echo "$N" > "$OUT/n"

for C in $CONTAINERS; do
(
  start=$(date +%s)
  sudo docker exec "$C" docker exec dt bash -lc '
    cd /workspaces/enablement-kubernetes-101 || exit 90
    source .devcontainer/util/source_framework.sh >/dev/null 2>&1
    dynatraceDeployOperator && deployApplicationMonitoring
  ' > "$OUT/$C.log" 2>&1
  rc=$?
  end=$(date +%s)
  echo "$C rc=$rc secs=$((end-start))" >> "$OUT/results.txt"
) &
done
wait
echo "all finished at $(date +%H:%M:%S)"
sort "$OUT/results.txt"
awk '{split($3,a,"="); s+=a[2]; if(a[2]>m)m=a[2]; n++; if($2=="rc=0")p++}
     END{printf "PASS %d/%d  mean %.0fs  max %.0fs\n", p, n, s/n, m}' "$OUT/results.txt"

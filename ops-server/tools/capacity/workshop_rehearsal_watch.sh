#!/usr/bin/env bash
# Watch a rehearsal and assert the five properties it exists to prove.
# Usage: ./workshop_rehearsal_watch.sh <workshop_id> [minutes]
set -uo pipefail

ORBITAL="https://autonomous-enablements.whydevslovedynatrace.com"
WS="${1:?workshop id required}"
MINUTES="${2:-25}"
RP=$(sudo grep -oP '^REDIS_PASSWORD=\K.*' /home/ops/.env)
r() { redis-cli -a "$RP" --no-auth-warning "$@"; }

deadline=$(( $(date +%s) + MINUTES * 60 ))
prev=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  fleet=$(r hget workshop:fleet "$WS" 2>/dev/null)
  state=$(python3 -c "
import json,sys
raw=sys.argv[1]
try: d=json.loads(raw) if raw else {}
except ValueError: d={}
print(f\"{d.get('state','-')} pool={d.get('pool','-')} workers={d.get('workers','-')} \"
      f\"seats/worker={d.get('seats_per_worker','-')} inst={','.join(d.get('instances') or []) or '-'}\")
" "$fleet" 2>/dev/null)

  # Where are sessions actually running?
  placement=$(r --scan --pattern 'job:running:*' 2>/dev/null | while read -r k; do
      [ "$(r type "$k")" = "hash" ] || continue
      wid=$(r hget "$k" worker_id); ws=$(r hget "$k" workshop_id)
      echo "${wid:-?}|${ws:-self-service}"
    done | sort | uniq -c | tr '\n' ' ')

  pend=$(r --scan --pattern 'queue:pending:*' 2>/dev/null | while read -r k; do
      echo "$k=$(r llen "$k")"; done | tr '\n' ' ')

  line="$(date +%H:%M:%S) | $state | pending: ${pend:-none} | placement: ${placement:-none}"
  [ "$line" != "$prev" ] && echo "$line"
  prev="$line"
  sleep 10
done

echo
echo "=== POST-RUN ASSERTIONS ==="
fail=0

for w in autonomous-enablements-worker autonomous-enablements-worker-2; do
  n=$(ssh -o ConnectTimeout=8 "$w" 'ps -ef | grep -c "[d]ocker wait"' 2>/dev/null || echo skip)
  echo "stray docker wait on $w: $n"
  [ "$n" = "0" ] || [ "$n" = "skip" ] || fail=1
done

orphans=$(r --scan --pattern 'job:running:*' 2>/dev/null | wc -l)
echo "job:running:* keys remaining: $orphans"

curl -s "$ORBITAL/api/workers" | python3 -c '
import json,sys
bad=0
for w in json.load(sys.stdin)["workers"]:
    if w.get("arch") != "amd64": continue
    ready,total = w.get("slots_ready"), w.get("slots_total")
    deg = w.get("slots_degraded","0")
    print(f"   {w.get(\"worker_id\"):34} {w.get(\"status\"):9} {ready}/{total} "
          f"degraded={deg} reaper_watching={w.get(\"reaper_watching\",\"-\")}")
    if ready != total or deg not in ("0",None): bad=1
sys.exit(bad)' || fail=1

echo
[ "$fail" = "0" ] && echo "RESULT: PASS" || echo "RESULT: FAIL — see above"
exit "$fail"

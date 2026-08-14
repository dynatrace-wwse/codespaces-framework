#!/usr/bin/env bash
# Unattended end-to-end rehearsal of a scheduled workshop.
#
# Proves, in one run and without a human touching anything after the start:
#
#   1. A workshop scheduled for T gets its OWN machines launched automatically
#      at T minus the prewarm lead, by the control loop, from a cold fleet.
#   2. Those machines boot the CURRENT agent code, not what the AMI was baked
#      with — verified from the code_ref they publish on their heartbeat.
#   3. "Provision all" admits learners in PHASES rather than in one burst.
#   4. A self-service session started DURING the workshop lands on the daily
#      worker and never on the workshop's machines.
#   5. When the workshop ends, sessions are torn down, the machines are
#      terminated, and the worker that stayed behind is left healthy — no
#      stray docker waits, no orphaned job keys, no short pool.
#
# Assertion 5 is the one with a known bug behind it: a mass teardown has twice
# wedged Docker's container reaping and left a worker advertising zero free
# slots while holding thirty healthy ones. Running the teardown at the end of a
# rehearsal, deliberately, is how that stops being a surprise on the day.
#
# Usage:  ./workshop_rehearsal.sh [roster_size]
set -uo pipefail

ORBITAL="https://autonomous-enablements.whydevslovedynatrace.com"
TENANT="${TENANT:-https://sro97894.apps.dynatrace.com}"
TRAINER="${TRAINER:-rehearsal-trainer@dynatrace.com}"
ROSTER_SIZE="${1:-8}"
OUT="${OUT:-/tmp/workshop-rehearsal-$(date +%H%M%S)}"
mkdir -p "$OUT"

RP=$(sudo grep -oP '^REDIS_PASSWORD=\K.*' /home/ops/.env)
r() { redis-cli -a "$RP" --no-auth-warning "$@"; }

say() { printf '\n\033[1m== %s\033[0m\n' "$*" | tee -a "$OUT/log"; }
note() { printf '   %s\n' "$*" | tee -a "$OUT/log"; }

# ── 1. state of the fleet before we start ───────────────────────────────────
say "Fleet before"
curl -s "$ORBITAL/api/workers" | python3 -c '
import json,sys
for w in json.load(sys.stdin)["workers"]:
    if w.get("arch") == "amd64" or w.get("role") == "master":
        print(f"   {w.get(\"worker_id\"):34} pool={w.get(\"pool\",\"?\"):18} "
              f"{w.get(\"status\"):9} {w.get(\"slots_ready\")}/{w.get(\"slots_total\")} "
              f"code={w.get(\"code_ref\") or \"(unstamped)\"}")' | tee -a "$OUT/log"

# ── 2. create the workshop, scheduled a few minutes out ─────────────────────
say "Creating workshop"
ROSTER=$(python3 -c "
import json,sys
n=int(sys.argv[1])
print(json.dumps([f'bot{i:02d}@rehearsal.invalid' for i in range(n)]))" "$ROSTER_SIZE")

START=$(python3 -c "
from datetime import datetime,timedelta,timezone
print((datetime.now(timezone.utc)+timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%S.000Z'))")

CREATE=$(curl -s -X POST "$ORBITAL/api/live/sessions" \
  -H 'Content-Type: application/json' \
  -d "{\"title\":\"REHEARSAL $(date +%H:%M)\",
       \"trainingId\":\"kubernetes-101\",
       \"trainerEmail\":\"$TRAINER\",
       \"tenant\":\"$TENANT\",
       \"scheduledAt\":\"$START\",
       \"durationMinutes\":25,
       \"roster\":$ROSTER}")
echo "$CREATE" > "$OUT/create.json"
WS=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('sessionId',''))" "$OUT/create.json")

if [ -z "$WS" ]; then
  note "FAILED to create workshop:"; cat "$OUT/create.json"; exit 1
fi
note "workshop   $WS"
note "starts at  $START  (roster $ROSTER_SIZE)"
note "output     $OUT"
echo "$WS" > "$OUT/workshop_id"

# ── 3. bots join ────────────────────────────────────────────────────────────
# Not optional. provision-all only provisions learners whose recorded join
# tenant matches the trainer's; anyone who never joined is reported
# "not-joined — will provision on entry" and NO job is queued. A rehearsal that
# skips this measures nothing and looks like a silent scheduling failure.
say "Bots joining"
joined=0
for i in $(seq 0 $((ROSTER_SIZE - 1))); do
  email=$(printf 'bot%02d@rehearsal.invalid' "$i")
  resp=$(curl -s -X POST "$ORBITAL/api/live/sessions/$WS/join" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$email\",\"tenant\":\"$TENANT\"}")
  echo "$resp" >> "$OUT/joins.jsonl"
  case "$resp" in *'"joined"'*|*'"ok"'*|*joinedAt*) joined=$((joined+1)) ;; esac
done
note "joined $joined/$ROSTER_SIZE"
[ "$joined" = "$ROSTER_SIZE" ] || note "WARNING: not everyone joined — provision-all will skip the rest"

cat <<EOF | tee -a "$OUT/log"

   Next: point the control loop at THIS workshop only, and let it run.
   Add to /home/ops/.env, then restart ops-dashboard:

     CONTROL_LOOP_APPLY=1
     CONTROL_LOOP_WORKSHOPS=$WS
     PREWARM_LEAD_MINUTES=3
     TEARDOWN_GRACE_MINUTES=1
     CONTROL_TICK_S=20
     WORKER_CODE_BRANCH=epic/workshop-pools-and-autoscale

   Then watch:  ./workshop_rehearsal_watch.sh $WS
EOF

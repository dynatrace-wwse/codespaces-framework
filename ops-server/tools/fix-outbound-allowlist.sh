#!/usr/bin/env bash
# Diagnose — and optionally repair — the JS-runtime outbound allowlist on a tenant.
#
#   ./fix-outbound-allowlist.sh <client-id> <client-secret> <account-urn> <tenant-url> [--apply|--create]
#
# Read-only by default.
#   --apply   add missing hosts to a list that is ALREADY enforced. Safe: it only
#             ever widens an existing restriction.
#   --create  write a list where none exists. Requires that you have SEEN the app
#             report a block (the script prints the exact curl). Absent that proof
#             this would enforce a restriction on a tenant that was open — causing
#             the very outage it exists to clear.
#
# Why this exists: on 2026-08-19 twelve tenants were recorded as "outbound open"
# purely because no settings object existed, and eight never provisioned anything.
# That inference is false, measured twice: `jxh41488` blocked sso.dynatrace.com,
# and `uxn36332` blocked autonomous-enablements… with no object at ANY scope. The
# only instrument that answers is the app itself, from inside the runtime.
#
# Exit 0 = nothing to do, or repaired. Exit 1 = action needed.

set -uo pipefail
CID="${1:?client id}"; CSEC="${2:?client secret}"; ACCT="${3:?urn:dtaccount:...}"
TURL="${4:?https://<env>.apps.dynatrace.com}"; MODE="${5:-}"

ENVID=$(sed -E 's#https?://([^.]+)\..*#\1#' <<<"$TURL")
APPS="${TURL%/}"
ENVURN="urn:dtenvironment:${ENVID}"
PROXY="${APPS}/platform/classic/environment-api"
SCHEMA="builtin:dt-javascript-runtime.allowed-outbound-connections"
case "$APPS" in
  *sprint*) SSO="https://sso-sprint.dynatracelabs.com/sso/oauth2/token"
            EXTRA="sso-sprint.dynatracelabs.com api-hardening.internal.dynatracelabs.com" ;;
  *dev*)    SSO="https://sso-dev.dynatracelabs.com/sso/oauth2/token"; EXTRA="" ;;
  *)        SSO="https://sso.dynatrace.com/sso/oauth2/token"
            EXTRA="sso.dynatrace.com api.dynatrace.com" ;;
esac
# Must stay in step with OUTBOUND_HOSTS in ops-server/dashboard/app_deploy.py and
# REQUIRED_HOSTS in dynatrace-app-enablements/api/selfTest.function.ts.
NEEDED="autonomous-enablements.whydevslovedynatrace.com raw.githubusercontent.com api.github.com wwse.apps.dynatrace.com $EXTRA"

bearer(){ curl -s -X POST "$SSO" -d grant_type=client_credentials -d "client_id=$CID" \
  -d "client_secret=$CSEC" -d "scope=$1" -d "resource=$2" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null; }

echo "Tenant : $TURL  (env $ENVID)"
B=$(bearer "settings:objects:read settings:objects:write" "$ENVURN")
if [ -z "$B" ]; then
  B=$(bearer "settings:objects:read" "$ENVURN")
  [ -n "$B" ] && echo "NOTE   : read-only — client has no settings:objects:write, --apply will not work"
fi
if [ -z "$B" ]; then
  echo "FAIL   : client cannot obtain settings:objects:read on this environment."
  echo "         Scopes cannot be edited on an existing OAuth client — create a new one."
  exit 1
fi

AL=$(curl -s "$PROXY/v2/settings/objects?schemaIds=$SCHEMA&scopes=environment&fields=objectId,value" \
     -H "Authorization: Bearer $B")
read -r STATE OBJID HOSTS <<<"$(python3 - "$AL" <<'PY'
import json,sys
try: items=json.loads(sys.argv[1]).get("items",[])
except Exception: items=[]
if not items:
    print("none - -"); raise SystemExit
o=items[0]; aoc=(o.get("value") or {}).get("allowedOutboundConnections",{})
print(("enforced" if aoc.get("enforced") else "open"), o.get("objectId","-"),
      ",".join(aoc.get("hostList",[])) or "-")
PY
)"

MISSING=""
for h in $NEEDED; do case ",$HOSTS," in *",$h,"*) ;; *) MISSING="$MISSING $h" ;; esac; done

case "$STATE" in
  open)
    echo "STATE  : allowlist object exists but is NOT enforced — outbound is open. Nothing to do."
    exit 0 ;;
  none)
    echo "STATE  : no allowlist object at environment scope."
    if [ "$MODE" != "--create" ]; then
      echo "         This does NOT prove outbound is open — that assumption is exactly what"
      echo "         failed on jxh41488, and on uxn36332 the app was blocked with no object"
      echo "         at ANY scope. Prove it first:"
      echo
      echo "           curl -X POST \\"
      echo "             $APPS/platform/app-engine/app-functions/v1/apps/my.dynatrace.enablements/api/fetchChangelog \\"
      echo "             -H 'Authorization: Bearer <app-engine:apps:run>' -d '{}'"
      echo
      echo "         If that answers \"host not in allowlist\", re-run with --create."
      exit 1
    fi
    # Only reachable with --create, i.e. a human has seen the app report a block.
    # Creating a list of exactly the hosts the app needs is strictly MORE
    # permissive than the default-deny that is demonstrably already in force,
    # so this repairs rather than tightens. Never do it on a guess.
    echo "CREATE : outbound is denied with no object present — creating one with the app's hosts"
    BODY=$(python3 - "$NEEDED" <<'PY2'
import json,sys
print(json.dumps([{"schemaId":"builtin:dt-javascript-runtime.allowed-outbound-connections",
                   "scope":"environment",
                   "value":{"allowedOutboundConnections":{"enforced":True,
                            "hostList":sys.argv[1].split()}}}]))
PY2
)
    C=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$PROXY/v2/settings/objects" \
        -H "Authorization: Bearer $B" -H 'Content-Type: application/json' -d "$BODY")
    if [ "$C" != 200 ] && [ "$C" != 201 ]; then
      echo "FAIL   : could not create the allowlist (HTTP $C) — needs settings:objects:write"
      exit 1
    fi
    echo "CREATED: allowlist object written with $(wc -w <<<"$NEEDED") host(s)."
    echo "         Re-run without --create to verify, then confirm with the app."
    exit 0 ;;
esac

echo "STATE  : allowlist is ENFORCED with $(tr ',' '\n' <<<"$HOSTS" | grep -c .) host(s)"
if [ -z "$MISSING" ]; then
  echo "OK     : every host the app needs is already allowed."
  exit 0
fi
echo "MISSING:$MISSING"

if [ "$MODE" != "--apply" ]; then
  echo
  echo "Re-run with --apply to add them, or add them by hand under"
  echo "Settings > Outbound connections on $APPS"
  exit 1
fi

NEW=$(python3 - "$HOSTS" "$MISSING" <<'PY'
import json,sys
have=[h for h in sys.argv[1].split(",") if h and h!="-"]
print(json.dumps({"value":{"allowedOutboundConnections":{"enforced":True,
      "hostList":have+sys.argv[2].split()}}}))
PY
)
C=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$PROXY/v2/settings/objects/$OBJID" \
    -H "Authorization: Bearer $B" -H 'Content-Type: application/json' -d "$NEW")
if [ "$C" != 200 ] && [ "$C" != 201 ] && [ "$C" != 204 ]; then
  echo "FAIL   : could not update the allowlist (HTTP $C) — needs settings:objects:write"
  exit 1
fi

# Re-read rather than trusting the write. The whole point of this exercise is to
# stop believing things we did not verify.
AL2=$(curl -s "$PROXY/v2/settings/objects?schemaIds=$SCHEMA&scopes=environment&fields=value" \
      -H "Authorization: Bearer $B")
LEFT=$(python3 - "$AL2" "$NEEDED" <<'PY'
import json,sys
items=json.loads(sys.argv[1]).get("items",[])
hosts=set(((items[0].get("value") or {}).get("allowedOutboundConnections",{}) or {}).get("hostList",[])) if items else set()
print(" ".join(h for h in sys.argv[2].split() if h not in hosts))
PY
)
if [ -n "$LEFT" ]; then echo "FAIL   : still missing after write:$LEFT"; exit 1; fi
echo "FIXED  : allowlist now contains every host the app needs (verified by re-read)."
echo "         Next: click 'Update now' in the app, then start a training to confirm."
exit 0

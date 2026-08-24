#!/usr/bin/env bash
# Verify a Dynatrace tenant is ready for the Enablement app BEFORE registering it.
# Creates nothing permanent: every probe cleans up after itself.
#
#   ./check-tenant-setup.sh <client-id> <client-secret> <account-urn> <tenant-url>
#
# Exit 0 = ready. Exit 1 = something is missing (each failure is named).
#
# ── Why this script no longer probes anything itself ─────────────────────────────────
#
# It used to. 267 lines of curl re-implemented, in bash, the same checks Orbital runs in
# python before it installs the app — and the two drifted, because nothing ever compared
# them. On 2026-08-24 an SE saw this page go all-green and Register Tenant answer HTTP 412
# in the same minute (tenant bnk46244). The scope list alone was hardcoded in six places.
#
# So the checks moved to ONE implementation, ops-server/dashboard/tenant_preflight.py, and
# this script POSTs to it. Both doors — this page and Register Tenant — now read the same
# `ready` verdict off the same report, which is the only way to guarantee they agree.
#
# The credential is sent over TLS to Orbital, used in memory for the check, and discarded;
# Orbital stores nothing, exactly as the register route does.

set -uo pipefail
CID="${1:?client id}"; CSEC="${2:?client secret}"; ACCT="${3:?urn:dtaccount:...}"; TURL="${4:?https://<env>.apps.dynatrace.com}"
ORBITAL="${ORBITAL_URL:-https://autonomous-enablements.whydevslovedynatrace.com}"

# Shape gate stays local: it costs nothing, and telling someone "that is a platform token,
# not an OAuth client" is faster here than a round-trip. Orbital enforces the same rules.
case "$CID" in
  dt0s02.*) ;;
  dt0s16.*|dt0s08.*) echo "  FAIL  client id is a platform token (dt0s16…), not an OAuth client (dt0s02…)"; exit 1 ;;
  dt0c01.*) echo "  FAIL  client id is a classic API token (dt0c01…), not an OAuth client (dt0s02…)"; exit 1 ;;
  *)        echo "  FAIL  client id must look like dt0s02.XXXXXXXX"; exit 1 ;;
esac

echo "Tenant : $TURL"
echo "Client : $CID"
echo

BODY=$(CID="$CID" CSEC="$CSEC" ACCT="$ACCT" TURL="$TURL" python3 -c '
import json, os
print(json.dumps({"tenant": os.environ["TURL"], "clientId": os.environ["CID"],
                  "clientSecret": os.environ["CSEC"], "accountUrn": os.environ["ACCT"]}))')

# --max-time generously above the probe budget: preflight mints and deletes real tokens.
RESP=$(printf '%s' "$BODY" | curl -sS --max-time 180 -w '\n%{http_code}' \
        -X POST "$ORBITAL/api/deploy/preflight" \
        -H 'Content-Type: application/json' --data-binary @- 2>&1)
CODE=$(tail -n1 <<<"$RESP"); RESP=$(sed '$d' <<<"$RESP")

if [ "$CODE" != 200 ]; then
  # A 4xx carries a real diagnosis from the shared gate; anything else means the check
  # could not run, which is NOT the same as a healthy tenant and must never read as one.
  DETAIL=$(python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("detail",""))
except Exception: print("")' <<<"$RESP" 2>/dev/null)
  case "$CODE" in
    400|403|412|429) echo "  FAIL  ${DETAIL:-check refused (HTTP $CODE)}"
                     echo; echo "NOT READY — ${DETAIL:-see above}"; exit 1 ;;
    *) echo "  SKIP  could not reach the verification service (HTTP $CODE)"
       echo; echo "NOT VERIFIED — the check did not run, so this tenant is not known to be"
       echo "ready or unready. Retry, or report it if it persists."; exit 1 ;;
  esac
fi

python3 - "$RESP" <<'PYEOF'
import json, sys

r = json.loads(sys.argv[1])
G, Y, R, X = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
MARK = {"pass": f"  {G}PASS{X}  ", "fail": f"  {R}FAIL{X}  ", "skip": f"  {Y}SKIP{X}  "}

for c in r.get("checks", []):
    tail = "" if c["status"] == "pass" else f" <-- {c['detail']}"
    scopes = f"  ({c['scopes']})" if c.get("scopes") and c["status"] != "pass" else ""
    print(f"{MARK[c['status']]}{c['title']}{scopes}{tail}")

missing = r.get("missingScopes") or []
if missing:
    print()
    print("  Scopes this client does not hold:")
    for m in missing:
        print(f"    - {m}")
    print("  Scopes CANNOT be added to an existing OAuth client — create a new one with the")
    print("  full list at myaccount.dynatrace.com > Identity & access management > OAuth clients.")

counts = {}
for c in r.get("checks", []):
    counts[c["status"]] = counts.get(c["status"], 0) + 1
skipped = f", {counts.get('skip', 0)} not proven" if counts.get("skip") else ""

print()
if r.get("ready"):
    tier = {"classic": " Learner tokens: classic (dt0c01).",
            "platform": " Learner tokens: gen3 platform path (classic creation is retired here)."
            }.get(r.get("learnerTokenTier"), "")
    warn = r.get("warnings") or []
    print(f"READY — {counts.get('pass', 0)} checks passed{skipped}.{tier} "
          f"Register this tenant in Orbital.")
    for w in warn:
        print(f"  note: {w}")
    sys.exit(0)

print(f"NOT READY — {counts.get('fail', 0)} of {len(r.get('checks', []))} checks failed.")
for b in r.get("blocking", []):
    print(f"  - {b}")
sys.exit(1)
PYEOF

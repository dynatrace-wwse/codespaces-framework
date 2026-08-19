#!/usr/bin/env bash
# Verify a Dynatrace tenant is ready for the Enablement app BEFORE registering it.
# Creates nothing permanent: every probe cleans up after itself.
#
#   ./check-tenant-setup.sh <client-id> <client-secret> <account-urn> <tenant-url>
#
# Exit 0 = ready. Exit 1 = something is missing (each failure is named).

set -uo pipefail
CID="${1:?client id}"; CSEC="${2:?client secret}"; ACCT="${3:?urn:dtaccount:...}"; TURL="${4:?https://<env>.apps.dynatrace.com}"
ENVID=$(sed -E 's#https?://([^.]+)\..*#\1#' <<<"$TURL")
APPS="${TURL%%/}"; APPS="${APPS%/}"
ENVURN="urn:dtenvironment:${ENVID}"
PROXY="${APPS}/platform/classic/environment-api"
case "$APPS" in
  *sprint*) SSO="https://sso-sprint.dynatracelabs.com/sso/oauth2/token"
            ACCT_API="https://api-hardening.internal.dynatracelabs.com"
            LIVE="${APPS/.sprint.apps./.sprint.}" ;;   # <env>.sprint.dynatracelabs.com — there is no sprint .live.
  *dev*)    SSO="https://sso-dev.dynatracelabs.com/sso/oauth2/token"
            ACCT_API="https://api-hardening.internal.dynatracelabs.com"
            LIVE="${APPS/.dev.apps./.dev.}" ;;
  *)        SSO="https://sso.dynatrace.com/sso/oauth2/token"
            ACCT_API="https://api.dynatrace.com"
            LIVE="${APPS/.apps./.live.}" ;;
esac
PASS=0; FAIL=0; WARN=0
# Scopes SSO actually issued a bearer for. Section 4 needs it: "granted but not
# effective" is only meaningful for a scope that was granted in the first place,
# and reporting it for one that was never granted just restates section 1 with a
# wrong explanation.
GRANTED=""
ok(){ printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
warn(){ printf '  \033[33mSKIP\033[0m  %s\n' "$1"; WARN=$((WARN+1)); }
no(){ printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }

bearer(){ curl -s -X POST "$SSO" -d grant_type=client_credentials -d "client_id=$CID" \
  -d "client_secret=$CSEC" -d "scope=$1" -d "resource=$2" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null; }

echo "Tenant : $TURL   (env $ENVID)"
echo "Client : $CID"
echo
echo "1. Scopes the client is granted"
for s in app-engine:apps:install app-engine:apps:run app-engine:apps:delete \
         settings:objects:read settings:objects:write app-settings:objects:read \
         environment-api:api-tokens:read environment-api:api-tokens:write \
         document:documents:read document:documents:write document:documents:delete \
         document:documents:admin; do
  if [ -n "$(bearer "$s" "$ENVURN")" ]; then ok "$s"; GRANTED="$GRANTED $s"
  else no "$s  <-- add this scope to the OAuth client"; fi
done
# ActiveGate minting is satisfied by EITHER family, so neither is a failure on
# its own — section 2 mints a real token and decides. Listing the classic one
# here as mandatory would fail a perfectly good gen3-only client.
if   [ -n "$(bearer environment-api:activegate-tokens:write "$ENVURN")" ]; then ok "environment-api:activegate-tokens:write"
elif [ -n "$(bearer fleet-management:activegate.tokens:write "$ENVURN")" ]; then ok "fleet-management:activegate.tokens:write (gen3 ActiveGate path)"
else no "no ActiveGate mint scope  <-- add environment-api:activegate-tokens:write OR fleet-management:activegate.tokens:write"; fi
for s in platform-token:tokens:write platform-token:tokens:manage; do
  [ -n "$(bearer "$s" "$ACCT")" ] && ok "$s (account)" || no "$s (account)  <-- add this scope"
done

echo
echo "2. Capabilities that actually matter (a granted scope is not proof)"
EXP=$(python3 -c 'import datetime;print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))')

# Learner-token tier: classic dt0c01 is the primary path; on environments where classic
# creation has been retired (HTTP 400, rolled out per environment) fall back to a live
# platform-token probe. Branch on HTTP status only — error text is not a contract.
TIER=""
B=$(bearer "environment-api:api-tokens:write" "$ENVURN")
if [ -z "$B" ]; then no "mint a learner token — skipped, scope not granted (see above)"; B=x; fi
R=$(curl -s -w '\n%{http_code}' -X POST "$PROXY/v2/apiTokens" -H "Authorization: Bearer $B" -H 'Content-Type: application/json' \
  -d "{\"name\":\"orbital-preflight\",\"expirationDate\":\"$EXP\",\"scopes\":[\"InstallerDownload\",\"activeGateTokenManagement.create\",\"entities.read\",\"settings.read\",\"settings.write\",\"DataExport\"]}")
MC=$(tail -n1 <<<"$R"); R=$(sed '$d' <<<"$R")
TID=$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' <<<"$R" 2>/dev/null)
TOK=$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' <<<"$R" 2>/dev/null)
if [ -n "$TOK" ]; then
  TIER="classic"
  ok "mint a learner token (classic dt0c01)"
  # Classic tokens authenticate against the classic (live) domain only — the
  # /platform/classic proxy rejects them with 401.
  C=$(curl -s -o /dev/null -w '%{http_code}' "$LIVE/api/v1/deployment/installer/agent/connectioninfo" -H "Authorization: Api-Token $TOK")
  if   [ "$C" = 200 ]; then ok "that token can install OneAgent/ActiveGate (HTTP 200)"
  elif [ "$C" = 000 ]; then warn "OneAgent/ActiveGate check skipped — $LIVE not reachable from here"
  else no "token minted but Operator calls fail (HTTP $C)"; fi
  curl -s -o /dev/null -X DELETE "$PROXY/v2/apiTokens/$TID" -H "Authorization: Bearer $B"
elif [ "$B" != x ] && [ "$MC" = 400 ]; then
  warn "classic token creation retired on this environment (HTTP 400) — probing the platform-token path"
  PB=$(bearer "platform-token:tokens:write platform-token:tokens:manage" "$ACCT")
  if [ -z "$PB" ]; then
    no "gen3 fallback impossible: client cannot obtain platform-token:tokens:write/manage (see above)"
  else
    ACCTID="${ACCT##*:}"
    PR=$(curl -s -X POST "$ACCT_API/iam/v1/accounts/$ACCTID/platform-tokens" -H "Authorization: Bearer $PB" -H 'Content-Type: application/json' \
      -d "{\"name\":\"orbital-preflight-gen3\",\"scope\":[\"fleet-management:oneagents:download\",\"fleet-management:oneagent.connection-info:read\",\"fleet-management:activegate.connection-info:read\",\"fleet-management:container-images:read\",\"fleet-management:activegate.tokens:create\",\"fleet-management:activegate.tokens:write\",\"settings:objects:read\",\"settings:objects:write\",\"storage:entities:read\",\"storage:logs:write\",\"storage:events:write\",\"storage:metrics:write\"],\"resource\":[\"$ENVURN\"],\"tags\":[\"enablement-preflight\"],\"expirationDate\":\"$EXP\"}")
    PTOK=$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' <<<"$PR" 2>/dev/null)
    PTID=$(python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("tokenId") or d.get("id") or "")' <<<"$PR" 2>/dev/null)
    if [ -z "$PTOK" ]; then
      no "cannot mint a platform token: $(head -c 150 <<<"$PR")"
    else
      C=$(curl -s -o /dev/null -w '%{http_code}' "$PROXY/v1/deployment/installer/agent/connectioninfo" -H "Authorization: Api-Token $PTOK")
      if [ "$C" = 200 ]; then
        TIER="gen3"
        ok "mint a learner token (gen3 platform dt0s16) + Operator install check (HTTP 200)"
      else
        # Scopes are on the token but its OWNER's IAM policy cannot exercise them —
        # detectable only by this live call, never by reading the token record back.
        no "platform token minted but Operator calls fail (HTTP $C) — the token owner's IAM policy lacks the permissions; bind environment:roles:manage-settings at environment level to the user who created the OAuth client, then re-run"
      fi
      [ -n "$PTID" ] && curl -s -o /dev/null -X DELETE "$ACCT_API/iam/v1/accounts/$ACCTID/platform-tokens/$PTID" -H "Authorization: Bearer $PB"
    fi
  fi
elif [ "$B" != x ]; then
  no "cannot mint a learner token (HTTP $MC): $(head -c 150 <<<"$R")"
fi

# ActiveGate token (dt0g02): two scope families mint it, and a client that has
# neither fails EVERY Kubernetes training when DynaKube starts. The classic
# scope is missing from some clients' catalogs — SSO answers 400 at the token
# endpoint, before any API call, so no IAM binding helps (measured on hpm49270,
# 2026-08-19: it killed a learner's session mid-workshop). fleet-management is
# the gen3 twin and exists exactly where the classic one does not.
AGOK=""
for AGS in environment-api:activegate-tokens:write fleet-management:activegate.tokens:write; do
  B=$(bearer "$AGS" "$ENVURN")
  [ -z "$B" ] && continue
  R=$(curl -s -X POST "$PROXY/v2/activeGateTokens" -H "Authorization: Bearer $B" \
    -H 'Content-Type: application/json' -d "{\"name\":\"orbital-preflight-ag\",\"activeGateType\":\"ENVIRONMENT\",\"expirationDate\":\"$EXP\"}")
  AGID=$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' <<<"$R" 2>/dev/null)
  if [ -n "$AGID" ]; then
    ok "mint an ActiveGate token (via $AGS)"
    curl -s -o /dev/null -X DELETE "$PROXY/v2/activeGateTokens/$AGID" -H "Authorization: Bearer $B"
    AGOK=1; break
  fi
done
[ -z "$AGOK" ] && no "cannot mint an ActiveGate token — every Kubernetes training will fail at DynaKube. <-- add environment-api:activegate-tokens:write OR fleet-management:activegate.tokens:write. Scopes cannot be edited on an existing client: create a NEW one"

B=$(bearer "settings:objects:read settings:objects:write" "$ENVURN")
C=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$PROXY/v2/settings/objects?validateOnly=true" \
  -H "Authorization: Bearer $B" -H 'Content-Type: application/json' \
  -d '[{"schemaId":"builtin:management-zones","scope":"environment","value":{"name":"orbital-preflight","rules":[]}}]')
if   [ "$C" = 200 ]; then ok "write environment settings (allowlist, telemetry, store the client)"
elif [ "$C" = 404 ]; then warn "settings-write check skipped — probe schema not present on this environment"
else no "cannot write environment settings (HTTP $C)"; fi

B=$(bearer "document:documents:read document:documents:write document:documents:delete document:documents:admin" "$ENVURN")
if [ -z "$B" ]; then no "store training content — skipped, document scopes not granted (see above)"; B=x; fi
BND="----orbital$$"
DOC=$(printf -- "--%s\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\norbital-preflight-doc\r\n--%s\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\ndashboard\r\n--%s\r\nContent-Disposition: form-data; name=\"content\"; filename=\"content\"\r\nContent-Type: application/json\r\n\r\n{}\r\n--%s--\r\n" "$BND" "$BND" "$BND" "$BND" \
  | curl -s -X POST "$APPS/platform/document/v1/documents" -H "Authorization: Bearer $B" \
      -H "Content-Type: multipart/form-data; boundary=$BND" --data-binary @-)
DID=$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' <<<"$DOC" 2>/dev/null)
if [ -n "$DID" ]; then
  OWN=$(python3 -c 'import sys,json;print(json.load(sys.stdin).get("owner",""))' <<<"$DOC" 2>/dev/null)
  ok "store training content (owner: ${OWN:0:8}… — one identity, no duplicate copies)"
  V=$(curl -s "$APPS/platform/document/v1/documents?filter=id%20%3D%20%27$DID%27&page-size=1" -H "Authorization: Bearer $B" \
      | python3 -c 'import sys,json;d=json.load(sys.stdin).get("documents",[]);print(d[0]["version"] if d else 1)' 2>/dev/null)
  curl -s -o /dev/null -X DELETE "$APPS/platform/document/v1/documents/$DID?optimistic-locking-version=$V" -H "Authorization: Bearer $B"
elif [ "$B" != x ]; then
  no "cannot store training content: $(head -c 150 <<<"$DOC")"
fi

# ── 3. Outbound allowlist ────────────────────────────────────────────────────
#
# The app's server-side functions run in the Dynatrace JS runtime, which blocks
# egress to any host missing from this settings object. When it bites, the
# learner sees "Blocked request to '<host>' (host not in allowlist)" — hours
# later, from inside a provisioning job, with nothing pointing back here.
#
# Orbital repairs this automatically during a deploy, but only if the client can
# write settings. Checking it BEFORE registration is what lets an SE fix their
# own tenant instead of discovering it during a delivery (bth17199 and uxn36332,
# 2026-08-19: both installed cleanly, neither could reach Orbital).
echo
echo "3. Outbound connections (the app's functions must reach these hosts)"
NEEDED="autonomous-enablements.whydevslovedynatrace.com raw.githubusercontent.com api.github.com wwse.apps.dynatrace.com"
case "$APPS" in
  *sprint*) NEEDED="$NEEDED sso-sprint.dynatracelabs.com api-hardening.internal.dynatracelabs.com" ;;
  *dev*)    NEEDED="$NEEDED" ;;
  *)        NEEDED="$NEEDED sso.dynatrace.com api.dynatrace.com" ;;
esac
B=$(bearer "settings:objects:read" "$ENVURN")
if [ -z "$B" ]; then
  warn "outbound allowlist not checked — settings:objects:read not granted"
else
  AL=$(curl -s "$PROXY/v2/settings/objects?schemaIds=builtin:dt-javascript-runtime.allowed-outbound-connections&scopes=environment&fields=value" \
       -H "Authorization: Bearer $B")
  # Three states, and only one of them is a problem: enforced-and-incomplete.
  ENF=$(python3 -c 'import sys,json
try: items=json.load(sys.stdin).get("items",[])
except Exception: items=[]
if not items: print("none"); raise SystemExit
aoc=(items[0].get("value") or {}).get("allowedOutboundConnections",{})
print("enforced" if aoc.get("enforced") else "open")' <<<"$AL" 2>/dev/null)
  if [ "$ENF" = "open" ]; then
    ok "outbound filtering is not enforced on this environment"
  elif [ "$ENF" = "none" ]; then
    # Do NOT report this as "open". A prod tenant with no object was assumed
    # open on 2026-08-19 and was not: twelve tenants got that verdict and eight
    # never provisioned anything. Only the app itself can settle it.
    warn "no allowlist object found — this does NOT prove outbound is open (it did not on jxh41488). Orbital's post-install self-test will confirm it from inside the app"
  else
    HOSTS=$(python3 -c 'import sys,json
items=json.load(sys.stdin).get("items",[])
aoc=(items[0].get("value") or {}).get("allowedOutboundConnections",{})
print(" ".join(aoc.get("hostList",[])))' <<<"$AL" 2>/dev/null)
    MISSING=""
    for h in $NEEDED; do case " $HOSTS " in *" $h "*) ;; *) MISSING="$MISSING $h" ;; esac; done
    if [ -z "$MISSING" ]; then ok "outbound allowlist already contains every host the app needs"
    else no "outbound allowlist is ENFORCED and missing:$MISSING  <-- add them under Settings > Outbound connections, or grant settings:objects:write so the deploy can add them"; fi
  fi
fi

# ── 4. Effective permissions ─────────────────────────────────────────────────
#
# SSO stamps scope names WITHOUT an entitlement check: effective permission is
# `scopes ∩ the owner's IAM policy`. Section 1 can therefore pass while every
# call using the scope is refused. That is not theoretical — on bos01241 a token
# carrying document:documents:admin got
#   403 {"code":403,"message":"Document not accessible: 8ff8e6fd-…"}
# on every use, so the app could not update lab documents that already existed
# under another owner, and four trainings silently stayed missing.
echo
echo "4. Effective permissions (SSO stamps scopes it does not enforce)"
# The bearer must CARRY the permissions being asked about. The resolve API
# answers for the PRESENTED TOKEN (its scopes n the owner's IAM), not for what
# the client could obtain — measured on COE 2026-08-19: asked with an
# app-engine:apps:run-only bearer every permission came back "false", including
# ones the very same client had just used successfully in section 2. Asking the
# wrong way turns this section into a permanent false alarm.
EPSCOPE="app-engine:apps:run document:documents:admin document:documents:write settings:objects:write"
B=$(bearer "$EPSCOPE" "$ENVURN")
if [ -z "$B" ]; then
  warn "effective-permission check skipped — SSO would not issue a bearer for: $EPSCOPE (a scope-catalog gap, already reported in section 1)"
else
  EP=$(curl -s -w '\n%{http_code}' -X POST "$APPS/platform/management/v1/effective-permissions:resolve" \
       -H "Authorization: Bearer $B" -H 'Content-Type: application/json' \
       -d '{"permissions":[{"permission":"document:documents:admin"},{"permission":"document:documents:write"},{"permission":"settings:objects:write"}]}')
  EPC=$(tail -n1 <<<"$EP"); EP=$(sed '$d' <<<"$EP")
  if [ "$EPC" != 200 ]; then
    warn "effective-permissions API not available here (HTTP $EPC) — section 2's live probes remain the proof"
  else
    DENIED=$(python3 -c 'import sys,json
print(" ".join(r.get("permission","") for r in (json.load(sys.stdin) or []) if r.get("granted")=="false"))' <<<"$EP" 2>/dev/null)
    # Only a scope the client HOLDS and cannot exercise is this section's finding.
    # One it was never granted is section 1's finding, already reported there.
    GAP=""
    for d in $DENIED; do case " $GRANTED " in *" $d "*) GAP="$GAP $d" ;; esac; done
    if [ -n "$GAP" ]; then
      no "granted by SSO but NOT effective:$GAP  <-- bind an IAM policy carrying these to the OAuth client's service user AT ENVIRONMENT LEVEL. Without it the app cannot update content another user owns, and those trainings stay missing from the catalog"
    elif [ -n "$DENIED" ]; then
      warn "not effective, because not granted:$DENIED (already reported in section 1)"
    else
      ok "every checked permission is effective, not just granted"
    fi
  fi
fi

echo
if [ "$FAIL" -eq 0 ]; then
  case "$TIER" in
    classic) T=" Learner tokens: classic (dt0c01)." ;;
    gen3)    T=" Learner tokens: gen3 platform path (classic creation is retired here)." ;;
    *)       T="" ;;
  esac
  echo "READY — $PASS checks passed${WARN:+, $WARN skipped}.${T} Register this tenant in Orbital."; exit 0
else echo "NOT READY — $FAIL of $((PASS+FAIL)) checks failed. Fix the scopes above and re-run."; exit 1; fi

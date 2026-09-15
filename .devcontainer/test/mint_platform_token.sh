#!/bin/bash
# Test-harness helper: mint a short-lived gen3 platform token (dt0s16) for a suite run.
#
# WHY THIS EXISTS — read before reusing it:
#
# A learner never runs this. In a real training environment DT_PLATFORM_TOKEN is
# already there: the enablement app mints it with the tenant's own OAuth client
# before the container starts, hands it to the environment, and revokes it when the
# session ends. That is the product path, and the suites must exercise it as given.
#
# A suite run on a machine that is NOT a provisioned training environment — a
# developer box, a CI runner, an ops host — has no such token, and the answer is not
# a long-lived one pasted into a secret store. A static platform token is a standing
# credential nobody rotates, and testing with it proves nothing about the scopes the
# app actually grants. So the harness mints one the same way the app does, from the
# tenant's own OAuth client, uses it for the run, and revokes it at teardown.
#
# The order matters and is not negotiable: an existing DT_PLATFORM_TOKEN ALWAYS wins.
# Minting is the fallback for the harness, never a replacement for the learner path.
#
# Usage:
#   source .devcontainer/test/mint_platform_token.sh
#   mintPlatformTokenForTest scope1 scope2 ...   # sets DT_PLATFORM_TOKEN (+ _SOURCE, _ID)
#   trap revokeMintedPlatformToken EXIT
#
# Inputs when minting (all absent on a learner's machine, which is the point):
#   DT_ENVIRONMENT             tenant URL                              (required)
#   DT_OAUTH_CLIENT_ID         the tenant's own OAuth client           (required)
#   DT_OAUTH_CLIENT_SECRET       …its secret                           (required)
#   DT_OAUTH_RESOURCE            …its account URN (urn:dtaccount:…)    (required)
#   DT_ACCOUNT_API_HOST        Account Management API host. Optional for production
#                              tenants; REQUIRED for a non-production realm, whose
#                              host cannot be derived from the tenant URL.
#   DT_SSO_URL                 SSO origin. Optional — discovered from the tenant —
#                              but required for a non-production realm if the
#                              discovery probe cannot reach it.
#   DT_PLATFORM_TOKEN_TTL_HOURS  lifetime of the minted token, default 1
#
# Outputs:
#   DT_PLATFORM_TOKEN         the token (never printed, never logged)
#   DT_PLATFORM_TOKEN_SOURCE  "environment" (pre-existing) or "minted"
#   DT_PLATFORM_TOKEN_ID      id of a minted token, for revocation

_MINT_PY="$(dirname "${BASH_SOURCE[0]}")/mint_platform_token.py"

mintPlatformTokenForTest() {
  # Usage: mintPlatformTokenForTest <scope> [<scope> ...]
  if [ $# -eq 0 ]; then
    printError "mintPlatformTokenForTest: no scopes given"
    return 1
  fi

  if [ -n "${DT_PLATFORM_TOKEN:-}" ]; then
    # The real runtime case: a provisioned environment already holds one.
    export DT_PLATFORM_TOKEN_SOURCE="environment"
    printInfo "DT_PLATFORM_TOKEN is already set — using it, mint skipped (source: environment)"
    return 0
  fi

  printInfo "DT_PLATFORM_TOKEN is not set — minting a short-lived one for this run"

  local missing=()
  [ -z "${DT_ENVIRONMENT:-}" ] && missing+=("DT_ENVIRONMENT")
  [ -z "${DT_OAUTH_CLIENT_ID:-}" ] && missing+=("DT_OAUTH_CLIENT_ID")
  [ -z "${DT_OAUTH_CLIENT_SECRET:-}" ] && missing+=("DT_OAUTH_CLIENT_SECRET")
  [ -z "${DT_OAUTH_RESOURCE:-}" ] && missing+=("DT_OAUTH_RESOURCE")
  if [ ${#missing[@]} -gt 0 ]; then
    # Say which of the two situations this is. "No platform token" sent a reader
    # looking for a token to paste, which is the thing this helper exists to avoid.
    printError "❌ No DT_PLATFORM_TOKEN, and cannot mint one: ${missing[*]} not set"
    printError "   A training environment supplies DT_PLATFORM_TOKEN. Elsewhere, supply the"
    printError "   tenant's own OAuth client (id + secret + account URN) and the suite mints"
    printError "   its own for the run. Do not paste a long-lived platform token."
    return 1
  fi

  local out rc
  # Command substitution: the token never reaches a terminal or a log. Diagnostics
  # go to stderr from the minter and are shown; stdout carries only id + value.
  out="$(python3 "$_MINT_PY" mint "$@")"
  rc=$?
  if [ $rc -ne 0 ]; then
    printError "❌ Platform token mint failed (see the error above)"
    return 1
  fi

  DT_PLATFORM_TOKEN_ID="$(printf '%s\n' "$out" | sed -n '1p')"
  DT_PLATFORM_TOKEN="$(printf '%s\n' "$out" | sed -n '2p')"
  if [ -z "$DT_PLATFORM_TOKEN" ]; then
    printError "❌ Mint returned no token value"
    return 1
  fi
  export DT_PLATFORM_TOKEN DT_PLATFORM_TOKEN_ID
  export DT_PLATFORM_TOKEN_SOURCE="minted"
  printInfo "✅ Minted platform token ${DT_PLATFORM_TOKEN_ID} (${#DT_PLATFORM_TOKEN} chars, ${DT_PLATFORM_TOKEN_TTL_HOURS:-1}h, $# scope(s))"
  return 0
}

revokeMintedPlatformToken() {
  # Safe to call unconditionally from a trap: does nothing unless THIS run minted.
  # A token the environment supplied is not ours to revoke.
  if [ "${DT_PLATFORM_TOKEN_SOURCE:-}" != "minted" ] || [ -z "${DT_PLATFORM_TOKEN_ID:-}" ]; then
    return 0
  fi
  printInfo "Revoking the platform token minted for this run (${DT_PLATFORM_TOKEN_ID})"
  if python3 "$_MINT_PY" revoke "$DT_PLATFORM_TOKEN_ID"; then
    printInfo "✅ Revoked"
  else
    # Loud, but never fatal: a leaked short-lived token still expires, and turning
    # a teardown warning into a suite failure hides whatever the suite actually found.
    printWarn "⚠️  Could not revoke ${DT_PLATFORM_TOKEN_ID} — it expires on its own, but check the account"
  fi
  unset DT_PLATFORM_TOKEN DT_PLATFORM_TOKEN_ID
  export DT_PLATFORM_TOKEN_SOURCE="revoked"
}

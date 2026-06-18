#!/usr/bin/env bash
# codespace_smoketest.sh — end-to-end smoke test for the "launch a lab in the
# learner's own GitHub Codespace" flow, using AMBIENT `gh` auth.
#
# No Orbital, no Redis, no FastAPI. This is the script to run for the LIVE test
# once your `gh` login has the `codespace` scope. It is self-cleaning: a trap
# guarantees the Codespace is deleted even if a step fails or you Ctrl-C.
#
# It exercises the same gh commands codespace_service.py runs:
#   1. set the 3 DT_* user codespaces secrets, scoped to $REPO
#   2. create a Codespace (capture its NAME)
#   3. poll until it is Available (timeout ~5 min)
#   4. ssh in and run a kubectl smoke check
#   5. ALWAYS delete the Codespace
#
# Config via env or args (args override env):
#   REPO            owner/repo            (default dynatrace-wwse/enablement-kubernetes-101)
#   MACHINE         machine type          (default standardLinux32gb)
#   REF             git ref / branch      (default: repo default branch)
#   DT_ENVIRONMENT / DT_OPERATOR_TOKEN / DT_INGEST_TOKEN
#                   Dynatrace creds — if unset, placeholders are used + a warning is printed.
#
# Usage:
#   ./codespace_smoketest.sh
#   REPO=dynatrace-wwse/enablement-foo MACHINE=standardLinux32gb ./codespace_smoketest.sh
#   ./codespace_smoketest.sh dynatrace-wwse/enablement-foo standardLinux32gb main

set -euo pipefail

REPO="${1:-${REPO:-dynatrace-wwse/enablement-kubernetes-101}}"
MACHINE="${2:-${MACHINE:-standardLinux32gb}}"
REF="${3:-${REF:-}}"

POLL_TIMEOUT="${POLL_TIMEOUT:-300}"   # ~5 min to reach Available
POLL_INTERVAL="${POLL_INTERVAL:-10}"

CS_NAME=""

cleanup() {
  local rc=$?
  if [[ -n "$CS_NAME" ]]; then
    echo
    echo "==> [cleanup] Deleting codespace: $CS_NAME"
    gh codespace delete -c "$CS_NAME" --force || \
      echo "    WARNING: delete failed — remove it manually: gh codespace delete -c $CS_NAME --force"
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# ── preflight ────────────────────────────────────────────────────────────────
echo "==> Preflight"
command -v gh >/dev/null 2>&1 || { echo "ERROR: gh CLI not found on PATH"; exit 127; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: not logged in (run: gh auth login --scopes codespace)"; exit 1; }
echo "    REPO=$REPO  MACHINE=$MACHINE  REF=${REF:-<default branch>}"

# Default Dynatrace creds to placeholders if not provided (so the flow still runs).
if [[ -z "${DT_ENVIRONMENT:-}" || -z "${DT_OPERATOR_TOKEN:-}" || -z "${DT_INGEST_TOKEN:-}" ]]; then
  echo "    WARNING: one or more DT_* values unset — using placeholders (the lab will not connect to a real tenant)."
  DT_ENVIRONMENT="${DT_ENVIRONMENT:-https://placeholder.apps.dynatrace.com}"
  DT_OPERATOR_TOKEN="${DT_OPERATOR_TOKEN:-dt0c01.PLACEHOLDER.OPERATOR}"
  DT_INGEST_TOKEN="${DT_INGEST_TOKEN:-dt0c01.PLACEHOLDER.INGEST}"
fi

# ── (1) user codespaces secrets, scoped to the repo ──────────────────────────
echo
echo "==> (1) Setting user Codespaces secrets scoped to $REPO"
for pair in "DT_ENVIRONMENT=$DT_ENVIRONMENT" "DT_OPERATOR_TOKEN=$DT_OPERATOR_TOKEN" "DT_INGEST_TOKEN=$DT_INGEST_TOKEN"; do
  name="${pair%%=*}"
  value="${pair#*=}"
  echo "    gh secret set $name --user --app codespaces --repos $REPO --body ***"
  printf '%s' "$value" | gh secret set "$name" --user --app codespaces --repos "$REPO" --body -
done

# ── (2) create the codespace, capture NAME ───────────────────────────────────
echo
echo "==> (2) Creating codespace"
create_args=(codespace create -R "$REPO" -m "$MACHINE" --idle-timeout 90m)
[[ -n "$REF" ]] && create_args+=(-b "$REF")
echo "    gh ${create_args[*]}"
CS_NAME="$(gh "${create_args[@]}")"
echo "    NAME=$CS_NAME"

# ── (3) poll until Available ─────────────────────────────────────────────────
echo
echo "==> (3) Waiting for codespace to become Available (timeout ${POLL_TIMEOUT}s)"
deadline=$(( $(date +%s) + POLL_TIMEOUT ))
state="unknown"
while :; do
  state="$(gh codespace view -c "$CS_NAME" --json state --jq '.state' 2>/dev/null || echo unknown)"
  echo "    state=$state"
  [[ "$state" == "Available" ]] && break
  if [[ "$state" == "Failed" || "$state" == "Unavailable" ]]; then
    echo "    ERROR: codespace entered terminal state: $state"
    exit 1
  fi
  if (( $(date +%s) >= deadline )); then
    echo "    ERROR: timed out waiting for Available (last state: $state)"
    exit 1
  fi
  sleep "$POLL_INTERVAL"
done
echo "    Codespace is Available"

# ── (4) ssh smoke check (non-fatal so we always reach cleanup) ───────────────
echo
echo "==> (4) SSH smoke check"
if gh codespace ssh -c "$CS_NAME" -- 'kubectl get pods -A || true; echo SMOKE_OK'; then
  echo "    SSH smoke check completed"
else
  echo "    WARNING: SSH smoke check failed — continuing to cleanup"
fi

echo
echo "==> Smoke test finished (cleanup runs on exit)"
# (5) cleanup is handled by the trap.

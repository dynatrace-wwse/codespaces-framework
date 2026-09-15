#!/bin/bash
# Integration test: dtwiz (dynatrace-oss/dtwiz) + K3d
#
# Validates the dtwiz-101 bootcamp training path end-to-end, platform-token-only:
# installs the dtwiz CLI, points it at a tenant with a PLATFORM token, and runs
# the same commands a learner runs — status, analyze, install kubernetes — then
# asserts the operator + DynaKube came up in a fresh K3d cluster and tears down.
#
# Why this suite exists: dtwiz uses DT_PLATFORM_TOKEN (dt0s16), NOT the classic
# DT_OPERATOR_TOKEN/DT_INGEST_TOKEN the other DT suites use, and operator >=1.10
# accepts the platform token directly in the DynaKube secret. This is the
# platform-token-native rollout the bootcamp trains on.
#
# Credentials — TWO ways in, and the order is the point:
#
#   1. DT_ENVIRONMENT + DT_PLATFORM_TOKEN. The learner's case: a provisioned
#      training environment already holds a platform token that the enablement app
#      minted with the tenant's own OAuth client. The suite uses it as given.
#
#   2. DT_ENVIRONMENT + DT_OAUTH_CLIENT_ID/_SECRET/_RESOURCE. Anywhere that is not
#      a provisioned environment — a dev box, a CI runner, the ops platform — the
#      suite MINTS its own short-lived dt0s16 from the tenant's own OAuth client,
#      runs with it, and revokes it at teardown.
#
# (2) is the harness path and exists so this suite can run where no platform token
# is lying around. It is NOT a substitute for (1): if DT_PLATFORM_TOKEN is set, the
# mint is skipped entirely. A long-lived platform token pasted into a secret store
# is the thing both paths exist to avoid — it is a standing credential nobody
# rotates, and it proves nothing about the scopes the app actually grants.
#
# Run: bash .devcontainer/test/integration_dtwiz_k3d.sh
source .devcontainer/util/source_framework.sh
source .devcontainer/test/mint_platform_token.sh

printInfoSection "=== dtwiz K3d integration test | arch: $ARCH ==="

_run_env=$(detectRunEnvironment)
printInfo "Environment: $_run_env | Arch: $ARCH"

# The scopes a dtwiz platform token carries, as dtwiz documents them (README
# "Platform token scopes" — the profile the Quickstart "auto discovery" token
# provisions). This list is the LEARNER's profile, copied, and that is the whole
# point: a harness token wider than the learner's would hide exactly the scope
# failure this suite is meant to catch. If a mint refuses one of these, that is a
# finding about what the tenant grants the client — report it, do not drop it.
DTWIZ_PLATFORM_SCOPES=(
  # Data acquisition
  data-acquisition:events:ingest data-acquisition:logs:ingest data-acquisition:metrics:ingest
  # Extensions
  extensions:configurations:read extensions:configurations:write
  extensions:definitions:read extensions:definitions:write
  # Fleet management — installer download, ActiveGate token creation, connection info
  fleet-management:activegate.connection-info:read fleet-management:activegate.tokens:create
  fleet-management:activegates:download fleet-management:activegates:read
  fleet-management:activegates:write fleet-management:cluster-id:read
  fleet-management:container-images:read fleet-management:oneagent.connection-info:read
  fleet-management:oneagent.tokens:read fleet-management:oneagents:download
  fleet-management:public-addresses:read
  # OpenPipeline ingest
  openpipeline:bizevents:ingest openpipeline:events:ingest openpipeline:events.custom:ingest
  openpipeline:events.davis:ingest openpipeline:events.sdlc:ingest
  openpipeline:events.sdlc.custom:ingest openpipeline:events.smartscape:ingest
  openpipeline:logs:ingest openpipeline:metrics:ingest openpipeline:security.events:ingest
  openpipeline:security.events.custom:ingest openpipeline:traces:ingest
  # RUM
  rum:frontends:write rum:manual-insertion-tags:read
  # Settings
  settings:objects:read settings:objects:write
  # Storage (Grail)
  storage:buckets:read storage:entities:read storage:events:read storage:events:write
  storage:files:write storage:logs:read storage:logs:write storage:metrics:read
  storage:metrics:write storage:smartscape:read storage:spans:read storage:system:read
)

# Credentials check — fail fast before any cluster setup.
assertEnvVariable DT_ENVIRONMENT

# One teardown, armed before anything can fail and re-armed after the dtwiz
# installer overwrites it (see step 2). Revoking a minted token is the part that
# must not be skipped: it is a live credential on a real tenant.
_dtwizTeardown() {
  revokeMintedPlatformToken
  # The dtwiz installer's own cleanup, which our trap displaced.
  case "${_dtwiz_installer_workdir:-}" in
    /tmp/*) rm -rf "$_dtwiz_installer_workdir" ;;
  esac
}

# Use the environment's platform token if it has one; otherwise mint one for this
# run. Registered for revocation before anything else can fail: a minted token
# must not outlive the run even if the next line is what breaks.
printInfoSection "Platform token"
trap '_dtwizTeardown' EXIT INT TERM
if ! mintPlatformTokenForTest "${DTWIZ_PLATFORM_SCOPES[@]}"; then
  exit 1
fi
# No pattern here on purpose: assertEnvVariable prints the value when a pattern
# fails to match, and the value is a credential.
assertEnvVariable DT_PLATFORM_TOKEN
printInfo "Platform token source: ${DT_PLATFORM_TOKEN_SOURCE}"

# Pre-test cleanup: remove any clusters left from postCreate so we start clean.
printInfo "Pre-test cleanup: removing any existing K3d clusters..."
k3d cluster list -o json 2>/dev/null \
  | python3 -c "import sys,json; [print(c['name']) for c in json.load(sys.stdin)]" 2>/dev/null \
  | xargs -r k3d cluster delete 2>/dev/null || true

# 1. Fresh K3d cluster (dtwiz names the cluster from the kube context)
printInfoSection "1/5  Starting K3d cluster"
export CLUSTER_ENGINE=k3d
# startK3dCluster returns 1 when the cluster never became reachable, and ignoring
# that return marched the whole suite on against no cluster: dtwiz installed, ran,
# and "failed" at the last assertion, which reads as a dtwiz defect and is not one.
# Measured 2026-09-15 on a host whose port 80 was already bound.
if ! startK3dCluster; then
  printError "❌ K3d cluster did not come up — nothing below this line can mean anything"
  printError "   If port 80/443 is taken on this host, set K3D_LB_HTTP_PORT/K3D_LB_HTTPS_PORT/K3D_API_PORT"
  exit 1
fi

# 2. Install the dtwiz CLI (official installer)
printInfoSection "2/5  Installing dtwiz CLI"
# Sourced, not executed, because that is what the dtwiz Quickstart tells a learner
# to do — and it has a consequence: the installer sets its OWN
# `trap 'rm -rf "$WORK_DIR"' EXIT INT TERM` in THIS shell, which silently replaces
# ours. The first version of this suite lost its token revocation exactly here and
# said nothing about it; the minted token simply outlived the run. Re-arm below.
source <(curl -sSL https://raw.githubusercontent.com/dynatrace-oss/dtwiz/main/scripts/install.sh)
_dtwiz_installer_workdir="${WORK_DIR:-}"
trap '_dtwizTeardown' EXIT INT TERM
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
if ! command -v dtwiz >/dev/null 2>&1; then
  printError "❌ dtwiz not on PATH after install"
  deleteK3dCluster; exit 1
fi
printInfo "dtwiz version: $(dtwiz --version 2>/dev/null || echo unknown)"

# 3. dtwiz connectivity — token must validate against the tenant
#
# This is also what proves a MINTED token is a working one. A mint returning 200
# is not a token that works: effective permission is the declared scopes ∩ the IAM
# policy of the token's owner, and nothing checks that at creation time. The first
# real call against the tenant is the check.
printInfoSection "3/5  dtwiz status (platform-token auth)"
if dtwiz status 2>&1 | tee /tmp/dtwiz_status.log | grep -qiE "Platform Token:.*valid|valid \("; then
  printInfo "✅ dtwiz authenticated to $DT_ENVIRONMENT (token source: ${DT_PLATFORM_TOKEN_SOURCE})"
else
  printError "❌ dtwiz status did not confirm a valid platform token"
  cat /tmp/dtwiz_status.log
  deleteK3dCluster; exit 1
fi
dtwiz analyze 2>&1 | head -20 || true

# 4. dtwiz install kubernetes — deploys the operator with the platform token
printInfoSection "4/5  dtwiz install kubernetes"
# `| tail -40` alone shows nothing until the command ends, and this command runs for
# over ten minutes (12m30s measured on ARM64/K3d, 2026-09-15) while it waits for the
# tenant to confirm the deployment. A run that looks hung for twelve minutes is one
# somebody kills. Tee the live output to a file so a stuck run can be read while it
# is stuck, and keep the last 40 lines on the console as before.
if ! dtwiz install kubernetes --yes 2>&1 | tee /tmp/dtwiz_install.log | tail -40; then
  printWarn "dtwiz install kubernetes returned non-zero — checking cluster state anyway"
fi

# 5. Assertions — operator + DynaKube present
printInfoSection "5/5  Assertions"
assertRunningPod dynatrace operator
if kubectl get dynakube -n dynatrace >/dev/null 2>&1; then
  printInfo "✅ DynaKube created by dtwiz:"
  kubectl get dynakube -n dynatrace --no-headers
else
  printError "❌ No DynaKube after dtwiz install kubernetes"
  kubectl -n dynatrace get pods
  deleteK3dCluster; exit 1
fi

# Cleanup
printInfoSection "Cleanup: deleting K3d cluster"
deleteK3dCluster

printInfoSection "✅ dtwiz K3d test PASSED (arch: $ARCH, token source: ${DT_PLATFORM_TOKEN_SOURCE})"

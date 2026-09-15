#!/usr/bin/env bats
# Tests for assertEnvVariable secret masking — fix/mask-secrets-in-assertions
#
# Sentinel design: "assertEnvVariable: failing pattern on *TOKEN*" would FAIL on the
# unfixed function (which emits $var_value raw) and PASSES on the fixed one (which
# emits [REDACTED …]). The companion "sentinel grep is live" test proves the grep
# would have caught the pre-fix disclosure — so the absence above is not a vacuous pass.
#
# Both directions are covered:
#   (1) sensitive var/token-shaped value → masked in error output
#   (2) non-sensitive var + plain value  → value still printed (diagnostic preserved)
#
# Fixture construction — tokens are assembled at runtime from separate components
# (class prefix, public-id segment, secret body) and never written as a complete
# three-segment literal.  This prevents secret-scanner false positives on obviously
# synthetic test data while keeping the runtime value in the exact shape the masking
# rule keys on.  Do NOT collapse these into inline literals — that re-triggers the
# scanner on a public repo and forces another round of this fix.

# Emit a synthetic DT-style token value.  No part of the full three-segment string
# ever appears as a literal in this file.
# Usage: _fake_token <class> <pubid> <body-char> <body-len>
# Example: _fake_token dt0s16 AAAAAAAAAAAAAAAAAAAAAAAA B 50
_fake_token() {
  local cls="$1" pub="$2" char="$3" len="$4"
  local body; body="$(printf "${char}%.0s" $(seq 1 "$len"))"
  printf '%s.%s.%s' "$cls" "$pub" "$body"
}

setup() {
  export TEST_DIR
  TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util"
  mkdir -p "$FAKE_REPO/.devcontainer/test"

  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export ENV_FILE="$FAKE_REPO/.devcontainer/.env"

  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN="" BLUE="" CYAN="" YELLOW="" ORANGE="" RED="" LILA="" NORMAL="" RESET=""
thickline="" halfline="" thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"
export ENV_FILE
COUNT_FILE="$REPO_PATH/.devcontainer/util/.count"
export COUNT_FILE
INSTANTIATION_TYPE="local-docker-container"
VARSEOF

  # Copy the real test_functions.sh (contains assertEnvVariable + _assertEnvVar_display)
  cp "$BATS_TEST_DIRNAME/../../test/test_functions.sh" \
     "$FAKE_REPO/.devcontainer/test/test_functions.sh"

  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"

  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" \
     "$FAKE_REPO/.devcontainer/util/functions.sh"

  kubectl() { return 1; }
  export -f kubectl
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source ".devcontainer/util/functions.sh"
}

# ============================================================
# _assertEnvVar_display unit tests
# ============================================================

@test "_assertEnvVar_display: *TOKEN* name → redacted, secret body absent" {
  source_functions
  local secret; secret="$(_fake_token dt0s16 AAAAAAAAAAAAAAAAAAAAAAAA B 50)"
  local result; result="$(_assertEnvVar_display "MY_TOKEN" "$secret")"
  [[ "$result" == *"REDACTED"* ]]
  [[ "$result" != *"AAAAAAAAAAAAAAAAAAAAAA"* ]]
  [[ "$result" != *"BBBBBBBBBBBBBB"* ]]
}

@test "_assertEnvVar_display: *SECRET* name → redacted" {
  source_functions
  local result; result="$(_assertEnvVar_display "MY_SECRET" "supersecretvalue99")"
  [[ "$result" == *"REDACTED"* ]]
  [[ "$result" != *"supersecret"* ]]
}

@test "_assertEnvVar_display: *PASSWORD* name → redacted" {
  source_functions
  local result; result="$(_assertEnvVar_display "DB_PASSWORD" "mypassword123")"
  [[ "$result" == *"REDACTED"* ]]
  [[ "$result" != *"mypassword"* ]]
}

@test "_assertEnvVar_display: *KEY* name → redacted" {
  source_functions
  local result; result="$(_assertEnvVar_display "API_KEY" "apikey12345")"
  [[ "$result" == *"REDACTED"* ]]
  [[ "$result" != *"apikey"* ]]
}

@test "_assertEnvVar_display: *CREDENTIAL* name → redacted" {
  source_functions
  local result; result="$(_assertEnvVar_display "GH_CREDENTIAL" "ghcr_secretstuff")"
  [[ "$result" == *"REDACTED"* ]]
  [[ "$result" != *"ghcr_secret"* ]]
}

@test "_assertEnvVar_display: dt0s16 value shape → redacted even with generic name" {
  source_functions
  local secret; secret="$(_fake_token dt0s16 SAMPLE8X Y 66)"
  local result; result="$(_assertEnvVar_display "FOO_VAR" "$secret")"
  [[ "$result" == *"REDACTED"* ]]
  [[ "$result" != *"SAMPLE8X"* ]]
}

@test "_assertEnvVar_display: dt0c01 value shape → redacted" {
  source_functions
  local secret; secret="$(_fake_token dt0c01 XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX Z 44)"
  local result; result="$(_assertEnvVar_display "SOME_VAR" "$secret")"
  [[ "$result" == *"REDACTED"* ]]
  [[ "$result" != *"XXXXXXXXXXXXXXXXX"* ]]
}

@test "_assertEnvVar_display: DT token redacted form shows token class prefix" {
  source_functions
  local secret; secret="$(_fake_token dt0s16 SAMPLE8X Y 66)"
  local result; result="$(_assertEnvVar_display "MY_TOKEN" "$secret")"
  # The public token class (e.g. dt0s16.) must appear — it is not sensitive
  local cls_prefix="dt0s16."
  [[ "$result" == *"$cls_prefix"* ]]
}

@test "_assertEnvVar_display: redacted form includes length" {
  source_functions
  # Assemble from parts — no three-segment literal in source
  local cls="dt0c01" pub="XXXX" body="YYYY"
  local secret="${cls}.${pub}.${body}"
  local expected_len="${#secret}"
  local result; result="$(_assertEnvVar_display "MY_TOKEN" "$secret")"
  [[ "$result" == *"len=$expected_len"* ]]
}

@test "_assertEnvVar_display: non-sensitive name + plain value → value unchanged" {
  source_functions
  local result; result="$(_assertEnvVar_display "MY_APP_URL" "https://staging.example.com")"
  [[ "$result" == "https://staging.example.com" ]]
}

@test "_assertEnvVar_display: non-sensitive name + short plain value → value unchanged" {
  source_functions
  local result; result="$(_assertEnvVar_display "APP_ENV" "staging")"
  [[ "$result" == "staging" ]]
}

# ============================================================
# assertEnvVariable — masking in the error path (both directions)
# ============================================================

# Sentinel: would FAIL on the unfixed function (raw value in output),
# PASSES on the fixed function (value is REDACTED).
@test "assertEnvVariable: failing pattern on *TOKEN* var — secret NOT in output (sentinel)" {
  source_functions
  local secret; secret="$(_fake_token dt0s16 AAAAAAAAAAAAAAAAAAAAAAAA B 50)"
  export MY_TOKEN="$secret"

  run assertEnvVariable MY_TOKEN "WILL_NOT_MATCH_THIS"

  [ "$status" -eq 1 ]
  [[ "$output" != *"AAAAAAAAAAAAAAAAAAAAAA"* ]]
  [[ "$output" != *"BBBBBBBBBBBBBB"* ]]
  [[ "$output" == *"REDACTED"* ]]
}

# Proves the grep above is live: the token IS findable when echoed raw.
# Without this, the absence test above could be vacuously true (wrong needle).
@test "assertEnvVariable: sentinel grep is live — finds fake token in raw echo" {
  local secret; secret="$(_fake_token dt0s16 AAAAAAAAAAAAAAAAAAAAAAAA B 50)"
  run bash -c "printf '%s' '$secret'"
  [[ "$output" == *"AAAAAAAAAAAAAAAAAAAAAA"* ]]
}

# Non-sensitive variable: value must still be printed (diagnostic preserved).
@test "assertEnvVariable: failing pattern on non-sensitive var — value IS in output" {
  source_functions
  export MY_APP_URL="https://staging.example.com"

  run assertEnvVariable MY_APP_URL "WILL_NOT_MATCH"

  [ "$status" -eq 1 ]
  [[ "$output" == *"staging.example.com"* ]]
}

# Token-shaped value in a non-sensitive-named variable is also masked.
# This test would FAIL if the value-shape branch of _assertEnvVar_display were
# removed (verified: output would show the raw token, not REDACTED).
@test "assertEnvVariable: failing pattern on token-shaped value in generic name — masked" {
  source_functions
  local secret; secret="$(_fake_token dt0c01 XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX Z 44)"
  export FOO_VAR="$secret"

  run assertEnvVariable FOO_VAR "WILL_NOT_MATCH"

  [ "$status" -eq 1 ]
  [[ "$output" == *"REDACTED"* ]]
  [[ "$output" != *"XXXXXXXXXXXXXXXXXXXXX"* ]]
}

# Passing match: function succeeds without disclosing the value.
@test "assertEnvVariable: matching pattern succeeds and does not print secret" {
  source_functions
  local cls="dt0s16"
  local secret; secret="$(_fake_token "$cls" AAAAAAAAAAAAAAAAAAAAAAAA B 50)"
  export MY_TOKEN="$secret"

  run assertEnvVariable MY_TOKEN "$cls"

  [ "$status" -eq 0 ]
  [[ "$output" != *"AAAAAAAAAAAAAAAAAAAAAA"* ]]
}

# Variable not set: error says the variable name, not the value (which is empty).
@test "assertEnvVariable: unset variable fails with var name in error" {
  source_functions
  unset UNSET_TOKEN

  run assertEnvVariable UNSET_TOKEN

  [ "$status" -eq 1 ]
  [[ "$output" == *"UNSET_TOKEN"* ]]
  [[ "$output" == *"not set"* ]]
}

# No pattern: asserting only presence succeeds and does not print the secret.
@test "assertEnvVariable: no pattern — presence only, secret not printed" {
  source_functions
  local secret; secret="$(_fake_token dt0s16 AAAAAAAAAAAAAAAAAAAAAAAA B 50)"
  export MY_TOKEN="$secret"

  run assertEnvVariable MY_TOKEN

  [ "$status" -eq 0 ]
  [[ "$output" != *"AAAAAAAAAAAAAAAAAAAAAA"* ]]
}

#!/usr/bin/env bats
# Tests for mintPlatformTokenForTest / revokeMintedPlatformToken.
#
# The decision these cover is the one that matters and the one that is easy to get
# backwards: a platform token supplied by the environment ALWAYS wins, and minting
# is the harness fallback. So every test here asserts against a STUB minter that
# records whether it was called — "did not mint" has to be observable, not assumed.

setup() {
  export TEST_DIR="$(mktemp -d)"
  export STUB_MARKER="$TEST_DIR/minter-was-called"

  # Minimal printers: the helper is written against the framework's, and pulling in
  # functions.sh here would test the framework's logging, not this decision.
  cat > "$TEST_DIR/printers.sh" <<'PEOF'
printInfo()  { echo "INFO: $*"; }
printWarn()  { echo "WARN: $*"; }
printError() { echo "ERROR: $*"; }
PEOF

  # A stub minter standing in for mint_platform_token.py: records the call and its
  # arguments, then answers in the real one's format (id on line 1, token on line 2).
  cat > "$TEST_DIR/mint_platform_token.py" <<'MEOF'
#!/usr/bin/env python3
import os, sys
open(os.environ["STUB_MARKER"], "a").write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1] == "mint":
    print("dt0s16.STUBID")
    print("dt0s16.STUBID.stub-token-value-that-is-long-enough-to-look-real")
sys.exit(0)
MEOF

  cp "$BATS_TEST_DIRNAME/../mint_platform_token.sh" "$TEST_DIR/mint_platform_token.sh"

  unset DT_PLATFORM_TOKEN DT_PLATFORM_TOKEN_ID DT_PLATFORM_TOKEN_SOURCE
  unset DT_OAUTH_CLIENT_ID DT_OAUTH_CLIENT_SECRET DT_OAUTH_RESOURCE
  export DT_ENVIRONMENT="https://tenant.example.com"
}

teardown() {
  rm -rf "$TEST_DIR"
}

_load() {
  source "$TEST_DIR/printers.sh"
  source "$TEST_DIR/mint_platform_token.sh"
}

@test "a platform token from the environment is used as-is and nothing is minted" {
  run bash -c "
    source '$TEST_DIR/printers.sh'
    source '$TEST_DIR/mint_platform_token.sh'
    export DT_PLATFORM_TOKEN='dt0s16.LEARNER.supplied-by-the-training-environment'
    mintPlatformTokenForTest scope:one scope:two
    echo \"source=\$DT_PLATFORM_TOKEN_SOURCE token=\$DT_PLATFORM_TOKEN\"
  "
  [ "$status" -eq 0 ]
  [[ "$output" == *"source=environment"* ]]
  [[ "$output" == *"token=dt0s16.LEARNER.supplied-by-the-training-environment"* ]]
  # The whole point: the minter was never invoked.
  [ ! -f "$STUB_MARKER" ]
}

@test "no token and no OAuth client fails with a message that names what is missing" {
  run bash -c "
    source '$TEST_DIR/printers.sh'
    source '$TEST_DIR/mint_platform_token.sh'
    mintPlatformTokenForTest scope:one
  "
  [ "$status" -ne 0 ]
  [[ "$output" == *"DT_OAUTH_CLIENT_ID"* ]]
  [[ "$output" == *"DT_OAUTH_CLIENT_SECRET"* ]]
  [[ "$output" == *"DT_OAUTH_RESOURCE"* ]]
  [ ! -f "$STUB_MARKER" ]
}

@test "no token plus an OAuth client mints, and passes the scopes through unchanged" {
  run bash -c "
    source '$TEST_DIR/printers.sh'
    source '$TEST_DIR/mint_platform_token.sh'
    export DT_OAUTH_CLIENT_ID=id DT_OAUTH_CLIENT_SECRET=secret DT_OAUTH_RESOURCE=urn:dtaccount:x
    mintPlatformTokenForTest settings:objects:read storage:entities:read
    echo \"source=\$DT_PLATFORM_TOKEN_SOURCE id=\$DT_PLATFORM_TOKEN_ID\"
  "
  [ "$status" -eq 0 ]
  [[ "$output" == *"source=minted"* ]]
  [[ "$output" == *"id=dt0s16.STUBID"* ]]
  grep -q "^mint settings:objects:read storage:entities:read$" "$STUB_MARKER"
}

@test "mint with no scopes is refused — a scopeless token would pass for free" {
  run bash -c "
    source '$TEST_DIR/printers.sh'
    source '$TEST_DIR/mint_platform_token.sh'
    export DT_OAUTH_CLIENT_ID=id DT_OAUTH_CLIENT_SECRET=secret DT_OAUTH_RESOURCE=urn:dtaccount:x
    mintPlatformTokenForTest
  "
  [ "$status" -ne 0 ]
  [ ! -f "$STUB_MARKER" ]
}

@test "revoke does nothing to a token the environment supplied" {
  run bash -c "
    source '$TEST_DIR/printers.sh'
    source '$TEST_DIR/mint_platform_token.sh'
    export DT_PLATFORM_TOKEN='dt0s16.LEARNER.supplied' DT_PLATFORM_TOKEN_SOURCE=environment
    revokeMintedPlatformToken
    echo \"token-still-set=\${DT_PLATFORM_TOKEN:+yes}\"
  "
  [ "$status" -eq 0 ]
  [[ "$output" == *"token-still-set=yes"* ]]
  [ ! -f "$STUB_MARKER" ]
}

@test "revoke removes a token this run minted" {
  run bash -c "
    source '$TEST_DIR/printers.sh'
    source '$TEST_DIR/mint_platform_token.sh'
    export DT_OAUTH_CLIENT_ID=id DT_OAUTH_CLIENT_SECRET=secret DT_OAUTH_RESOURCE=urn:dtaccount:x
    mintPlatformTokenForTest settings:objects:read
    revokeMintedPlatformToken
    echo \"token-still-set=\${DT_PLATFORM_TOKEN:+yes} source=\$DT_PLATFORM_TOKEN_SOURCE\"
  "
  [ "$status" -eq 0 ]
  [[ "$output" != *"token-still-set=yes"* ]]
  [[ "$output" == *"source=revoked"* ]]
  grep -q "^revoke dt0s16.STUBID$" "$STUB_MARKER"
}

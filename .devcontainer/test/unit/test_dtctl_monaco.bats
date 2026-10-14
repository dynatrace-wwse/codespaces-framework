#!/usr/bin/env bats
# Tests for enableDtctl / disableDtctl / installMonaco / deployMonaco /
# undeployMonaco / verifySha256 (ENH-089, ENH-091).
#
# Nothing touches the network or a tenant: curl is a stub that serves release
# assets from a fixtures dir, and the "released" dtctl / monaco binaries are
# stub scripts that log their arguments. The release checksums are computed
# from those fixtures, so a one-byte change to an asset MUST be refused.
#
# Token values are sentinels; the tests grep every output and every file the
# functions write for them.

DTCTL_SENTINEL="dt0s16.SENTINELDTCTL.secretpart"
MONACO_SENTINEL="dt0s16.SENTINELMONACO.secretpart"

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  export XDG_CONFIG_HOME="$HOME/.config"
  export XDG_DATA_HOME="$HOME/.local/share"
  mkdir -p "$HOME"
  unset DTCTL_CONFIG DTCTL_VERSION MONACO_VERSION DTCTL_TOKEN_VAR MONACO_TOKEN_VAR DT_SESSION_ID GITHUB_USER

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test"
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export FRAMEWORK_CACHE=""
  # The machine is whatever the uname stub says (_releaseArch reads uname -m only).
  export FAKE_UNAME_M="x86_64"

  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN="" BLUE="" CYAN="" YELLOW="" ORANGE="" RED="" LILA="" NORMAL="" RESET=""
thickline="" halfline="" thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"
export ENV_FILE
VARSEOF
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  export BIN="$TEST_DIR/bin" FIXTURES="$TEST_DIR/fixtures"
  export CURL_LOG="$TEST_DIR/curl.log" DTCTL_LOG="$TEST_DIR/dtctl.log" MONACO_LOG="$TEST_DIR/monaco.log"
  export INSTALL_DIR="$TEST_DIR/install"
  export DTCTL_INSTALL_DIR="$INSTALL_DIR" MONACO_INSTALL_DIR="$INSTALL_DIR"
  mkdir -p "$BIN" "$FIXTURES" "$INSTALL_DIR"
  # Only the stubs and the system tools: no real dtctl/monaco can leak in.
  export PATH="$INSTALL_DIR:$BIN:/usr/bin:/bin"

  make_curl_stub
  make_uname_stub
  make_fixtures

  export DT_ENVIRONMENT="https://abc12345.apps.dynatrace.com"
  export DT_HOSTGROUP="Learner_Ab12"
  export DT_DTCTL_TOKEN="$DTCTL_SENTINEL"
  export DT_MONACO_TOKEN="$MONACO_SENTINEL"
}

teardown() {
  rm -rf "$TEST_DIR"
}

make_curl_stub() {
  cat > "$BIN/curl" <<'EOF'
#!/bin/bash
ARGS="$*"
out="" url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift; out="$1" ;;
    -w|--max-time|--connect-timeout) shift ;;
    -*) ;;
    *) url="$1" ;;
  esac
  shift
done
echo "$url" >> "$CURL_LOG"
[ -n "${CURL_ARGS_LOG:-}" ] && echo "$ARGS" >> "$CURL_ARGS_LOG"
case "$url" in
  https://api.github.com/repos/*/releases/latest) echo '{"url": "x", "tag_name": "v9.9.9", "name": "v9.9.9"}'; exit 0 ;;
esac
f="$FIXTURES/$(basename "$url")"
[ -f "$f" ] || exit 22
if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi
EOF
  chmod +x "$BIN/curl"
}

make_uname_stub() {
  cat > "$BIN/uname" <<'EOF'
#!/bin/bash
[ "$1" = "-m" ] && { echo "$FAKE_UNAME_M"; exit 0; }
exec /usr/bin/uname "$@"
EOF
  chmod +x "$BIN/uname"
}

make_fixtures() {
  local stage="$TEST_DIR/stage"
  mkdir -p "$stage"
  # The "released" dtctl: logs arguments, writes a config shaped like dtctl's.
  cat > "$stage/dtctl" <<'EOF'
#!/bin/bash
echo "dtctl $*" >> "$DTCTL_LOG"
cfg="${DTCTL_CONFIG:-$XDG_CONFIG_HOME/dtctl/config}"
mkdir -p "$(dirname "$cfg")"
# A child that echoes the token (what _devToolStep must mask).
[ -n "${DTCTL_STUB_ECHO_TOKEN:-}" ] && echo "debug: token=$DT_DTCTL_TOKEN"
[ -n "${DTCTL_STUB_FAIL:-}" ] && [ "$1 $2" = "config set-context" ] && exit 1
# Real dtctl 0.43 opens the config for writing on every `config` write and
# fails on a read-only file ("failed to write config file: permission denied").
if [ "$1" = "config" ] && [ "$2" != "current-context" ] && [ -f "$cfg" ] && [ ! -w "$cfg" ]; then
  echo "Error: failed to write config file: open $cfg: permission denied" >&2; exit 1
fi
if [ "$1 $2" = "config set-context" ]; then
  # Like real dtctl, setting an existing context updates it, not a second copy.
  grep -qxF "    - name: $3" "$cfg" 2>/dev/null && : > "$cfg"
  echo "    - name: $3" >> "$cfg"; shift 3
  while [ $# -gt 0 ]; do
    case "$1" in
      --environment) echo "        environment: $2" >> "$cfg" ;;
      --safety-level) echo "        safety-level: $2" >> "$cfg" ;;
    esac
    shift
  done
elif [ "$1 $2" = "config set-credentials" ]; then
  [ "$4" = "--token" ] && echo "      token: $5" >> "$cfg"
elif [ "$1 $2" = "config use-context" ]; then
  # Real dtctl 0.42 rewrites the config with every ${VAR} expanded here.
  [ -f "$cfg" ] && perl -pi -e 's/\$\{(\w+)\}/$ENV{$1}/g' "$cfg"
fi
exit 0
EOF
  chmod +x "$stage/dtctl"
  local a
  for a in amd64 arm64; do
    # A different mtime per arch: the two archives must not share a sha256,
    # or a test could not tell which arch's pin was checked.
    tar --mtime="@$([ "$a" = amd64 ] && echo 1000000000 || echo 1100000000)" \
      -czf "$FIXTURES/dtctl_9.9.9_linux_${a}.tar.gz" -C "$stage" dtctl
  done
  (cd "$FIXTURES" && sha256sum dtctl_9.9.9_linux_amd64.tar.gz dtctl_9.9.9_linux_arm64.tar.gz > checksums.txt)

  # The "released" monaco: logs arguments + whether the token is present.
  cat > "$FIXTURES/monaco-linux-amd64" <<'EOF'
#!/bin/bash
echo "monaco $* | LEARNER_PREFIX=${LEARNER_PREFIX:-} | token-set=$([ -n "$DT_MONACO_TOKEN" ] && echo yes || echo no) | cwd=$PWD" >> "$MONACO_LOG"
if [ "$1 $2" = "generate deletefile" ]; then
  dir="" file="delete.yaml"
  while [ $# -gt 0 ]; do
    case "$1" in -o) shift; dir="$1" ;; --file) shift; file="$1" ;; esac
    shift
  done
  [ -n "${MONACO_STUB_NO_DELETEFILE:-}" ] || echo "delete: []" > "$dir/$file"
fi
exit "${MONACO_STUB_RC:-0}"
EOF
  { cat "$FIXTURES/monaco-linux-amd64"; echo "# arm64 build"; } > "$FIXTURES/monaco-linux-arm64"
  chmod +x "$FIXTURES/monaco-linux-arm64"
  for a in amd64 arm64; do
    (cd "$FIXTURES" && sha256sum "monaco-linux-$a" > "monaco-linux-$a.sha256")
  done
}

corrupt_one_byte() {
  printf 'X' | dd of="$1" bs=1 seek=40 count=1 conv=notrunc 2>/dev/null
}

source_functions() {
  cd "$FAKE_REPO"
  source .devcontainer/util/variables.sh
  source .devcontainer/util/functions.sh
}

make_monaco_project() {
  # make_monaco_project <phase> — one dashboard document + one settings object.
  local d="$FAKE_REPO/monaco/$1"
  mkdir -p "$d/dashboards"
  cat > "$d/dashboards/config.yaml" <<'EOF'
configs:
- id: learner-dashboard
  config:
    name:
      type: environment
      name: LEARNER_PREFIX
    template: dashboard.json
    parameters:
      prefix:
        type: environment
        name: LEARNER_PREFIX
  type:
    document:
      kind: dashboard
EOF
  echo '{}' > "$d/dashboards/dashboard.json"
  cat > "$d/settings.yaml" <<'EOF'
configs:
  - id: learner-tag
    type: {settings: {schema: builtin:tags.auto-tagging, scope: environment}}
    config:
      template: tag.json
EOF
}

refute() {
  # `! cmd` does not fail a bats test (set -e ignores a negated command).
  if "$@"; then echo "unexpectedly succeeded: $*"; return 1; fi
}

no_sentinel_anywhere() {
  # Fails if either token value appears in the captured output or in any file
  # the functions wrote (config, run dir, stub logs).
  if echo "$output" | grep -q "SENTINEL"; then echo "token value in output"; return 1; fi
  if grep -rls "SENTINEL" "$HOME" "$FAKE_REPO" "$CURL_LOG" "$DTCTL_LOG" "$MONACO_LOG"; then
    echo "token value written to the files above"; return 1
  fi
}

# ── verifySha256 ──────────────────────────────────────────────────────────────

@test "verifySha256: matching checksum passes" {
  source_functions
  run verifySha256 "$FIXTURES/monaco-linux-amd64" "$(cut -d' ' -f1 "$FIXTURES/monaco-linux-amd64.sha256")"
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "Checksum OK"
}

@test "verifySha256: one-byte-corrupt file is refused (negative control)" {
  source_functions
  local good
  good=$(cut -d' ' -f1 "$FIXTURES/monaco-linux-amd64.sha256")
  corrupt_one_byte "$FIXTURES/monaco-linux-amd64"
  run verifySha256 "$FIXTURES/monaco-linux-amd64" "$good"
  [ "$status" -eq 1 ]
  echo "$output" | grep -q "MISMATCH"
}

@test "verifySha256: empty or malformed expected checksum is refused, not passed" {
  source_functions
  run verifySha256 "$FIXTURES/monaco-linux-amd64" ""
  [ "$status" -eq 1 ]
  run verifySha256 "$FIXTURES/monaco-linux-amd64" "deadbeef"
  [ "$status" -eq 1 ]
  run verifySha256 "$TEST_DIR/absent" "$(printf '%064d' 0)"
  [ "$status" -eq 1 ]
}

# ── enableDtctl ───────────────────────────────────────────────────────────────

@test "enableDtctl: latest release verified, installed, readonly context with a token REFERENCE" {
  source_functions
  run enableDtctl
  [ "$status" -eq 0 ]
  [ -x "$INSTALL_DIR/dtctl" ]
  grep -q "api.github.com/repos/dynatrace-oss/dtctl/releases/latest" "$CURL_LOG"
  grep -q "/v9.9.9/dtctl_9.9.9_linux_amd64.tar.gz" "$CURL_LOG"
  grep -q "/v9.9.9/checksums.txt" "$CURL_LOG"
  grep -q -- "config set-context enablement --environment https://abc12345.apps.dynatrace.com --token-ref enablement --safety-level readonly" "$DTCTL_LOG"
  grep -q -- "config use-context enablement" "$DTCTL_LOG"
  grep -qF 'safety-level: readonly' "$XDG_CONFIG_HOME/dtctl/config"
  grep -qF 'token: ${DT_DTCTL_TOKEN}' "$XDG_CONFIG_HOME/dtctl/config"
  no_sentinel_anywhere
}

@test "enableDtctl: one-byte-corrupt archive is refused — nothing installed, dtctl never run" {
  source_functions
  corrupt_one_byte "$FIXTURES/dtctl_9.9.9_linux_amd64.tar.gz"
  run enableDtctl
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "MISMATCH"
  [ ! -e "$INSTALL_DIR/dtctl" ]
  [ ! -e "$DTCTL_LOG" ]
}

@test "enableDtctl: asset missing from checksums.txt is refused" {
  source_functions
  grep -v amd64 "$FIXTURES/checksums.txt" > "$FIXTURES/c" && mv "$FIXTURES/c" "$FIXTURES/checksums.txt"
  run enableDtctl
  [ "$status" -ne 0 ]
  [ ! -e "$INSTALL_DIR/dtctl" ]
}

@test "enableDtctl: credentials are written AFTER use-context (dtctl expands \${VAR} on use-context)" {
  source_functions
  run enableDtctl
  [ "$status" -eq 0 ]
  [ "$(grep -n 'config use-context' "$DTCTL_LOG" | cut -d: -f1)" -lt "$(grep -n 'config set-credentials' "$DTCTL_LOG" | cut -d: -f1)" ]
}

@test "enableDtctl: a config holding the token VALUE is refused and removed" {
  source_functions
  # A dtctl that bakes the value in, whatever the order.
  sed -i 's|echo "      token: \$5"|{ echo "      token: \$5"; echo "      leaked: $DT_DTCTL_TOKEN"; }|' "$TEST_DIR/stage/dtctl"
  tar -czf "$FIXTURES/dtctl_9.9.9_linux_amd64.tar.gz" -C "$TEST_DIR/stage" dtctl
  (cd "$FIXTURES" && sha256sum dtctl_9.9.9_linux_amd64.tar.gz > checksums.txt)
  run enableDtctl
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "held the token VALUE" || { echo "$output" | sed "s/SENTINEL[A-Za-z.]*/<redacted>/g"; false; }
  [ ! -e "$XDG_CONFIG_HOME/dtctl/config" ]
  if echo "$output" | grep -q SENTINEL; then echo "value printed"; return 1; fi
}

@test "enableDtctl: a value WITHOUT the reference line is still found and removed" {
  source_functions
  # A dtctl that writes the expanded value instead of the reference.
  sed -i 's|echo "      token: \$5"|echo "      token: $DT_DTCTL_TOKEN"|' "$TEST_DIR/stage/dtctl"
  tar -czf "$FIXTURES/dtctl_9.9.9_linux_amd64.tar.gz" -C "$TEST_DIR/stage" dtctl
  (cd "$FIXTURES" && sha256sum dtctl_9.9.9_linux_amd64.tar.gz > checksums.txt)
  run enableDtctl
  [ "$status" -ne 0 ]
  [ ! -e "$XDG_CONFIG_HOME/dtctl/config" ]
  if echo "$output" | grep -q SENTINEL; then echo "value printed"; return 1; fi
}

@test "enableDtctl: a failing dtctl command after use-context leaves no token value on disk" {
  source_functions
  # use-context expands (as dtctl 0.42 does), then set-credentials fails.
  sed -i 's|^exit 0$|[ "$1 $2" = "config set-credentials" ] \&\& exit 1; exit 0|' "$TEST_DIR/stage/dtctl"
  sed -i 's|echo "    - name: \$3" >> "\$cfg"; shift 3|echo "    - name: $3" >> "$cfg"; echo "      token: \${DT_DTCTL_TOKEN}" >> "$cfg"; shift 3|' "$TEST_DIR/stage/dtctl"
  tar -czf "$FIXTURES/dtctl_9.9.9_linux_amd64.tar.gz" -C "$TEST_DIR/stage" dtctl
  (cd "$FIXTURES" && sha256sum dtctl_9.9.9_linux_amd64.tar.gz > checksums.txt)
  run enableDtctl
  [ "$status" -ne 0 ]
  if grep -rqs SENTINEL "$HOME"; then echo "token value left on disk"; return 1; fi
}

@test "enableDtctl --readwrite opts into readwrite-mine; never readwrite-all" {
  source_functions
  run enableDtctl --readwrite
  [ "$status" -eq 0 ]
  grep -qF 'safety-level: readwrite-mine' "$XDG_CONFIG_HOME/dtctl/config"
  refute grep -q "readwrite-all" "$XDG_CONFIG_HOME/dtctl/config"
}

@test "enableDtctl: DTCTL_VERSION pin skips the API and downloads that version" {
  source_functions
  cp "$FIXTURES/dtctl_9.9.9_linux_amd64.tar.gz" "$FIXTURES/dtctl_1.2.3_linux_amd64.tar.gz"
  (cd "$FIXTURES" && sha256sum dtctl_1.2.3_linux_amd64.tar.gz > checksums.txt)
  DTCTL_VERSION=v1.2.3 run enableDtctl
  [ "$status" -eq 0 ]
  refute grep -q "api.github.com" "$CURL_LOG"
  grep -q "/v1.2.3/dtctl_1.2.3_linux_amd64.tar.gz" "$CURL_LOG"
}

@test "version pins that are not x.y.z are refused before any download" {
  source_functions
  DTCTL_VERSION="1.2.3/../../evil" run enableDtctl
  [ "$status" -ne 0 ]
  MONACO_VERSION="latest" run installMonaco
  [ "$status" -ne 0 ]
  [ ! -e "$CURL_LOG" ]
}

@test "enableDtctl: arm64 machine downloads the arm64 asset; unknown arch refused" {
  source_functions
  FAKE_UNAME_M=aarch64 run enableDtctl
  [ "$status" -eq 0 ]
  grep -q "dtctl_9.9.9_linux_arm64.tar.gz" "$CURL_LOG"
  FAKE_UNAME_M=s390x run enableDtctl
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "unsupported architecture"
}

@test "enableDtctl: --token-var changes the reference, and the value is never written" {
  source_functions
  export MY_TOKEN="$DTCTL_SENTINEL"
  run enableDtctl --token-var MY_TOKEN
  [ "$status" -eq 0 ]
  grep -qF 'token: ${MY_TOKEN}' "$XDG_CONFIG_HOME/dtctl/config"
  no_sentinel_anywhere
}

@test "enableDtctl: refuses without DT_ENVIRONMENT, an unknown flag, or a bad variable name" {
  source_functions
  DT_ENVIRONMENT="" run enableDtctl
  [ "$status" -ne 0 ]
  run enableDtctl --bogus
  [ "$status" -eq 2 ]
  run enableDtctl --token-var 'X;rm -rf /'
  [ "$status" -ne 0 ]
  [ ! -e "$INSTALL_DIR/dtctl" ]
}

@test "enableDtctl: unset token variable warns but still configures" {
  source_functions
  unset DT_DTCTL_TOKEN
  run enableDtctl
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "DT_DTCTL_TOKEN is not set"
}

# ── disableDtctl ──────────────────────────────────────────────────────────────

@test "disableDtctl removes the binary and dtctl's config and token files" {
  source_functions
  enableDtctl >/dev/null
  mkdir -p "$XDG_DATA_HOME/dtctl/oauth-tokens" && touch "$XDG_DATA_HOME/dtctl/oauth-tokens/t"
  [ -x "$INSTALL_DIR/dtctl" ]
  run disableDtctl
  [ "$status" -eq 0 ]
  [ ! -e "$INSTALL_DIR/dtctl" ]
  [ ! -e "$XDG_CONFIG_HOME/dtctl" ]
  [ ! -e "$XDG_DATA_HOME/dtctl" ]
}

# ── installMonaco ─────────────────────────────────────────────────────────────

@test "installMonaco: latest release verified against .sha256 and installed" {
  source_functions
  run installMonaco
  [ "$status" -eq 0 ]
  [ -x "$INSTALL_DIR/monaco" ]
  grep -q "api.github.com/repos/Dynatrace/dynatrace-configuration-as-code/releases/latest" "$CURL_LOG"
  grep -q "/v9.9.9/monaco-linux-amd64.sha256" "$CURL_LOG"
}

@test "installMonaco: one-byte-corrupt binary is refused and not installed" {
  source_functions
  corrupt_one_byte "$FIXTURES/monaco-linux-amd64"
  run installMonaco
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "MISMATCH"
  [ ! -e "$INSTALL_DIR/monaco" ]
}

@test "installMonaco: MONACO_VERSION pin skips the API" {
  source_functions
  MONACO_VERSION=2.30.1 run installMonaco
  [ "$status" -eq 0 ]
  refute grep -q "api.github.com" "$CURL_LOG"
  grep -q "/v2.30.1/monaco-linux-amd64" "$CURL_LOG"
}

# ── deployMonaco ──────────────────────────────────────────────────────────────

@test "deployMonaco tenant: per-learner project <repo>-<DT_SESSION_ID>, LEARNER_PREFIX, delete file first" {
  source_functions
  make_monaco_project tenant
  run deployMonaco tenant
  [ "$status" -eq 0 ]
  local run_dir="$FAKE_REPO/.devcontainer/.monaco/tenant"
  grep -qx "  - name: test-enablement-learner-ab12" "$run_dir/manifest.yaml"
  grep -qx "            name: DT_MONACO_TOKEN" "$run_dir/manifest.yaml"
  [ -f "$run_dir/delete.yaml" ]
  [ -f "$run_dir/project/dashboards/config.yaml" ]
  [ "$(cat "$FAKE_REPO/.devcontainer/.monaco/.gitignore")" = "*" ]
  # deletefile generated BEFORE deploy, both with the learner prefix and the token present
  sed -n 1p "$MONACO_LOG" | grep -q "^monaco generate deletefile manifest.yaml"
  sed -n 2p "$MONACO_LOG" | grep -q "^monaco deploy manifest.yaml -e learner | LEARNER_PREFIX=learner-ab12 | token-set=yes"
  no_sentinel_anywhere
}

@test "deployMonaco: two learners get two different projects" {
  source_functions
  make_monaco_project tenant
  DT_HOSTGROUP="alice" MONACO_RUN_DIR="$TEST_DIR/a" run deployMonaco tenant
  [ "$status" -eq 0 ]
  DT_HOSTGROUP="bob" MONACO_RUN_DIR="$TEST_DIR/b" run deployMonaco tenant
  [ "$status" -eq 0 ]
  grep -q "name: test-enablement-alice$" "$TEST_DIR/a/tenant/manifest.yaml"
  grep -q "name: test-enablement-bob$" "$TEST_DIR/b/tenant/manifest.yaml"
}

@test "deployMonaco env: a classic type is refused as not supported — non-zero, nothing deployed" {
  source_functions
  make_monaco_project env
  cat > "$FAKE_REPO/monaco/env/synthetic.yaml" <<'EOF'
configs:
- id: http-check
  config:
    name: check
    template: monitor.json
  type: synthetic-monitor
EOF
  run deployMonaco env
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "not supported yet"
  echo "$output" | grep -q "synthetic.yaml: synthetic-monitor"
  [ ! -e "$MONACO_LOG" ]
}

@test "deployMonaco: block-form classic api and automation types are refused too" {
  source_functions
  mkdir -p "$FAKE_REPO/monaco/tenant"
  cat > "$FAKE_REPO/monaco/tenant/a.yaml" <<'EOF'
configs:
  - id: web
    type:
      api: application-web
    config: {name: app, template: a.json}
  - id: wf
    config: {name: wf, template: w.json}
    type:
      automation:
        resource: workflow
EOF
  run deployMonaco tenant
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "a.yaml: api"
  echo "$output" | grep -q "a.yaml: automation"
}

@test "deployMonaco: a configs file the parser cannot read fails closed" {
  source_functions
  mkdir -p "$FAKE_REPO/monaco/tenant"
  echo 'configs: [{id: x, type: synthetic-monitor, config: {name: a, template: a.json}}]' > "$FAKE_REPO/monaco/tenant/flow.yaml"
  echo 'something: else' > "$FAKE_REPO/monaco/tenant/other.yaml"
  run deployMonaco tenant
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "flow.yaml: (configs: not a block list"
  echo "$output" | grep -q "other.yaml: (no top-level configs: list"
  [ ! -e "$MONACO_LOG" ]
}

@test "deployMonaco: parameter 'type: environment' is NOT mistaken for a config type" {
  source_functions
  make_monaco_project tenant
  run _monacoUnsupportedTypes "$FAKE_REPO/monaco/tenant"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "deployMonaco: no monaco/<phase> folder is a no-op (opt-in)" {
  source_functions
  run deployMonaco tenant
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "nothing to deploy"
  [ ! -e "$MONACO_LOG" ]
  [ ! -e "$CURL_LOG" ]
}

@test "deployMonaco: refuses without the token, without a session id, or with a bad phase" {
  source_functions
  make_monaco_project tenant
  DT_MONACO_TOKEN="" run deployMonaco tenant
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "DT_MONACO_TOKEN is not set"
  DT_HOSTGROUP="" run deployMonaco tenant
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "No usable DT_SESSION_ID"
  run deployMonaco everything
  [ "$status" -eq 2 ]
  [ ! -e "$MONACO_LOG" ]
}

@test "deployMonaco: a session id that is not RFC-1123 is refused" {
  source_functions
  make_monaco_project tenant
  DT_SESSION_ID="a: b" run deployMonaco tenant
  [ "$status" -ne 0 ]
  echo "$output" | grep -q "No usable DT_SESSION_ID"
}

@test "deployMonaco: redeploy keeps the previous delete entries (removed configs stay deletable)" {
  source_functions
  make_monaco_project tenant
  deployMonaco tenant >/dev/null
  local d="$FAKE_REPO/.devcontainer/.monaco/tenant/delete.yaml"
  printf 'delete:\n- project: p\n  type: document\n  id: gone-dashboard\n- project: p\n  type: document\n  id: kept\n' > "$d"
  # The stub generates a fresh file holding only "kept".
  sed -i 's|echo "delete: \[\]" > "\$dir/\$file"|printf "delete:\\n- project: p\\n  type: document\\n  id: kept\\n" > "$dir/$file"|' "$INSTALL_DIR/monaco"
  run deployMonaco tenant
  [ "$status" -eq 0 ]
  grep -q "id: gone-dashboard" "$d"
  [ "$(grep -c 'id: kept' "$d")" -eq 1 ]
  [ "$(head -1 "$d")" = "delete:" ]
}

@test "deployMonaco: no delete file → refuses to deploy" {
  source_functions
  make_monaco_project tenant
  MONACO_STUB_NO_DELETEFILE=1 run deployMonaco tenant
  [ "$status" -ne 0 ]
  refute grep -q "^monaco deploy" "$MONACO_LOG"
}

@test "deployMonaco: a failed monaco deploy returns non-zero" {
  source_functions
  make_monaco_project tenant
  MONACO_STUB_RC=1 run deployMonaco tenant
  [ "$status" -ne 0 ]
}

# ── undeployMonaco ────────────────────────────────────────────────────────────

@test "undeployMonaco deletes with the persisted delete file and clears the run dir" {
  source_functions
  make_monaco_project tenant
  deployMonaco tenant >/dev/null
  run undeployMonaco
  [ "$status" -eq 0 ]
  grep -q "^monaco delete --manifest manifest.yaml --file delete.yaml -e learner" "$MONACO_LOG"
  [ ! -e "$FAKE_REPO/.devcontainer/.monaco/tenant" ]
  echo "$output" | grep -q "monaco/env: nothing deployed"
  no_sentinel_anywhere
}

@test "undeployMonaco: failed delete keeps the delete file and returns non-zero" {
  source_functions
  make_monaco_project tenant
  deployMonaco tenant >/dev/null
  MONACO_STUB_RC=1 run undeployMonaco tenant
  [ "$status" -ne 0 ]
  [ -f "$FAKE_REPO/.devcontainer/.monaco/tenant/delete.yaml" ]
}

@test "undeployMonaco: nothing deployed is a clean no-op" {
  source_functions
  run undeployMonaco
  [ "$status" -eq 0 ]
  [ ! -e "$MONACO_LOG" ]
}

# ── zsh: the learner's shell ──────────────────────────────────────────────────

@test "zsh: deploy + undeploy both phases work when sourced into zsh (no word splitting)" {
  command -v zsh >/dev/null || skip "zsh not installed"
  make_monaco_project tenant
  make_monaco_project env
  run zsh -c 'cd "$FAKE_REPO" && source .devcontainer/util/variables.sh && source .devcontainer/util/functions.sh \
    && deployMonaco tenant && deployMonaco env && undeployMonaco'
  [ "$status" -eq 0 ]
  [ "$(grep -c '^monaco delete' "$MONACO_LOG")" -eq 2 ]
}

# ── installDevTools (every environment, from finalizePostCreation) ────────────

pin_devtools() {
  # The framework pin is 9.9.9, with reviewed hashes taken from the fixtures.
  export DTCTL_VERSION=9.9.9 MONACO_VERSION=9.9.9
  export DTCTL_PIN_VERSION=9.9.9 MONACO_PIN_VERSION=9.9.9
  local a A
  for a in amd64 arm64; do
    A=$(echo "$a" | tr '[:lower:]' '[:upper:]')
    export "DTCTL_PIN_SHA256_$A=$(sha256sum "$FIXTURES/dtctl_9.9.9_linux_$a.tar.gz" | cut -d' ' -f1)"
    export "DTCTL_PIN_BIN_SHA256_$A=$(sha256sum "$TEST_DIR/stage/dtctl" | cut -d' ' -f1)"
    export "MONACO_PIN_SHA256_$A=$(sha256sum "$FIXTURES/monaco-linux-$a" | cut -d' ' -f1)"
    export "MONACO_PIN_BIN_SHA256_$A=$(sha256sum "$FIXTURES/monaco-linux-$a" | cut -d' ' -f1)"
  done
}

# fake_present <tool> <version> — a present binary that reports <version>;
# prints its sha256.
fake_present() {
  printf '#!/bin/bash\necho "%s version %s"\n' "$1" "$2" > "$INSTALL_DIR/$1"
  chmod +x "$INSTALL_DIR/$1"
  sha256sum "$INSTALL_DIR/$1" | cut -d' ' -f1
}

@test "installDevTools: default installs dtctl and Monaco at the pinned versions, verified" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  run installDevTools
  [ "$status" -eq 0 ]
  [ -x "$INSTALL_DIR/dtctl" ]
  [ -x "$INSTALL_DIR/monaco" ]
  grep -q "dtctl/releases/download/v9.9.9/checksums.txt" "$CURL_LOG"
  grep -q "configuration-as-code/releases/download/v9.9.9/monaco-linux-amd64.sha256" "$CURL_LOG"
  refute grep -q "releases/latest" "$CURL_LOG"
  echo "$output" | grep -q "Checksum OK"
}

@test "installDevTools: no DT_DTCTL_TOKEN → binary only, no dtctl context" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  run installDevTools
  [ "$status" -eq 0 ]
  refute grep -q "config set-context" "$DTCTL_LOG"
  [ ! -f "$XDG_CONFIG_HOME/dtctl/config" ]
}

@test "installDevTools: DT_DTCTL_TOKEN set → readonly context with a REFERENCE, value never written" {
  source_functions
  pin_devtools
  run installDevTools
  [ "$status" -eq 0 ]
  grep -q "safety-level: readonly" "$XDG_CONFIG_HOME/dtctl/config"
  grep -qF 'token: ${DT_DTCTL_TOKEN}' "$XDG_CONFIG_HOME/dtctl/config"
  no_sentinel_anywhere
}

@test "installDevTools: token set but no DT_ENVIRONMENT → installed, warned, no context" {
  source_functions
  pin_devtools
  unset DT_ENVIRONMENT
  run installDevTools
  [ "$status" -eq 0 ]
  [ -x "$INSTALL_DIR/dtctl" ]
  echo "$output" | grep -q "DT_ENVIRONMENT is not"
  refute grep -q "config set-context" "$DTCTL_LOG"
}

@test "installDevTools: FRAMEWORK_DEVTOOLS=off (any case) skips everything — no download" {
  source_functions
  pin_devtools
  local v
  for v in off OFF false 0 no; do
    FRAMEWORK_DEVTOOLS="$v" run installDevTools
    [ "$status" -eq 0 ]
    echo "$output" | grep -q "skipped"
  done
  [ ! -s "$CURL_LOG" ]
  [ ! -e "$INSTALL_DIR/dtctl" ]
  [ ! -e "$INSTALL_DIR/monaco" ]
}

@test "installDevTools: FRAMEWORK_DEVTOOLS=off from .devcontainer/.env is honoured" {
  echo "FRAMEWORK_DEVTOOLS=off" > "$FAKE_REPO/.devcontainer/.env"
  echo '[ -e "$ENV_FILE" ] && source "$ENV_FILE"' >> "$FAKE_REPO/.devcontainer/util/variables.sh"
  source_functions
  pin_devtools
  run installDevTools
  [ "$status" -eq 0 ]
  [ ! -s "$CURL_LOG" ]
}

@test "installDevTools: corrupt dtctl archive is refused, NOT fatal, Monaco still installs" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  corrupt_one_byte "$FIXTURES/dtctl_9.9.9_linux_amd64.tar.gz"
  run installDevTools
  [ "$status" -eq 0 ]
  [ ! -e "$INSTALL_DIR/dtctl" ]
  [ -x "$INSTALL_DIR/monaco" ]
  echo "$output" | grep -q "MISMATCH"
  echo "$output" | grep -q "devtools (non-fatal): dtctl not installed"
}

@test "installDevTools: corrupt Monaco binary is refused, NOT fatal" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  corrupt_one_byte "$FIXTURES/monaco-linux-amd64"
  run installDevTools
  [ "$status" -eq 0 ]
  [ ! -e "$INSTALL_DIR/monaco" ]
  [ -x "$INSTALL_DIR/dtctl" ]
  echo "$output" | grep -q "devtools (non-fatal): Monaco not installed"
}

@test "installDevTools: a failure is not counted as a container-creation error" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  rm -f "$FIXTURES"/dtctl_* "$FIXTURES"/monaco-*
  run installDevTools
  [ "$status" -eq 0 ]
  # Its real output holds tagged "failed" lines...
  echo "$output" | grep -i -E 'error|failed' | grep -qF 'devtools (non-fatal)'
  # ...and verifyContainerCreation's own filter drops every one of them.
  local left
  left=$(printf '%s\n' "$output" | grep -i -E 'error|failed' | _creationNoiseFilter || true)
  [ -z "$left" ]
}

@test "_creationNoiseFilter: drops a tagged devtools line, keeps an untagged real failure" {
  source_functions
  local tagged="devtools (non-fatal): [x| ERROR |t| dtctl: download of v9.9.9 failed  |"
  [ -z "$(printf '%s\n' "$tagged" | _creationNoiseFilter || true)" ]
  # Negative control: the same line without the tag is a real error and stays.
  [ -n "$(printf '%s\n' "[x| ERROR |t| dtctl: download of v9.9.9 failed  |" | _creationNoiseFilter || true)" ]
}

@test "verifyContainerCreation uses _creationNoiseFilter (the filter the tests exercise)" {
  grep -qF '"$raw_errors" | _creationNoiseFilter' "$BATS_TEST_DIRNAME/../../util/functions.sh"
}

@test "installDevTools: binaries matching the pinned sha256 + version are kept, not downloaded" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  export DTCTL_PIN_BIN_SHA256_AMD64="$(fake_present dtctl 9.9.9)"
  export MONACO_PIN_BIN_SHA256_AMD64="$(fake_present monaco 9.9.9)"
  run installDevTools
  [ "$status" -eq 0 ]
  [ ! -s "$CURL_LOG" ]
  echo "$output" | grep -q "dtctl v9.9.9 already present"
  echo "$output" | grep -q "Monaco v9.9.9 already present"
}

@test "installDevTools: a present binary reporting the RIGHT version but other bytes is replaced" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  fake_present dtctl 9.9.9 >/dev/null     # says 9.9.9, but its sha256 is NOT the pin
  fake_present monaco 9.9.9 >/dev/null
  run installDevTools
  [ "$status" -eq 0 ]
  grep -q "dtctl_9.9.9_linux_amd64.tar.gz" "$CURL_LOG"
  grep -q "monaco-linux-amd64" "$CURL_LOG"
  refute grep -q 'echo "dtctl version 9.9.9"' "$INSTALL_DIR/dtctl"
}

@test "installDevTools: a binary at a DIFFERENT version is replaced by the verified pin" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  export DTCTL_PIN_BIN_SHA256_AMD64="$(fake_present dtctl 9.9.10)"   # bytes match, version does not
  run installDevTools
  [ "$status" -eq 0 ]
  grep -q "dtctl_9.9.9_linux_amd64.tar.gz" "$CURL_LOG"
  refute grep -q 'echo "dtctl version 9.9.10"' "$INSTALL_DIR/dtctl"
}

@test "installDevTools: asset whose sha256 differs from the REVIEWED pin is refused (release file agrees)" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  # The release checksum files still match the assets: only the committed pin catches this.
  export DTCTL_PIN_SHA256_AMD64="$(printf '%064d' 1)" MONACO_PIN_SHA256_AMD64="$(printf '%064d' 2)"
  run installDevTools
  [ "$status" -eq 0 ]
  [ ! -e "$INSTALL_DIR/dtctl" ]
  [ ! -e "$INSTALL_DIR/monaco" ]
  [ "$(echo "$output" | grep -c "MISMATCH")" -eq 2 ]
}

@test "installDtctl: a version other than the pin warns it has no reviewed sha256" {
  source_functions
  pin_devtools
  export DTCTL_VERSION=9.9.8
  cp "$FIXTURES/dtctl_9.9.9_linux_amd64.tar.gz" "$FIXTURES/dtctl_9.9.8_linux_amd64.tar.gz"
  (cd "$FIXTURES" && sha256sum dtctl_9.9.8_linux_amd64.tar.gz >> checksums.txt)
  run installDtctl
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "no reviewed sha256 pinned"
}

@test "variables.sh: committed pins are 64-hex for both tools and both arches" {
  local real="$BATS_TEST_DIRNAME/../../util/variables.sh" v
  for v in DTCTL_PIN_SHA256_AMD64 DTCTL_PIN_SHA256_ARM64 DTCTL_PIN_BIN_SHA256_AMD64 DTCTL_PIN_BIN_SHA256_ARM64 \
           MONACO_PIN_SHA256_AMD64 MONACO_PIN_SHA256_ARM64; do
    grep -qE "^$v=[0-9a-f]{64}$" "$real"
  done
}

@test "installDevTools: arm64 machine installs the arm64 assets against the arm64 pins" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  export FAKE_UNAME_M=aarch64
  run installDevTools
  [ "$status" -eq 0 ]
  grep -q "dtctl_9.9.9_linux_arm64.tar.gz" "$CURL_LOG"
  grep -q "monaco-linux-arm64" "$CURL_LOG"
  refute grep -q "amd64" "$CURL_LOG"
  [ -x "$INSTALL_DIR/dtctl" ]
  [ -x "$INSTALL_DIR/monaco" ]
}

@test "_releaseArch: \$ARCH cannot override the machine (uname -m only)" {
  source_functions
  export FAKE_UNAME_M=aarch64
  ARCH=x86_64 run _releaseArch
  [ "$output" = "arm64" ]
  export FAKE_UNAME_M=x86_64
  ARCH=aarch64 run _releaseArch
  [ "$output" = "amd64" ]
}

@test "installDevTools: no passwordless sudo → sudo -n fails fast, non-fatal" {
  [ "$(id -u)" -ne 0 ] || skip "root can write anywhere: the sudo path is never taken"
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  export SUDO_LOG="$TEST_DIR/sudo.log" SUBDIR="$INSTALL_DIR/locked"
  # A sudo that needs a password: -n fails at once, anything else would hang.
  printf '#!/bin/bash\necho "sudo $*" >> "$SUDO_LOG"\nif [ "$1" = "-n" ]; then echo "sudo: a password is required" >&2; exit 1; fi\nsleep 30; exit 1\n' > "$BIN/sudo"
  chmod +x "$BIN/sudo"
  mkdir -p "$SUBDIR"; chmod 0555 "$SUBDIR"
  DTCTL_INSTALL_DIR="$SUBDIR" MONACO_INSTALL_DIR="$SUBDIR" run timeout 20 bash -c \
    'cd "$FAKE_REPO"; source .devcontainer/util/variables.sh; source .devcontainer/util/functions.sh; installDevTools'
  chmod 0755 "$SUBDIR"
  [ "$status" -eq 0 ]
  [ -s "$SUDO_LOG" ]
  refute grep -qv '^sudo -n ' "$SUDO_LOG"
  echo "$output" | grep -q "devtools (non-fatal): dtctl not installed"
  echo "$output" | grep -q "devtools (non-fatal): Monaco not installed"
}

@test "_removeBinary uses sudo -n for a non-writable directory" {
  [ "$(id -u)" -ne 0 ] || skip "root can write anywhere: the sudo path is never taken"
  source_functions
  export SUDO_LOG="$TEST_DIR/sudo.log" SUBDIR="$INSTALL_DIR/locked"
  printf '#!/bin/bash\necho "sudo $*" >> "$SUDO_LOG"; [ "$1" = "-n" ] || sleep 30; exit 1\n' > "$BIN/sudo"
  chmod +x "$BIN/sudo"
  mkdir -p "$SUBDIR"; touch "$SUBDIR/dtctl"; chmod 0555 "$SUBDIR"
  run timeout 10 bash -c 'cd "$FAKE_REPO"; source .devcontainer/util/variables.sh; source .devcontainer/util/functions.sh; _removeBinary "$SUBDIR/dtctl"'
  chmod 0755 "$SUBDIR"
  [ "$status" -ne 124 ]
  grep -q '^sudo -n rm -f ' "$SUDO_LOG"
}

@test "installDevTools: every download has --connect-timeout 10 and fits one time budget" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  export CURL_ARGS_LOG="$TEST_DIR/curl-args.log" DEVTOOLS_TIME_BUDGET=60
  run installDevTools
  [ "$status" -eq 0 ]
  [ "$(wc -l < "$CURL_ARGS_LOG")" -eq 4 ]
  refute grep -v -- "--connect-timeout 10 " "$CURL_ARGS_LOG"
  local mt
  for mt in $(grep -o -- "--max-time [0-9]*" "$CURL_ARGS_LOG" | awk '{print $2}'); do
    [ "$mt" -le 60 ]
  done
}

@test "installDevTools: exhausted time budget downloads nothing, non-fatal" {
  source_functions
  pin_devtools
  unset DT_DTCTL_TOKEN
  DEVTOOLS_TIME_BUDGET=0 run installDevTools
  [ "$status" -eq 0 ]
  [ ! -s "$CURL_LOG" ]
  echo "$output" | grep -q "time budget exhausted"
  echo "$output" | grep -q "devtools (non-fatal): dtctl not installed"
  echo "$output" | grep -q "devtools (non-fatal): Monaco not installed"
}

@test "installDtctl / installMonaco: a failing mktemp refuses, no download" {
  source_functions
  pin_devtools
  printf '#!/bin/bash\nexit 1\n' > "$BIN/mktemp"; chmod +x "$BIN/mktemp"
  run installDtctl
  [ "$status" -eq 1 ]
  echo "$output" | grep -q "could not create a temporary directory"
  run installMonaco
  [ "$status" -eq 1 ]
  echo "$output" | grep -q "could not create a temporary directory"
  refute grep -q "releases/download" "$CURL_LOG"
}

@test "installDevTools: a child echoing the token has the value masked in the replay" {
  source_functions
  pin_devtools
  export DTCTL_STUB_ECHO_TOKEN=1
  run installDevTools
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "token=<redacted>"
  no_sentinel_anywhere
}

@test "installDevTools: the token is masked on the FAILURE replay too" {
  source_functions
  pin_devtools
  export DTCTL_STUB_ECHO_TOKEN=1 DTCTL_STUB_FAIL=1
  run installDevTools
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "devtools (non-fatal): debug: token=<redacted>"
  no_sentinel_anywhere
}

@test "enableDtctl: config left read-only, a later dtctl config write fails without leaking; re-run works" {
  [ "$(id -u)" -ne 0 ] || skip "root ignores file modes"
  source_functions
  run enableDtctl
  [ "$status" -eq 0 ]
  local cfg="$XDG_CONFIG_HOME/dtctl/config"
  [ ! -w "$cfg" ]
  run "$INSTALL_DIR/dtctl" config use-context enablement
  [ "$status" -ne 0 ]
  refute grep -q "SENTINEL" "$cfg"
  run enableDtctl
  [ "$status" -eq 0 ]
  [ ! -w "$cfg" ]
  grep -qF 'token: ${DT_DTCTL_TOKEN}' "$cfg"
}

@test "_maskTokens works under zsh (the learner's shell)" {
  command -v zsh >/dev/null || skip "zsh not installed"
  run zsh -c 'cd "$FAKE_REPO"; source .devcontainer/util/variables.sh; source .devcontainer/util/functions.sh >/dev/null; _maskTokens "a $DT_DTCTL_TOKEN b $DT_MONACO_TOKEN c"'
  [ "$status" -eq 0 ]
  [ "$output" = "a <redacted> b <redacted> c" ]
}

@test "finalizePostCreation runs installDevTools (every repo gets it with no edits)" {
  source_functions
  installDevTools() { echo "INSTALL_DEVTOOLS_RAN"; }
  verifyContainerCreation() { :; }
  postCodespaceTracker() { :; }
  CODESPACE_NAME="learner" run finalizePostCreation
  echo "$output" | grep -q "INSTALL_DEVTOOLS_RAN"
}

@test "variables.sh pins DTCTL_VERSION and MONACO_VERSION to x.y.z (never 'latest')" {
  source_functions
  local real="$BATS_TEST_DIRNAME/../../util/variables.sh" d m
  d=$(sed -n 's/^DTCTL_PIN_VERSION=//p' "$real")
  m=$(sed -n 's/^MONACO_PIN_VERSION=//p' "$real")
  grep -qxF 'DTCTL_VERSION="${DTCTL_VERSION:-$DTCTL_PIN_VERSION}"' "$real"
  grep -qxF 'MONACO_VERSION="${MONACO_VERSION:-$MONACO_PIN_VERSION}"' "$real"
  _isVersion "$d"
  _isVersion "$m"
}

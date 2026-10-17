#!/usr/bin/env bats
# Tests deployGitlab / gitlabSeedRepo / uninstallGitlab / gitlabCredentials and
# installCodeServer / startCodeServer / stopCodeServer / registerCodeServer
# (Perform developer sessions).
#
# Nothing touches a cluster or the network: kubectl, helm, curl and code-server
# are stubs on PATH that log their arguments and keep secret state in files.
# The secrets the stubs mint are sentinels, and the tests grep EVERY output and
# every logged argument list for them: a secret on a command line or in a log
# is a failure, not a style issue.

PAT_SENTINEL="glpat-SENTINELPAT0123456789"
RUNNER_SENTINEL="glrt-SENTINELRUNNER0123456789"

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  export XDG_CACHE_HOME="$HOME/.cache"
  export XDG_DATA_HOME="$HOME/.local/share"
  mkdir -p "$HOME"
  unset ORBITAL_ENVIRONMENT ORBITAL_JOB_ID CODESPACES CODESPACE_NAME K3D_CLUSTER_NAME \
        PASSWORD HASHED_PASSWORD CODE_SERVER_BIND CODE_SERVER_REGISTER GITLAB_EXTERNAL_URL \
        CODE_SERVER_VERSION CODE_SERVER_SHA256

  export FAKE_REPO="$TEST_DIR/workspaces/test-enablement"
  mkdir -p "$FAKE_REPO/.devcontainer/util" "$FAKE_REPO/.devcontainer/test"
  export REPO_PATH="$FAKE_REPO"
  export RepositoryName="test-enablement"
  export FRAMEWORK_CACHE=""
  export APP_REGISTRY="$TEST_DIR/app-registry"
  export EXTERNAL_IP="10.0.0.1"
  export MAGIC_DOMAIN="sslip.io"
  export CODE_SERVER_PORT=18443

  cat > "$FAKE_REPO/.devcontainer/util/variables.sh" <<'VARSEOF'
LOGNAME="test"
GREEN="" BLUE="" CYAN="" YELLOW="" ORANGE="" RED="" LILA="" NORMAL="" RESET=""
thickline="" halfline="" thinline=""
ENV_FILE="$REPO_PATH/.devcontainer/.env"
export ENV_FILE
VARSEOF
  # The real pins (versions + reviewed sha256), nothing else.
  grep -E '^(CODE_SERVER_PIN_|GITLAB_VERSION|GITLAB_RUNNER_CHART_VERSION)' \
    "$BATS_TEST_DIRNAME/../../util/variables.sh" >> "$FAKE_REPO/.devcontainer/util/variables.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/test/test_functions.sh"
  echo '# stub' > "$FAKE_REPO/.devcontainer/util/my_functions.sh"
  cp "$BATS_TEST_DIRNAME/../../util/functions.sh" "$FAKE_REPO/.devcontainer/util/functions.sh"

  export BIN="$TEST_DIR/bin" STATE="$TEST_DIR/state" ARGS_LOG="$TEST_DIR/args.log"
  mkdir -p "$BIN" "$STATE"
  : > "$ARGS_LOG"
  export PATH="$BIN:$PATH"
  make_kubectl_stub
  make_helm_stub
  make_curl_stub
}

teardown() {
  # Never leave a stub code-server running.
  local p
  p=$(cat "$XDG_CACHE_HOME/code-server/pid" 2>/dev/null)
  [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null; kill "$p" 2>/dev/null
  rm -rf "$TEST_DIR"
}

source_functions() {
  cd "$FAKE_REPO"
  source .devcontainer/util/variables.sh
  source .devcontainer/util/functions.sh
}

make_kubectl_stub() {
  # Logs argv. Secrets live in $STATE/secret-<name>-<key>. `create secret
  # --from-file=k=/dev/stdin` stores stdin; `get secret -o jsonpath` returns it
  # base64'd. `exec ... gitlab-rails` mints the PAT sentinel; `exec ... curl`
  # plays the GitLab API (the token must arrive on stdin, never in argv).
  cat > "$BIN/kubectl" <<'EOF'
#!/bin/bash
echo "kubectl $*" >> "$ARGS_LOG"
[ -n "${KUBECTL_NO_CLUSTER:-}" ] && [ "$1" = "cluster-info" ] && exit 1
args="$*"
case "$args" in
  *"create secret generic"*)
    name=$(echo "$args" | sed -E 's/.*generic ([^ ]+).*/\1/')
    for a in "$@"; do
      case "$a" in --from-file=*=/dev/stdin) k=${a#--from-file=}; k=${k%%=*}; cat > "$STATE/secret-$name-$k" ;; esac
    done
    [[ "$args" == *"--dry-run=client"* ]] && echo "apiVersion: v1"
    exit 0 ;;
  *"get secret"*"jsonpath"*)
    name=$(echo "$args" | sed -E 's/.*get secret ([^ ]+).*/\1/')
    key=$(echo "$args" | sed -E 's/.*\{\.data\.([^}]+)\}.*/\1/')
    [ -f "$STATE/secret-$name-$key" ] || exit 1
    base64 -w0 < "$STATE/secret-$name-$key"; exit 0 ;;
  *"get secret"*)
    name=$(echo "$args" | sed -E 's/.*get secret ([^ ]+).*/\1/')
    ls "$STATE"/secret-"$name"-* >/dev/null 2>&1; exit $? ;;
  *"gitlab-rails runner"*)
    echo "some rails warning"; echo; echo "GLPAT_SENTINEL_PLACEHOLDER"; exit 0 ;;
  *"exec -i deploy/gitlab"*"runners/verify"*)
    read -r t; [ "$t" = "$(cat "$STATE/secret-gitlab-runner-secret-runner-token" 2>/dev/null)" ] && printf 200 || printf 403; exit 0 ;;
  *"exec -i deploy/gitlab"*"curl"*)
    read -r hdr
    echo "$hdr" | grep -q "PRIVATE-TOKEN: $(cat "$STATE/secret-gitlab-pat-token" 2>/dev/null)" || { printf '\nHTTP 401\n'; exit 0; }
    case "$args" in
      *"/api/v4/user/runners"*) printf '{"id":7,"token":"RUNNER_SENTINEL_PLACEHOLDER"}\nHTTP 201\n' ;;
      *"/api/v4/user"*) printf '{"id":1}\nHTTP 200\n' ;;
      *"/api/v4/projects/root%2F"*) [ -f "$STATE/project" ] && printf '\nHTTP 200\n' || printf '\nHTTP 404\n' ;;
      *"/api/v4/projects"*) touch "$STATE/project"; printf '\nHTTP 201\n' ;;
      *) printf '\nHTTP 404\n' ;;
    esac
    exit 0 ;;
  *"apply -f -"*) cat >> "$STATE/applied.yaml"; echo "---" >> "$STATE/applied.yaml"; exit 0 ;;
esac
# create ... --dry-run=client -o yaml (namespace, configmap) -> emit something
[[ "$args" == *"--dry-run=client"* ]] && echo "apiVersion: v1"
exit 0
EOF
  sed -i "s/GLPAT_SENTINEL_PLACEHOLDER/$PAT_SENTINEL/; s/RUNNER_SENTINEL_PLACEHOLDER/$RUNNER_SENTINEL/" "$BIN/kubectl"
  chmod +x "$BIN/kubectl"
}

make_helm_stub() {
  printf '#!/bin/bash\necho "helm $*" >> "$ARGS_LOG"\ncase "$*" in *"--values -"*) cat > /dev/null ;; esac\nexit 0\n' > "$BIN/helm"
  chmod +x "$BIN/helm"
}

make_curl_stub() {
  # healthz: 200 once the stub code-server wrote its "listening" marker.
  # Release downloads: served from $TEST_DIR/fixtures.
  cat > "$BIN/curl" <<'EOF'
#!/bin/bash
echo "curl $*" >> "$ARGS_LOG"
out="" url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) shift; out="$1" ;; http*://*) url="$1" ;; esac
  shift
done
case "$url" in
  */healthz) [ -f "$TEST_DIR/cs-listening" ] && printf 200 || printf 000; exit 0 ;;
  https://github.com/coder/*)
    f="$TEST_DIR/fixtures/$(basename "$url")"
    [ -f "$f" ] || exit 22
    cp "$f" "$out"; exit 0 ;;
esac
exit 7
EOF
  chmod +x "$BIN/curl"
}

make_codeserver_fixture() {
  # make_codeserver_fixture <version> — a release tarball whose code-server
  # logs its argv + whether PASSWORD reached it, marks itself listening, sleeps.
  local v="$1" a d
  a=$(case "$(uname -m)" in x86_64|amd64) echo amd64 ;; *) echo arm64 ;; esac)
  d="$TEST_DIR/stage/code-server-${v}-linux-${a}"
  mkdir -p "$d/bin" "$TEST_DIR/fixtures"
  cat > "$d/bin/code-server" <<EOF
#!/bin/bash
if [ "\$1" = "--version" ]; then echo "$v abc123 with Code 1.0"; exit 0; fi
echo "code-server \$* | password-env=\$([ -n "\$PASSWORD" ] && echo yes || echo no)" >> "\$ARGS_LOG"
touch "\$TEST_DIR/cs-listening"
trap 'rm -f "\$TEST_DIR/cs-listening"; exit 0' TERM
while :; do sleep 1; done
EOF
  chmod +x "$d/bin/code-server"
  tar -C "$TEST_DIR/stage" -czf "$TEST_DIR/fixtures/code-server-${v}-linux-${a}.tar.gz" "code-server-${v}-linux-${a}"
  sha256sum "$TEST_DIR/fixtures/code-server-${v}-linux-${a}.tar.gz" | cut -d' ' -f1
}

no_secret_anywhere() {
  # no_secret_anywhere <text> — fails when <text>, the argv log, the registry
  # or anything under $HOME holds a generated or minted secret.
  local pw
  pw=$(cat "$STATE/secret-gitlab-root-password" 2>/dev/null || true)
  for s in "$PAT_SENTINEL" "$RUNNER_SENTINEL" ${pw:+"$pw"} ${PASSWORD:+"$PASSWORD"}; do
    if printf "%s" "$1" | grep -qF "$s"; then echo "secret in output: ${s:0:6}"; return 1; fi
    if grep -qF "$s" "$ARGS_LOG" "$APP_REGISTRY" 2>/dev/null; then echo "secret in argv/registry"; return 1; fi
    if grep -rqF "$s" "$HOME" 2>/dev/null; then echo "secret on disk under HOME: $(grep -rlF "$s" "$HOME")"; return 1; fi
  done
}

# ============================================================
# Omnibus config
# ============================================================

@test "_gitlabOmnibusConfig: https URL forces the forwarded scheme, plain 80, memory-constrained" {
  source_functions
  run _gitlabOmnibusConfig "https://gitlab--j1.staging.example.com"
  [ "$status" -eq 0 ]
  [[ "$output" == *"external_url 'https://gitlab--j1.staging.example.com'"* ]]
  [[ "$output" == *"nginx['listen_https'] = false"* ]]
  [[ "$output" == *"'X-Forwarded-Proto' => 'https'"* ]]
  [[ "$output" == *"'Host' => 'gitlab--j1.staging.example.com'"* ]]
  [[ "$output" == *"puma['worker_processes'] = 0"* ]]
  [[ "$output" == *"registry['enable'] = false"* ]]
}

@test "_gitlabOmnibusConfig: http URL does not claim https" {
  source_functions
  run _gitlabOmnibusConfig "http://gitlab.10.0.0.1.sslip.io"
  [ "$status" -eq 0 ]
  [[ "$output" != *"X-Forwarded-Proto"* ]]
  [[ "$output" == *"'Host' => 'gitlab.10.0.0.1.sslip.io'"* ]]
}

@test "_gitlabExternalUrl: Orbital subdomain on the staging domain from ORBITAL_BASE_URL" {
  export ORBITAL_ENVIRONMENT=true ORBITAL_JOB_ID=job1
  export ORBITAL_BASE_URL=https://staging.autonomous-enablements.whydevslovedynatrace.com
  source_functions
  # ORBITAL_APP_DOMAIN is derived in the real variables.sh; mimic it here.
  export ORBITAL_APP_DOMAIN=staging.autonomous-enablements.whydevslovedynatrace.com
  run _gitlabExternalUrl
  [ "$output" = "https://gitlab--job1.staging.autonomous-enablements.whydevslovedynatrace.com" ]
}

@test "variables.sh: ORBITAL_APP_DOMAIN follows ORBITAL_BASE_URL" {
  run bash -c "unset ORBITAL_APP_DOMAIN; ORBITAL_BASE_URL=https://staging.example.com/x; REPO_PATH=/nonexistent; source '$BATS_TEST_DIRNAME/../../util/variables.sh' >/dev/null 2>&1; echo \"\$ORBITAL_APP_DOMAIN\""
  [ "$output" = "staging.example.com" ]
}

# ============================================================
# deployGitlab
# ============================================================

@test "deployGitlab: refuses without a cluster" {
  export KUBECTL_NO_CLUSTER=1
  source_functions
  run deployGitlab
  [ "$status" -eq 1 ]
  [[ "$output" == *"no Kubernetes cluster"* ]]
  ! grep -q "helm" "$ARGS_LOG"
}

@test "deployGitlab: full run registers gitlab once, installs the runner, prints no secret" {
  export ORBITAL_ENVIRONMENT=true ORBITAL_JOB_ID=job1 ORBITAL_APP_DOMAIN=staging.example.com
  source_functions
  run deployGitlab
  [ "$status" -eq 0 ]
  # password generated into the secret through stdin
  [ -s "$STATE/secret-gitlab-root-password" ]
  [ "$(wc -c < "$STATE/secret-gitlab-root-password")" -eq 24 ]
  # PAT + runner token stored
  [ "$(cat "$STATE/secret-gitlab-pat-token")" = "$PAT_SENTINEL" ]
  [ "$(cat "$STATE/secret-gitlab-runner-secret-runner-token")" = "$RUNNER_SENTINEL" ]
  # registered like any app: exactly one row, Orbital subdomain in field 7
  [ "$(grep -c '^gitlab|' "$APP_REGISTRY")" -eq 1 ]
  [ "$(awk -F'|' '$1=="gitlab"{print $2"|"$3"|"$4"|"$7}' "$APP_REGISTRY")" = "gitlab|gitlab|80|gitlab--job1" ]
  grep -q 'proxy-body-size: "512m"' "$STATE/applied.yaml"
  grep -q 'image: gitlab/gitlab-ce:18.4.0-ce.0' "$STATE/applied.yaml"
  grep -q 'helm upgrade --install gitlab-runner gitlab-runner --repo https://charts.gitlab.io --version 0.81.0' "$ARGS_LOG"
  [[ "$output" == *"https://gitlab--job1.staging.example.com"* ]]
  [[ "$output" == *"get secret gitlab-root"* ]]
  no_secret_anywhere "$output"
}

@test "deployGitlab: re-run keeps the password, the token and the runner; one registry row" {
  source_functions
  run deployGitlab
  [ "$status" -eq 0 ]
  local pw_before
  pw_before=$(cat "$STATE/secret-gitlab-root-password")
  : > "$ARGS_LOG"
  run deployGitlab
  [ "$status" -eq 0 ]
  [ "$(cat "$STATE/secret-gitlab-root-password")" = "$pw_before" ]
  ! grep -q "create secret generic gitlab-root" "$ARGS_LOG"
  ! grep -q "gitlab-rails runner" "$ARGS_LOG"
  ! grep -q "create secret generic gitlab-runner-secret" "$ARGS_LOG"
  [[ "$output" == *"token present and valid"* ]]
  [ "$(grep -c '^gitlab|' "$APP_REGISTRY")" -eq 1 ]
  no_secret_anywhere "$output"
}

@test "deployGitlab: a token GitLab no longer accepts is re-minted" {
  source_functions
  run deployGitlab
  printf 'glpat-revoked' > "$STATE/secret-gitlab-pat-token"
  # the stub API compares against the stored token, so make it reject this one
  sed -i 's#grep -q "PRIVATE-TOKEN: \$(cat "\$STATE/secret-gitlab-pat-token" 2>/dev/null)"#grep -q "PRIVATE-TOKEN: glpat-SENTINEL"#' "$BIN/kubectl"
  : > "$ARGS_LOG"
  run deployGitlab
  [ "$status" -eq 0 ]
  grep -q "gitlab-rails runner" "$ARGS_LOG"
  [ "$(cat "$STATE/secret-gitlab-pat-token")" = "$PAT_SENTINEL" ]
}

@test "gitlabCredentials: tells how to read the password, never the password" {
  source_functions
  run deployGitlab
  run gitlabCredentials
  [[ "$output" == *"User: root"* ]]
  [[ "$output" == *"kubectl -n gitlab get secret gitlab-root"* ]]
  no_secret_anywhere "$output"
}

@test "deployApp: gitlab is in the menu (9 / i / gitlab, -d uninstalls)" {
  source_functions
  deployGitlab() { echo "DEPLOY_CALLED"; }
  uninstallGitlab() { echo "UNINSTALL_CALLED"; }
  run deployApp gitlab
  [[ "$output" == *"DEPLOY_CALLED"* ]]
  run deployApp 9 -d
  [[ "$output" == *"UNINSTALL_CALLED"* ]]
  run showDeployAppUsage
  [[ "$output" == *"gitlab"* ]]
}

@test "uninstallGitlab: removes runner, registry row and namespace" {
  source_functions
  run deployGitlab
  run uninstallGitlab
  [ "$status" -eq 0 ]
  grep -q "helm uninstall gitlab-runner -n gitlab" "$ARGS_LOG"
  grep -q "kubectl delete namespace gitlab" "$ARGS_LOG"
  ! grep -q '^gitlab|' "$APP_REGISTRY"
}

# ============================================================
# gitlabSeedRepo
# ============================================================

@test "gitlabSeedRepo: argument handling" {
  source_functions
  run gitlabSeedRepo
  [ "$status" -eq 1 ]; [[ "$output" == *"usage"* ]]
  run gitlabSeedRepo onlyname
  [ "$status" -eq 1 ]
  run gitlabSeedRepo "../evil" "$FAKE_REPO"
  [ "$status" -eq 1 ]; [[ "$output" == *"invalid project name"* ]]
  run gitlabSeedRepo "a b" "$FAKE_REPO"
  [ "$status" -eq 1 ]
  run gitlabSeedRepo good "$TEST_DIR/nope"
  [ "$status" -eq 1 ]; [[ "$output" == *"not a directory"* ]]
}

@test "gitlabSeedRepo: creates the project once, needs gitlab in the registry" {
  source_functions
  run deployGitlab
  : > "$APP_REGISTRY"   # deployed, but not registered
  run gitlabSeedRepo demo "$FAKE_REPO"
  [ "$status" -eq 1 ]
  [[ "$output" == *"not in the app registry"* ]]
  [ -f "$STATE/project" ]          # the project was created first
  : > "$ARGS_LOG"
  run gitlabSeedRepo demo "$FAKE_REPO"
  ! grep -q -- "-X POST.*/api/v4/projects " "$ARGS_LOG"
  ! grep -q "name=demo" "$ARGS_LOG"
  no_secret_anywhere "$output"
}

# ============================================================
# code-server
# ============================================================

@test "installCodeServer: a version with no reviewed sha256 is refused" {
  source_functions
  run installCodeServer 9.9.9
  [ "$status" -eq 1 ]
  [[ "$output" == *"no reviewed sha256"* ]]
  ! grep -q "github.com/coder" "$ARGS_LOG"
}

@test "installCodeServer: the pinned version refuses bytes that are not the reviewed release" {
  source_functions
  make_codeserver_fixture "$CODE_SERVER_PIN_VERSION" >/dev/null
  run installCodeServer
  [ "$status" -eq 1 ]
  [[ "$output" == *"MISMATCH"* ]]
  [ ! -e "$HOME/.local/bin/code-server" ]
}

@test "installCodeServer: verified install, settings written, re-run downloads nothing" {
  source_functions
  export CODE_SERVER_SHA256
  CODE_SERVER_SHA256=$(make_codeserver_fixture 9.9.9)
  run installCodeServer 9.9.9
  [ "$status" -eq 0 ]
  [ -x "$HOME/.local/bin/code-server" ]
  local s="$XDG_DATA_HOME/code-server/User/settings.json"
  grep -q '"security.workspace.trust.enabled": false' "$s"
  grep -q '"terminal.integrated.defaultProfile.linux": "zsh"' "$s"
  grep -q '"chat.disableAIFeatures": true' "$s"
  local n
  n=$(grep -c "github.com/coder" "$ARGS_LOG")
  run installCodeServer 9.9.9
  [ "$status" -eq 0 ]
  [[ "$output" == *"already installed"* ]]
  [ "$(grep -c "github.com/coder" "$ARGS_LOG")" -eq "$n" ]
}

@test "installCodeServer: a learner's own settings are kept" {
  source_functions
  export CODE_SERVER_SHA256
  CODE_SERVER_SHA256=$(make_codeserver_fixture 9.9.9)
  mkdir -p "$XDG_DATA_HOME/code-server/User"
  echo '{"editor.fontSize": 20}' > "$XDG_DATA_HOME/code-server/User/settings.json"
  run installCodeServer 9.9.9
  [ "$(cat "$XDG_DATA_HOME/code-server/User/settings.json")" = '{"editor.fontSize": 20}' ]
}

@test "startCodeServer: argument handling" {
  source_functions
  run startCodeServer "$TEST_DIR/nope"
  [ "$status" -eq 1 ]; [[ "$output" == *"not a directory"* ]]
  CODE_SERVER_PORT=abc run startCodeServer "$FAKE_REPO"
  [ "$status" -eq 1 ]; [[ "$output" == *"invalid CODE_SERVER_PORT"* ]]
}

@test "startCodeServer: under Orbital, no PASSWORD means it refuses to start" {
  export ORBITAL_ENVIRONMENT=true ORBITAL_JOB_ID=job1
  source_functions
  export CODE_SERVER_SHA256 CODE_SERVER_VERSION=9.9.9
  CODE_SERVER_SHA256=$(make_codeserver_fixture 9.9.9)
  run startCodeServer "$FAKE_REPO"
  [ "$status" -eq 1 ]
  [[ "$output" == *"PASSWORD is not set"* ]]
  ! grep -q "^code-server --bind" "$ARGS_LOG"
}

@test "startCodeServer: local default is auth none on 127.0.0.1, and it is not registered" {
  source_functions
  export CODE_SERVER_SHA256 CODE_SERVER_VERSION=9.9.9
  CODE_SERVER_SHA256=$(make_codeserver_fixture 9.9.9)
  run startCodeServer "$FAKE_REPO"
  [ "$status" -eq 0 ]
  grep -q "^code-server --bind-addr 127.0.0.1:18443 --auth none " "$ARGS_LOG"
  [ ! -s "$APP_REGISTRY" ]
  run stopCodeServer
  [[ "$output" == *"stopped"* ]]
}

@test "startCodeServer: Orbital + PASSWORD -> password auth on 0.0.0.0, secret never in argv, registered as ide" {
  export ORBITAL_ENVIRONMENT=true ORBITAL_JOB_ID=job1
  source_functions
  export CODE_SERVER_SHA256 CODE_SERVER_VERSION=9.9.9
  CODE_SERVER_SHA256=$(make_codeserver_fixture 9.9.9)
  export PASSWORD="cs-SENTINEL-pass-42"
  docker() { echo "172.18.0.1"; }   # k3d network gateway
  run startCodeServer "$FAKE_REPO"
  [ "$status" -eq 0 ]
  grep -q "^code-server --bind-addr 0.0.0.0:18443 --auth password .*| password-env=yes" "$ARGS_LOG"
  [ "$(awk -F'|' '$1=="ide"{print $2"|"$3"|"$4"|"$7}' "$APP_REGISTRY")" = "ide|ide|18443|ide--job1" ]
  grep -q 'addresses: \["172.18.0.1"\]' "$STATE/applied.yaml"
  no_secret_anywhere "$output"
  # already healthy -> no second server, still one registry row
  run startCodeServer "$FAKE_REPO"
  [ "$status" -eq 0 ]
  [[ "$output" == *"already running"* ]]
  [ "$(grep -c '^code-server --bind' "$ARGS_LOG")" -eq 1 ]
  [ "$(grep -c '^ide|' "$APP_REGISTRY")" -eq 1 ]
  # stop ends the process
  local pid
  pid=$(cat "$XDG_CACHE_HOME/code-server/pid")
  run stopCodeServer
  sleep 1
  ! kill -0 "$pid" 2>/dev/null
}

@test "registerCodeServer: never registers an IDE without a password" {
  export ORBITAL_ENVIRONMENT=true ORBITAL_JOB_ID=job1
  source_functions
  docker() { echo "172.18.0.1"; }
  run registerCodeServer
  [ "$status" -eq 0 ]
  [[ "$output" == *"no password"* ]]
  [ ! -s "$APP_REGISTRY" ]
}

@test "registerCodeServer: refuses a gateway that is not an IPv4 address" {
  export ORBITAL_ENVIRONMENT=true ORBITAL_JOB_ID=job1 PASSWORD=x
  source_functions
  docker() { echo ""; }
  run registerCodeServer
  [ "$status" -eq 1 ]
  [ ! -s "$APP_REGISTRY" ]
}

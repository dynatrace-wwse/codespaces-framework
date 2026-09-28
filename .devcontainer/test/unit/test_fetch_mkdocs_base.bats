#!/usr/bin/env bats
# Tests for fetchMkdocsBase (called by installMkdocs)
# A repo whose mkdocs.yaml INHERITs mkdocs-base.yaml must get, for a local
# `mkdocs serve`, the same framework files the docs CI workflow fetches:
# mkdocs-base.yaml AND docs/stylesheets/extra.css, at FRAMEWORK_VERSION.
# curl is stubbed on PATH; no test touches the network.

BASE_URL="https://raw.githubusercontent.com/dynatrace-wwse/codespaces-framework"

setup() {
  export TEST_DIR="$(mktemp -d)"
  export HOME="$TEST_DIR/home"
  mkdir -p "$HOME"
  export REPO_PATH="$TEST_DIR/workspaces/test-repo"
  mkdir -p "$REPO_PATH/.devcontainer/util" "$REPO_PATH/docs"
  export FRAMEWORK_VERSION="9.9.9"
  unset FRAMEWORK_CACHE

  # Minimal stubs so functions.sh can be sourced
  echo '# variables stub' > "$REPO_PATH/.devcontainer/util/variables.sh"
  mkdir -p "$REPO_PATH/.devcontainer/test"
  echo '# test stub' > "$REPO_PATH/.devcontainer/test/test_functions.sh"

  # curl stub: logs every URL to $CURL_LOG and writes "<body of URL>" to -o.
  # CURL_FAIL_MATCH=<substring> makes calls whose URL contains it fail (exit 22)
  # after writing a partial body, like an interrupted transfer would.
  export CURL_LOG="$TEST_DIR/curl.log"
  : > "$CURL_LOG"
  mkdir -p "$TEST_DIR/bin"
  cat > "$TEST_DIR/bin/curl" <<'STUB'
#!/bin/bash
out="" url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *)  url="$1"; shift ;;
  esac
done
echo "$url" >> "$CURL_LOG"
if [ -n "$CURL_FAIL_MATCH" ] && [[ "$url" == *"$CURL_FAIL_MATCH"* ]]; then
  [ -n "$out" ] && printf 'partial' > "$out"
  echo "curl: (22) The requested URL returned error: 404" >&2
  exit 22
fi
[ -n "$out" ] && printf 'body of %s\n' "$url" > "$out"
exit 0
STUB
  chmod +x "$TEST_DIR/bin/curl"
  export PATH="$TEST_DIR/bin:$PATH"
  unset CURL_FAIL_MATCH
}

teardown() {
  rm -rf "$TEST_DIR"
}

source_functions_only() {
  cd "$REPO_PATH"
  source "$BATS_TEST_DIRNAME/../../util/functions.sh" 2>/dev/null || true
}

inherit_repo() {
  printf 'INHERIT: mkdocs-base.yaml\n\nsite_name: Test\n' > "$REPO_PATH/mkdocs.yaml"
}

@test "fetchMkdocsBase: INHERIT repo gets mkdocs-base.yaml and extra.css at FRAMEWORK_VERSION" {
  inherit_repo
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  grep -qx "${BASE_URL}/9.9.9/mkdocs-base.yaml" "$CURL_LOG"
  grep -qx "${BASE_URL}/9.9.9/docs/stylesheets/extra.css" "$CURL_LOG"
  [ "$(cat "$REPO_PATH/docs/stylesheets/extra.css")" = "body of ${BASE_URL}/9.9.9/docs/stylesheets/extra.css" ]
  [ -f "$REPO_PATH/mkdocs-base.yaml" ]
}

@test "fetchMkdocsBase: base already present, extra.css missing -> fetches extra.css only" {
  inherit_repo
  echo "cached base" > "$REPO_PATH/mkdocs-base.yaml"
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  [ -f "$REPO_PATH/docs/stylesheets/extra.css" ]
  ! grep -q "mkdocs-base.yaml" "$CURL_LOG"
  [ "$(cat "$REPO_PATH/mkdocs-base.yaml")" = "cached base" ]
}

@test "fetchMkdocsBase: repo's own extra.css is left untouched" {
  inherit_repo
  mkdir -p "$REPO_PATH/docs/stylesheets"
  echo "/* repo's own */" > "$REPO_PATH/docs/stylesheets/extra.css"
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  [ "$(cat "$REPO_PATH/docs/stylesheets/extra.css")" = "/* repo's own */" ]
  ! grep -q "extra.css" "$CURL_LOG"
}

@test "fetchMkdocsBase: extra.css committed in git but deleted locally is not fetched over" {
  inherit_repo
  mkdir -p "$REPO_PATH/docs/stylesheets"
  echo "/* committed */" > "$REPO_PATH/docs/stylesheets/extra.css"
  git -C "$REPO_PATH" init -q
  git -C "$REPO_PATH" add docs/stylesheets/extra.css
  git -C "$REPO_PATH" -c user.email=t@t -c user.name=t commit -qm init
  rm "$REPO_PATH/docs/stylesheets/extra.css"
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  ! grep -q "extra.css" "$CURL_LOG"
  [ ! -e "$REPO_PATH/docs/stylesheets/extra.css" ]
  [[ "$output" == *"committed"* ]]
}

@test "fetchMkdocsBase: non-INHERIT repo is untouched" {
  printf 'site_name: Own config\ntheme:\n  name: material\n' > "$REPO_PATH/mkdocs.yaml"
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  [ ! -s "$CURL_LOG" ]
  [ ! -e "$REPO_PATH/mkdocs-base.yaml" ]
  [ ! -e "$REPO_PATH/docs/stylesheets" ]
}

@test "fetchMkdocsBase: repo without mkdocs.yaml is untouched" {
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  [ ! -s "$CURL_LOG" ]
}

@test "fetchMkdocsBase: extra.css curl failure warns, returns 0, leaves no partial file" {
  inherit_repo
  export CURL_FAIL_MATCH="extra.css"
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARN"* ]]
  [[ "$output" == *"extra.css"* ]]
  [ ! -e "$REPO_PATH/docs/stylesheets/extra.css" ]
  # the base still arrived
  [ -f "$REPO_PATH/mkdocs-base.yaml" ]
  # nothing left behind that a later run would mistake for the real file
  [ -z "$(ls -A "$REPO_PATH/docs/stylesheets")" ]
}

@test "fetchMkdocsBase: mkdocs-base.yaml curl failure warns, returns 0, leaves no partial file" {
  inherit_repo
  export CURL_FAIL_MATCH="mkdocs-base.yaml"
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARN"* ]]
  [[ "$output" == *"mkdocs-base.yaml"* ]]
  [ ! -e "$REPO_PATH/mkdocs-base.yaml" ]
}

@test "fetchMkdocsBase: curl failure does not kill the sourced shell" {
  inherit_repo
  export CURL_FAIL_MATCH="raw.githubusercontent.com"
  source_functions_only
  # Called directly (not via `run`), in this shell: an `exit` would end the test here.
  fetchMkdocsBase > /dev/null
  echo "still alive"
}

@test "fetchMkdocsBase: empty FRAMEWORK_VERSION warns and fetches nothing" {
  inherit_repo
  export FRAMEWORK_VERSION=""
  source_functions_only
  run fetchMkdocsBase
  [ "$status" -eq 0 ]
  [[ "$output" == *"WARN"* ]]
  [ ! -s "$CURL_LOG" ]
}

@test "installMkdocs: still starts mkdocs when the fetch fails" {
  inherit_repo
  export CURL_FAIL_MATCH="raw.githubusercontent.com"
  source_functions_only
  installRunme() { :; }
  pip() { :; }
  exposeMkdocs() { echo "EXPOSED"; }
  run installMkdocs
  [ "$status" -eq 0 ]
  [[ "$output" == *"EXPOSED"* ]]
}

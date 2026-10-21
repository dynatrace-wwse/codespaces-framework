#!/usr/bin/env bats
# /entrypoint.sh must hand over to its command on BOTH docker-group branches.
#
# On a container's first start the socket GID differs from the image's docker GID, so the
# entrypoint groupmods and `exec sg docker`s the command. The groupmod persists in the
# container layer, so on `docker restart` the GIDs match and the entrypoint used to print
# "No changes needed." and return: the command never ran and the container exited 0.
# stat/getent/sudo/hostname are shimmed on PATH; nothing touches the host.

setup() {
  export TEST_DIR="$(mktemp -d)"
  mkdir -p "$TEST_DIR/bin"
  ENTRYPOINT="$BATS_TEST_DIRNAME/../../entrypoint.sh"
  cat > "$TEST_DIR/bin/stat" <<SH
#!/bin/sh
echo "\${SOCK_GID}"
SH
  cat > "$TEST_DIR/bin/getent" <<SH
#!/bin/sh
echo "docker:x:\${GROUP_GID}:"
SH
  printf '#!/bin/sh\nexit 0\n' > "$TEST_DIR/bin/sudo"
  printf '#!/bin/sh\necho testhost\n' > "$TEST_DIR/bin/hostname"
  chmod +x "$TEST_DIR/bin/"*
  export PATH="$TEST_DIR/bin:$PATH"
}

teardown() {
  rm -rf "$TEST_DIR"
}

@test "GID match (a restarted container) runs the command" {
  SOCK_GID=2375 GROUP_GID=2375 run bash "$ENTRYPOINT" echo RAN-THE-COMMAND
  [ "$status" -eq 0 ]
  [[ "$output" == *"No changes needed."* ]]
  [[ "$output" == *"RAN-THE-COMMAND"* ]]
}

@test "GID match propagates the command's exit code" {
  SOCK_GID=2375 GROUP_GID=2375 run bash "$ENTRYPOINT" sh -c 'exit 7'
  [ "$status" -eq 7 ]
}

@test "GID match keeps an argument with spaces as one argument" {
  SOCK_GID=2375 GROUP_GID=2375 run bash "$ENTRYPOINT" printf '[%s]' "a b"
  [ "$status" -eq 0 ]
  [[ "$output" == *"[a b]"* ]]
}

@test "GID match with no command still returns cleanly" {
  SOCK_GID=2375 GROUP_GID=2375 run bash "$ENTRYPOINT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"No changes needed."* ]]
}

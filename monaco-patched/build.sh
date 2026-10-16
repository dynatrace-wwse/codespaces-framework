#!/usr/bin/env bash
# Reproducible build of the framework's patched Monaco (ENH-114).
#
# Upstream Monaco v2.30.1 + enh114-skip-discovery.patch: discover the classic
# environment URL only when the manifest also has a classic access token — the
# only time that URL is used. Stock Monaco calls
# /platform/metadata/v1/classic-environment-domain on every platform-token run,
# which needs 'app-engine:apps:run', a scope a learner's token must not carry.
#
# Upstream proposal: https://github.com/Dynatrace/dynatrace-configuration-as-code/discussions/2208
# Drop this build (and MONACO_PIN_URL in variables.sh) when upstream ships it.
#
# Same flags as upstream's release (Makefile $(RELEASES) target: CGO_ENABLED=0,
# -a, -tags netgo, -ldflags '-X …MonitoringAsCode=<v> -w -extldflags "-static"')
# and the same Go as their v2.30.1 binaries (go version -m: go1.26.8), plus
# -trimpath so the output does not depend on the build directory. Two runs of
# this script produce byte-identical binaries.
#
# Usage: build.sh <out-dir>     needs git and any go >= 1.21 (GOTOOLCHAIN fetches
#                               go$GO_VERSION from proxy.golang.org, sumdb-verified)
set -euo pipefail

UPSTREAM_REPO=https://github.com/Dynatrace/dynatrace-configuration-as-code.git
UPSTREAM_TAG=v2.30.1
UPSTREAM_COMMIT=8eb538d04978b457395781c31e1178044252fa61
GO_VERSION=1.26.8
VERSION=2.30.1+wwse.1
ARCHES="amd64 arm64"

here=$(cd "$(dirname "$0")" && pwd)
out=$(mkdir -p "${1:?usage: build.sh <out-dir>}" && cd "$1" && pwd)
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT

git -c advice.detachedHead=false clone -q --depth 1 --branch "$UPSTREAM_TAG" "$UPSTREAM_REPO" "$src"
got=$(git -C "$src" rev-parse HEAD)
if [ "$got" != "$UPSTREAM_COMMIT" ]; then
  echo "tag $UPSTREAM_TAG is $got, expected $UPSTREAM_COMMIT — refusing" >&2
  exit 1
fi
git -C "$src" apply --verbose "$here/enh114-skip-discovery.patch"

cd "$src"
for arch in $ARCHES; do
  GOTOOLCHAIN="go$GO_VERSION" GOFLAGS=-mod=readonly CGO_ENABLED=0 GOOS=linux GOARCH="$arch" \
    go build -a -trimpath -tags netgo \
      -ldflags "-X github.com/dynatrace/dynatrace-configuration-as-code/v2/pkg/version.MonitoringAsCode=$VERSION -w -extldflags \"-static\"" \
      -o "$out/monaco-linux-$arch" ./cmd/monaco
  (cd "$out" && sha256sum "monaco-linux-$arch" > "monaco-linux-$arch.sha256")
done
cp "$here/enh114-skip-discovery.patch" "$here/build.sh" "$out/"
(cd "$out" && cat monaco-linux-*.sha256)

#!/usr/bin/env bash
#
# Orbital restore drill.
#
# Restores a DR snapshot into a THROWAWAY Redis on a scratch port and asserts
# the data actually came back. A backup that has never been restored is not a
# backup — this is the thing that turns the snapshot from a hope into a fact.
#
# Safety: this script must never be able to damage production. It refuses to
# run against port 6379 or against Redis's real data directory, and the
# throwaway instance binds 127.0.0.1 only with no persistence of its own.
#
# Usage:
#   orbital-restore-drill.sh                    # drill the LATEST snapshot
#   orbital-restore-drill.sh 20260826T184932Z   # drill a specific one
#
set -euo pipefail

BACKUP_ROOT="${ORBITAL_BACKUP_ROOT:-/var/backups/orbital}"
SCRATCH_PORT="${ORBITAL_DRILL_PORT:-63790}"
PROD_REDIS_DIR="/var/lib/redis"

log()  { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { printf '  ✗ %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
pass() { printf '  ✓ %s\n' "$*"; }
die()  { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (snapshots are 700 root)"

# ── Guardrails ───────────────────────────────────────────────────────────────
[ "$SCRATCH_PORT" != "6379" ] || die "refusing to drill on the production Redis port"
command -v redis-server >/dev/null || die "redis-server not installed"

SNAP="${1:-$(cat "${BACKUP_ROOT}/LATEST" 2>/dev/null || true)}"
[ -n "$SNAP" ] || die "no snapshot given and ${BACKUP_ROOT}/LATEST is missing"
SRC="${BACKUP_ROOT}/${SNAP}"
[ -d "$SRC" ] || die "snapshot not found: ${SRC}"
[ -f "${SRC}/dump.rdb" ] || die "snapshot has no dump.rdb: ${SRC}"

SCRATCH="$(mktemp -d /tmp/orbital-drill.XXXXXX)"
[ "$SCRATCH" != "$PROD_REDIS_DIR" ] || die "scratch dir resolved to the production data dir"
chmod 700 "$SCRATCH"

DRILL_PID=""
cleanup() {
    [ -n "$DRILL_PID" ] && kill "$DRILL_PID" 2>/dev/null || true
    [ -n "$DRILL_PID" ] && wait "$DRILL_PID" 2>/dev/null || true
    rm -rf "$SCRATCH"
}
trap cleanup EXIT

FAILURES=0
log "drilling snapshot ${SNAP}"

# ── 1. Manifest integrity ────────────────────────────────────────────────────
# Verify before restoring: a corrupt dump.rdb that loads to an empty database
# would otherwise look like a successful restore of an empty system.
if [ -f "${SRC}/MANIFEST.txt" ]; then
    if ( cd "$SRC" && sha256sum --quiet -c <(grep -E '^[0-9a-f]{64}  ' MANIFEST.txt) ) 2>/dev/null; then
        pass "manifest checksums match"
    else
        fail "manifest checksum mismatch — snapshot is corrupt"
    fi
else
    fail "no MANIFEST.txt in snapshot (written by orbital-backup.sh)"
fi

# ── 2. Boot a throwaway Redis on the snapshot ────────────────────────────────
cp "${SRC}/dump.rdb" "${SCRATCH}/dump.rdb"
redis-server \
    --port "$SCRATCH_PORT" \
    --bind 127.0.0.1 \
    --dir "$SCRATCH" \
    --dbfilename dump.rdb \
    --appendonly no \
    --save '' \
    --daemonize no \
    --logfile "${SCRATCH}/redis.log" &
DRILL_PID=$!

for _ in $(seq 1 30); do
    redis-cli -h 127.0.0.1 -p "$SCRATCH_PORT" ping >/dev/null 2>&1 && break
    sleep 1
done

R=(redis-cli -h 127.0.0.1 -p "$SCRATCH_PORT")
if "${R[@]}" ping >/dev/null 2>&1; then
    pass "throwaway Redis up on :${SCRATCH_PORT}"
else
    fail "throwaway Redis never became ready — see ${SCRATCH}/redis.log"
    echo; log "RESULT: FAILED (${FAILURES} check(s))"; exit 1
fi

# ── 3. Key count matches what the snapshot recorded ──────────────────────────
RESTORED="$("${R[@]}" dbsize)"
if [ -f "${SRC}/redis-dbsize.txt" ]; then
    EXPECTED="$(tr -dc '0-9' < "${SRC}/redis-dbsize.txt")"
    if [ "$RESTORED" = "$EXPECTED" ]; then
        pass "key count ${RESTORED} matches snapshot record"
    else
        fail "key count ${RESTORED} != recorded ${EXPECTED}"
    fi
else
    fail "snapshot has no redis-dbsize.txt to compare against"
fi
[ "$RESTORED" -gt 0 ] && pass "database is non-empty" || fail "database restored EMPTY"

# ── 4. Structural assertions ─────────────────────────────────────────────────
# Key count alone can be satisfied by garbage. Check that the structures the
# control plane actually depends on came back with the right types.
check_type() {
    local key="$1" want="$2" got
    got="$("${R[@]}" type "$key" 2>/dev/null || echo none)"
    if [ "$got" = "$want" ]; then
        pass "${key} is ${want}"
    elif [ "$got" = "none" ]; then
        fail "${key} MISSING from the restored database"
    else
        fail "${key} is ${got}, expected ${want}"
    fi
}
check_type workshop:fleet hash

FLEET_N="$("${R[@]}" hlen workshop:fleet 2>/dev/null || echo 0)"
[ "$FLEET_N" -gt 0 ] \
    && pass "workshop:fleet holds ${FLEET_N} entries" \
    || fail "workshop:fleet is empty — fleet state did not survive"

# No --count: redis-cli 7.0.15 rejects it ("Unrecognized option"). `|| true`
# because pipefail would otherwise turn an empty scan into a script abort
# rather than the assertion failure it should be.
TENANTS="$({ "${R[@]}" --scan --pattern 'tenant:registry:*' 2>/dev/null || true; } | wc -l)"
[ "$TENANTS" -gt 0 ] \
    && pass "${TENANTS} tenant:registry:* keys restored" \
    || fail "no tenant:registry:* keys — the tenant registry did not survive"

JOBS="$({ "${R[@]}" --scan --pattern 'job:*' 2>/dev/null || true; } | wc -l)"
[ "$JOBS" -gt 0 ] \
    && pass "${JOBS} job:* keys restored" \
    || fail "no job:* keys — build history did not survive"

# ── 5. Credential material is present in the snapshot ────────────────────────
# Not printed, only counted: a snapshot that restores Redis but lost the env
# files cannot actually rebuild the platform.
for f in master.env; do
    if [ -s "${SRC}/${f}" ]; then
        pass "${f} present ($(grep -cE '^[A-Z_0-9]+=' "${SRC}/${f}") keys)"
    else
        fail "${f} missing from snapshot"
    fi
done
[ -f "${SRC}/letsencrypt.tar.gz" ] && pass "TLS material present" || fail "letsencrypt.tar.gz missing"

# ── 6. Prove we never touched production ─────────────────────────────────────
if [ "$("${R[@]}" config get port | tail -1)" = "$SCRATCH_PORT" ]; then
    pass "drill ran on :${SCRATCH_PORT}, production :6379 untouched"
else
    fail "drill instance is not on the expected port"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    log "RESULT: PASS — snapshot ${SNAP} is restorable"
    exit 0
fi
log "RESULT: FAILED (${FAILURES} check(s)) — snapshot ${SNAP} is NOT trustworthy"
exit 1

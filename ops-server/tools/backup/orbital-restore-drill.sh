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
#   orbital-restore-drill.sh                    # drill the LATEST local snapshot
#   orbital-restore-drill.sh 20260826T184932Z   # drill a specific local one
#   orbital-restore-drill.sh --s3               # drill the NEWEST OFFSITE copy
#
# --s3 is the one that actually proves disaster recovery. A local drill only
# shows the snapshot on the master is readable — but the master is the host
# whose loss the backups exist for. Only the offsite path is evidence.
#
set -euo pipefail

BACKUP_ROOT="${ORBITAL_BACKUP_ROOT:-/var/backups/orbital}"
SCRATCH_PORT="${ORBITAL_DRILL_PORT:-63790}"
PROD_REDIS_DIR="/var/lib/redis"
# shellcheck disable=SC1091
[ -r /etc/default/orbital-backup ] && . /etc/default/orbital-backup
S3_URI="${ORBITAL_BACKUP_S3_URI:-}"

log()  { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { printf '  ✗ %s\n' "$*"; FAILURES=$((FAILURES + 1)); }
pass() { printf '  ✓ %s\n' "$*"; }
die()  { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (snapshots are 700 root)"

# ── Guardrails ───────────────────────────────────────────────────────────────
[ "$SCRATCH_PORT" != "6379" ] || die "refusing to drill on the production Redis port"
command -v redis-server >/dev/null || die "redis-server not installed"

SCRATCH="$(mktemp -d /tmp/orbital-drill.XXXXXX)"
[ "$SCRATCH" != "$PROD_REDIS_DIR" ] || die "scratch dir resolved to the production data dir"
chmod 700 "$SCRATCH"

FROM_S3=0
if [ "${1:-}" = "--s3" ]; then
    FROM_S3=1
    [ -n "$S3_URI" ] || die "--s3 given but ORBITAL_BACKUP_S3_URI is not configured"
    command -v aws >/dev/null || die "aws CLI not found"

    # Newest object by key: the timestamp is lexicographically sortable by
    # construction (YYYYmmddTHHMMSSZ), so this does not depend on S3 ordering.
    OBJ="$(aws s3 ls "${S3_URI%/}/" 2>/dev/null | awk '{print $4}' | grep -E '^orbital-.*\.tar\.gz$' | sort | tail -1)"
    [ -n "$OBJ" ] || die "no snapshots found at ${S3_URI}"

    mkdir -p "${SCRATCH}/dl"
    aws s3 cp "${S3_URI%/}/${OBJ}" "${SCRATCH}/dl/${OBJ}" >/dev/null \
        || die "could not download ${OBJ} from S3"
    tar -C "${SCRATCH}/dl" -xzf "${SCRATCH}/dl/${OBJ}" \
        || die "could not extract ${OBJ}"

    SNAP="${OBJ#orbital-}"; SNAP="${SNAP%.tar.gz}"
    SRC="${SCRATCH}/dl/${SNAP}"
    [ -d "$SRC" ] || die "tarball did not contain the expected directory ${SNAP}"
else
    SNAP="${1:-$(cat "${BACKUP_ROOT}/LATEST" 2>/dev/null || true)}"
    [ -n "$SNAP" ] || die "no snapshot given and ${BACKUP_ROOT}/LATEST is missing"
    SRC="${BACKUP_ROOT}/${SNAP}"
fi

[ -d "$SRC" ] || die "snapshot not found: ${SRC}"
[ -f "${SRC}/dump.rdb" ] || die "snapshot has no dump.rdb: ${SRC}"


DRILL_PID=""
cleanup() {
    [ -n "$DRILL_PID" ] && kill "$DRILL_PID" 2>/dev/null || true
    [ -n "$DRILL_PID" ] && wait "$DRILL_PID" 2>/dev/null || true
    rm -rf "$SCRATCH"
}
trap cleanup EXIT

FAILURES=0
log "drilling snapshot ${SNAP} ($([ "$FROM_S3" = "1" ] && echo "OFFSITE from ${S3_URI}" || echo "local"))"

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

# ── 3. Key count ─────────────────────────────────────────────────────────────
# An EXACT match is the wrong assertion here and fails at random. 1368 of
# Orbital's ~1590 keys carry a TTL (job logs, shell tokens, auth-role cache),
# Redis drops already-expired keys when loading an RDB, and a drill runs
# minutes-to-a-month after the snapshot. The first offsite drill failed on
# "1591 != 1593" purely because two TTLs elapsed in the intervening 64 seconds.
#
# What must be true: every NON-VOLATILE key survives. Volatile ones are allowed
# to have expired, and the shortfall is reported rather than hidden.
#
# keys= and expires= are read from the single INFO KEYSPACE line so both are
# sampled atomically; dbsize is a separate round trip and can disagree with it.
RESTORED="$("${R[@]}" dbsize)"
[ "$RESTORED" -gt 0 ] && pass "database is non-empty (${RESTORED} keys)" || fail "database restored EMPTY"

if [ -f "${SRC}/redis-keyspace.txt" ] \
   && grep -qE '^db0:keys=[0-9]+,expires=[0-9]+' "${SRC}/redis-keyspace.txt"; then
    SNAP_KEYS="$(grep -oP '^db0:keys=\K[0-9]+'    "${SRC}/redis-keyspace.txt")"
    SNAP_VOL="$(grep -oP '^db0:.*expires=\K[0-9]+' "${SRC}/redis-keyspace.txt")"
    PERSISTENT=$((SNAP_KEYS - SNAP_VOL))

    if [ "$RESTORED" -ge "$SNAP_KEYS" ]; then
        pass "key count ${RESTORED} >= snapshot's ${SNAP_KEYS} (no expiry in between)"
    elif [ "$RESTORED" -ge "$PERSISTENT" ]; then
        pass "key count ${RESTORED} of ${SNAP_KEYS}; $((SNAP_KEYS - RESTORED)) volatile key(s) expired since the snapshot, all ${PERSISTENT} non-volatile survived"
    else
        fail "key count ${RESTORED} is below the ${PERSISTENT} non-volatile keys the snapshot held — real data loss, not TTL expiry"
    fi
else
    fail "snapshot has no parseable redis-keyspace.txt to compare against"
fi

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
    log "RESULT: PASS — snapshot ${SNAP} is restorable ($([ "$FROM_S3" = "1" ] && echo offsite || echo local))"
    exit 0
fi
log "RESULT: FAILED (${FAILURES} check(s)) — snapshot ${SNAP} is NOT trustworthy"
exit 1

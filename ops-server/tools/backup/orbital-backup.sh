#!/usr/bin/env bash
#
# Orbital disaster-recovery snapshot.
#
# Captures everything needed to rebuild the control plane on a fresh host:
# Redis state, the environment files from every host in the fleet, and the
# service/proxy/TLS configuration that makes the box answer on its domain.
#
# Runs as root (needs /home/ops/.env, /etc/letsencrypt, /var/lib/redis).
#
# Local snapshots always happen. The offsite copy to S3 is attempted only when
# ORBITAL_BACKUP_S3_URI is set AND the credentials actually work — a missing or
# expired credential downgrades the run to local-only with a warning rather
# than failing, because a local backup is strictly better than no backup. Set
# ORBITAL_BACKUP_S3_REQUIRED=1 to invert that and make offsite mandatory.
#
# Usage:
#   orbital-backup.sh                 # snapshot + prune + optional offsite
#   ORBITAL_BACKUP_KEEP_DAYS=14 ...   # override local retention
#
set -euo pipefail

BACKUP_ROOT="${ORBITAL_BACKUP_ROOT:-/var/backups/orbital}"
KEEP_DAYS="${ORBITAL_BACKUP_KEEP_DAYS:-7}"
S3_URI="${ORBITAL_BACKUP_S3_URI:-}"
S3_KMS_KEY="${ORBITAL_BACKUP_S3_KMS_KEY:-}"
S3_REQUIRED="${ORBITAL_BACKUP_S3_REQUIRED:-0}"
OPS_ENV="${ORBITAL_OPS_ENV_FILE:-/home/ops/.env}"
WORKERS="${ORBITAL_BACKUP_WORKERS:-autonomous-enablements-worker autonomous-enablements-worker-2}"
# The user whose ssh config and keys reach the workers. Root has neither.
SSH_AS="${ORBITAL_BACKUP_SSH_USER:-ubuntu}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_ROOT}/${TS}"

log()  { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '%s  WARN: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf '%s  FATAL: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

umask 077
mkdir -p "$DEST"
chmod 700 "$BACKUP_ROOT" "$DEST"

# ── Redis ────────────────────────────────────────────────────────────────────
# BGSAVE is asynchronous. Copying dump.rdb without waiting yields the PREVIOUS
# snapshot, silently — the file exists and looks fine, it is just stale. Poll
# rdb_bgsave_in_progress and compare rdb_last_save_time to prove a new one landed.
backup_redis() {
    local pw redis_dir before after waited
    pw="$(grep -oP '^REDIS_PASSWORD=\K.*' "$OPS_ENV")" \
        || die "no REDIS_PASSWORD in $OPS_ENV"
    local -a rcli=(redis-cli -a "$pw" --no-auth-warning)

    "${rcli[@]}" ping >/dev/null || die "redis unreachable"

    before="$("${rcli[@]}" info persistence | grep -oP 'rdb_last_save_time:\K[0-9]+')"
    "${rcli[@]}" bgsave >/dev/null

    waited=0
    while [ "$("${rcli[@]}" info persistence | grep -oP 'rdb_bgsave_in_progress:\K[0-9]+')" = "1" ]; do
        sleep 1; waited=$((waited + 1))
        [ "$waited" -lt 300 ] || die "BGSAVE still running after 300s"
    done

    after="$("${rcli[@]}" info persistence | grep -oP 'rdb_last_save_time:\K[0-9]+')"
    [ "$after" -gt "$before" ] || die "BGSAVE did not produce a new snapshot (${before} -> ${after})"
    [ "$("${rcli[@]}" info persistence | grep -oP 'rdb_last_bgsave_status:\K\w+')" = "ok" ] \
        || die "rdb_last_bgsave_status is not ok"

    redis_dir="$("${rcli[@]}" config get dir | tail -1)"
    cp -a "${redis_dir}/dump.rdb" "${DEST}/dump.rdb"
    # AOF is the finer-grained record; RDB alone loses up to a minute.
    [ -d "${redis_dir}/appendonlydir" ] && cp -a "${redis_dir}/appendonlydir" "${DEST}/appendonlydir"

    "${rcli[@]}" info keyspace  > "${DEST}/redis-keyspace.txt"
    "${rcli[@]}" dbsize | tr -d '\r' > "${DEST}/redis-dbsize.txt"
    log "redis: $(cat "${DEST}/redis-dbsize.txt") keys, $(du -h "${DEST}/dump.rdb" | cut -f1) rdb"
}

# ── Environment files (master + every worker) ────────────────────────────────
# These are the crown jewels: OAuth clients, platform tokens, GitHub tokens.
# They are also the single thing that cannot be reconstructed from git.
backup_envs() {
    cp -a "$OPS_ENV" "${DEST}/master.env"
    local w
    for w in $WORKERS; do
        if sudo -u "$SSH_AS" timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=10 "$w" \
               'sudo -n cat /home/ops/.env' > "${DEST}/${w}.env" 2>/dev/null \
           && [ -s "${DEST}/${w}.env" ]; then
            log "env: captured ${w}"
        else
            rm -f "${DEST}/${w}.env"
            warn "env: could NOT capture ${w} — snapshot is incomplete for that host"
        fi
    done
}

# ── Service, proxy and TLS configuration ─────────────────────────────────────
backup_config() {
    cp -a /etc/redis/redis.conf              "${DEST}/redis.conf"
    cp -a /etc/nginx/sites-enabled           "${DEST}/nginx-sites-enabled"
    [ -d /etc/letsencrypt ] && tar -C /etc -czf "${DEST}/letsencrypt.tar.gz" letsencrypt
    cp -a /etc/ssh/sshd_config               "${DEST}/sshd_config" 2>/dev/null || true
    [ -d /etc/ssh/sshd_config.d ] && cp -a /etc/ssh/sshd_config.d "${DEST}/sshd_config.d"

    mkdir -p "${DEST}/systemd"
    local u
    for u in ops-dashboard ops-webhook ops-worker ops-sync-daemon ops-nightly \
             ops-docker-cleanup ops-gen2scan oauth2-proxy orbital-backup; do
        systemctl cat "${u}.service" > "${DEST}/systemd/${u}.service" 2>/dev/null || true
        systemctl cat "${u}.timer"   > "${DEST}/systemd/${u}.timer"   2>/dev/null || true
    done
    systemctl list-unit-files --no-pager > "${DEST}/systemd/unit-files.txt" 2>/dev/null || true
}

# ── Manifest ─────────────────────────────────────────────────────────────────
# Checksums are what make a restore verifiable rather than hopeful.
write_manifest() {
    {
        echo "# Orbital DR snapshot"
        echo "timestamp_utc: ${TS}"
        echo "host:          $(hostname)"
        echo "redis_keys:    $(cat "${DEST}/redis-dbsize.txt" 2>/dev/null || echo unknown)"
        echo "workers:       ${WORKERS}"
        echo
        echo "## sha256"
    } > "${DEST}/MANIFEST.txt"
    ( cd "$DEST" && find . -type f ! -name MANIFEST.txt -print0 \
        | sort -z | xargs -0 sha256sum ) >> "${DEST}/MANIFEST.txt"
}

# ── Offsite ──────────────────────────────────────────────────────────────────
offsite() {
    [ -n "$S3_URI" ] || { log "offsite: ORBITAL_BACKUP_S3_URI unset — local-only"; return 0; }

    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        if [ "$S3_REQUIRED" = "1" ]; then
            die "offsite required but AWS credentials are not usable"
        fi
        warn "offsite: AWS credentials not usable — local-only this run"
        return 0
    fi

    local tarball="${BACKUP_ROOT}/orbital-${TS}.tar.gz"
    tar -C "$BACKUP_ROOT" -czf "$tarball" "$TS"
    chmod 600 "$tarball"

    local -a sse=(--sse aws:kms)
    [ -n "$S3_KMS_KEY" ] && sse+=(--sse-kms-key-id "$S3_KMS_KEY")

    if aws s3 cp "$tarball" "${S3_URI%/}/orbital-${TS}.tar.gz" "${sse[@]}" >/dev/null; then
        log "offsite: uploaded to ${S3_URI%/}/orbital-${TS}.tar.gz"
        rm -f "$tarball"
    else
        rm -f "$tarball"
        [ "$S3_REQUIRED" = "1" ] && die "offsite upload failed"
        warn "offsite: upload failed — local snapshot retained"
    fi
}

# ── Prune ────────────────────────────────────────────────────────────────────
# Only ever removes directories matching the timestamp shape this script writes,
# so a stray file under the backup root is never swept up.
prune() {
    local d n=0
    while IFS= read -r d; do
        rm -rf "$d"; n=$((n + 1))
    done < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
                  -regextype posix-extended -regex '.*/[0-9]{8}T[0-9]{6}Z' \
                  -mtime "+${KEEP_DAYS}")
    [ "$n" -gt 0 ] && log "prune: removed ${n} snapshot(s) older than ${KEEP_DAYS} days"
    return 0
}

log "snapshot -> ${DEST}"
backup_redis
backup_envs
backup_config
write_manifest
chmod -R go-rwx "$DEST"

# LATEST is published BEFORE the offsite attempt. The local snapshot is complete
# and verifiable at this point, and with ORBITAL_BACKUP_S3_REQUIRED=1 a failed
# upload aborts the run — publishing afterwards would leave LATEST pointing at
# yesterday, so the restore drill would silently keep validating a stale
# snapshot while today's good one sat there unreferenced.
echo "$TS" > "${BACKUP_ROOT}/LATEST"

offsite
prune
log "done: $(du -sh "$DEST" | cut -f1) in ${DEST}"

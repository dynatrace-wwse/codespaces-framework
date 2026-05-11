#!/usr/bin/env bash
# Tiered Docker host cleanup — runs via systemd ops-docker-cleanup.timer.
# Safe to run on both master and worker nodes.
#
# Tier 1 (always):  stopped containers, anonymous volumes, dangling images, builder cache
# Tier 2 (>threshold): all images unused for IMAGE_AGE_HOURS (keeps images used recently)
#
# Override thresholds:
#   DOCKER_DISK_THRESHOLD  (default: 80)  — % used before tier-2 kicks in
#   DOCKER_IMAGE_AGE_HOURS (default: 168) — hours before an image is considered stale

set -euo pipefail

THRESHOLD="${DOCKER_DISK_THRESHOLD:-80}"
AGE_HOURS="${DOCKER_IMAGE_AGE_HOURS:-168}"

log() { echo "[ops-docker-cleanup] $*"; }

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    log "Docker not available — skipping"
    exit 0
fi

used_pct=$(df / --output=pcent | tail -1 | tr -d '% ')
log "Disk before cleanup: ${used_pct}% used"

# Tier 1: always safe — only removes stopped/exited containers, anonymous
# (unnamed) volumes, and dangling (<none>) images.  Named images are untouched.
log "Tier 1: pruning stopped containers, anonymous volumes, dangling images, builder cache..."
docker container prune -f
docker volume prune -f
docker image prune -f
docker builder prune -f

used_after_t1=$(df / --output=pcent | tail -1 | tr -d '% ')
log "Disk after tier 1: ${used_after_t1}% used"

# Tier 2: prune ALL images not actively used in the last AGE_HOURS.
# Keeps images that were used by a container recently (including docker:25-dind
# as long as a Sysbox job ran within the window).
if (( used_after_t1 >= THRESHOLD )); then
    log "Disk at ${used_after_t1}% (>= ${THRESHOLD}%) — tier 2: pruning images unused for ${AGE_HOURS}h..."
    docker image prune -a --filter "until=${AGE_HOURS}h" -f
    used_after_t2=$(df / --output=pcent | tail -1 | tr -d '% ')
    log "Disk after tier 2: ${used_after_t2}% used"
    if (( used_after_t2 >= 95 )); then
        log "WARNING: disk still at ${used_after_t2}% — manual intervention may be needed (run freeUpSpace)"
    fi
else
    log "Disk below threshold (${THRESHOLD}%) — tier 2 skipped"
fi

log "Done."

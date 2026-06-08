#!/usr/bin/env bash
# Evening sequence: stress tests → agentic validation → nightly build → report
# Run with: nohup bash run_evening_sequence.sh > /tmp/evening-sequence.log 2>&1 &

set -uo pipefail  # no -e: validation exits 1 on failures, must not abort sequence

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OPS_VENV="/home/ops/ops-venv/bin/python"
PYTHON="python3"
DATE="$(date -u +%Y%m%d)"
LOG_DIR="/tmp/evening-$DATE"
REDIS_PWD="50258583a5c8d515dc8a553a26e1a17d"
ORBITAL_API="https://autonomous-enablements.whydevslovedynatrace.com"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date -u '+%H:%M:%S UTC')] $*" | tee -a "$LOG_DIR/sequence.log"
}

wait_workers_free() {
    local arch="${1:-amd64}"
    local timeout_min="${2:-60}"
    local waited=0
    log "Waiting for $arch workers to drain (max ${timeout_min}min)..."
    while true; do
        active=$(curl -s "$ORBITAL_API/api/workers" | python3 -c "
import sys, json
workers = json.load(sys.stdin).get('workers', [])
print(sum(int(w.get('active_jobs',0)) for w in workers if w.get('arch')=='$arch'))
" 2>/dev/null || echo 99)
        log "  $arch active jobs: $active"
        if [[ "$active" -eq 0 ]]; then
            log "  $arch workers free."
            return 0
        fi
        if [[ $waited -ge $((timeout_min * 60)) ]]; then
            log "  Drain timeout after ${timeout_min}min — continuing anyway"
            return 0
        fi
        sleep 60
        waited=$((waited + 60))
    done
}

# ─────────────────────────────────────────────
log "=========================================="
log "EVENING SEQUENCE STARTED"
log "Log dir: $LOG_DIR"
log "Framework: $FRAMEWORK_DIR"
log "=========================================="

# ─────────────────────────────────────────────
log ""
log "══ PHASE 1: STRESS TEST — AMD64 ══"
log "  Repo: enablement-kubernetes-101 (K3d workload, ~30-60 min per job)"
log "  Config: max=14 jobs, step=2, wave=5 min"
log ""

cd "$SCRIPT_DIR"
PYTHONUNBUFFERED=1 $PYTHON -u stress_test_direct.py \
    --repo "dynatrace-wwse/enablement-kubernetes-101" \
    --arch amd64 \
    --max-jobs 14 \
    --step 2 \
    --wave-minutes 5 \
    --output "$LOG_DIR/stress-amd64.json" \
    2>&1 | tee "$LOG_DIR/stress-amd64.log"

log "AMD64 stress test complete."

# ─────────────────────────────────────────────
log ""
log "══ PHASE 2: STRESS TEST — ARM64 (Master) ══"
log "  Repo: enablement-kubernetes-101 (same workload, master as target)"
log "  Config: max=5 jobs, step=1, wave=5 min, sat-threshold=70%"
log ""

PYTHONUNBUFFERED=1 $PYTHON -u stress_test_direct.py \
    --repo "dynatrace-wwse/enablement-kubernetes-101" \
    --arch arm64 \
    --max-jobs 5 \
    --step 1 \
    --wave-minutes 5 \
    --output "$LOG_DIR/stress-arm64.json" \
    2>&1 | tee "$LOG_DIR/stress-arm64.log"

log "ARM64 stress test complete."

# ─────────────────────────────────────────────
log ""
log "══ PHASE 3: DRAIN — wait for AMD workers to finish stress test jobs ══"
log ""

wait_workers_free "amd64" 90

# ─────────────────────────────────────────────
log ""
log "══ PHASE 4: AGENTIC VALIDATION (all repos, shell steps) ══"
log ""

cd "$SCRIPT_DIR"
PYTHONUNBUFFERED=1 $PYTHON -u agentic_validator.py \
    --no-ui \
    2>&1 | tee "$LOG_DIR/validation.log"

log "Agentic validation complete."

# ─────────────────────────────────────────────
log ""
log "══ PHASE 5: NIGHTLY BUILD (all repos + framework tests) ══"
log ""

sudo -u ops bash -c "
    cd /home/ops/enablement-framework/codespaces-framework/ops-server && \
    PYTHONPATH=/home/ops/enablement-framework/codespaces-framework/ops-server \
    /home/ops/ops-venv/bin/python -m nightly.scheduler nightly \
    --stagger 5 --parallel 6 --include-framework
" 2>&1 | tee "$LOG_DIR/nightly.log"

log "Nightly build queued."

# ─────────────────────────────────────────────
log ""
log "══ PHASE 6: GENERATING COMBINED REPORT ══"
log ""

cd "$SCRIPT_DIR"
$PYTHON generate_evening_report.py \
    --log-dir "$LOG_DIR" \
    --output "$LOG_DIR/EVENING_REPORT.md"

log ""
log "=========================================="
log "EVENING SEQUENCE COMPLETE"
log "Report: $LOG_DIR/EVENING_REPORT.md"
log "=========================================="

# Print report to stdout/log
echo ""
cat "$LOG_DIR/EVENING_REPORT.md"

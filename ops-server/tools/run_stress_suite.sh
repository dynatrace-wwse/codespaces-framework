#!/usr/bin/env bash
# Full stress test suite: astroshop → cleanup → todoapp → cleanup → report
# Run with: nohup bash run_stress_suite.sh > /tmp/stress-suite.log 2>&1 &

set -uo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATE="$(date -u +%Y%m%d-%H%M)"
LOG_DIR="/tmp/stress-suite-$DATE"
WORKER_ID="worker-x86_64-amd001"
REDIS_AUTH="50258583a5c8d515dc8a553a26e1a17d"
PYTHON="python3"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date -u '+%H:%M:%S UTC')] $*" | tee -a "$LOG_DIR/suite.log"
}

worker_status() {
    redis-cli -a "$REDIS_AUTH" hgetall "worker:$WORKER_ID" 2>/dev/null \
        | grep -v Warning \
        | paste - - \
        | awk '{printf "    %-20s %s\n", $1, $2}'
}

verify_worker_healthy() {
    log "Verifying worker health..."
    local attempts=0
    while [[ $attempts -lt 30 ]]; do
        local active
        active=$(redis-cli -a "$REDIS_AUTH" hget "worker:$WORKER_ID" active_jobs 2>/dev/null | grep -v Warning || echo 99)
        local slots
        slots=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 autonomous-enablements-worker \
            "docker ps --filter 'name=sb-slot' --format '{{.Names}}'" 2>/dev/null | wc -l || echo 0)
        log "  active_jobs=$active  pre-warm slots=$slots/6"
        if [[ "$active" == "0" && "$slots" -ge 6 ]]; then
            log "  Worker healthy ✓"
            worker_status
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 15
    done
    log "  WARNING: worker not fully clean after timeout — continuing"
    worker_status
}

# ──────────────────────────────────────────────────────────────────────────────
log "=========================================="
log "STRESS TEST SUITE STARTED"
log "Log dir: $LOG_DIR"
log "Worker:  $WORKER_ID"
log "=========================================="

# ── TEST 1: ASTROSHOP ─────────────────────────────────────────────────────────
log ""
log "══ TEST 1: ASTROSHOP ══"
log "  Branch: stress-test/astroshop | Max containers: 8"
log ""

PYTHONUNBUFFERED=1 $PYTHON -u "$TOOLS/capacity_stress_test.py" \
    --variant astroshop \
    --max-containers 8 \
    --output "$LOG_DIR/report-astroshop.md" \
    2>&1 | tee "$LOG_DIR/astroshop.log"

log ""
log "Astroshop test complete."

# ── CLEANUP 1 ─────────────────────────────────────────────────────────────────
log ""
log "══ CLEANUP after astroshop ══"

$PYTHON "$TOOLS/cleanup_worker.py" "$WORKER_ID" 2>&1 | tee -a "$LOG_DIR/suite.log"

verify_worker_healthy

# ── TEST 2: TODOAPP ───────────────────────────────────────────────────────────
log ""
log "══ TEST 2: TODOAPP ══"
log "  Branch: main | Max containers: 8"
log ""

PYTHONUNBUFFERED=1 $PYTHON -u "$TOOLS/capacity_stress_test.py" \
    --variant todoapp \
    --max-containers 8 \
    --output "$LOG_DIR/report-todoapp.md" \
    2>&1 | tee "$LOG_DIR/todoapp.log"

log ""
log "TodoApp test complete."

# ── CLEANUP 2 ─────────────────────────────────────────────────────────────────
log ""
log "══ FINAL CLEANUP ══"

$PYTHON "$TOOLS/cleanup_worker.py" "$WORKER_ID" 2>&1 | tee -a "$LOG_DIR/suite.log"

verify_worker_healthy

# ── COMBINED REPORT ───────────────────────────────────────────────────────────
log ""
log "══ COMBINED REPORT ══"

REPORT="$LOG_DIR/COMBINED_REPORT.md"
{
    echo "# Capacity Stress Test — Combined Report"
    echo "Date: $(date -u '+%Y-%m-%d %H:%M UTC')"
    echo "Worker: $WORKER_ID (autonomous-enablements-worker, 172.31.10.70)"
    echo "Instance: 8 vCPU / 15GiB RAM"
    echo ""
    echo "---"
    echo ""
    if [[ -f "$LOG_DIR/report-astroshop.md" ]]; then
        cat "$LOG_DIR/report-astroshop.md"
    else
        echo "## ASTROSHOP — report missing"
    fi
    echo ""
    echo "---"
    echo ""
    if [[ -f "$LOG_DIR/report-todoapp.md" ]]; then
        cat "$LOG_DIR/report-todoapp.md"
    else
        echo "## TODOAPP — report missing"
    fi
} > "$REPORT"

log ""
log "=========================================="
log "SUITE COMPLETE"
log "Report: $REPORT"
log "=========================================="

echo ""
cat "$REPORT"

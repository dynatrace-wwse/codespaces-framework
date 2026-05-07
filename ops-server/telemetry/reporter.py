"""Telemetry reporter — sends CI/agent results to codespaces-tracker and DT."""

import base64
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from webhook.config import TRACKER_ENDPOINT, TRACKER_TOKEN, DT_ENVIRONMENT

log = logging.getLogger("ops-telemetry")

_FW_VER_RE = re.compile(r'^FRAMEWORK_VERSION="?\$\{?FRAMEWORK_VERSION:?-?([^}"\s]+)"?\}?', re.M)


def extract_framework_version(repo_dir: str | Path) -> str:
    """Read FRAMEWORK_VERSION pinned in a repo's source_framework.sh.

    Falls back to ``unknown`` if the file is absent or unparseable. Used to
    stamp telemetry events with the version of the framework actually under
    test rather than a hardcoded constant.
    """
    candidates = [
        Path(repo_dir) / ".devcontainer" / "util" / "source_framework.sh",
        Path(repo_dir) / ".devcontainer" / "source_framework.sh",
    ]
    for path in candidates:
        try:
            text = path.read_text(errors="replace")
        except (OSError, FileNotFoundError):
            continue
        m = _FW_VER_RE.search(text)
        if m:
            return m.group(1).strip()
    return "unknown"


def _base_payload(
    *,
    repo: str,
    framework_version: str = "unknown",
    arch: str = "",
    branch: str = "",
    commit_sha: str = "",
    triggered_by: str = "",
    worker_id: str = "",
    job_id: str = "",
    nightly_run_id: str = "",
) -> dict:
    """Common fields for every ops.* BizEvent."""
    return {
        "repository": repo,
        "repository.name": repo.split("/")[-1] if "/" in repo else repo,
        "codespace.type": "ops-server",
        "codespace.app_id": f"dynatrace-wwse-{repo.split('/')[-1]}",
        "environment": DT_ENVIRONMENT,
        "framework.version": framework_version,
        "ops.arch": arch,
        "ops.branch": branch,
        "ops.commit_sha": commit_sha,
        "ops.triggered_by": triggered_by,
        "ops.worker_id": worker_id,
        "ops.job_id": job_id,
        "ops.nightly.run_id": nightly_run_id,
        "ops.event.timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def report_test_result(
    repo: str,
    passed: bool,
    duration_seconds: int,
    error_detail: str = "",
    nightly_run_id: str = "",
    *,
    framework_version: str = "unknown",
    arch: str = "",
    branch: str = "",
    commit_sha: str = "",
    triggered_by: str = "",
    worker_id: str = "",
    job_id: str = "",
    status: str = "completed",
    failed_step: str = "",
    failure_summary: str = "",
):
    """Report an integration test result to the codespaces tracker.

    Schema extended (2026-05-07): now carries arch/branch/commit_sha/triggered_by/
    worker_id/framework_version. Status taxonomy: completed | failed | terminated.
    """
    payload = _base_payload(
        repo=repo,
        framework_version=framework_version,
        arch=arch, branch=branch, commit_sha=commit_sha,
        triggered_by=triggered_by, worker_id=worker_id, job_id=job_id,
        nightly_run_id=nightly_run_id,
    )
    payload.update({
        "ops.event.type": "test.result",
        "ops.test.passed": passed,
        "ops.test.status": status,
        "ops.test.duration": duration_seconds,
        "ops.test.errors_detail": error_detail[:500],
        "ops.test.failed_step": failed_step,
        "ops.test.failure_summary": failure_summary[:500],
    })
    await _post_to_tracker(payload)


async def report_build_started(
    repo: str,
    *,
    framework_version: str = "unknown",
    arch: str = "",
    branch: str = "",
    commit_sha: str = "",
    triggered_by: str = "",
    worker_id: str = "",
    job_id: str = "",
    nightly_run_id: str = "",
    queue_wait_ms: int = 0,
):
    """Emit when a worker picks up a job (after lock acquisition)."""
    payload = _base_payload(
        repo=repo, framework_version=framework_version,
        arch=arch, branch=branch, commit_sha=commit_sha,
        triggered_by=triggered_by, worker_id=worker_id, job_id=job_id,
        nightly_run_id=nightly_run_id,
    )
    payload.update({
        "ops.event.type": "build.started",
        "ops.build.queue_wait_ms": queue_wait_ms,
    })
    await _post_to_tracker(payload)


async def report_build_deferred(
    repo: str,
    *,
    arch: str = "",
    branch: str = "",
    triggered_by: str = "",
    worker_id: str = "",
    job_id: str = "",
    holder_job_id: str = "",
):
    """Emit when ``running:lock:{triple}`` blocks an enqueue.

    Surfaces concurrency contention for dashboards: how often the no-concurrent-
    runs rule is exercised, which triples are most contended.
    """
    payload = _base_payload(
        repo=repo,
        arch=arch, branch=branch,
        triggered_by=triggered_by, worker_id=worker_id, job_id=job_id,
    )
    payload.update({
        "ops.event.type": "build.deferred",
        "ops.build.lock_holder_job_id": holder_job_id,
    })
    await _post_to_tracker(payload)


async def report_agent_action(
    repo: str,
    action_type: str,
    success: bool,
    details: dict | None = None,
):
    """Report a Claude agent action (fix, migration, scaffold, review)."""
    payload = {
        "repository": repo,
        "repository.name": repo.split("/")[-1] if "/" in repo else repo,
        "ops.event.type": f"agent.{action_type}",
        "ops.agent.success": success,
        "ops.agent.details": json.dumps(details or {}),
        "codespace.type": "ops-server",
        "codespace.app_id": f"dynatrace-wwse-{repo.split('/')[-1]}",
        "environment": DT_ENVIRONMENT,
        "framework.version": "ops-server",
    }
    await _post_to_tracker(payload)


async def report_nightly_summary(
    run_id: str,
    total: int,
    passed: int,
    failed: int,
    duration_seconds: int,
):
    """Report a nightly run summary."""
    payload = {
        "repository": "dynatrace-wwse/codespaces-framework",
        "repository.name": "codespaces-framework",
        "ops.event.type": "nightly.summary",
        "ops.nightly.run_id": run_id,
        "ops.nightly.total": total,
        "ops.nightly.passed": passed,
        "ops.nightly.failed": failed,
        "ops.nightly.pass_rate": round(passed / total * 100, 1) if total > 0 else 0,
        "ops.nightly.duration": duration_seconds,
        "codespace.type": "ops-server",
        "codespace.app_id": "dynatrace-wwse-ops-server",
        "environment": DT_ENVIRONMENT,
        "framework.version": "ops-server",
    }
    await _post_to_tracker(payload)


async def report_sync_drift(repo: str, current_version: str, target_version: str):
    """Report a framework version drift detection."""
    payload = {
        "repository": repo,
        "repository.name": repo.split("/")[-1] if "/" in repo else repo,
        "ops.event.type": "sync.drift",
        "ops.sync.current_version": current_version,
        "ops.sync.target_version": target_version,
        "ops.sync.drifted": current_version != target_version,
        "codespace.type": "ops-server",
        "codespace.app_id": f"dynatrace-wwse-{repo.split('/')[-1]}",
        "environment": DT_ENVIRONMENT,
        "framework.version": "ops-server",
    }
    await _post_to_tracker(payload)


async def _post_to_tracker(payload: dict):
    """Post a JSON payload to the codespaces tracker endpoint."""
    if not TRACKER_ENDPOINT:
        log.warning("ENDPOINT_CODESPACES_TRACKER not set — skipping telemetry")
        return

    headers = {
        "Content-Type": "application/json",
        # Tracker expects the raw base64 token, NOT "Basic <token>".
        # Matches the framework's postCodespacesTracker in functions.sh.
        "Authorization": TRACKER_TOKEN,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(TRACKER_ENDPOINT, json=payload, headers=headers)
            if resp.status_code == 200:
                log.info("Telemetry sent: %s for %s", payload.get("ops.event.type"), payload.get("repository.name"))
            else:
                log.warning(
                    "Tracker returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
    except Exception as e:
        log.error("Failed to post telemetry: %s", e)

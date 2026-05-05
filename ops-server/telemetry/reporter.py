"""Telemetry reporter — sends CI/agent results to codespaces-tracker and DT."""

import base64
import json
import logging
from datetime import datetime, timezone

import httpx

from webhook.config import TRACKER_ENDPOINT, TRACKER_TOKEN, DT_ENVIRONMENT

log = logging.getLogger("ops-telemetry")


async def report_test_result(
    repo: str,
    passed: bool,
    duration_seconds: int,
    error_detail: str = "",
    nightly_run_id: str = "",
):
    """Report an integration test result to the codespaces tracker."""
    payload = {
        "repository": repo,
        "repository.name": repo.split("/")[-1] if "/" in repo else repo,
        "ops.event.type": "test.result",
        "ops.test.passed": passed,
        "ops.test.duration": duration_seconds,
        "ops.test.errors_detail": error_detail[:500],
        "ops.nightly.run_id": nightly_run_id,
        "codespace.type": "ops-server",
        "codespace.app_id": f"dynatrace-wwse-{repo.split('/')[-1]}",
        "environment": DT_ENVIRONMENT,
        "framework.version": "ops-server",
    }
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
        "Authorization": f"Basic {TRACKER_TOKEN}",
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

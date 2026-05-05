"""GitHub webhook listener — routes events to job queue."""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
import redis.asyncio as redis

from webhook.config import WEBHOOK_SECRET, REDIS_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ops-webhook")

app = FastAPI(title="Enablement Ops Webhook", version="1.0.0")
pool: redis.Redis | None = None


@app.on_event("startup")
async def startup():
    global pool
    pool = redis.from_url(REDIS_URL, decode_responses=True)
    log.info("Connected to Redis at %s", REDIS_URL)


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.aclose()


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not WEBHOOK_SECRET:
        log.warning("WEBHOOK_SECRET not set — skipping signature verification")
        return True
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook")
async def webhook(request: Request):
    """Receive and route GitHub webhook events."""
    payload = await request.body()

    # Verify signature
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(payload, signature):
        raise HTTPException(status_code= 401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "ping")
    delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")

    if event_type == "ping":
        log.info("Ping received (delivery: %s)", delivery_id)
        return {"status": "pong"}

    data = json.loads(payload)
    action = data.get("action", "")
    repo_full = data.get("repository", {}).get("full_name", "unknown")

    log.info("Event: %s.%s on %s (delivery: %s)", event_type, action, repo_full, delivery_id)

    job = route_event(event_type, action, data, repo_full, delivery_id)

    if job:
        await pool.rpush(f"queue:{job['queue']}", json.dumps(job))
        log.info("Queued job: %s → queue:%s", job["type"], job["queue"])
        return {"status": "queued", "queue": job["queue"], "type": job["type"]}

    return {"status": "ignored", "event": f"{event_type}.{action}"}


def route_event(
    event_type: str, action: str, data: dict, repo: str, delivery_id: str
) -> dict | None:
    """Route a GitHub event to the appropriate job queue. Returns job dict or None."""

    timestamp = datetime.now(timezone.utc).isoformat()
    base = {
        "repo": repo,
        "delivery_id": delivery_id,
        "timestamp": timestamp,
    }

    # ── Issues ───────────────────────────────────────────────────────────
    if event_type == "issues" and action in ("opened", "labeled"):
        issue = data.get("issue", {})
        labels = [l["name"] for l in issue.get("labels", [])]
        issue_url = issue.get("html_url", "")
        title = issue.get("title", "")
        body = issue.get("body", "") or ""

        if "bug" in labels:
            return {
                **base,
                "queue": "agent",
                "type": "fix-issue",
                "issue_number": issue["number"],
                "issue_url": issue_url,
                "title": title,
                "body": body,
                "labels": labels,
            }

        if "gen3-migration" in labels:
            return {
                **base,
                "queue": "agent",
                "type": "migrate-gen3",
                "issue_number": issue["number"],
                "issue_url": issue_url,
                "title": title,
                "body": body,
            }

        if "new-enablement" in labels:
            return {
                **base,
                "queue": "agent",
                "type": "scaffold-lab",
                "issue_number": issue["number"],
                "issue_url": issue_url,
                "title": title,
                "body": body,
            }

    # ── Pull Requests ────────────────────────────────────────────────────
    if event_type == "pull_request" and action == "opened":
        pr = data.get("pull_request", {})
        # Skip PRs created by the ops bot itself
        if pr.get("user", {}).get("login", "") == "ops-bot":
            return None
        return {
            **base,
            "queue": "agent",
            "type": "review-pr",
            "pr_number": pr["number"],
            "pr_url": pr.get("html_url", ""),
            "title": pr.get("title", ""),
        }

    # ── CI Check Failures ────────────────────────────────────────────────
    if event_type == "check_suite" and action == "completed":
        suite = data.get("check_suite", {})
        if suite.get("conclusion") == "failure":
            # Only act on PRs, not pushes to main
            prs = suite.get("pull_requests", [])
            if prs:
                return {
                    **base,
                    "queue": "agent",
                    "type": "fix-ci",
                    "check_suite_id": suite["id"],
                    "conclusion": suite["conclusion"],
                    "pr_numbers": [pr["number"] for pr in prs],
                    "head_sha": suite.get("head_sha", ""),
                }

    # ── Push to main (trigger sync validation) ───────────────────────────
    if event_type == "push":
        ref = data.get("ref", "")
        if ref == "refs/heads/main":
            return {
                **base,
                "queue": "sync",
                "type": "validate-after-push",
                "head_sha": data.get("after", ""),
                "commits": len(data.get("commits", [])),
            }

    return None


@app.get("/health")
async def health():
    """Health check endpoint."""
    try:
        await pool.ping()
        queue_lengths = {}
        for q in ("agent", "sync", "test"):
            queue_lengths[q] = await pool.llen(f"queue:{q}")
        return {
            "status": "healthy",
            "redis": "connected",
            "queues": queue_lengths,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/status")
async def status():
    """Show recent job history and queue state."""
    recent = await pool.lrange("jobs:completed", -20, -1)
    return {
        "queues": {
            "agent": await pool.llen("queue:agent"),
            "sync": await pool.llen("queue:sync"),
            "test": await pool.llen("queue:test"),
        },
        "recent_completed": [json.loads(j) for j in recent],
    }


def start():
    """Entry point for systemd service."""
    import uvicorn
    from webhook.config import HOST, PORT

    uvicorn.run(
        "webhook.server:app",
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    start()

"""Pure helpers for training-test enqueue dedupe + queued visibility.

Dependency-free (no redis / httpx / fastapi) so that BOTH sides of the
lifecycle import the *same* lock-key construction:

  * dashboard/app.py  — SET NX at enqueue (dedupe) + queue:training listing
  * workers/manager.py — DEL in _run_training_test's finally (release)

and so the logic is unit-testable without a running Redis. See
ISSUES-TO-SOLVE.md "BUG: training-test trigger — no queued feedback +
duplicate enqueues".
"""

# Auto-expire backstop for the enqueue lock: a crashed / killed / lost run can
# never wedge a repo forever. 2h comfortably exceeds the 90-min (5400s) hard
# runner timeout in workers/manager.py:_run_training_test.
TRAINING_LOCK_TTL_SECONDS = 7200


def training_lock_key(repo: str, ref: str) -> str:
    """Redis STRING key that dedupes one queued-or-running training-test per
    ``(repo, ref)``.

    Set ``NX EX TRAINING_LOCK_TTL_SECONDS`` at enqueue (dashboard/app.py) and
    released in the worker's ``_run_training_test`` finally-block — same
    lifecycle as ``running:lock:*``. Scoped by repo+ref so the nightly (which
    enqueues *distinct* repos) is never blocked; MUST NOT lock on the bare
    job type.
    """
    return f"training:lock:{repo}:{ref or ''}"


def already_queued_response(repo: str, ref: str, holder: str = "") -> dict:
    """Body returned (with HTTP 409) when a training-test for this ``(repo,
    ref)`` is already queued or running, instead of pushing a duplicate.

    ``holder`` is the ``requested_by`` recorded on the existing lock (best
    effort — may be empty if the lock value was lost)."""
    return {
        "status": "already-queued",
        "repo": repo,
        "ref": ref,
        "type": "training-test",
        "requested_by": holder,
        "detail": (
            f"A training-test for {repo} @ {ref or 'catalog branch'} is "
            f"already queued or running — not enqueuing a duplicate."
        ),
    }


def queue_item_view(job: dict, queue_key: str, arch: str, position: int,
                    requested_by: str) -> dict:
    """Map one raw queued job (already JSON-decoded) to a dashboard queue row.

    ``requested_by`` is passed in already masked/unmasked by the caller so
    this helper stays free of the PII-masking dependency.
    """
    return {
        "queue":        queue_key,
        "arch":         job.get("arch", arch),
        "position":     position,
        "job_id":       job.get("job_id", ""),
        "repo":         job.get("repo", ""),
        "ref":          job.get("ref") or job.get("branch")
                        or job.get("head_branch") or "main",
        "type":         job.get("type", "integration-test"),
        "queued_at":    job.get("timestamp") or job.get("queued_at"),
        "requested_by": requested_by,
    }

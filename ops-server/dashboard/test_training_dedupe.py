"""Pure-logic tests for training-test enqueue dedupe + queued visibility
(dashboard/training_dedupe.py).

No Redis, no FastAPI — exercises only the helpers the /api/builds/trigger
(training-test branch), /api/queue/list, and workers/manager.py
_run_training_test share: the lock-key construction (must be identical on the
SET-NX enqueue side and the DEL release side), the already-queued 409 body
shape, and the queue LRANGE -> dashboard-row mapping.

Runnable two ways:
  - pytest:     python3 -m pytest dashboard/test_training_dedupe.py
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_training_dedupe
"""

from dashboard import training_dedupe as td

REPO = "dynatrace-wwse/live-debugger"


# ── Lock key construction ────────────────────────────────────────────────────

def test_lock_key_is_repo_and_ref_scoped():
    # The whole point: repo+ref, never bare type — so the nightly (distinct
    # repos) is never blocked by a manual trigger.
    assert td.training_lock_key(REPO, "main") == \
        "training:lock:dynatrace-wwse/live-debugger:main"


def test_lock_key_distinct_repos_do_not_collide():
    a = td.training_lock_key("dynatrace-wwse/kubernetes-101", "main")
    b = td.training_lock_key("dynatrace-wwse/dtwiz-101", "main")
    assert a != b


def test_lock_key_distinct_refs_do_not_collide():
    assert td.training_lock_key(REPO, "main") != td.training_lock_key(REPO, "dev")


def test_lock_key_empty_ref_is_stable():
    # Nightly enqueues carry no ref -> "" ; must be deterministic, not crash.
    assert td.training_lock_key(REPO, "") == "training:lock:dynatrace-wwse/live-debugger:"
    assert td.training_lock_key(REPO, None) == td.training_lock_key(REPO, "")


def test_enqueue_and_release_agree_on_the_key():
    # Simulate the two sides. Enqueue (app.py): ref defaults to "main".
    enqueue_ref = "main"
    enqueue_key = td.training_lock_key(REPO, enqueue_ref)
    # Release (manager.py): ref = job.get("ref") or job.get("branch") or "".
    job = {"repo": REPO, "ref": "main", "type": "training-test"}
    release_ref = job.get("ref") or job.get("branch") or ""
    release_key = td.training_lock_key(job["repo"], release_ref)
    assert enqueue_key == release_key, "dedupe is broken if the keys diverge"


def test_lock_ttl_exceeds_runner_timeout():
    # A crashed run must auto-clear; the backstop TTL must outlast the 90-min
    # (5400s) hard runner timeout so it never expires mid-run.
    assert td.TRAINING_LOCK_TTL_SECONDS > 5400


# ── already-queued 409 body ──────────────────────────────────────────────────

def test_already_queued_response_shape():
    body = td.already_queued_response(REPO, "main", holder="alice")
    assert body["status"] == "already-queued"
    assert body["repo"] == REPO
    assert body["ref"] == "main"
    assert body["type"] == "training-test"
    assert body["requested_by"] == "alice"
    assert "already queued or running" in body["detail"]


def test_already_queued_response_tolerates_missing_holder():
    body = td.already_queued_response(REPO, "main")
    assert body["requested_by"] == ""
    assert body["status"] == "already-queued"


# ── queue LRANGE -> dashboard row ────────────────────────────────────────────

def test_queue_item_view_training_job():
    job = {
        "type": "training-test", "repo": REPO, "arch": "amd64", "ref": "main",
        "timestamp": "2026-08-03T10:00:00+00:00", "requested_by": "bob",
        "job_id": "mk3p9aqz-7f3a",
    }
    row = td.queue_item_view(job, "queue:training", "amd64", 0, "bob")
    assert row["queue"] == "queue:training"
    assert row["type"] == "training-test"
    assert row["repo"] == REPO
    assert row["ref"] == "main"
    assert row["arch"] == "amd64"
    assert row["position"] == 0
    assert row["queued_at"] == "2026-08-03T10:00:00+00:00"
    assert row["requested_by"] == "bob"      # caller passes it already masked
    assert row["job_id"] == "mk3p9aqz-7f3a"


def test_queue_item_view_nightly_training_job_has_no_ref():
    # Nightly (scheduler.py) enqueues without ref/requested_by.
    job = {"type": "training-test", "repo": REPO, "arch": "amd64",
           "timestamp": "2026-08-03T02:00:00+00:00", "nightly_run_id": "nightly-x"}
    row = td.queue_item_view(job, "queue:training", "amd64", 2, "")
    assert row["ref"] == "main"              # falls back, never blank/None
    assert row["requested_by"] == ""
    assert row["position"] == 2


def test_queue_item_view_default_arch_for_test_queue():
    job = {"type": "integration-test", "repo": REPO, "branch": "dev"}
    row = td.queue_item_view(job, "queue:test:arm64", "arm64", 1, "")
    assert row["arch"] == "arm64"            # falls back to the queue's arch
    assert row["ref"] == "dev"               # branch used when ref absent


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print("all training-dedupe tests passed")

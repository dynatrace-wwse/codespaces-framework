"""Tests for _dead_worker_candidate — the terminate reconciler's orphan gate.

Regression cover for 2026-08-10: every Codespace record was reaped ~8 s after
provision because `worker_id="github-codespaces"` is a display label, not a
registered worker. That took the in-app terminal (shell-token 404), the relay
sshd install gate (/api/codespace/orbital/{name} → false), the ssh_ready hold,
idle/expiry reaping and History with it.

Run: /home/ops/ops-venv/bin/python -m workers.test_dead_worker_candidate
  or pytest workers/test_dead_worker_candidate.py
"""

import workers.manager as m


# ── Codespaces are never reapable, whatever worker_id claims ────────────────────

def test_codespace_is_never_a_candidate():
    rec = {"provider": "codespace", "worker_id": "github-codespaces"}
    assert m._dead_worker_candidate(rec, "didactic-goggles-abc", set()) is False


def test_codespace_not_reaped_even_when_flagged_terminating():
    # _expiry_reaper owns codespace cleanup; this gate must stay out of it.
    rec = {"provider": "codespace", "worker_id": "github-codespaces",
           "terminating": "1"}
    assert m._dead_worker_candidate(rec, "cs-1", set()) is False


def test_codespace_with_an_odd_worker_id_still_excluded():
    rec = {"provider": "codespace", "worker_id": "wamd001"}
    assert m._dead_worker_candidate(rec, "cs-2", set()) is False


# ── Real dead-worker orphans must still be reaped ───────────────────────────────

def test_remote_worker_orphan_is_a_candidate():
    rec = {"provider": "sysbox", "worker_id": "wamd001"}
    assert m._dead_worker_candidate(rec, "job-1", set()) is True


def test_remote_worker_orphan_candidate_when_provider_absent():
    # Older records predate the provider field — they must not become immune.
    rec = {"worker_id": "worker-x86_64-spot-0e9b"}
    assert m._dead_worker_candidate(rec, "job-2", set()) is True


def test_master_job_is_not_a_candidate():
    rec = {"provider": "sysbox", "worker_id": "master"}
    assert m._dead_worker_candidate(rec, "job-3", set()) is False


def test_live_local_task_is_not_a_candidate():
    rec = {"provider": "sysbox", "worker_id": "wamd001"}
    assert m._dead_worker_candidate(rec, "job-4", {"job-4": object()}) is False


def test_missing_worker_id_is_not_a_candidate():
    assert m._dead_worker_candidate({"provider": "sysbox"}, "job-5", set()) is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all dead-worker-candidate tests passed")


# ── a job nobody has claimed yet has no dead worker ─────────────────────────

def test_a_queued_job_is_never_an_orphan():
    """MEASURED 2026-08-14 on a 12-seat load test, and it took paced admission
    to expose it.

    api_arena_provision writes worker_id="queued" as a placeholder before the
    job is enqueued. No worker registers under that name, so the reconciler
    concluded the owning worker had vanished and deleted the record. While a
    job sat in the queue for a second or two the race almost never fired; the
    pacer holds a learner for minutes, and then it fired on all ten who waited
    while the two admitted in the opening burst were fine.

    Nothing looked broken afterwards — the worker recreated a bare record and
    the session came up. What vanished was workshop_id (so ending the workshop
    terminates nothing), dt_token_ids (so terminate cannot revoke), expires_at
    and arena_user.
    """
    from workers.manager import _dead_worker_candidate

    rec = {"worker_id": "queued", "workshop_id": "ws_x",
           "dt_token_ids": '["dt0c01.A"]', "arena_user": "bot@x.io"}
    assert _dead_worker_candidate(rec, "job-1", {}) is False


def test_a_record_with_no_worker_yet_is_never_an_orphan():
    from workers.manager import _dead_worker_candidate
    assert _dead_worker_candidate({"worker_id": ""}, "job-1", {}) is False
    assert _dead_worker_candidate({}, "job-1", {}) is False


def test_a_real_worker_id_is_still_a_candidate():
    """The gate must keep doing its job: a reclaimed spot worker's records
    really do have to be reaped, or a re-provisioning learner matches a dead
    session."""
    from workers.manager import _dead_worker_candidate
    assert _dead_worker_candidate(
        {"worker_id": "worker-x86_64-spot-3a5794e5"}, "job-1", {}) is True

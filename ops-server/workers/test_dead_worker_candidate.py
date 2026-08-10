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

"""Tests for the slot re-init race hardening (2026-08-03 load test).

Pure logic — no Redis/Docker — so it runs anywhere.

Runnable two ways (mirrors test_scheduler.py / test_terminate.py):
  - pytest:     /home/ops/ops-venv/bin/python -m pytest worker-agent/test_slot_reinit.py
  - standalone: cd ops-server && /home/ops/ops-venv/bin/python -m worker-agent.test_slot_reinit

Regression contract: under mass-terminate, a terminate's ``docker rm -f`` of a
slot container races the pool's re-init ``docker run`` for the same name —
'docker: Conflict. The container name "/sb-slot-amd001-3" is already in use'.
That single failure used to remove the slot from the pool until the next agent
restart. Now the conflict is detected (force-remove + one in-place retry) and
release() retries the whole re-init with bounded backoff before giving up.
"""

from .agent import (
    SLOT_REINIT_ATTEMPTS,
    SLOT_REINIT_BACKOFF_MAX_S,
    _is_container_name_conflict,
    _reinit_backoff_s,
)


# ── name-conflict detection ──────────────────────────────────────────────────

def test_conflict_detected_from_journal_message():
    # Exact shape seen on amd001 during the 2026-08-03 mass-terminate.
    err = ('docker: Conflict. The container name "/sb-slot-amd001-3" is already '
           'in use by container "1f2e3d". You have to remove (or rename) that '
           'container to be able to reuse that name.')
    assert _is_container_name_conflict(err) is True


def test_conflict_detected_case_variants():
    assert _is_container_name_conflict("Conflict. The container name ...") is True
    assert _is_container_name_conflict('name "/x" is already in use by container') is True


def test_non_conflict_errors_not_matched():
    assert _is_container_name_conflict("") is False
    assert _is_container_name_conflict(None) is False
    assert _is_container_name_conflict("docker: Error response from daemon: OCI runtime create failed") is False
    assert _is_container_name_conflict("No such container: sb-slot-amd001-3") is False


# ── re-init backoff schedule ─────────────────────────────────────────────────

def test_backoff_doubles_and_caps():
    assert [_reinit_backoff_s(a) for a in range(1, SLOT_REINIT_ATTEMPTS + 1)] == [5, 10, 20, 40, 60]


def test_backoff_never_exceeds_cap():
    for a in range(1, 20):
        assert _reinit_backoff_s(a) <= SLOT_REINIT_BACKOFF_MAX_S


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    print(f"{'FAILED' if failures else 'OK'} ({failures} failures)")
    sys.exit(1 if failures else 0)

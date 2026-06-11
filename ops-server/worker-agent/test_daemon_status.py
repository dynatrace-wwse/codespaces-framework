"""Tests for the daemon provisioning-failure → session status mapping.

Pure logic — no Redis/Docker — so it runs anywhere.

Runnable two ways (mirrors test_scheduler.py):
  - pytest:     /home/ops/ops-venv/bin/python -m pytest worker-agent/test_daemon_status.py
  - standalone: cd ops-server && /home/ops/ops-venv/bin/python -m worker-agent.test_daemon_status

Covers the provisioning red-banner contract: a daemon whose postCreate/postStart
exits non-zero (e.g. the repo's variablesNeeded failing closed on a missing DT
token) must NOT report "Daemon ready" and must surface as a failed session.
"""

from .executor import daemon_provisioned
from .agent import daemon_failed


def test_provisioned_only_on_zero_exit():
    assert daemon_provisioned(0) is True
    assert daemon_provisioned(1) is False
    assert daemon_provisioned(124) is False   # setup timeout


def test_daemon_failed_on_nonzero_exit():
    job = {"type": "daemon"}
    assert daemon_failed(job, {"exit_code": 1}) is True
    assert daemon_failed(job, {"exit_code": 124}) is True


def test_daemon_ok_on_zero_or_missing_exit():
    job = {"type": "daemon"}
    assert daemon_failed(job, {"exit_code": 0}) is False
    assert daemon_failed(job, {}) is False          # healthy / terminated daemon
    assert daemon_failed(job, None) is False


def test_non_daemon_never_flagged():
    # Integration tests keep their completed-with-passed-flag semantics.
    assert daemon_failed({"type": "integration-test"}, {"exit_code": 1}) is False
    assert daemon_failed({"type": "fix-issue"}, {"exit_code": 2}) is False


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)

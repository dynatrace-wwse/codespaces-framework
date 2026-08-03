#!/usr/bin/env python3
"""Tests for the pure helpers in tools/bootcamp_loadtest.py.

Covers the 2026-08-03 load-test hardening:
  - bot-email matching (teardown must only ever touch bot identities)
  - queue-entry matching (drain removes ONLY queued bot provisions)
  - per-user state-path selection (cross-user /tmp permission collisions)
  - save_state never crashes on an unwritable path
  - sourceTenant stamped on every emitted bizevent (--tenant)

Run:  python3 -m pytest tools/test_bootcamp_loadtest.py
  or: python3 tools/test_bootcamp_loadtest.py
"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootcamp_loadtest as blt  # noqa: E402


# ── bot-email matching ───────────────────────────────────────────────────────

def test_is_bot_email_matches_default_pattern():
    assert blt.is_bot_email("bot01@bootcamp.dev")
    assert blt.is_bot_email("bot7@bootcamp.dev")       # unpadded index
    assert blt.is_bot_email("bot150@bootcamp.dev")     # >2 digits
    assert blt.is_bot_email("  Bot03@BOOTCAMP.DEV  ")  # case/whitespace-insensitive


def test_is_bot_email_rejects_non_bots():
    assert not blt.is_bot_email("sergio@dynatrace.com")
    assert not blt.is_bot_email("bot01@dynatrace.com")   # wrong domain
    assert not blt.is_bot_email("robot01@bootcamp.dev")  # prefix must anchor at start
    assert not blt.is_bot_email("bot@bootcamp.dev")      # no index
    assert not blt.is_bot_email("bot01@bootcamp.devx")   # domain must anchor at end
    assert not blt.is_bot_email("")
    assert not blt.is_bot_email(None)


def test_bot_email_roundtrips_through_matcher():
    for i in (1, 9, 42, 100):
        assert blt.is_bot_email(blt.bot_email(i))


# ── queue-entry matching ─────────────────────────────────────────────────────

def _queued(requested_by):
    return json.dumps({
        "job_id": "enablement-abc123", "type": "daemon",
        "repo": "dynatrace-wwse/enablement-kubernetes-101",
        "arch": "amd64", "requested_by": requested_by,
    })


def test_queue_entry_bot_email_matches_bot_jobs():
    assert blt.queue_entry_bot_email(_queued("bot05@bootcamp.dev")) == "bot05@bootcamp.dev"
    assert blt.queue_entry_bot_email(_queued("BOT05@bootcamp.dev")) == "bot05@bootcamp.dev"


def test_queue_entry_bot_email_never_matches_other_users():
    assert blt.queue_entry_bot_email(_queued("student@example.com")) is None
    assert blt.queue_entry_bot_email(_queued("")) is None
    # Nightly/CI jobs have no requested_by at all.
    assert blt.queue_entry_bot_email(json.dumps({"job_id": "x", "repo": "r"})) is None


def test_queue_entry_bot_email_tolerates_garbage():
    assert blt.queue_entry_bot_email("not json {") is None
    assert blt.queue_entry_bot_email("") is None
    assert blt.queue_entry_bot_email(json.dumps(["a", "list"])) is None
    assert blt.queue_entry_bot_email(json.dumps("a string")) is None


# ── state-path selection ─────────────────────────────────────────────────────

def test_state_file_is_per_user():
    assert blt.state_file_for("alice") == "/tmp/bootcamp_loadtest_state-alice.json"
    assert blt.state_file_for("bob") != blt.state_file_for("alice")


def test_module_state_file_uses_invoking_user_and_keeps_legacy_fallback():
    assert blt.STATE_FILE == blt.state_file_for(getpass.getuser())
    assert blt.LEGACY_STATE_FILE == "/tmp/bootcamp_loadtest_state.json"
    assert blt.STATE_FILE != blt.LEGACY_STATE_FILE


def test_save_state_warns_but_does_not_crash_on_unwritable_path(monkeypatch=None):
    orig = blt.STATE_FILE
    blt.STATE_FILE = "/nonexistent-dir/bootcamp_loadtest_state.json"
    try:
        blt.save_state({"sessions": {}})  # must not raise (PermissionError regression)
    finally:
        blt.STATE_FILE = orig


# ── terminate target merge (live scan ∪ state file) ──────────────────────────

def test_merge_prefers_live_and_dedupes_state_overlap():
    live = {"bot01@bootcamp.dev": ["job-live-1"], "bot02@bootcamp.dev": ["job-live-2"]}
    state = {"bot01@bootcamp.dev": {"jobId": "job-live-1"},        # tracked AND live
             "bot03@bootcamp.dev": {"jobId": "job-stale-3"}}       # state-only (stale run)
    targets = blt.merge_terminate_targets(live, state)
    assert targets == [
        ("bot01@bootcamp.dev", "job-live-1"),
        ("bot02@bootcamp.dev", "job-live-2"),
        ("bot03@bootcamp.dev", "job-stale-3"),
    ]


def test_merge_handles_multiple_live_sessions_per_bot():
    # active-sessions is a list — a bot can own >1 running env after a crashed run.
    live = {"bot01@bootcamp.dev": ["job-a", "job-b"]}
    assert blt.merge_terminate_targets(live, {}) == [
        ("bot01@bootcamp.dev", "job-a"), ("bot01@bootcamp.dev", "job-b")]


def test_merge_survives_empty_stale_or_malformed_state():
    # The stale-state bug: --terminate must still work from the live scan alone.
    live = {"bot01@bootcamp.dev": ["job-1"]}
    assert blt.merge_terminate_targets(live, {}) == [("bot01@bootcamp.dev", "job-1")]
    assert blt.merge_terminate_targets(live, None) == [("bot01@bootcamp.dev", "job-1")]
    assert blt.merge_terminate_targets(
        live, {"bot02@bootcamp.dev": {}, "bot03@bootcamp.dev": None}
    ) == [("bot01@bootcamp.dev", "job-1")]
    assert blt.merge_terminate_targets({}, {}) == []
    assert blt.merge_terminate_targets(None, None) == []


# ── sourceTenant stamping ────────────────────────────────────────────────────

def test_base_event_stamps_default_source_tenant():
    ev = blt.base_event("bot01@bootcamp.dev", "started", {"stepCount": 4})
    assert ev["sourceTenant"] == blt.COE_APPS


def test_base_event_stamps_custom_source_tenant_on_every_event_type():
    sro = "https://sro97894.apps.dynatrace.com"
    for etype in ("started", "step.completed", "question.answered", "completed"):
        ev = blt.base_event("bot01@bootcamp.dev", etype, {}, sro)
        assert ev["sourceTenant"] == sro, etype
        assert ev["event.type"] == f"com.dynatrace.enablement.training.{etype}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    print(f"{'FAILED' if failures else 'OK'} ({failures} failures)")
    sys.exit(1 if failures else 0)

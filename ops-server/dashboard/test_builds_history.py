"""History tab: provisioning-job filters + timestamp ordering.

The pure helpers behind ``/api/builds/history``. They exist as functions rather
than inline expressions because the ordering bug they fix is invisible in a
hand-check: ``jobs:completed`` order is *append* order, not timestamp order.
"""
import json

from dashboard.app import (
    _job_tenant,
    _job_user,
    _merge_agent_history,
    _trigger_family,
    _ts_sort_key,
)


# ── trigger families ─────────────────────────────────────────────────────────

def test_enablement_app_is_its_own_family():
    assert _trigger_family("enablement-app") == "enablement-app"


def test_legacy_arena_collapses_onto_enablement_app():
    # "arena" is the pre-rename value. If it stayed separate, filtering by
    # Enablement App would silently miss every older provisioning job.
    assert _trigger_family("arena") == "enablement-app"


def test_rerun_by_user_collapses_so_the_dropdown_is_not_one_entry_per_person():
    assert _trigger_family("rerun-by-sergio") == "rerun"
    assert _trigger_family("rerun-by-someone-else") == "rerun"


def test_pull_request_actions_collapse_to_webhook():
    assert _trigger_family("pull_request.opened") == "webhook"
    assert _trigger_family("pull_request.synchronize") == "webhook"


def test_empty_trigger_reads_as_webhook():
    # Matches the endpoint's long-standing inference for records predating the field.
    assert _trigger_family("") == "webhook"
    assert _trigger_family(None) == "webhook"


def test_unknown_trigger_passes_through_untouched():
    assert _trigger_family("stress-test") == "stress-test"


# ── identity extraction ──────────────────────────────────────────────────────

def test_user_prefers_tenant_identity_then_falls_back():
    assert _job_user({"tenant_user": "a@x", "user": "b", "requested_by": "c"}) == "a@x"
    assert _job_user({"user": "b", "requested_by": "c"}) == "b"
    assert _job_user({"requested_by": "c"}) == "c"
    assert _job_user({}) == ""


def test_tenant_is_blank_for_ci_jobs():
    # CI/framework work has no tenant; the column must render empty, not "None".
    assert _job_tenant({"repo": "dynatrace-wwse/k8s-101"}) == ""
    assert _job_tenant({"tenant": "https://sro97894.apps.dynatrace.com"}) == \
        "https://sro97894.apps.dynatrace.com"


# ── ordering ─────────────────────────────────────────────────────────────────

def test_sort_key_prefers_started_at():
    older = {"started_at": "2026-08-18T09:00:00+00:00", "finished_at": "2026-08-18T12:00:00+00:00"}
    newer = {"started_at": "2026-08-18T10:00:00+00:00", "finished_at": "2026-08-18T10:05:00+00:00"}
    assert _ts_sort_key(newer) > _ts_sort_key(older)


def test_sort_key_falls_back_to_finished_at():
    assert _ts_sort_key({"finished_at": "2026-08-18T10:00:00+00:00"}) > 0


def test_naive_timestamps_are_read_as_utc_not_crashed_on():
    # Legacy records were written without an offset. Comparing naive to aware
    # raises TypeError, which would 500 the whole tab.
    naive = _ts_sort_key({"started_at": "2026-08-18T10:00:00"})
    aware = _ts_sort_key({"started_at": "2026-08-18T10:00:00+00:00"})
    assert naive == aware


def test_unparseable_and_missing_timestamps_sink_to_the_bottom():
    assert _ts_sort_key({"started_at": "not-a-date"}) == 0.0
    assert _ts_sort_key({}) == 0.0
    assert _ts_sort_key({"started_at": None, "finished_at": ""}) == 0.0


def test_merged_agent_records_sort_by_time_not_by_list_position():
    """The bug this fixes: _merge_agent_history APPENDS the archive to the tail,
    so iterating the merged list in reverse put the OLDEST agent job first."""
    completed = [
        json.dumps({"job_id": "ci-old", "started_at": "2026-08-01T00:00:00+00:00"}),
        json.dumps({"job_id": "ci-new", "started_at": "2026-08-18T00:00:00+00:00"}),
    ]
    agent = [json.dumps({"job_id": "agent-ancient", "started_at": "2026-07-01T00:00:00+00:00"})]
    merged = [json.loads(r) for r in _merge_agent_history(completed, agent)]

    # Reverse-iteration (the old behaviour) leads with the ancient agent job.
    assert list(reversed(merged))[0]["job_id"] == "agent-ancient"
    # Sorting by timestamp puts it last, where it belongs.
    merged.sort(key=_ts_sort_key, reverse=True)
    assert [r["job_id"] for r in merged] == ["ci-new", "ci-old", "agent-ancient"]


# ── endpoint behaviour (TestClient over a fake Redis) ────────────────────────

import dashboard.app as a  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_client = TestClient(a.app, raise_server_exceptions=False)
_BEARER = {"Authorization": "Bearer history-test-token"}


def _job(job_id, **kw):
    base = {
        "job_id": job_id,
        "repo": "dynatrace-wwse/k8s-101",
        "type": "integration-test",
        "arch": "amd64",
        "ref": "main",
        "status": "completed",
        "result": {"passed": True, "duration_seconds": 60},
        "worker_id": "wamd001",
    }
    base.update(kw)
    return json.dumps(base)


# Deliberately stored in a NON-chronological order, the way jobs:completed
# actually ends up: append order is completion order, and the agent archive is
# appended to the tail regardless of age.
_COMPLETED = [
    _job("ci-mid", timestamp="2026-08-10T10:00:00+00:00"),
    _job("enablement-126cf7812984", type="daemon", trigger="enablement-app",
         requested_by="learner@example.com",
         tenant="https://sro97894.apps.dynatrace.com",
         timestamp="2026-08-18T08:00:00+00:00"),
    _job("enablement-aaaabbbbcccc", type="daemon", trigger="arena",
         requested_by="OTHER@example.com",
         tenant="https://geu80787.apps.dynatrace.com",
         timestamp="2026-08-02T08:00:00+00:00"),
    _job("ci-newest", timestamp="2026-08-18T23:00:00+00:00",
         trigger="rerun-by-sergio"),
]
_AGENT = [_job("agent-ancient", type="fix-ci", timestamp="2026-07-01T00:00:00+00:00")]


class _HistoryPool:
    async def lrange(self, key, start, end):
        if key == "jobs:completed":
            return list(_COMPLETED)
        if key == "agent:jobs:completed":
            return list(_AGENT)
        return []


def setup_module(_module):
    _module._saved_pool = a.pool
    _module._saved_tokens = a.ORBITAL_TOKENS
    a.pool = _HistoryPool()
    a.ORBITAL_TOKENS = ("history-test-token",)


def teardown_module(_module):
    a.pool = _module._saved_pool
    a.ORBITAL_TOKENS = _module._saved_tokens


def _get(query=""):
    r = _client.get(f"/api/builds/history{query}", headers=_BEARER)
    assert r.status_code == 200, r.text
    return r.json()


def test_history_is_gated_dashboard_only():
    # No bearer, no X-Auth-User → the DASHBOARD_ONLY_READS middleware answers.
    # This is what makes returning unmasked emails/tenants acceptable here.
    assert _client.get("/api/builds/history").status_code == 401


def test_rows_are_newest_first_across_the_agent_merge():
    ids = [r["job_id"] for r in _get()["rows"]]
    assert ids == ["ci-newest", "enablement-126cf7812984", "ci-mid",
                   "enablement-aaaabbbbcccc", "agent-ancient"]


def test_limit_truncates_after_sorting_not_before():
    data = _get("?limit=1")
    assert [r["job_id"] for r in data["rows"]] == ["ci-newest"]
    assert data["total_returned"] == 1
    assert data["total_matched"] == 5   # so the UI can say "1 of 5"


def test_filter_by_enablement_app_trigger_includes_legacy_arena_rows():
    ids = [r["job_id"] for r in _get("?trigger=enablement-app")["rows"]]
    assert ids == ["enablement-126cf7812984", "enablement-aaaabbbbcccc"]


def test_trigger_dropdown_values_are_collapsed_families():
    assert set(_get()["filters"]["triggers"]) == {"enablement-app", "rerun", "webhook"}


def test_filter_by_daemon_job_type():
    rows = _get("?type=daemon")["rows"]
    assert {r["job_id"] for r in rows} == {"enablement-126cf7812984",
                                           "enablement-aaaabbbbcccc"}


def test_filter_by_daemon_id_is_substring_and_case_insensitive():
    # The value a troubleshooter reads off the worker is the container name
    # sb-enablement-126cf7812984; the id inside it is what pastes in here.
    assert [r["job_id"] for r in _get("?daemon=126CF781")["rows"]] == \
        ["enablement-126cf7812984"]


def test_filter_by_tenant_substring():
    assert [r["job_id"] for r in _get("?tenant=sro97894")["rows"]] == \
        ["enablement-126cf7812984"]


def test_filter_by_user_is_case_insensitive():
    assert [r["job_id"] for r in _get("?user=other@example.com")["rows"]] == \
        ["enablement-aaaabbbbcccc"]


def test_rows_carry_tenant_user_and_trigger_family():
    row = next(r for r in _get()["rows"] if r["job_id"] == "enablement-126cf7812984")
    assert row["tenant"] == "https://sro97894.apps.dynatrace.com"
    assert row["user"] == "learner@example.com"
    assert row["trigger"] == "enablement-app"
    assert row["trigger_family"] == "enablement-app"


def test_ci_rows_have_empty_tenant_and_user():
    row = next(r for r in _get()["rows"] if r["job_id"] == "ci-newest")
    assert row["tenant"] == "" and row["user"] == ""


def test_tenant_dropdown_lists_only_real_tenants():
    assert _get()["filters"]["tenants"] == [
        "https://geu80787.apps.dynatrace.com",
        "https://sro97894.apps.dynatrace.com",
    ]


def test_distinct_filters_survive_an_active_filter():
    # Dropdowns must not collapse to "only what is currently shown", or the user
    # can filter themselves into a corner with no way back.
    data = _get("?tenant=sro97894")
    assert len(data["rows"]) == 1
    assert set(data["filters"]["triggers"]) == {"enablement-app", "rerun", "webhook"}

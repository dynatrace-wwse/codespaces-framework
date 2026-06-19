"""Agentic History — agent jobs are archived to a dedicated list so they survive
the 500-cap jobs:completed rollover; api_builds_history merges them (dedupe by id)."""
import json
from dashboard.app import _merge_agent_history
from workers.manager import AGENT_JOB_TYPES


def test_merge_appends_only_new_agent_jobs():
    completed = [json.dumps({"job_id": "a", "type": "integration-test"}),
                 json.dumps({"job_id": "b", "type": "fix-issue"})]
    agent = [json.dumps({"job_id": "b", "type": "fix-issue"}),   # dup of completed → skip
             json.dumps({"job_id": "c", "type": "fix-ci"})]      # new → appended
    merged = _merge_agent_history(completed, agent)
    ids = [json.loads(r)["job_id"] for r in merged]
    assert ids == ["a", "b", "c"]


def test_merge_no_agent_jobs_is_noop():
    completed = [json.dumps({"job_id": "a"})]
    assert _merge_agent_history(completed, []) == completed


def test_merge_tolerates_bad_json():
    merged = _merge_agent_history(["{bad", json.dumps({"job_id": "a"})],
                                  ["alsobad", json.dumps({"job_id": "z"})])
    assert json.dumps({"job_id": "z"}) in merged  # the valid new one is appended


def test_agent_job_types_cover_dashboard_agent_set():
    for t in ("fix-ci", "fix-issue", "review-pr", "migrate-gen3", "scaffold-lab", "deploy-ghpages"):
        assert t in AGENT_JOB_TYPES

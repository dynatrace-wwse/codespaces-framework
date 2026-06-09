"""Tests for the content distribution service security guards + profile logic.

Runnable two ways:
  - pytest:   /home/ops/ops-venv/bin/python -m pytest dashboard/test_content_service.py
  - standalone: /home/ops/ops-venv/bin/python -m dashboard.test_content_service
"""

import asyncio
import json
import tempfile
from pathlib import Path

from fastapi import HTTPException

from dashboard import content_service as cs


def _setup(tmp: Path, keys=("k1",)):
    """Point the module at a temp profiles dir + known keys."""
    profiles = tmp / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (profiles / "all.json").write_text(json.dumps({
        "profileId": "all",
        "sources": [
            {"key": "lb", "category": "learning-byte", "categoryLabel": "Learning Bytes",
             "repo": "dynatrace-wwse/enablement-learning-bytes", "branch": "main"},
        ],
    }))
    cs.PROFILES_DIR = profiles
    cs.CONTENT_KEYS = set(keys)


def _expect_http(status, fn, *args, **kwargs):
    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn(*args, **kwargs))
        else:
            fn(*args, **kwargs)
    except HTTPException as e:
        assert e.status_code == status, f"expected {status}, got {e.status_code}"
        return
    raise AssertionError(f"expected HTTPException {status}, none raised")


def test_require_key():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        # missing / wrong key → 401
        _expect_http(401, cs._require_key, None)
        _expect_http(401, cs._require_key, "nope")
        # valid key → no raise
        cs._require_key("k1")
        # no keys configured → fail closed (503)
        cs.CONTENT_KEYS = set()
        _expect_http(503, cs._require_key, "k1")


def test_valid_id():
    assert cs._valid_id("all")
    assert cs._valid_id("se-onboarding")
    assert cs._valid_id("a_b-1")
    assert not cs._valid_id("")
    assert not cs._valid_id("../etc")
    assert not cs._valid_id("a/b")


def test_require_writer():
    _expect_http(401, cs._require_writer, None)
    _expect_http(401, cs._require_writer, "")
    cs._require_writer("alice")  # any non-empty X-Auth-User passes (nginx pre-validated)


def test_allowed_repos():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        repos = cs._allowed_repos()
        assert "dynatrace-wwse/enablement-learning-bytes" in repos


def test_proxy_rejects_non_allowlisted_repo():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        # valid key, but the repo is not in any profile → 403
        _expect_http(403, cs.proxy_raw, "dynatrace-wwse", "some-private-repo", "README.md",
                     ref="main", x_content_key="k1")


def test_proxy_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(400, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes",
                     "../secret", ref="main", x_content_key="k1")
        _expect_http(400, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes",
                     "docs/x.md", ref="../main", x_content_key="k1")


def test_proxy_requires_key_before_anything():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(401, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes",
                     "mkdocs.yaml", ref="main", x_content_key=None)


def test_load_profile_rejects_bad_id():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(400, cs._load_profile, "../etc")
        _expect_http(404, cs._load_profile, "does-not-exist")
        assert cs._load_profile("all")["profileId"] == "all"


def test_put_profile_validates_sources():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        # empty sources → 400
        _expect_http(400, cs.put_profile, "myprofile", {"sources": []}, x_auth_user="alice")
        # source without owner/repo → 400
        _expect_http(400, cs.put_profile, "myprofile", {"sources": [{"repo": "norepo"}]}, x_auth_user="alice")
        # valid → writes the file
        res = asyncio.run(cs.put_profile("myprofile",
            {"description": "x", "sources": [{"repo": "dynatrace-wwse/enablement-kubernetes-101", "category": "hands-on"}]},
            x_auth_user="alice"))
        assert res["ok"] and res["sources"] == 1
        assert (cs.PROFILES_DIR / "myprofile.json").is_file()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} content-service tests passed")

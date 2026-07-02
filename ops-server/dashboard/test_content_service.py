"""Tests for the content distribution service: Dynatrace-domain auth, tenant→profile
resolution, repo allowlist, path-traversal, writer gate, and table validation.

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


def _setup(tmp: Path, tenants=None, defaults=None):
    content = tmp / "content"
    profiles = content / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    for pid in ("all", "minimal", "core"):
        (profiles / f"{pid}.json").write_text(json.dumps({
            "profileId": pid,
            "sources": [{"key": "lb", "category": "learning-byte", "categoryLabel": "Learning Bytes",
                         "repo": "dynatrace-wwse/enablement-learning-bytes", "branch": "main"}],
        }))
    cs.PROFILES_DIR = profiles
    cs.TENANT_MAP_FILE = content / "tenant_map.json"
    cs.TENANT_MAP_FILE.write_text(json.dumps({
        "defaults": defaults or {"prod": "core", "sprint": "all", "dev": "all"},
        "tenants": tenants or {"geu80787": "all"},
    }))


def _expect_http(status, fn, *args, **kwargs):
    try:
        asyncio.run(fn(*args, **kwargs)) if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)
    except HTTPException as e:
        assert e.status_code == status, f"expected {status}, got {e.status_code}"
        return
    raise AssertionError(f"expected HTTPException {status}, none raised")


def test_classify_tenant_domains():
    assert cs.classify_tenant("https://geu80787.apps.dynatrace.com/") == ("geu80787", "prod")
    assert cs.classify_tenant("https://abc12345.sprint.apps.dynatracelabs.com") == ("abc12345", "sprint")
    assert cs.classify_tenant("https://xyz.dev.apps.dynatracelabs.com/ui") == ("xyz", "dev")


def test_classify_tenant_rejects_non_dynatrace():
    _expect_http(403, cs.classify_tenant, None)
    _expect_http(403, cs.classify_tenant, "https://evil.example.com")
    _expect_http(403, cs.classify_tenant, "https://geu80787.apps.dynatrace.com.evil.com")  # suffix spoof


def test_resolve_profile():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        # explicit tenant mapping wins
        assert cs.resolve_profile("geu80787", "prod") == "all"
        # unknown tenant → per-domain default: prod→core, sprint/dev→all
        assert cs.resolve_profile("other", "prod") == "core"
        assert cs.resolve_profile("other", "sprint") == "all"
        assert cs.resolve_profile("other", "dev") == "all"
        # unknown domain → 'all' fallback
        assert cs.resolve_profile("other", "weird") == "all"


def test_register_tenant():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d), tenants={})
        # a prod tenant registers with the prod default (core)
        r = asyncio.run(cs.register_tenant({"tenant": "https://cust1.apps.dynatrace.com"}, x_auth_user="alice"))
        assert r == {"ok": True, "tenant": "cust1", "domain": "prod", "profile": "core", "added": True}
        # idempotent: second register does not change an existing entry
        r2 = asyncio.run(cs.register_tenant({"tenant": "https://cust1.apps.dynatrace.com"}, x_auth_user="alice"))
        assert r2["added"] is False and r2["profile"] == "core"
        # a sprint tenant registers with 'all'
        rs = asyncio.run(cs.register_tenant({"tenant": "https://x.sprint.apps.dynatracelabs.com"}, x_auth_user="alice"))
        assert rs["domain"] == "sprint" and rs["profile"] == "all"
        # persisted
        assert json.loads(cs.TENANT_MAP_FILE.read_text())["tenants"]["cust1"] == "core"


def test_register_tenant_requires_writer_and_dt_domain():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(401, cs.register_tenant, {"tenant": "https://x.apps.dynatrace.com"}, x_auth_user=None)
        _expect_http(403, cs.register_tenant, {"tenant": "https://evil.example.com"}, x_auth_user="alice")


def test_manifest_resolves_by_tenant():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d), tenants={"cust1": "minimal"})
        res = asyncio.run(cs.get_manifest(tenant="https://cust1.apps.dynatrace.com"))
        assert res["tenant"] == "cust1"
        assert res["domain"] == "prod"
        assert res["profileId"] == "minimal"
        assert isinstance(res["sources"], list)


def test_manifest_rejects_non_dynatrace_domain():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(403, cs.get_manifest, tenant="https://evil.example.com")
        _expect_http(403, cs.get_manifest, tenant=None)


def test_proxy_requires_dynatrace_tenant():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(403, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes",
                     "mkdocs.yaml", ref="main", tenant=None)
        _expect_http(403, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes",
                     "mkdocs.yaml", ref="main", tenant="https://evil.example.com")


def test_proxy_rejects_non_allowlisted_repo():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(403, cs.proxy_raw, "dynatrace-wwse", "some-private-repo", "README.md",
                     ref="main", tenant="https://geu80787.apps.dynatrace.com")


def test_proxy_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        t = "https://geu80787.apps.dynatrace.com"
        _expect_http(400, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes", "../secret", ref="main", tenant=t)
        _expect_http(400, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes", "docs/x.md", ref="../m", tenant=t)


def test_valid_id_and_load_profile():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        assert cs._valid_id("all") and not cs._valid_id("../etc")
        _expect_http(400, cs._load_profile, "../etc")
        _expect_http(404, cs._load_profile, "nope")
        assert cs._load_profile("all")["profileId"] == "all"


def test_require_writer():
    _expect_http(401, cs._require_writer, None)
    cs._require_writer("alice")


def test_tenant_map_put_validates_profiles():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        # unknown profile → 400
        _expect_http(400, cs.put_tenant_map, {"defaults": {"prod": "ghost"}, "tenants": {}}, x_auth_user="alice")
        # valid → writes
        res = asyncio.run(cs.put_tenant_map(
            {"defaults": {"prod": "all", "sprint": "minimal"}, "tenants": {"cust9": "all"}},
            x_auth_user="alice"))
        assert res["ok"] and res["tenants"] == 1
        saved = json.loads(cs.TENANT_MAP_FILE.read_text())
        assert saved["tenants"]["cust9"] == "all"


def test_tenant_map_put_requires_writer():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        _expect_http(401, cs.put_tenant_map, {"defaults": {}, "tenants": {}}, x_auth_user=None)


# ── Managed training sources (Trainings tab) ────────────────────────────────────

class _GhResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload or {}
    def json(self): return self._p


def test_parse_repo():
    assert cs._parse_repo("https://github.com/dynatrace-wwse/enablement-kubernetes-101") == "dynatrace-wwse/enablement-kubernetes-101"
    assert cs._parse_repo("dynatrace-wwse/enablement-kubernetes-101.git") == "dynatrace-wwse/enablement-kubernetes-101"
    assert cs._parse_repo("https://github.com/owner/repo/tree/main") == "owner/repo"
    assert cs._parse_repo("not a repo") is None
    assert cs._parse_repo("") is None


def test_validate_repo_detects_training(monkeypatch=None):
    async def fake_gh(path):
        if path.endswith("/repos/o/r"): return _GhResp(200, {"default_branch": "main"})
        if "/git/trees/" in path: return _GhResp(200, {"tree": [{"path": ".devcontainer/devcontainer.json"}, {"path": "mkdocs.yml"}]})
        return _GhResp(404)
    orig = cs._gh_get; cs._gh_get = fake_gh
    try:
        r = asyncio.run(cs._validate_repo("o/r", "main"))
    finally:
        cs._gh_get = orig
    assert r["valid"] is True
    assert r["delivery"] == "hands-on"  # has .devcontainer


def test_validate_repo_rejects_non_training_and_missing(monkeypatch=None):
    async def gh_plain(path):
        if path.endswith("/repos/o/r"): return _GhResp(200, {"default_branch": "main"})
        if "/git/trees/" in path: return _GhResp(200, {"tree": [{"path": "README.md"}]})
        return _GhResp(404)
    orig = cs._gh_get
    cs._gh_get = gh_plain
    try:
        r = asyncio.run(cs._validate_repo("o/r"))
        assert r["valid"] is False and "no mkdocs" in r["reason"].lower()
        cs._gh_get = lambda path: _coro(_GhResp(404))
        r2 = asyncio.run(cs._validate_repo("o/missing"))
        assert r2["valid"] is False and "not found" in r2["reason"].lower()
    finally:
        cs._gh_get = orig


async def _coro(v): return v


def test_add_and_remove_source(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        cs.SOURCES_FILE = Path(d) / "content" / "sources.json"
        async def fake_gh(path):
            if path.endswith("/repos/dynatrace-wwse/enablement-dql-301"): return _GhResp(200, {"default_branch": "main"})
            if "/git/trees/" in path: return _GhResp(200, {"tree": [{"path": "mkdocs.yml"}]})
            return _GhResp(404)
        orig = cs._gh_get; cs._gh_get = fake_gh
        try:
            res = asyncio.run(cs.add_source({"repo": "https://github.com/dynatrace-wwse/enablement-dql-301", "category": "hands-on"}, x_auth_user="alice"))
            assert res["ok"] and res["source"]["repo"] == "dynatrace-wwse/enablement-dql-301"
            assert res["source"]["delivery"] == "self-paced"  # mkdocs only
            listed = asyncio.run(cs.list_sources(x_auth_user="alice"))
            assert len(listed["sources"]) == 1
            # the new repo is now in the proxy allowlist
            assert "dynatrace-wwse/enablement-dql-301" in cs._allowed_repos()
            # duplicate add → 409
            _expect_http(409, cs.add_source, {"repo": "dynatrace-wwse/enablement-dql-301"}, x_auth_user="alice")
            # remove
            rm = asyncio.run(cs.remove_source("dynatrace-wwse", "enablement-dql-301", x_auth_user="alice"))
            assert rm["ok"]
            assert asyncio.run(cs.list_sources(x_auth_user="alice"))["sources"] == []
            _expect_http(404, cs.remove_source, "dynatrace-wwse", "nope", x_auth_user="alice")
        finally:
            cs._gh_get = orig


def test_add_source_rejects_invalid_repo(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d)); cs.SOURCES_FILE = Path(d) / "content" / "sources.json"
        _expect_http(400, cs.add_source, {"repo": "garbage"}, x_auth_user="alice")
    _expect_http(401, cs.list_sources, None)


def test_parse_repo_ref_branch_extraction():
    assert cs._parse_repo_ref("https://github.com/o/r/tree/feat/my-lab") == ("o/r", "feat/my-lab")
    assert cs._parse_repo_ref("https://github.com/o/r/tree/main") == ("o/r", "main")
    assert cs._parse_repo_ref("https://github.com/o/r") == ("o/r", None)
    assert cs._parse_repo_ref("o/r") == ("o/r", None)


def test_proxy_accepts_slash_branch_ref():
    # feat/x is a legal branch name — must NOT 400; traversal/absolute still rejected.
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d))
        t = "https://geu80787.apps.dynatrace.com"
        _expect_http(400, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes", "docs/x.md", ref="/abs", tenant=t)
        _expect_http(400, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes", "docs/x.md", ref="", tenant=t)
        _expect_http(400, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes", "docs/x.md", ref="feat/../../x", tenant=t)
        # slash branch passes validation and reaches the upstream fetch (mock it to 404)
        class _R:
            status_code = 404
            content = b""
            headers = {}
        class _C:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **k): return _R()
        orig = cs.httpx.AsyncClient
        cs.httpx.AsyncClient = _C
        try:
            _expect_http(404, cs.proxy_raw, "dynatrace-wwse", "enablement-learning-bytes", "docs/x.md",
                         ref="feat/my-lab", tenant=t)
        finally:
            cs.httpx.AsyncClient = orig


def test_add_source_switches_branch_and_manifest_prefers_catalog():
    with tempfile.TemporaryDirectory() as d:
        _setup(Path(d)); cs.SOURCES_FILE = Path(d) / "content" / "sources.json"
        async def fake_gh(path):
            if path.endswith("/repos/dynatrace-wwse/enablement-learning-bytes"):
                return _GhResp(200, {"default_branch": "main"})
            if "/git/trees/" in path:
                return _GhResp(200, {"tree": [{"path": "mkdocs.yml"}]})
            return _GhResp(404)
        orig_gh = cs._gh_get; cs._gh_get = fake_gh
        async def fake_sha(owner, repo, branch):
            return f"sha-{branch}"
        orig_sha = cs._latest_sha; cs._latest_sha = fake_sha
        try:
            r1 = asyncio.run(cs.add_source(
                {"repo": "dynatrace-wwse/enablement-learning-bytes", "category": "learning-byte"},
                x_auth_user="alice"))
            assert r1["ok"] and r1["source"]["branch"] == "main"
            # re-add with a branch → switches, no 409
            r2 = asyncio.run(cs.add_source(
                {"repo": "https://github.com/dynatrace-wwse/enablement-learning-bytes/tree/feat/test-lab",
                 "category": "learning-byte"},
                x_auth_user="alice"))
            assert r2.get("branchSwitched") and r2["source"]["branch"] == "feat/test-lab"
            # manifest for a profile that references this repo @ main now delivers the catalog branch
            built = asyncio.run(cs._build_sources(cs._load_profile("all")))
            src = next(s for s in built if s["repo"] == "dynatrace-wwse/enablement-learning-bytes")
            assert src["branch"] == "feat/test-lab"
            assert src["version"] == "sha-feat/test-lab"
        finally:
            cs._gh_get = orig_gh
            cs._latest_sha = orig_sha


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} content-service tests passed")

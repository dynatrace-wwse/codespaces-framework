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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} content-service tests passed")

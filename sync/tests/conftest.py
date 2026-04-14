"""Shared fixtures for sync CLI tests."""

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from sync.core.repos import RepoEntry


# ---------------------------------------------------------------------------
# Sample repos.yaml data
# ---------------------------------------------------------------------------

SAMPLE_REPOS_YAML = {
    "repos": [
        {
            "name": "enablement-kubernetes-otel",
            "repo": "dynatrace-wwse/enablement-kubernetes-otel",
            "status": "active",
            "maintainer": "@alice",
            "description": "K8s + OTel enablement lab",
            "tags": ["kubernetes", "opentelemetry"],
            "title": "Kubernetes & OTel",
            "primary_tag": "kubernetes",
            "duration": "3h",
        },
        {
            "name": "enablement-genai",
            "repo": "dynatrace-wwse/enablement-genai",
            "status": "active",
            "maintainer": "@bob",
            "description": "GenAI observability",
            "tags": ["gen-ai"],
            "title": "GenAI",
            "primary_tag": "gen-ai",
            "duration": "2h",
            "image_tier": "ai",
        },
        {
            "name": "enablement-archived-lab",
            "repo": "dynatrace-wwse/enablement-archived-lab",
            "status": "archived",
            "maintainer": "@carol",
            "description": "Old lab, now archived",
        },
        {
            "name": "workshop-destination-auto",
            "repo": "dynatrace-wwse/workshop-destination-auto",
            "status": "active",
            "maintainer": "@dave",
            "description": "Workshop on workflows",
            "sync_managed": False,
            "tags": ["workflows"],
        },
        {
            "name": "codespaces-framework",
            "repo": "dynatrace-wwse/codespaces-framework",
            "status": "active",
            "maintainer": "@eve",
            "description": "The framework itself",
            "sync_managed": False,
            "ci": False,
            "listed": False,
        },
        {
            "name": "experimental-lab",
            "repo": "dynatrace-wwse/experimental-lab",
            "status": "experimental",
            "maintainer": "@frank",
            "description": "Experimental lab",
        },
    ]
}


@pytest.fixture
def sample_repos_yaml_path(tmp_path):
    """Write sample repos.yaml to a temp file and return its path."""
    path = tmp_path / "repos.yaml"
    path.write_text(yaml.dump(SAMPLE_REPOS_YAML, default_flow_style=False))
    return path


@pytest.fixture
def sample_repos(sample_repos_yaml_path):
    """Return list of RepoEntry from sample data."""
    from sync.core.repos import load_repos
    return load_repos(sample_repos_yaml_path)


@pytest.fixture
def active_sync_repos(sample_repos):
    """Return only active + sync_managed repos (the sync targets)."""
    from sync.core.repos import filter_sync_targets
    return filter_sync_targets(sample_repos)


@pytest.fixture
def make_repo_entry():
    """Factory fixture to create a RepoEntry with defaults."""
    def _make(**kwargs):
        defaults = {
            "name": "test-repo",
            "repo": "dynatrace-wwse/test-repo",
            "status": "active",
            "maintainer": "@tester",
            "description": "A test repo",
        }
        defaults.update(kwargs)
        return RepoEntry(**defaults)
    return _make


@pytest.fixture
def source_framework_content():
    """Return a valid source_framework.sh content with version 1.2.5."""
    return 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'


@pytest.fixture
def make_args():
    """Factory to create argparse-like namespace objects."""
    def _make(**kwargs):
        return SimpleNamespace(**kwargs)
    return _make


# ---------------------------------------------------------------------------
# Mock git repo structure helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_repo_dir(tmp_path):
    """Create a minimal mock repo directory structure."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    dc = repo / ".devcontainer"
    dc.mkdir()
    util = dc / "util"
    util.mkdir()
    (util / "source_framework.sh").write_text(
        'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'
    )
    (dc / "Makefile").write_text("# thin Makefile")
    (dc / "devcontainer.json").write_text("{}")
    (dc / "post-create.sh").write_text("#!/bin/bash")
    (dc / "post-start.sh").write_text("#!/bin/bash")
    return repo

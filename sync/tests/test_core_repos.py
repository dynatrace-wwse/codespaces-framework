"""Tests for sync.core.repos — RepoEntry model, loading, validation, filtering."""

import yaml
import pytest
from pathlib import Path

from sync.core.repos import (
    RepoEntry,
    load_repos,
    validate_repos,
    filter_sync_targets,
    VALID_STATUSES,
    VALID_IMAGE_TIERS,
    REQUIRED_FIELDS,
)


# ---------------------------------------------------------------------------
# RepoEntry dataclass
# ---------------------------------------------------------------------------

class TestRepoEntry:
    def test_basic_construction(self):
        r = RepoEntry(
            name="my-lab",
            repo="dynatrace-wwse/my-lab",
            status="active",
            maintainer="@alice",
            description="A lab",
        )
        assert r.name == "my-lab"
        assert r.repo == "dynatrace-wwse/my-lab"
        assert r.status == "active"
        assert r.maintainer == "@alice"
        assert r.description == "A lab"

    def test_defaults(self):
        r = RepoEntry(
            name="x",
            repo="org/x",
            status="active",
            maintainer="@m",
            description="d",
        )
        assert r.sync_managed is True
        assert r.ci is True
        assert r.prebuilds is False
        assert r.image_tier == "k8s"
        assert r.tags == []
        assert r.architectures == ["amd64", "arm64"]
        assert r.title == ""
        assert r.primary_tag == ""
        assert r.icon_key == ""
        assert r.duration == ""
        assert r.is_template is False
        assert r.listed is True

    def test_sync_managed_false_override(self):
        r = RepoEntry(
            name="x",
            repo="org/x",
            status="active",
            maintainer="@m",
            description="d",
            sync_managed=False,
        )
        assert r.sync_managed is False

    def test_owner_property(self):
        r = RepoEntry(
            name="x",
            repo="dynatrace-wwse/my-repo",
            status="active",
            maintainer="@m",
            description="d",
        )
        assert r.owner == "dynatrace-wwse"

    def test_repo_name_property(self):
        r = RepoEntry(
            name="x",
            repo="dynatrace-wwse/my-repo",
            status="active",
            maintainer="@m",
            description="d",
        )
        assert r.repo_name == "my-repo"

    def test_url_property(self):
        r = RepoEntry(
            name="x",
            repo="dynatrace-wwse/my-repo",
            status="active",
            maintainer="@m",
            description="d",
        )
        assert r.url == "github.com/dynatrace-wwse/my-repo"

    def test_tags_default_is_independent(self):
        """Ensure default tags list is not shared between instances."""
        r1 = RepoEntry(name="a", repo="o/a", status="active", maintainer="@m", description="d")
        r2 = RepoEntry(name="b", repo="o/b", status="active", maintainer="@m", description="d")
        r1.tags.append("x")
        assert r2.tags == []

    def test_architectures_default_is_independent(self):
        r1 = RepoEntry(name="a", repo="o/a", status="active", maintainer="@m", description="d")
        r2 = RepoEntry(name="b", repo="o/b", status="active", maintainer="@m", description="d")
        r1.architectures.append("s390x")
        assert "s390x" not in r2.architectures

    def test_registry_fields(self):
        r = RepoEntry(
            name="x",
            repo="org/x",
            status="active",
            maintainer="@m",
            description="d",
            title="My Title",
            primary_tag="kubernetes",
            icon_key="k8s",
            duration="3h",
            is_template=True,
            listed=False,
        )
        assert r.title == "My Title"
        assert r.primary_tag == "kubernetes"
        assert r.icon_key == "k8s"
        assert r.duration == "3h"
        assert r.is_template is True
        assert r.listed is False


# ---------------------------------------------------------------------------
# load_repos
# ---------------------------------------------------------------------------

class TestLoadRepos:
    def test_loads_from_yaml(self, sample_repos_yaml_path):
        repos = load_repos(sample_repos_yaml_path)
        assert len(repos) == 6
        assert all(isinstance(r, RepoEntry) for r in repos)

    def test_names_match(self, sample_repos_yaml_path):
        repos = load_repos(sample_repos_yaml_path)
        names = [r.name for r in repos]
        assert "enablement-kubernetes-otel" in names
        assert "enablement-genai" in names
        assert "enablement-archived-lab" in names
        assert "workshop-destination-auto" in names

    def test_sync_managed_respected(self, sample_repos_yaml_path):
        repos = load_repos(sample_repos_yaml_path)
        workshop = next(r for r in repos if r.name == "workshop-destination-auto")
        assert workshop.sync_managed is False

    def test_image_tier_override(self, sample_repos_yaml_path):
        repos = load_repos(sample_repos_yaml_path)
        genai = next(r for r in repos if r.name == "enablement-genai")
        assert genai.image_tier == "ai"

    def test_default_image_tier(self, sample_repos_yaml_path):
        repos = load_repos(sample_repos_yaml_path)
        k8s = next(r for r in repos if r.name == "enablement-kubernetes-otel")
        assert k8s.image_tier == "k8s"

    def test_empty_repos_yaml(self, tmp_path):
        path = tmp_path / "repos.yaml"
        path.write_text(yaml.dump({"repos": []}, default_flow_style=False))
        repos = load_repos(path)
        assert repos == []

    def test_no_repos_key(self, tmp_path):
        path = tmp_path / "repos.yaml"
        path.write_text(yaml.dump({}, default_flow_style=False))
        repos = load_repos(path)
        assert repos == []

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_repos(tmp_path / "nonexistent.yaml")

    def test_extra_fields_raise(self, tmp_path):
        """Extra unknown fields should raise TypeError."""
        data = {
            "repos": [
                {
                    "name": "x",
                    "repo": "org/x",
                    "status": "active",
                    "maintainer": "@m",
                    "description": "d",
                    "unknown_field": True,
                }
            ]
        }
        path = tmp_path / "repos.yaml"
        path.write_text(yaml.dump(data, default_flow_style=False))
        with pytest.raises(TypeError):
            load_repos(path)


# ---------------------------------------------------------------------------
# validate_repos
# ---------------------------------------------------------------------------

class TestValidateRepos:
    def test_valid_repos(self, sample_repos):
        errors = validate_repos(sample_repos)
        assert errors == []

    def test_duplicate_name(self, make_repo_entry):
        r1 = make_repo_entry(name="dupe", repo="org/dupe1")
        r2 = make_repo_entry(name="dupe", repo="org/dupe2")
        errors = validate_repos([r1, r2])
        assert any("duplicate name" in e for e in errors)

    def test_invalid_status(self, make_repo_entry):
        r = make_repo_entry(status="defunct")
        errors = validate_repos([r])
        assert any("status" in e and "invalid" in e for e in errors)

    def test_invalid_image_tier(self, make_repo_entry):
        r = make_repo_entry(image_tier="gpu")
        errors = validate_repos([r])
        assert any("image_tier" in e and "invalid" in e for e in errors)

    def test_repo_missing_slash(self, make_repo_entry):
        r = make_repo_entry(repo="noslash")
        errors = validate_repos([r])
        assert any("owner/name" in e for e in errors)

    def test_maintainer_missing_at(self, make_repo_entry):
        r = make_repo_entry(maintainer="alice")
        errors = validate_repos([r])
        assert any("should start with @" in e for e in errors)

    def test_multiple_errors(self, make_repo_entry):
        r = make_repo_entry(status="bogus", image_tier="gpu", repo="noslash", maintainer="x")
        errors = validate_repos([r])
        assert len(errors) == 4  # status, image_tier, repo format, maintainer

    def test_all_valid_statuses(self, make_repo_entry):
        for status in VALID_STATUSES:
            r = make_repo_entry(status=status)
            errors = validate_repos([r])
            assert not any("status" in e for e in errors)

    def test_all_valid_image_tiers(self, make_repo_entry):
        for tier in VALID_IMAGE_TIERS:
            r = make_repo_entry(image_tier=tier)
            errors = validate_repos([r])
            assert not any("image_tier" in e for e in errors)


# ---------------------------------------------------------------------------
# filter_sync_targets
# ---------------------------------------------------------------------------

class TestFilterSyncTargets:
    def test_filters_active_and_sync_managed(self, sample_repos):
        targets = filter_sync_targets(sample_repos)
        for r in targets:
            assert r.status == "active"
            assert r.sync_managed is True

    def test_excludes_archived(self, sample_repos):
        targets = filter_sync_targets(sample_repos)
        names = [r.name for r in targets]
        assert "enablement-archived-lab" not in names

    def test_excludes_sync_managed_false(self, sample_repos):
        targets = filter_sync_targets(sample_repos)
        names = [r.name for r in targets]
        assert "workshop-destination-auto" not in names
        assert "codespaces-framework" not in names

    def test_excludes_experimental(self, sample_repos):
        targets = filter_sync_targets(sample_repos)
        names = [r.name for r in targets]
        assert "experimental-lab" not in names

    def test_includes_active_sync_managed(self, sample_repos):
        targets = filter_sync_targets(sample_repos)
        names = [r.name for r in targets]
        assert "enablement-kubernetes-otel" in names
        assert "enablement-genai" in names

    def test_count(self, sample_repos):
        """Only the 2 active+sync_managed repos from sample data."""
        targets = filter_sync_targets(sample_repos)
        assert len(targets) == 2

    def test_empty_list(self):
        assert filter_sync_targets([]) == []

    def test_all_archived(self, make_repo_entry):
        repos = [make_repo_entry(name="a", status="archived")]
        assert filter_sync_targets(repos) == []

    def test_all_unmanaged(self, make_repo_entry):
        repos = [make_repo_entry(name="a", sync_managed=False)]
        assert filter_sync_targets(repos) == []

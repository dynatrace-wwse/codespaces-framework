"""Tests for sync.commands.migrate — Category A/B file classification, templates, devcontainer validation."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sync.commands.migrate import (
    CATEGORY_A_FILES,
    CATEGORY_A_DIRS,
    CATEGORY_B_FILES,
    REPO_CUSTOM_FILES,
    SOURCE_FRAMEWORK_TEMPLATE,
    THIN_MAKEFILE,
    DEVCONTAINER_CHECKS,
    DEVCONTAINER_REQUIRED_RUNARGS,
    DEVCONTAINER_REQUIRED_MOUNTS,
    _get_category_a,
    _strip_jsonc_comments,
    _parse_devcontainer,
    _validate_devcontainer,
    _resolve_repo_path,
)


# ---------------------------------------------------------------------------
# Category A file classification
# ---------------------------------------------------------------------------

class TestCategoryA:
    def test_includes_core_files(self):
        assert ".devcontainer/util/functions.sh" in CATEGORY_A_FILES
        assert ".devcontainer/util/variables.sh" in CATEGORY_A_FILES
        assert ".devcontainer/Dockerfile" in CATEGORY_A_FILES
        assert ".devcontainer/entrypoint.sh" in CATEGORY_A_FILES

    def test_includes_dirs(self):
        assert ".devcontainer/apps" in CATEGORY_A_DIRS
        assert ".devcontainer/p10k" in CATEGORY_A_DIRS
        assert ".devcontainer/yaml" in CATEGORY_A_DIRS

    def test_get_category_a_returns_all_tiers_same(self):
        """Currently all tiers share the same Category A set."""
        for tier in ("minimal", "k8s", "ai"):
            files, dirs = _get_category_a(tier)
            assert files == list(CATEGORY_A_FILES)
            assert dirs == list(CATEGORY_A_DIRS)

    def test_category_b_is_makefile(self):
        assert ".devcontainer/Makefile" in CATEGORY_B_FILES

    def test_repo_custom_files(self):
        assert ".devcontainer/devcontainer.json" in REPO_CUSTOM_FILES
        assert ".devcontainer/post-create.sh" in REPO_CUSTOM_FILES
        assert ".devcontainer/util/source_framework.sh" in REPO_CUSTOM_FILES
        assert ".devcontainer/util/my_functions.sh" in REPO_CUSTOM_FILES
        assert ".devcontainer/test/integration.sh" in REPO_CUSTOM_FILES

    def test_no_overlap_a_and_custom(self):
        """Category A files should never overlap with custom files."""
        for f in CATEGORY_A_FILES:
            assert f not in REPO_CUSTOM_FILES, f"overlap: {f}"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

class TestTemplates:
    def test_source_framework_template_has_placeholder(self):
        assert "%s" in SOURCE_FRAMEWORK_TEMPLATE

    def test_source_framework_template_renders(self):
        rendered = SOURCE_FRAMEWORK_TEMPLATE % "1.2.7"
        assert "1.2.7" in rendered
        assert 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.7}"' in rendered

    def test_thin_makefile_has_targets(self):
        assert "start:" in THIN_MAKEFILE
        assert "build:" in THIN_MAKEFILE
        assert "clean-cache:" in THIN_MAKEFILE
        assert "integration:" in THIN_MAKEFILE

    def test_thin_makefile_reads_version(self):
        """Makefile extracts version from source_framework.sh."""
        assert "source_framework.sh" in THIN_MAKEFILE


# ---------------------------------------------------------------------------
# _strip_jsonc_comments
# ---------------------------------------------------------------------------

class TestStripJsoncComments:
    def test_line_comments(self):
        text = '{"key": "value"} // comment'
        result = _strip_jsonc_comments(text)
        assert "//" not in result
        assert '"key"' in result

    def test_block_comments(self):
        text = '{"key": /* block */ "value"}'
        result = _strip_jsonc_comments(text)
        assert "/* block */" not in result

    def test_no_comments(self):
        text = '{"key": "value"}'
        assert _strip_jsonc_comments(text) == text

    def test_comment_inside_string_preserved(self):
        text = '{"url": "http://example.com"}'
        result = _strip_jsonc_comments(text)
        assert "http://example.com" in result

    def test_multiline_block_comment(self):
        text = '{\n/* line1\nline2 */\n"key": "value"\n}'
        result = _strip_jsonc_comments(text)
        assert "line1" not in result
        assert '"key"' in result


# ---------------------------------------------------------------------------
# _parse_devcontainer
# ---------------------------------------------------------------------------

class TestParseDevcontainer:
    def test_valid_json(self, tmp_path):
        dc = tmp_path / "devcontainer.json"
        dc.write_text('{"image": "test"}')
        result = _parse_devcontainer(dc)
        assert result == {"image": "test"}

    def test_jsonc_with_comments(self, tmp_path):
        dc = tmp_path / "devcontainer.json"
        dc.write_text('{\n// comment\n"image": "test"\n}')
        result = _parse_devcontainer(dc)
        assert result["image"] == "test"

    def test_trailing_commas(self, tmp_path):
        dc = tmp_path / "devcontainer.json"
        dc.write_text('{"a": 1, "b": 2,}')
        result = _parse_devcontainer(dc)
        assert result["a"] == 1

    def test_invalid_json(self, tmp_path):
        dc = tmp_path / "devcontainer.json"
        dc.write_text("not json at all {{{")
        result = _parse_devcontainer(dc)
        assert result is None

    def test_missing_file(self, tmp_path):
        dc = tmp_path / "nonexistent.json"
        result = _parse_devcontainer(dc)
        assert result is None


# ---------------------------------------------------------------------------
# _validate_devcontainer
# ---------------------------------------------------------------------------

class TestValidateDevcontainer:
    def _write_valid_dc(self, repo_path):
        """Write a fully valid devcontainer.json."""
        dc_dir = repo_path / ".devcontainer"
        dc_dir.mkdir(parents=True, exist_ok=True)
        dc = {
            "image": "shinojosa/dt-enablement:v1.2",
            "overrideCommand": False,
            "remoteUser": "vscode",
            "postCreateCommand": "./.devcontainer/post-create.sh",
            "postStartCommand": "./.devcontainer/post-start.sh",
            "runArgs": ["--init", "--privileged", "--network=host"],
            "mounts": ["source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"],
            "features": {},
            "customizations": {"vscode": {"extensions": []}},
        }
        (dc_dir / "devcontainer.json").write_text(json.dumps(dc))

    def test_valid(self, tmp_path):
        self._write_valid_dc(tmp_path)
        issues = _validate_devcontainer(tmp_path)
        assert issues == []

    def test_missing_file(self, tmp_path):
        issues = _validate_devcontainer(tmp_path)
        assert any("MISSING" in i for i in issues)

    def test_wrong_image(self, tmp_path):
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        dc = {"image": "wrong-image:latest", "overrideCommand": False,
              "remoteUser": "vscode",
              "postCreateCommand": "./.devcontainer/post-create.sh",
              "postStartCommand": "./.devcontainer/post-start.sh",
              "runArgs": ["--init", "--privileged", "--network=host"],
              "mounts": ["source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"],
              "features": {}, "customizations": {"vscode": {"extensions": []}}}
        (dc_dir / "devcontainer.json").write_text(json.dumps(dc))
        issues = _validate_devcontainer(tmp_path)
        assert any("image" in i for i in issues)

    def test_missing_runargs(self, tmp_path):
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        dc = {"image": "shinojosa/dt-enablement:v1.2", "overrideCommand": False,
              "remoteUser": "vscode",
              "postCreateCommand": "./.devcontainer/post-create.sh",
              "postStartCommand": "./.devcontainer/post-start.sh",
              "runArgs": [],  # missing required args
              "mounts": ["source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"],
              "features": {}, "customizations": {"vscode": {"extensions": []}}}
        (dc_dir / "devcontainer.json").write_text(json.dumps(dc))
        issues = _validate_devcontainer(tmp_path)
        assert len(issues) >= 3  # --init, --privileged, --network=host

    def test_non_empty_features(self, tmp_path):
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        dc = {"image": "shinojosa/dt-enablement:v1.2", "overrideCommand": False,
              "remoteUser": "vscode",
              "postCreateCommand": "./.devcontainer/post-create.sh",
              "postStartCommand": "./.devcontainer/post-start.sh",
              "runArgs": ["--init", "--privileged", "--network=host"],
              "mounts": ["source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"],
              "features": {"ghcr.io/some/feature:1": {}},
              "customizations": {"vscode": {"extensions": []}}}
        (dc_dir / "devcontainer.json").write_text(json.dumps(dc))
        issues = _validate_devcontainer(tmp_path)
        assert any("features" in i for i in issues)

    def test_non_empty_extensions(self, tmp_path):
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        dc = {"image": "shinojosa/dt-enablement:v1.2", "overrideCommand": False,
              "remoteUser": "vscode",
              "postCreateCommand": "./.devcontainer/post-create.sh",
              "postStartCommand": "./.devcontainer/post-start.sh",
              "runArgs": ["--init", "--privileged", "--network=host"],
              "mounts": ["source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"],
              "features": {},
              "customizations": {"vscode": {"extensions": ["ms-python.python"]}}}
        (dc_dir / "devcontainer.json").write_text(json.dumps(dc))
        issues = _validate_devcontainer(tmp_path)
        assert any("extensions" in i for i in issues)


# ---------------------------------------------------------------------------
# _resolve_repo_path
# ---------------------------------------------------------------------------

class TestResolveRepoPath:
    def test_returns_sibling_path(self):
        path = _resolve_repo_path("my-repo")
        # Should be a sibling of codespaces-framework
        assert path.name == "my-repo"
        assert "enablement-framework" in str(path) or path.parent.exists()

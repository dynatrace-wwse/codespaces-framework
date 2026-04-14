"""Tests for miscellaneous commands: bump-repo-version, cleanup-branches, revert, protect-main, list-issues, migrate-mkdocs, generate-registry."""

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sync.core.github_api import GHAPIError


# ---------------------------------------------------------------------------
# bump-repo-version
# ---------------------------------------------------------------------------

class TestBumpRepoVersion:
    @patch("sync.commands.bump_repo_version.create_tag")
    @patch("sync.commands.bump_repo_version.get_branch_sha", return_value="abc123")
    @patch("sync.commands.bump_repo_version.get_default_branch", return_value="main")
    @patch("sync.commands.bump_repo_version.get_latest_tags")
    def test_bump_patch(self, mock_tags, mock_branch, mock_sha, mock_create, capsys):
        from sync.commands.bump_repo_version import run
        mock_tags.return_value = ["v1.2.7_1.0.0"]

        args = SimpleNamespace(part="patch", repo="org/my-repo")
        run(args)
        out = capsys.readouterr().out
        assert "v1.2.7_1.0.1" in out
        mock_create.assert_called_once()

    @patch("sync.commands.bump_repo_version.get_latest_tags")
    def test_no_combined_tags(self, mock_tags, capsys):
        from sync.commands.bump_repo_version import run
        mock_tags.return_value = ["1.0.0"]  # no combined tag

        args = SimpleNamespace(part="patch", repo="org/my-repo")
        with pytest.raises(SystemExit):
            run(args)

    def test_invalid_repo_format(self, capsys):
        from sync.commands.bump_repo_version import run
        args = SimpleNamespace(part="patch", repo="noslash")
        with pytest.raises(SystemExit):
            run(args)

    @patch("sync.commands.bump_repo_version.get_latest_tags")
    def test_tag_already_exists(self, mock_tags, capsys):
        from sync.commands.bump_repo_version import run
        mock_tags.return_value = ["v1.2.7_1.0.0", "v1.2.7_1.0.1"]

        args = SimpleNamespace(part="patch", repo="org/my-repo")
        with pytest.raises(SystemExit):
            run(args)


# ---------------------------------------------------------------------------
# cleanup-branches
# ---------------------------------------------------------------------------

class TestCleanupBranches:
    @patch("sync.commands.cleanup_branches.subprocess.run")
    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_dry_run(self, mock_load, mock_resolve, mock_subp, make_repo_entry, capsys, tmp_path):
        from sync.commands.cleanup_branches import run, _get_merged_local, _get_merged_remote
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir
        mock_subp.return_value = MagicMock(
            returncode=0,
            stdout="  sync/framework-1.2.5\n* main\n",
        )

        with patch("sync.commands.cleanup_branches._get_merged_local", return_value=["sync/framework-1.2.5"]):
            with patch("sync.commands.cleanup_branches._get_merged_remote", return_value=[]):
                args = SimpleNamespace(repo=None, dry_run=True)
                run(args)
                out = capsys.readouterr().out
                assert "DRY RUN" in out

    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_missing_clone(self, mock_load, mock_resolve, make_repo_entry, capsys, tmp_path):
        from sync.commands.cleanup_branches import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_resolve.return_value = tmp_path / "nonexistent"

        args = SimpleNamespace(repo=None, dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "not found" in out


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------

class TestRevert:
    @patch("sync.commands.revert.subprocess.run")
    @patch("sync.commands.revert._resolve_repo_path")
    @patch("sync.commands.revert.filter_sync_targets")
    @patch("sync.commands.revert.load_repos")
    def test_clean_repo(self, mock_load, mock_filter, mock_resolve, mock_subp,
                         make_repo_entry, capsys, tmp_path):
        from sync.commands.revert import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir
        mock_subp.return_value = MagicMock(returncode=0, stdout="")

        args = SimpleNamespace(repo=None)
        run(args)
        out = capsys.readouterr().out
        assert "clean" in out

    @patch("sync.commands.revert._resolve_repo_path")
    @patch("sync.commands.revert.filter_sync_targets")
    @patch("sync.commands.revert.load_repos")
    def test_missing_clone(self, mock_load, mock_filter, mock_resolve,
                            make_repo_entry, capsys, tmp_path):
        from sync.commands.revert import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_resolve.return_value = tmp_path / "nonexistent"

        args = SimpleNamespace(repo=None)
        run(args)
        out = capsys.readouterr().out
        assert "not found" in out


# ---------------------------------------------------------------------------
# protect-main
# ---------------------------------------------------------------------------

class TestProtectMain:
    @patch("sync.commands.protect_main.subprocess.run")
    @patch("sync.commands.protect_main.load_repos")
    def test_dry_run(self, mock_load, mock_subp, make_repo_entry, capsys):
        from sync.commands.protect_main import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_subp.return_value = MagicMock(returncode=1, stdout="")

        args = SimpleNamespace(repo=None, dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "would apply" in out


# ---------------------------------------------------------------------------
# list-issues
# ---------------------------------------------------------------------------

class TestListIssues:
    @patch("sync.commands.list_issues._get_issues")
    @patch("sync.commands.list_issues.load_repos")
    def test_no_issues(self, mock_load, mock_issues, make_repo_entry, capsys):
        from sync.commands.list_issues import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_issues.return_value = []

        args = SimpleNamespace(repo=None, label=None)
        run(args)
        out = capsys.readouterr().out
        assert "No open issues" in out

    @patch("sync.commands.list_issues._get_issues")
    @patch("sync.commands.list_issues.load_repos")
    def test_with_issues(self, mock_load, mock_issues, make_repo_entry, capsys):
        from sync.commands.list_issues import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_issues.return_value = [
            {"number": 1, "title": "Bug report", "url": "http://issue/1",
             "labels": [{"name": "bug"}], "author": {"login": "user1"},
             "createdAt": "2024-01-15T00:00:00Z"},
        ]

        args = SimpleNamespace(repo=None, label=None)
        run(args)
        out = capsys.readouterr().out
        assert "#1" in out
        assert "Bug report" in out
        assert "1 open issues" in out


# ---------------------------------------------------------------------------
# migrate-mkdocs
# ---------------------------------------------------------------------------

class TestMigrateMkdocs:
    @patch("sync.commands.migrate_mkdocs.get_file_content")
    def test_already_migrated(self, mock_content, capsys):
        from sync.commands.migrate_mkdocs import run
        mock_content.return_value = "INHERIT: mkdocs-base.yaml\nsite_name: test"

        args = SimpleNamespace(repo="org/my-repo", dry_run=False)
        run(args)
        out = capsys.readouterr().out
        assert "already using INHERIT" in out

    @patch("sync.commands.migrate_mkdocs.get_file_content")
    def test_dry_run(self, mock_content, capsys):
        from sync.commands.migrate_mkdocs import run
        mock_content.return_value = "site_name: My Lab\nrepo_name: my-lab\nnav:\n  - Home: index.md\n"

        args = SimpleNamespace(repo="org/my-repo", dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "Would migrate" in out
        assert "INHERIT: mkdocs-base.yaml" in out

    @patch("sync.commands.migrate_mkdocs.get_file_content")
    def test_api_error(self, mock_content, capsys):
        from sync.commands.migrate_mkdocs import run
        mock_content.side_effect = GHAPIError("contents", 404, "Not Found")

        args = SimpleNamespace(repo="org/my-repo", dry_run=False)
        with pytest.raises(SystemExit):
            run(args)

    def test_invalid_repo_format(self, capsys):
        from sync.commands.migrate_mkdocs import run
        args = SimpleNamespace(repo="noslash", dry_run=False)
        with pytest.raises(SystemExit):
            run(args)


# ---------------------------------------------------------------------------
# generate-registry
# ---------------------------------------------------------------------------

class TestGenerateRegistry:
    @patch("sync.commands.generate_registry.get_latest_tags")
    @patch("sync.commands.generate_registry.load_repos")
    def test_generates_html(self, mock_load, mock_tags, make_repo_entry, tmp_path, capsys):
        from sync.commands.generate_registry import run
        repos = [make_repo_entry(name="lab1", repo="org/lab1")]
        mock_load.return_value = repos
        mock_tags.return_value = ["v1.2.7_1.0.0"]

        out_path = tmp_path / "registry.html"
        args = SimpleNamespace(output=str(out_path))
        run(args)

        content = out_path.read_text()
        assert "lab1" in content
        assert "REGISTRY-START" in content
        assert "REGISTRY-END" in content
        assert "Generated registry" in capsys.readouterr().out

    @patch("sync.commands.generate_registry.get_latest_tags")
    @patch("sync.commands.generate_registry.load_repos")
    def test_injects_into_existing(self, mock_load, mock_tags, make_repo_entry, tmp_path, capsys):
        from sync.commands.generate_registry import run
        repos = [make_repo_entry(name="lab1", repo="org/lab1")]
        mock_load.return_value = repos
        mock_tags.return_value = []

        out_path = tmp_path / "registry.html"
        out_path.write_text(
            "<html><!-- REGISTRY-START -->old<!-- REGISTRY-END --></html>"
        )

        args = SimpleNamespace(output=str(out_path))
        run(args)

        content = out_path.read_text()
        assert "old" not in content
        assert "lab1" in content
        assert "Updated registry" in capsys.readouterr().out

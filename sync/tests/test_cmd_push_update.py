"""Tests for sync.commands.push_update — full sync workflow."""

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.push_update import run, _update_repo, SYNC_BRANCH_PREFIX


class TestUpdateRepo:
    @patch("sync.commands.push_update.local_git")
    @patch("sync.commands.push_update._migrate_repo")
    def test_dry_run_would_update(self, mock_migrate, mock_git, make_repo_entry, tmp_path):
        repo = make_repo_entry()
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()
        dc = repo_path / ".devcontainer" / "util"
        dc.mkdir(parents=True)
        (dc / "source_framework.sh").write_text(
            'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'
        )

        mock_git.ensure_cloned.return_value = repo_path
        mock_git.pull_main.return_value = MagicMock(success=True)

        result = _update_repo(repo, "1.2.7", dry_run=True, force=False, auto_merge=False)
        assert result["status"] == "would-update"
        assert "1.2.5" in result["message"]
        assert "1.2.7" in result["message"]

    @patch("sync.commands.push_update.local_git.ensure_cloned")
    def test_clone_error(self, mock_clone, make_repo_entry):
        from sync.core.local_git import GitError
        repo = make_repo_entry()
        mock_clone.side_effect = GitError(["clone"], Path("/tmp"), "failed")

        result = _update_repo(repo, "1.2.7", dry_run=False, force=False, auto_merge=False)
        assert result["status"] == "error"
        assert "clone failed" in result["message"]

    @patch("sync.commands.push_update.local_git")
    def test_pull_failure(self, mock_git, make_repo_entry, tmp_path):
        repo = make_repo_entry()
        mock_git.ensure_cloned.return_value = tmp_path
        mock_git.pull_main.return_value = MagicMock(success=False, message="conflict")

        result = _update_repo(repo, "1.2.7", dry_run=False, force=False, auto_merge=False)
        assert result["status"] == "error"
        assert result.get("needs_manual") is True

    @patch("sync.commands.push_update.local_git")
    def test_already_at_version_skipped(self, mock_git, make_repo_entry, tmp_path):
        repo = make_repo_entry()
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        dc = repo_path / ".devcontainer" / "util"
        dc.mkdir(parents=True)

        # Write template that matches the expected template for version 1.2.7
        from sync.commands.migrate import SOURCE_FRAMEWORK_TEMPLATE, THIN_MAKEFILE
        (dc / "source_framework.sh").write_text(SOURCE_FRAMEWORK_TEMPLATE % "1.2.7")
        (repo_path / ".devcontainer" / "Makefile").write_text(THIN_MAKEFILE)

        mock_git.ensure_cloned.return_value = repo_path
        mock_git.pull_main.return_value = MagicMock(success=True)

        result = _update_repo(repo, "1.2.7", dry_run=False, force=False, auto_merge=False)
        assert result["status"] == "skipped"
        assert "already at 1.2.7" in result["message"]


class TestRunCommand:
    @patch("sync.commands.push_update._update_repo")
    @patch("sync.commands.push_update.filter_sync_targets")
    @patch("sync.commands.push_update.load_repos")
    def test_dry_run(self, mock_load, mock_filter, mock_update, make_repo_entry, capsys):
        repos = [make_repo_entry(name="lab1")]
        mock_load.return_value = repos
        mock_filter.return_value = repos
        mock_update.return_value = {
            "repo": "org/lab1",
            "status": "would-update",
            "message": "1.2.5 -> 1.2.7",
        }

        args = SimpleNamespace(
            framework_version="1.2.7",
            dry_run=True,
            force=False,
            auto_merge=False,
            json_output=False,
            repo=None,
        )
        run(args)
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "Would update 1 repos" in out

    @patch("sync.commands.push_update._update_repo")
    @patch("sync.commands.push_update.filter_sync_targets")
    @patch("sync.commands.push_update.load_repos")
    def test_specific_repo_filter(self, mock_load, mock_filter, mock_update, make_repo_entry, capsys):
        repos = [
            make_repo_entry(name="lab1", repo="org/lab1"),
            make_repo_entry(name="lab2", repo="org/lab2"),
        ]
        mock_load.return_value = repos
        mock_update.return_value = {
            "repo": "org/lab1",
            "status": "created",
            "message": "1.2.5 -> 1.2.7",
            "url": "https://github.com/org/lab1/pull/1",
        }

        args = SimpleNamespace(
            framework_version="1.2.7",
            dry_run=False,
            force=False,
            auto_merge=False,
            json_output=False,
            repo="lab1",
        )
        run(args)
        # Should only process lab1
        assert mock_update.call_count == 1

    @patch("sync.commands.push_update.filter_sync_targets")
    @patch("sync.commands.push_update.load_repos")
    def test_no_repos_found(self, mock_load, mock_filter, make_repo_entry, capsys):
        mock_load.return_value = []
        mock_filter.return_value = []

        args = SimpleNamespace(
            framework_version="1.2.7",
            dry_run=False,
            force=False,
            auto_merge=False,
            json_output=False,
            repo=None,
        )
        run(args)
        out = capsys.readouterr().out
        assert "No sync-managed" in out

    @patch("sync.commands.push_update.load_repos")
    def test_specific_repo_not_found(self, mock_load, capsys):
        mock_load.return_value = []
        args = SimpleNamespace(
            framework_version="1.2.7",
            dry_run=False,
            force=False,
            auto_merge=False,
            json_output=False,
            repo="nonexistent",
        )
        with pytest.raises(SystemExit):
            run(args)

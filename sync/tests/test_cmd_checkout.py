"""Tests for sync.commands.checkout — checkout main across repos."""

from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.checkout import run, _repo_status, _last_commit


class TestRepoStatus:
    @patch("sync.commands.checkout.subprocess.run")
    def test_clean(self, mock_run):
        mock_run.return_value = MagicMock(stdout="")
        assert _repo_status(Path("/repo")) == "clean"

    @patch("sync.commands.checkout.subprocess.run")
    def test_changes(self, mock_run):
        mock_run.return_value = MagicMock(stdout=" M file1.txt\n M file2.txt\n")
        result = _repo_status(Path("/repo"))
        assert "2 changed file(s)" in result


class TestLastCommit:
    @patch("sync.commands.checkout.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc1234 feat: thing (2 hours ago)\n",
        )
        result = _last_commit(Path("/repo"))
        assert "abc1234" in result

    @patch("sync.commands.checkout.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _last_commit(Path("/repo")) == "unknown"


class TestCheckoutCommand:
    @patch("sync.commands.checkout.get_current_branch")
    @patch("sync.commands.checkout.get_repo_path")
    @patch("sync.commands.checkout.filter_sync_targets")
    @patch("sync.commands.checkout.load_repos")
    def test_not_cloned(self, mock_load, mock_filter, mock_path, mock_branch,
                         make_repo_entry, capsys, tmp_path):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        missing = tmp_path / "nonexistent"
        mock_path.return_value = missing

        args = SimpleNamespace(repo=None, pull=False)
        run(args)
        out = capsys.readouterr().out
        assert "not cloned" in out
        assert "1 not cloned" in out

    @patch("sync.commands.checkout.subprocess.run")
    @patch("sync.commands.checkout.get_current_branch", return_value="main")
    @patch("sync.commands.checkout.get_repo_path")
    @patch("sync.commands.checkout.filter_sync_targets")
    @patch("sync.commands.checkout.load_repos")
    def test_checkout_success(self, mock_load, mock_filter, mock_path, mock_branch,
                               mock_subp, make_repo_entry, capsys, tmp_path):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        mock_path.return_value = repo_dir
        mock_subp.return_value = MagicMock(returncode=0, stdout="")

        args = SimpleNamespace(repo=None, pull=False)
        run(args)
        out = capsys.readouterr().out
        assert "branch: main" in out
        assert "1 checked out" in out

    @patch("sync.commands.checkout.subprocess.run")
    @patch("sync.commands.checkout.pull_main")
    @patch("sync.commands.checkout.get_current_branch", return_value="main")
    @patch("sync.commands.checkout.get_repo_path")
    @patch("sync.commands.checkout.filter_sync_targets")
    @patch("sync.commands.checkout.load_repos")
    def test_checkout_with_pull(self, mock_load, mock_filter, mock_path, mock_branch,
                                 mock_pull, mock_subp, make_repo_entry, capsys, tmp_path):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        mock_path.return_value = repo_dir
        mock_pull.return_value = MagicMock(success=True, message="up to date on main")
        mock_subp.return_value = MagicMock(returncode=0, stdout="")

        args = SimpleNamespace(repo=None, pull=True)
        run(args)
        out = capsys.readouterr().out
        assert "up to date" in out

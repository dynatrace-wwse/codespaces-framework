"""Tests for sync.commands.clone — clone repos from repos.yaml."""

from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.clone import run
from sync.core.local_git import GitError


class TestCloneCommand:
    @patch("sync.commands.clone.ensure_cloned")
    @patch("sync.commands.clone.get_repo_path")
    @patch("sync.commands.clone.filter_sync_targets")
    @patch("sync.commands.clone.load_repos")
    def test_already_cloned(self, mock_load, mock_filter, mock_path, mock_clone,
                             make_repo_entry, capsys, tmp_path):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        mock_path.return_value = repo_dir

        args = SimpleNamespace(repo=None, clone_all=False)
        run(args)
        out = capsys.readouterr().out
        assert "already cloned" in out
        mock_clone.assert_not_called()

    @patch("sync.commands.clone.ensure_cloned")
    @patch("sync.commands.clone.get_repo_path")
    @patch("sync.commands.clone.filter_sync_targets")
    @patch("sync.commands.clone.load_repos")
    def test_clone_new_repo(self, mock_load, mock_filter, mock_path, mock_clone,
                             make_repo_entry, capsys, tmp_path):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        mock_path.return_value = tmp_path / "nonexistent"
        mock_clone.return_value = tmp_path / "lab1"

        args = SimpleNamespace(repo=None, clone_all=False)
        run(args)
        out = capsys.readouterr().out
        assert "cloned" in out
        assert "1 cloned" in out

    @patch("sync.commands.clone.ensure_cloned")
    @patch("sync.commands.clone.get_repo_path")
    @patch("sync.commands.clone.filter_sync_targets")
    @patch("sync.commands.clone.load_repos")
    def test_clone_error(self, mock_load, mock_filter, mock_path, mock_clone,
                          make_repo_entry, capsys, tmp_path):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        mock_path.return_value = tmp_path / "nonexistent"
        mock_clone.side_effect = GitError(["clone"], Path("/tmp"), "auth failed")

        args = SimpleNamespace(repo=None, clone_all=False)
        run(args)
        out = capsys.readouterr().out
        assert "1 errors" in out

    @patch("sync.commands.clone.get_repo_path")
    @patch("sync.commands.clone.load_repos")
    def test_clone_all_flag(self, mock_load, mock_path, make_repo_entry, capsys, tmp_path):
        """--all includes non-sync-managed repos."""
        repos = [
            make_repo_entry(name="lab1", sync_managed=True),
            make_repo_entry(name="lab2", sync_managed=False, repo="org/lab2"),
        ]
        mock_load.return_value = repos

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        mock_path.return_value = repo_dir

        args = SimpleNamespace(repo=None, clone_all=True)
        run(args)
        out = capsys.readouterr().out
        assert "2 repos" in out

    @patch("sync.commands.clone.load_repos")
    def test_specific_repo_not_found(self, mock_load, capsys):
        mock_load.return_value = []
        args = SimpleNamespace(repo="nonexistent", clone_all=False)
        with pytest.raises(SystemExit):
            run(args)

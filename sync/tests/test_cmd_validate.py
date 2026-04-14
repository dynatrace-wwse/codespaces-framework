"""Tests for sync.commands.validate — repos.yaml + local validation."""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.validate import run


class TestValidateCommand:
    @patch("sync.commands.validate._validate_local")
    @patch("sync.commands.validate._resolve_repo_path")
    @patch("sync.commands.validate.check_repo_exists")
    @patch("sync.commands.validate.load_repos")
    def test_valid_repos(self, mock_load, mock_check, mock_resolve, mock_local,
                         make_repo_entry, capsys):
        repos = [
            make_repo_entry(name="lab1", repo="org/lab1"),
            make_repo_entry(name="lab2", repo="org/lab2"),
        ]
        mock_load.return_value = repos
        mock_check.return_value = (True, False)
        mock_resolve.return_value = "/tmp/lab1"
        mock_local.return_value = None

        args = SimpleNamespace(repo=None)
        run(args)
        out = capsys.readouterr().out
        assert "Schema validation passed" in out
        assert "All 2 repos accessible" in out

    @patch("sync.commands.validate.check_repo_exists")
    @patch("sync.commands.validate.load_repos")
    def test_schema_error(self, mock_load, mock_check, make_repo_entry, capsys):
        # Duplicate names trigger schema error
        repos = [
            make_repo_entry(name="dupe", repo="org/dupe1"),
            make_repo_entry(name="dupe", repo="org/dupe2"),
        ]
        mock_load.return_value = repos

        args = SimpleNamespace(repo=None)
        with pytest.raises(SystemExit):
            run(args)
        out = capsys.readouterr().out
        assert "Schema validation failed" in out

    @patch("sync.commands.validate._validate_local")
    @patch("sync.commands.validate._resolve_repo_path")
    @patch("sync.commands.validate.check_repo_exists")
    @patch("sync.commands.validate.load_repos")
    def test_repo_not_found_on_github(self, mock_load, mock_check, mock_resolve,
                                       mock_local, make_repo_entry, capsys):
        repos = [make_repo_entry(name="missing", repo="org/missing")]
        mock_load.return_value = repos
        mock_check.return_value = (False, False)

        args = SimpleNamespace(repo=None)
        with pytest.raises(SystemExit):
            run(args)
        out = capsys.readouterr().out
        assert "not found" in out

    @patch("sync.commands.validate._validate_local")
    @patch("sync.commands.validate._resolve_repo_path")
    @patch("sync.commands.validate.check_repo_exists")
    @patch("sync.commands.validate.load_repos")
    def test_archived_mismatch(self, mock_load, mock_check, mock_resolve,
                                mock_local, make_repo_entry, capsys):
        # Repo is archived on GH but status is "active"
        repos = [make_repo_entry(name="lab1", repo="org/lab1", status="active")]
        mock_load.return_value = repos
        mock_check.return_value = (True, True)

        args = SimpleNamespace(repo=None)
        with pytest.raises(SystemExit):
            run(args)
        out = capsys.readouterr().out
        assert "archived on GitHub" in out

    @patch("sync.commands.validate._validate_local")
    @patch("sync.commands.validate._resolve_repo_path")
    @patch("sync.commands.validate.check_repo_exists")
    @patch("sync.commands.validate.load_repos")
    def test_filter_by_repo_name(self, mock_load, mock_check, mock_resolve,
                                  mock_local, make_repo_entry, capsys):
        repos = [
            make_repo_entry(name="lab1", repo="org/lab1"),
            make_repo_entry(name="lab2", repo="org/lab2"),
        ]
        mock_load.return_value = repos
        mock_check.return_value = (True, False)
        mock_resolve.return_value = "/tmp/lab1"

        args = SimpleNamespace(repo="lab1")
        run(args)
        out = capsys.readouterr().out
        assert "All 1 repos accessible" in out

    @patch("sync.commands.validate.load_repos")
    def test_repo_not_found_in_yaml(self, mock_load, make_repo_entry, capsys):
        repos = [make_repo_entry(name="lab1", repo="org/lab1")]
        mock_load.return_value = repos

        args = SimpleNamespace(repo="nonexistent")
        with pytest.raises(SystemExit):
            run(args)

    @patch("sync.commands.validate.load_repos")
    def test_skips_codespaces_framework(self, mock_load, make_repo_entry, capsys):
        """codespaces-framework is always skipped during validation."""
        repos = [
            make_repo_entry(name="codespaces-framework", repo="org/codespaces-framework"),
            make_repo_entry(name="lab1", repo="org/lab1"),
        ]
        mock_load.return_value = repos

        with patch("sync.commands.validate.check_repo_exists", return_value=(True, False)):
            with patch("sync.commands.validate._resolve_repo_path", return_value="/tmp/lab1"):
                with patch("sync.commands.validate._validate_local"):
                    args = SimpleNamespace(repo=None)
                    run(args)
                    out = capsys.readouterr().out
                    assert "All 1 repos accessible" in out

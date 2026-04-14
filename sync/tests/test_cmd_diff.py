"""Tests for sync.commands.diff_cmd — preview what push-update would change."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sync.commands.diff_cmd import run
from sync.core.github_api import GHAPIError


class TestDiffCommand:
    @patch("sync.commands.diff_cmd.get_file_content")
    @patch("sync.commands.diff_cmd.filter_sync_targets")
    @patch("sync.commands.diff_cmd.load_repos")
    def test_shows_repos_needing_update(self, mock_load, mock_filter, mock_content, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'

        args = SimpleNamespace(json_output=False, framework_version="1.2.7")
        run(args)
        out = capsys.readouterr().out
        assert "1.2.5 -> 1.2.7" in out
        assert "1 repo(s) need updating" in out

    @patch("sync.commands.diff_cmd.get_file_content")
    @patch("sync.commands.diff_cmd.filter_sync_targets")
    @patch("sync.commands.diff_cmd.load_repos")
    def test_all_up_to_date(self, mock_load, mock_filter, mock_content, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.7}"'

        args = SimpleNamespace(json_output=False, framework_version="1.2.7")
        run(args)
        out = capsys.readouterr().out
        assert "Nothing to update" in out

    @patch("sync.commands.diff_cmd.get_file_content")
    @patch("sync.commands.diff_cmd.filter_sync_targets")
    @patch("sync.commands.diff_cmd.load_repos")
    def test_json_output(self, mock_load, mock_filter, mock_content, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'

        args = SimpleNamespace(json_output=True, framework_version="1.2.7")
        run(args)
        data = json.loads(capsys.readouterr().out)
        assert data["target_version"] == "1.2.7"
        assert len(data["diffs"]) == 1
        assert data["diffs"][0]["current"] == "1.2.5"
        assert data["diffs"][0]["target"] == "1.2.7"

    @patch("sync.commands.diff_cmd.get_file_content")
    @patch("sync.commands.diff_cmd.filter_sync_targets")
    @patch("sync.commands.diff_cmd.load_repos")
    def test_api_error_in_diff(self, mock_load, mock_filter, mock_content, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_content.side_effect = GHAPIError("contents", 404, "Not Found")

        args = SimpleNamespace(json_output=False, framework_version="1.2.7")
        run(args)
        out = capsys.readouterr().out
        assert "ERROR" in out

    @patch("sync.commands.diff_cmd.get_latest_tags")
    @patch("sync.commands.diff_cmd.get_file_content")
    @patch("sync.commands.diff_cmd.filter_sync_targets")
    @patch("sync.commands.diff_cmd.load_repos")
    def test_auto_detect_version(self, mock_load, mock_filter, mock_content, mock_tags, make_repo_entry, capsys):
        """When --framework-version not given, auto-detect from tags."""
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_tags.return_value = ["1.2.7", "1.2.6"]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'

        args = SimpleNamespace(json_output=False, framework_version=None)
        run(args)
        out = capsys.readouterr().out
        assert "1.2.7" in out

    @patch("sync.commands.diff_cmd.get_file_content")
    @patch("sync.commands.diff_cmd.filter_sync_targets")
    @patch("sync.commands.diff_cmd.load_repos")
    def test_not_migrated_repo(self, mock_load, mock_filter, mock_content, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        # Content without FRAMEWORK_VERSION
        mock_content.return_value = "#!/bin/bash\necho hello"

        args = SimpleNamespace(json_output=True, framework_version="1.2.7")
        run(args)
        data = json.loads(capsys.readouterr().out)
        assert data["diffs"][0]["current"] == "not-migrated"

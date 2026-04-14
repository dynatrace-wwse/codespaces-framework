"""Tests for sync.commands.status — version drift display."""

import json
import base64
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.status import run, _get_latest_framework_version


# ---------------------------------------------------------------------------
# _get_latest_framework_version
# ---------------------------------------------------------------------------

class TestGetLatestFrameworkVersion:
    @patch("sync.commands.status.get_latest_tags")
    def test_finds_latest(self, mock_tags):
        mock_tags.return_value = ["v1.2.5_1.0.0", "1.2.7", "1.2.6", "1.2.5"]
        result = _get_latest_framework_version()
        assert result == "1.2.7"

    @patch("sync.commands.status.get_latest_tags")
    def test_ignores_combined_tags(self, mock_tags):
        mock_tags.return_value = ["v1.2.7_2.0.0", "1.2.5"]
        result = _get_latest_framework_version()
        assert result == "1.2.5"

    @patch("sync.commands.status.get_latest_tags")
    def test_no_semver_tags(self, mock_tags):
        mock_tags.return_value = ["v1.2.5_1.0.0", "latest"]
        result = _get_latest_framework_version()
        assert result == "unknown"

    @patch("sync.commands.status.get_latest_tags")
    def test_empty_tags(self, mock_tags):
        mock_tags.return_value = []
        result = _get_latest_framework_version()
        assert result == "unknown"


# ---------------------------------------------------------------------------
# run (status command)
# ---------------------------------------------------------------------------

class TestStatusCommand:
    @patch("sync.commands.status.get_latest_tags")
    @patch("sync.commands.status.get_file_content")
    @patch("sync.commands.status.filter_sync_targets")
    @patch("sync.commands.status.load_repos")
    def test_all_up_to_date(self, mock_load, mock_filter, mock_content, mock_tags, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        # Framework version
        mock_tags.side_effect = [
            ["1.2.7"],  # framework tags (called by _get_latest_framework_version)
            ["v1.2.7_1.0.0"],  # repo tags
        ]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.7}"'

        args = SimpleNamespace(json_output=False)
        run(args)
        out = capsys.readouterr().out
        assert "1/1 repos up to date" in out

    @patch("sync.commands.status.get_latest_tags")
    @patch("sync.commands.status.get_file_content")
    @patch("sync.commands.status.filter_sync_targets")
    @patch("sync.commands.status.load_repos")
    def test_behind_repos(self, mock_load, mock_filter, mock_content, mock_tags, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        mock_tags.side_effect = [
            ["1.2.7"],  # framework
            ["v1.2.5_1.0.0"],  # repo tags (old)
        ]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'

        args = SimpleNamespace(json_output=False)
        run(args)
        out = capsys.readouterr().out
        assert "Behind" in out
        assert "0/1 repos up to date" in out

    @patch("sync.commands.status.get_latest_tags")
    @patch("sync.commands.status.get_file_content")
    @patch("sync.commands.status.filter_sync_targets")
    @patch("sync.commands.status.load_repos")
    def test_json_output(self, mock_load, mock_filter, mock_content, mock_tags, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        mock_tags.side_effect = [
            ["1.2.7"],
            ["v1.2.7_1.0.0"],
        ]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.7}"'

        args = SimpleNamespace(json_output=True)
        run(args)
        data = json.loads(capsys.readouterr().out)
        assert "framework_version" in data
        assert "repos" in data
        assert data["repos"][0]["status"] == "up-to-date"

    @patch("sync.commands.status.get_latest_tags")
    @patch("sync.commands.status.get_file_content")
    @patch("sync.commands.status.filter_sync_targets")
    @patch("sync.commands.status.load_repos")
    def test_api_error(self, mock_load, mock_filter, mock_content, mock_tags, make_repo_entry, capsys):
        from sync.core.github_api import GHAPIError

        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_tags.return_value = ["1.2.7"]
        mock_content.side_effect = GHAPIError("contents", 404, "Not Found")

        args = SimpleNamespace(json_output=False)
        run(args)
        out = capsys.readouterr().out
        assert "Errors" in out

"""Tests for sync.commands.tag — combined version tags on consumer repos."""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.tag import run
from sync.core.github_api import GHAPIError


class TestTagCommand:
    @patch("sync.commands.tag.create_tag")
    @patch("sync.commands.tag.get_branch_sha")
    @patch("sync.commands.tag.get_default_branch")
    @patch("sync.commands.tag.get_latest_tags")
    @patch("sync.commands.tag.get_file_content")
    @patch("sync.commands.tag.filter_sync_targets")
    @patch("sync.commands.tag.load_repos")
    def test_dry_run(self, mock_load, mock_filter, mock_content, mock_tags,
                      mock_branch, mock_sha, mock_create, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.7}"'
        mock_tags.side_effect = [
            # pre-flight tags call
            ["v1.2.5_1.0.0"],
            # tagging loop call
            ["v1.2.5_1.0.0"],
        ]

        args = SimpleNamespace(
            framework_version="1.2.7", force=False, bump=None,
            dry_run=True, release=False,
        )
        run(args)
        out = capsys.readouterr().out
        assert "would tag" in out.lower()
        mock_create.assert_not_called()

    @patch("sync.commands.tag.get_file_content")
    @patch("sync.commands.tag.filter_sync_targets")
    @patch("sync.commands.tag.load_repos")
    def test_preflight_fails_if_behind(self, mock_load, mock_filter, mock_content,
                                        make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_content.return_value = 'FRAMEWORK_VERSION="${FRAMEWORK_VERSION:-1.2.5}"'

        args = SimpleNamespace(
            framework_version="1.2.7", force=False, bump=None,
            dry_run=False, release=False,
        )
        with pytest.raises(SystemExit):
            run(args)

    @patch("sync.commands.tag.create_tag")
    @patch("sync.commands.tag.get_branch_sha", return_value="abc123")
    @patch("sync.commands.tag.get_default_branch", return_value="main")
    @patch("sync.commands.tag.get_latest_tags")
    @patch("sync.commands.tag.get_file_content")
    @patch("sync.commands.tag.filter_sync_targets")
    @patch("sync.commands.tag.load_repos")
    def test_force_skips_preflight(self, mock_load, mock_filter, mock_content, mock_tags,
                                    mock_branch, mock_sha, mock_create, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_tags.return_value = []  # no existing tags

        args = SimpleNamespace(
            framework_version="1.2.7", force=True, bump=None,
            dry_run=False, release=False,
        )
        run(args)
        mock_create.assert_called_once()
        # Tag should be v1.2.7_1.0.0 (default repo version)
        tag_arg = mock_create.call_args[0][2]
        assert tag_arg == "v1.2.7_1.0.0"

    @patch("sync.commands.tag.create_tag")
    @patch("sync.commands.tag.get_branch_sha", return_value="abc123")
    @patch("sync.commands.tag.get_default_branch", return_value="main")
    @patch("sync.commands.tag.get_latest_tags")
    @patch("sync.commands.tag.get_file_content")
    @patch("sync.commands.tag.filter_sync_targets")
    @patch("sync.commands.tag.load_repos")
    def test_bump_patch(self, mock_load, mock_filter, mock_content, mock_tags,
                         mock_branch, mock_sha, mock_create, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_tags.return_value = ["v1.2.5_1.0.0"]  # existing combined tag

        args = SimpleNamespace(
            framework_version="1.2.7", force=True, bump="patch",
            dry_run=False, release=False,
        )
        run(args)
        tag_arg = mock_create.call_args[0][2]
        assert tag_arg == "v1.2.7_1.0.1"

    @patch("sync.commands.tag.get_latest_tags")
    @patch("sync.commands.tag.get_file_content")
    @patch("sync.commands.tag.filter_sync_targets")
    @patch("sync.commands.tag.load_repos")
    def test_tag_already_exists(self, mock_load, mock_filter, mock_content, mock_tags,
                                 make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", repo="org/lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_tags.return_value = ["v1.2.7_1.0.0"]  # tag already exists

        args = SimpleNamespace(
            framework_version="1.2.7", force=True, bump=None,
            dry_run=False, release=False,
        )
        run(args)
        out = capsys.readouterr().out
        assert "already exists" in out

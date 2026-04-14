"""Tests for sync.cli — CLI argument parsing and command dispatch."""

import sys
from unittest.mock import patch, MagicMock

import pytest

from sync.cli import main


class TestCLI:
    def test_no_args_exits(self):
        with patch("sys.argv", ["sync"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 2  # argparse error

    @patch("sync.commands.list_cmd.run")
    def test_list_command(self, mock_run):
        with patch("sys.argv", ["sync", "list"]):
            main()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args.command == "list"
        assert args.json_output is False

    @patch("sync.commands.list_cmd.run")
    def test_list_json(self, mock_run):
        with patch("sys.argv", ["sync", "list", "--json"]):
            main()
        args = mock_run.call_args[0][0]
        assert args.json_output is True

    @patch("sync.commands.list_cmd.run")
    def test_list_sync_managed(self, mock_run):
        with patch("sys.argv", ["sync", "list", "--sync-managed"]):
            main()
        args = mock_run.call_args[0][0]
        assert args.sync_managed is True

    @patch("sync.commands.status.run")
    def test_status_command(self, mock_run):
        with patch("sys.argv", ["sync", "status"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.diff_cmd.run")
    def test_diff_command(self, mock_run):
        with patch("sys.argv", ["sync", "diff"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.push_update.run")
    def test_push_update_requires_version(self, mock_run):
        with patch("sys.argv", ["sync", "push-update"]):
            with pytest.raises(SystemExit):
                main()

    @patch("sync.commands.push_update.run")
    def test_push_update_with_version(self, mock_run):
        with patch("sys.argv", ["sync", "push-update", "--framework-version", "1.2.7"]):
            main()
        args = mock_run.call_args[0][0]
        assert args.framework_version == "1.2.7"
        assert args.dry_run is False

    @patch("sync.commands.push_update.run")
    def test_push_update_dry_run(self, mock_run):
        with patch("sys.argv", ["sync", "push-update", "--framework-version", "1.2.7", "--dry-run"]):
            main()
        args = mock_run.call_args[0][0]
        assert args.dry_run is True

    @patch("sync.commands.validate.run")
    def test_validate_command(self, mock_run):
        with patch("sys.argv", ["sync", "validate"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.release.run")
    def test_release_command(self, mock_run):
        with patch("sys.argv", ["sync", "release"]):
            main()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args.part is None

    @patch("sync.commands.release.run")
    def test_release_with_part(self, mock_run):
        with patch("sys.argv", ["sync", "release", "--part", "minor"]):
            main()
        args = mock_run.call_args[0][0]
        assert args.part == "minor"

    @patch("sync.commands.clone.run")
    def test_clone_command(self, mock_run):
        with patch("sys.argv", ["sync", "clone"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.checkout.run")
    def test_checkout_command(self, mock_run):
        with patch("sys.argv", ["sync", "checkout"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.ci_status.run")
    def test_ci_status_command(self, mock_run):
        with patch("sys.argv", ["sync", "ci-status"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.list_pr.run")
    def test_list_pr_command(self, mock_run):
        with patch("sys.argv", ["sync", "list-pr"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.migrate.run")
    def test_migrate_command(self, mock_run):
        with patch("sys.argv", ["sync", "migrate"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.tag.run")
    def test_tag_requires_version(self, mock_run):
        with patch("sys.argv", ["sync", "tag"]):
            with pytest.raises(SystemExit):
                main()

    @patch("sync.commands.tag.run")
    def test_tag_with_version(self, mock_run):
        with patch("sys.argv", ["sync", "tag", "--framework-version", "1.2.7"]):
            main()
        args = mock_run.call_args[0][0]
        assert args.framework_version == "1.2.7"

    @patch("sync.commands.generate_json.run")
    def test_generate_json_command(self, mock_run):
        with patch("sys.argv", ["sync", "generate-json"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.revert.run")
    def test_revert_command(self, mock_run):
        with patch("sys.argv", ["sync", "revert"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.cleanup_branches.run")
    def test_cleanup_branches_command(self, mock_run):
        with patch("sys.argv", ["sync", "cleanup-branches"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.protect_main.run")
    def test_protect_main_command(self, mock_run):
        with patch("sys.argv", ["sync", "protect-main"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.bump_repo_version.run")
    def test_bump_repo_version_requires_repo(self, mock_run):
        with patch("sys.argv", ["sync", "bump-repo-version"]):
            with pytest.raises(SystemExit):
                main()

    @patch("sync.commands.bump_repo_version.run")
    def test_bump_repo_version(self, mock_run):
        with patch("sys.argv", ["sync", "bump-repo-version", "--repo", "org/my-repo"]):
            main()
        args = mock_run.call_args[0][0]
        assert args.repo == "org/my-repo"
        assert args.part == "patch"  # default

    @patch("sync.commands.list_issues.run")
    def test_list_issues_command(self, mock_run):
        with patch("sys.argv", ["sync", "list-issues"]):
            main()
        mock_run.assert_called_once()

    @patch("sync.commands.migrate_mkdocs.run")
    def test_migrate_mkdocs_requires_repo(self, mock_run):
        with patch("sys.argv", ["sync", "migrate-mkdocs"]):
            with pytest.raises(SystemExit):
                main()

    @patch("sync.commands.generate_registry.run")
    def test_generate_registry_requires_output(self, mock_run):
        with patch("sys.argv", ["sync", "generate-registry"]):
            with pytest.raises(SystemExit):
                main()

"""Tests for sync.commands.list_pr — list, approve, merge, close PRs."""

import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.list_pr import run, _get_ci_status, _get_prs, SYNC_BRANCH_PREFIX


# ---------------------------------------------------------------------------
# _get_ci_status
# ---------------------------------------------------------------------------

class TestGetCiStatus:
    def test_passing(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "SUCCESS", "status": ""},
            {"conclusion": "NEUTRAL", "status": ""},
        ]}
        assert _get_ci_status(pr) == "passing"

    def test_failing(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "SUCCESS", "status": ""},
            {"conclusion": "FAILURE", "status": ""},
        ]}
        assert _get_ci_status(pr) == "failing"

    def test_pending(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "SUCCESS", "status": ""},
            {"conclusion": "", "status": "IN_PROGRESS"},
        ]}
        assert _get_ci_status(pr) == "pending"

    def test_no_checks(self):
        pr = {"statusCheckRollup": []}
        assert _get_ci_status(pr) == "none"

    def test_missing_rollup(self):
        pr = {}
        assert _get_ci_status(pr) == "none"

    def test_cancelled_is_failing(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "CANCELLED", "status": ""},
        ]}
        assert _get_ci_status(pr) == "failing"

    def test_skipped_is_passing(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "SKIPPED", "status": ""},
        ]}
        assert _get_ci_status(pr) == "passing"

    def test_queued_is_pending(self):
        pr = {"statusCheckRollup": [
            {"conclusion": "", "status": "QUEUED"},
        ]}
        assert _get_ci_status(pr) == "pending"

    def test_empty_conclusion_is_pending(self):
        """Empty conclusion with no status is pending."""
        pr = {"statusCheckRollup": [
            {"conclusion": "", "status": ""},
        ]}
        assert _get_ci_status(pr) == "pending"


# ---------------------------------------------------------------------------
# _get_prs
# ---------------------------------------------------------------------------

class TestGetPrs:
    @patch("sync.commands.list_pr._gh")
    def test_returns_prs(self, mock_gh):
        mock_gh.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {"number": 1, "url": "http://pr/1", "title": "PR 1",
                 "headRefName": "sync/framework-1.2.7", "statusCheckRollup": []},
            ]),
        )
        prs = _get_prs("org/repo")
        assert len(prs) == 1

    @patch("sync.commands.list_pr._gh")
    def test_empty_on_error(self, mock_gh):
        mock_gh.return_value = MagicMock(returncode=1, stdout="")
        assert _get_prs("org/repo") == []

    @patch("sync.commands.list_pr._gh")
    def test_head_filter(self, mock_gh):
        mock_gh.return_value = MagicMock(returncode=0, stdout="[]")
        _get_prs("org/repo", head="sync/framework-1.2.7")
        cmd_args = mock_gh.call_args[0][0]
        assert "--head" in cmd_args
        assert "sync/framework-1.2.7" in cmd_args


# ---------------------------------------------------------------------------
# run (list-pr command)
# ---------------------------------------------------------------------------

class TestListPrCommand:
    @patch("sync.commands.list_pr._get_prs")
    @patch("sync.commands.list_pr.load_repos")
    def test_no_open_prs(self, mock_load, mock_prs, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_prs.return_value = []

        args = SimpleNamespace(
            framework_version=None, repo=None, approve=False, merge=False,
            close=False, comment=None, failed=False,
        )
        run(args)
        out = capsys.readouterr().out
        assert "no open PRs" in out

    @patch("sync.commands.list_pr._get_prs")
    @patch("sync.commands.list_pr.load_repos")
    def test_passing_pr(self, mock_load, mock_prs, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_prs.return_value = [
            {"number": 42, "url": "http://pr/42", "title": "Sync update",
             "headRefName": "sync/framework-1.2.7",
             "statusCheckRollup": [{"conclusion": "SUCCESS", "status": ""}]},
        ]

        args = SimpleNamespace(
            framework_version=None, repo=None, approve=False, merge=False,
            close=False, comment=None, failed=False,
        )
        run(args)
        out = capsys.readouterr().out
        assert "#42" in out
        assert "1 passing" in out

    @patch("sync.commands.list_pr._approve_pr")
    @patch("sync.commands.list_pr._get_prs")
    @patch("sync.commands.list_pr.load_repos")
    def test_approve_passing(self, mock_load, mock_prs, mock_approve, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_prs.return_value = [
            {"number": 42, "url": "http://pr/42", "title": "Sync",
             "headRefName": "sync/framework-1.2.7",
             "statusCheckRollup": [{"conclusion": "SUCCESS", "status": ""}]},
        ]
        mock_approve.return_value = True

        args = SimpleNamespace(
            framework_version=None, repo=None, approve=True, merge=False,
            close=False, comment=None, failed=False,
        )
        run(args)
        out = capsys.readouterr().out
        assert "approved" in out
        mock_approve.assert_called_once_with("dynatrace-wwse/test-repo", 42)

    @patch("sync.commands.list_pr._close_pr")
    @patch("sync.commands.list_pr._get_prs")
    @patch("sync.commands.list_pr.load_repos")
    def test_close_prs(self, mock_load, mock_prs, mock_close, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_prs.return_value = [
            {"number": 42, "url": "http://pr/42", "title": "Sync",
             "headRefName": "sync/framework-1.2.7",
             "statusCheckRollup": [{"conclusion": "SUCCESS", "status": ""}]},
        ]
        mock_close.return_value = True

        args = SimpleNamespace(
            framework_version=None, repo=None, approve=False, merge=False,
            close=True, comment="Superseded", failed=False,
        )
        run(args)
        out = capsys.readouterr().out
        assert "closed" in out
        mock_close.assert_called_once_with("dynatrace-wwse/test-repo", 42, "Superseded")

    @patch("sync.commands.list_pr._get_prs")
    @patch("sync.commands.list_pr.load_repos")
    def test_version_filter(self, mock_load, mock_prs, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_prs.return_value = []

        args = SimpleNamespace(
            framework_version="1.2.7", repo=None, approve=False, merge=False,
            close=False, comment=None, failed=False,
        )
        run(args)
        out = capsys.readouterr().out
        assert "sync/framework-1.2.7" in out

    @patch("sync.commands.list_pr.load_repos")
    def test_repo_not_found(self, mock_load, capsys):
        mock_load.return_value = []
        args = SimpleNamespace(
            framework_version=None, repo="nonexistent", approve=False,
            merge=False, close=False, comment=None, failed=False,
        )
        with pytest.raises(SystemExit):
            run(args)

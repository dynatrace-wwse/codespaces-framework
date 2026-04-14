"""Tests for sync.commands.ci_status — CI run status across repos."""

import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.ci_status import run, _get_latest_run, _get_all_latest_runs, _icon


class TestIcon:
    def test_success(self):
        assert _icon({"status": "completed", "conclusion": "success"}) == "\u2705"

    def test_failure(self):
        assert _icon({"status": "completed", "conclusion": "failure"}) == "\u274c"

    def test_in_progress(self):
        assert _icon({"status": "in_progress", "conclusion": ""}) == "\u23f3"

    def test_unknown(self):
        icon = _icon({"status": "unknown", "conclusion": "unknown"})
        assert icon == "\u2753"


class TestGetLatestRun:
    @patch("sync.commands.ci_status.subprocess.run")
    def test_returns_runs(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"name": "CI", "status": "completed", "conclusion": "success"}]),
        )
        runs = _get_latest_run("org/repo")
        assert len(runs) == 1
        assert runs[0]["name"] == "CI"

    @patch("sync.commands.ci_status.subprocess.run")
    def test_empty_on_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _get_latest_run("org/repo") == []


class TestGetAllLatestRuns:
    @patch("sync.commands.ci_status.subprocess.run")
    def test_deduplicates_workflows(self, mock_run):
        runs = [
            {"name": "CI", "workflowName": "Integration", "status": "completed", "conclusion": "success"},
            {"name": "CI2", "workflowName": "Integration", "status": "completed", "conclusion": "failure"},
            {"name": "Deploy", "workflowName": "Deploy", "status": "completed", "conclusion": "success"},
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(runs),
        )
        result = _get_all_latest_runs("org/repo")
        # Should keep first per workflow
        assert len(result) == 2
        names = [r["workflowName"] for r in result]
        assert "Integration" in names
        assert "Deploy" in names


class TestCiStatusCommand:
    @patch("sync.commands.ci_status._get_all_latest_runs")
    @patch("sync.commands.ci_status.load_repos")
    def test_all_passing(self, mock_load, mock_runs, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", ci=True)
        mock_load.return_value = [repo]
        mock_runs.return_value = [
            {"workflowName": "CI", "name": "CI", "status": "completed",
             "conclusion": "success", "headBranch": "main", "url": "http://ci"},
        ]

        args = SimpleNamespace(repo=None, all_workflows=False)
        run(args)
        out = capsys.readouterr().out
        assert "1 passing" in out
        assert "0 failing" in out

    @patch("sync.commands.ci_status._get_all_latest_runs")
    @patch("sync.commands.ci_status.load_repos")
    def test_failing_repo(self, mock_load, mock_runs, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", ci=True)
        mock_load.return_value = [repo]
        mock_runs.return_value = [
            {"workflowName": "CI", "name": "CI", "status": "completed",
             "conclusion": "failure", "headBranch": "main", "url": "http://ci"},
        ]

        args = SimpleNamespace(repo=None, all_workflows=False)
        run(args)
        out = capsys.readouterr().out
        assert "1 failing" in out
        assert "Failing repos" in out

    @patch("sync.commands.ci_status._get_all_latest_runs")
    @patch("sync.commands.ci_status.load_repos")
    def test_no_runs(self, mock_load, mock_runs, make_repo_entry, capsys):
        repo = make_repo_entry(name="lab1", ci=True)
        mock_load.return_value = [repo]
        mock_runs.return_value = []

        args = SimpleNamespace(repo=None, all_workflows=False)
        run(args)
        out = capsys.readouterr().out
        assert "no workflow runs" in out

    @patch("sync.commands.ci_status._get_all_latest_runs")
    @patch("sync.commands.ci_status.load_repos")
    def test_filters_ci_enabled(self, mock_load, mock_runs, make_repo_entry, capsys):
        repos = [
            make_repo_entry(name="lab1", ci=True),
            make_repo_entry(name="lab2", ci=False),
        ]
        mock_load.return_value = repos
        mock_runs.return_value = []

        args = SimpleNamespace(repo=None, all_workflows=False)
        run(args)
        out = capsys.readouterr().out
        assert "1 repos" in out  # only ci-enabled repos

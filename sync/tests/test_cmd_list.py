"""Tests for sync.commands.list_cmd — list command."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sync.commands.list_cmd import run


@pytest.fixture
def mock_load_repos(sample_repos):
    with patch("sync.commands.list_cmd.load_repos", return_value=sample_repos) as m:
        yield m


class TestListCommand:
    def test_list_all(self, mock_load_repos, capsys):
        args = SimpleNamespace(ci_enabled=False, sync_managed=False, json_output=False)
        run(args)
        out = capsys.readouterr().out
        assert "6 repo(s)" in out

    def test_list_sync_managed(self, mock_load_repos, capsys):
        args = SimpleNamespace(ci_enabled=False, sync_managed=True, json_output=False)
        run(args)
        out = capsys.readouterr().out
        # Only repos where sync_managed=True (default): k8s, genai, archived, experimental
        assert "enablement-kubernetes-otel" in out

    def test_list_ci_enabled(self, mock_load_repos, capsys):
        args = SimpleNamespace(ci_enabled=True, sync_managed=False, json_output=False)
        run(args)
        out = capsys.readouterr().out
        # codespaces-framework has ci=False, so should be excluded
        assert "codespaces-framework" not in out

    def test_list_json_output(self, mock_load_repos, capsys):
        args = SimpleNamespace(ci_enabled=False, sync_managed=False, json_output=True)
        run(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "repos" in data
        assert len(data["repos"]) == 6
        first = data["repos"][0]
        assert "name" in first
        assert "repo" in first
        assert "sync_managed" in first

    def test_list_json_with_filters(self, mock_load_repos, capsys):
        args = SimpleNamespace(ci_enabled=False, sync_managed=True, json_output=True)
        run(args)
        data = json.loads(capsys.readouterr().out)
        for r in data["repos"]:
            assert r["sync_managed"] is True

    def test_table_header_present(self, mock_load_repos, capsys):
        args = SimpleNamespace(ci_enabled=False, sync_managed=False, json_output=False)
        run(args)
        out = capsys.readouterr().out
        assert "Repository" in out
        assert "Status" in out
        assert "Maintainer" in out

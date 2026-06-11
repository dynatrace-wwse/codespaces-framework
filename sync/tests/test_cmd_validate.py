"""Tests for sync.commands.validate — repos.yaml + local validation."""

import textwrap
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.validate import run, check_post_create_credentials


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


def _pc(body):
    return textwrap.dedent(body).lstrip("\n")


class TestPostCreateCredentialGuard:
    """The DEPLOYS-DT contract: a post-create.sh that calls
    dynatraceDeployOperator must validate credentials with
    `variablesNeeded ... DT_OPERATOR_TOKEN:true` first."""

    def test_validate_before_deploy_passes(self):
        content = _pc(
            """
            #!/bin/bash
            source .devcontainer/util/source_framework.sh
            variablesNeeded DT_ENVIRONMENT:true DT_OPERATOR_TOKEN:true DT_INGEST_TOKEN:false || exit 1
            setUpTerminal
            startK3dCluster
            dynatraceDeployOperator
            deployApplicationMonitoring
            """
        )
        assert check_post_create_credentials(content) == []

    def test_deploy_without_validate_fails(self):
        content = _pc(
            """
            #!/bin/bash
            source .devcontainer/util/source_framework.sh
            setUpTerminal
            dynatraceDeployOperator
            deployApplicationMonitoring
            """
        )
        issues = check_post_create_credentials(content)
        assert len(issues) == 1
        assert "never calls variablesNeeded" in issues[0]

    def test_validate_after_deploy_fails(self):
        content = _pc(
            """
            #!/bin/bash
            source .devcontainer/util/source_framework.sh
            dynatraceDeployOperator
            variablesNeeded DT_OPERATOR_TOKEN:true || exit 1
            """
        )
        issues = check_post_create_credentials(content)
        assert len(issues) == 1
        assert "after" in issues[0]

    def test_no_deploy_is_not_applicable(self):
        content = _pc(
            """
            #!/bin/bash
            source .devcontainer/util/source_framework.sh
            setUpTerminal
            startK3dCluster
            deployTodoApp
            """
        )
        assert check_post_create_credentials(content) == []

    def test_commented_deploy_does_not_trigger(self):
        # k8s-101 ships dynatraceDeployOperator commented out (students run it
        # manually as the lab) — the guard must not flag it.
        content = _pc(
            """
            #!/bin/bash
            source .devcontainer/util/source_framework.sh
            #dynatraceDeployOperator
            deployTodoApp
            """
        )
        assert check_post_create_credentials(content) == []

    def test_validate_requires_operator_token_true(self):
        # A variablesNeeded that does not require the operator token does not
        # satisfy the contract.
        content = _pc(
            """
            #!/bin/bash
            variablesNeeded DT_ENVIRONMENT:true
            dynatraceDeployOperator
            """
        )
        issues = check_post_create_credentials(content)
        assert len(issues) == 1
        assert "never calls variablesNeeded" in issues[0]

    def test_indented_deploy_still_triggers(self):
        content = _pc(
            """
            #!/bin/bash
            if true; then
                dynatraceDeployOperator
            fi
            """
        )
        issues = check_post_create_credentials(content)
        assert len(issues) == 1

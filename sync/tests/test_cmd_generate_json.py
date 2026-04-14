"""Tests for sync.commands.generate_json — registry JSON generation."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sync.commands.generate_json import run


class TestGenerateJsonCommand:
    @patch("sync.commands.generate_json.load_repos")
    def test_stdout_output(self, mock_load, make_repo_entry, capsys):
        repos = [
            make_repo_entry(
                name="lab1",
                repo="org/lab1",
                listed=True,
                title="Lab One",
                tags=["k8s", "otel"],
                primary_tag="k8s",
                duration="3h",
                icon_key="",
            ),
            make_repo_entry(
                name="lab2",
                repo="org/lab2",
                listed=True,
                title="",
                tags=[],
                primary_tag="",
                duration="",
                is_template=True,
                icon_key="custom",
            ),
        ]
        mock_load.return_value = repos

        args = SimpleNamespace(output=None)
        run(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data) == 2

        # First entry
        assert data[0]["repo"] == "lab1"
        assert data[0]["title"] == "Lab One"
        assert data[0]["tags"] == ["k8s", "otel"]
        assert data[0]["primaryTag"] == "k8s"
        assert data[0]["duration"] == "3h"
        assert "iconKey" not in data[0]  # empty icon_key omitted

        # Second entry — fallback title from name
        assert data[1]["title"] == "lab2"
        assert data[1]["primaryTag"] == ""
        assert data[1]["iconKey"] == "custom"
        assert data[1]["isTemplate"] is True

    @patch("sync.commands.generate_json.load_repos")
    def test_excludes_unlisted(self, mock_load, make_repo_entry, capsys):
        repos = [
            make_repo_entry(name="listed", listed=True),
            make_repo_entry(name="unlisted", listed=False),
        ]
        mock_load.return_value = repos

        args = SimpleNamespace(output=None)
        run(args)
        data = json.loads(capsys.readouterr().out)
        names = [d["repo"] for d in data]
        assert "listed" not in names or len(data) == 1

    @patch("sync.commands.generate_json.load_repos")
    def test_excludes_non_active(self, mock_load, make_repo_entry, capsys):
        repos = [
            make_repo_entry(name="active", status="active", listed=True),
            make_repo_entry(name="archived", status="archived", listed=True),
        ]
        mock_load.return_value = repos

        args = SimpleNamespace(output=None)
        run(args)
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1

    @patch("sync.commands.generate_json.load_repos")
    def test_file_output(self, mock_load, make_repo_entry, tmp_path, capsys):
        repos = [make_repo_entry(name="lab1", listed=True)]
        mock_load.return_value = repos

        out_path = tmp_path / "repos.json"
        args = SimpleNamespace(output=str(out_path))
        run(args)

        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert len(data) == 1
        # Should also print confirmation
        assert "Wrote 1 repos" in capsys.readouterr().out

    @patch("sync.commands.generate_json.load_repos")
    def test_primary_tag_fallback(self, mock_load, make_repo_entry, capsys):
        """primary_tag falls back to first tag."""
        repos = [
            make_repo_entry(
                name="lab1",
                listed=True,
                tags=["first-tag", "second"],
                primary_tag="",
            ),
        ]
        mock_load.return_value = repos

        args = SimpleNamespace(output=None)
        run(args)
        data = json.loads(capsys.readouterr().out)
        assert data[0]["primaryTag"] == "first-tag"

    @patch("sync.commands.generate_json.load_repos")
    def test_empty_repos(self, mock_load, capsys):
        mock_load.return_value = []
        args = SimpleNamespace(output=None)
        run(args)
        data = json.loads(capsys.readouterr().out)
        assert data == []

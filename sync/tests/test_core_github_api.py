"""Tests for sync.core.github_api — GitHub API wrapper, token, error handling."""

import json
import base64
from unittest.mock import patch, MagicMock

import pytest

from sync.core.github_api import (
    GHAPIError,
    get_token,
    _gh_api,
    check_rate_limit,
    check_repo_exists,
    get_file_content,
    get_file_sha,
    get_default_branch,
    get_latest_tags,
    create_branch,
    get_branch_sha,
    update_file,
    create_pr,
    enable_auto_merge,
    create_tag,
    create_release,
    branch_exists,
)


# ---------------------------------------------------------------------------
# GHAPIError
# ---------------------------------------------------------------------------

class TestGHAPIError:
    def test_message(self):
        e = GHAPIError("/repos/foo", 404, "Not Found")
        assert "GitHub API error" in str(e)
        assert "/repos/foo" in str(e)
        assert "Not Found" in str(e)
        assert e.code == 404
        assert e.endpoint == "/repos/foo"


# ---------------------------------------------------------------------------
# get_token
# ---------------------------------------------------------------------------

class TestGetToken:
    @patch.dict("os.environ", {"SYNC_TOKEN": "my-secret-token"})
    def test_env_var(self):
        assert get_token() == "my-secret-token"

    @patch.dict("os.environ", {}, clear=True)
    @patch("sync.core.github_api.subprocess.run")
    def test_fallback_to_gh_auth(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="gh-token-123\n",
        )
        # Remove SYNC_TOKEN from env
        import os
        os.environ.pop("SYNC_TOKEN", None)
        assert get_token() == "gh-token-123"

    @patch.dict("os.environ", {}, clear=True)
    @patch("sync.core.github_api.subprocess.run")
    def test_both_fail_exits(self, mock_run):
        mock_run.side_effect = FileNotFoundError("gh not found")
        import os
        os.environ.pop("SYNC_TOKEN", None)
        with pytest.raises(SystemExit):
            get_token()


# ---------------------------------------------------------------------------
# _gh_api
# ---------------------------------------------------------------------------

class TestGhApi:
    @patch("sync.core.github_api.subprocess.run")
    def test_get_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"rate": {}}',
            stderr="",
        )
        result = _gh_api("GET", "rate_limit")
        assert result == {"rate": {}}

    @patch("sync.core.github_api.subprocess.run")
    def test_get_empty_response(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        result = _gh_api("GET", "rate_limit")
        assert result == {}

    @patch("sync.core.github_api.subprocess.run")
    def test_error_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Not Found",
        )
        with pytest.raises(GHAPIError) as exc_info:
            _gh_api("GET", "repos/foo/bar")
        assert exc_info.value.code == 1

    @patch("sync.core.github_api.subprocess.run")
    def test_post_with_data(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id": 1}',
            stderr="",
        )
        result = _gh_api("POST", "repos/foo/bar/pulls", {"title": "test"})
        assert result == {"id": 1}
        # Verify input was passed
        call_args = mock_run.call_args
        assert call_args.kwargs.get("input") or call_args[1].get("input")


# ---------------------------------------------------------------------------
# check_repo_exists
# ---------------------------------------------------------------------------

class TestCheckRepoExists:
    @patch("sync.core.github_api._gh_api")
    def test_exists_not_archived(self, mock_api):
        mock_api.return_value = {"archived": False}
        exists, archived = check_repo_exists("org", "repo")
        assert exists is True
        assert archived is False

    @patch("sync.core.github_api._gh_api")
    def test_exists_archived(self, mock_api):
        mock_api.return_value = {"archived": True}
        exists, archived = check_repo_exists("org", "repo")
        assert exists is True
        assert archived is True

    @patch("sync.core.github_api._gh_api")
    def test_not_found(self, mock_api):
        mock_api.side_effect = GHAPIError("repos/org/repo", 1, "Not Found")
        exists, archived = check_repo_exists("org", "repo")
        assert exists is False
        assert archived is False


# ---------------------------------------------------------------------------
# get_file_content
# ---------------------------------------------------------------------------

class TestGetFileContent:
    @patch("sync.core.github_api._gh_api")
    def test_decodes_base64(self, mock_api):
        content = "Hello, world!"
        encoded = base64.b64encode(content.encode()).decode()
        mock_api.return_value = {"content": encoded}
        result = get_file_content("org", "repo", "file.txt")
        assert result == content

    @patch("sync.core.github_api._gh_api")
    def test_with_ref(self, mock_api):
        mock_api.return_value = {"content": base64.b64encode(b"x").decode()}
        get_file_content("org", "repo", "file.txt", ref="v1.2.3")
        endpoint = mock_api.call_args[0][1]
        assert "?ref=v1.2.3" in endpoint


# ---------------------------------------------------------------------------
# get_latest_tags
# ---------------------------------------------------------------------------

class TestGetLatestTags:
    @patch("sync.core.github_api._gh_api")
    def test_returns_tag_names(self, mock_api):
        mock_api.return_value = [
            {"name": "v1.2.5_1.0.0"},
            {"name": "v1.2.4_1.0.0"},
            {"name": "1.2.5"},
        ]
        tags = get_latest_tags("org", "repo")
        assert tags == ["v1.2.5_1.0.0", "v1.2.4_1.0.0", "1.2.5"]

    @patch("sync.core.github_api._gh_api")
    def test_api_error_returns_empty(self, mock_api):
        mock_api.side_effect = GHAPIError("tags", 1, "error")
        assert get_latest_tags("org", "repo") == []


# ---------------------------------------------------------------------------
# branch_exists
# ---------------------------------------------------------------------------

class TestBranchExists:
    @patch("sync.core.github_api._gh_api")
    def test_exists(self, mock_api):
        mock_api.return_value = {"ref": "refs/heads/main"}
        assert branch_exists("org", "repo", "main") is True

    @patch("sync.core.github_api._gh_api")
    def test_not_exists(self, mock_api):
        mock_api.side_effect = GHAPIError("ref", 1, "Not Found")
        assert branch_exists("org", "repo", "nonexistent") is False


# ---------------------------------------------------------------------------
# create_release
# ---------------------------------------------------------------------------

class TestCreateRelease:
    @patch("sync.core.github_api._gh_api")
    def test_creates_release(self, mock_api):
        mock_api.return_value = {"html_url": "https://github.com/org/repo/releases/1"}
        result = create_release("org", "repo", "v1.0.0", "Release 1.0.0", "body")
        assert result["html_url"] == "https://github.com/org/repo/releases/1"
        call_data = mock_api.call_args[0][2]
        assert call_data["tag_name"] == "v1.0.0"
        assert call_data["generate_release_notes"] is True

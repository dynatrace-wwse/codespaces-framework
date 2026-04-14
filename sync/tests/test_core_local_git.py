"""Tests for sync.core.local_git — Git operations, GitResult, error handling."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sync.core.local_git import (
    GitResult,
    GitError,
    _run_git,
    get_repo_path,
    ensure_cloned,
    pull_main,
    create_branch,
    has_changes,
    commit,
    push,
    create_pr,
    enable_auto_merge,
    get_current_branch,
    get_default_branch,
    REPOS_BASE,
)


# ---------------------------------------------------------------------------
# GitResult / GitError
# ---------------------------------------------------------------------------

class TestGitResult:
    def test_success(self):
        r = GitResult(success=True, message="ok")
        assert r.success is True
        assert r.message == "ok"
        assert r.path is None
        assert r.branch is None
        assert r.needs_manual is False

    def test_failure_with_path(self):
        r = GitResult(success=False, message="fail", path=Path("/tmp"), needs_manual=True)
        assert r.success is False
        assert r.needs_manual is True


class TestGitError:
    def test_message_format(self):
        e = GitError(["push", "origin"], Path("/repo"), "permission denied")
        assert "git push origin" in str(e)
        assert "/repo" in str(e)
        assert "permission denied" in str(e)


# ---------------------------------------------------------------------------
# _run_git
# ---------------------------------------------------------------------------

class TestRunGit:
    @patch("sync.core.local_git.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
        result = _run_git(["status"], Path("/repo"))
        mock_run.assert_called_once_with(
            ["git", "status"],
            cwd=Path("/repo"),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    @patch("sync.core.local_git.subprocess.run")
    def test_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="fatal: error")
        with pytest.raises(GitError):
            _run_git(["push"], Path("/repo"))

    @patch("sync.core.local_git.subprocess.run")
    def test_check_false_no_raise(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = _run_git(["status"], Path("/repo"), check=False)
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# get_repo_path
# ---------------------------------------------------------------------------

class TestGetRepoPath:
    def test_returns_path(self):
        path = get_repo_path("my-repo")
        assert path == REPOS_BASE / "my-repo"
        assert isinstance(path, Path)


# ---------------------------------------------------------------------------
# ensure_cloned
# ---------------------------------------------------------------------------

class TestEnsureCloned:
    def test_already_cloned(self, tmp_path):
        """When repo dir exists with .git, return path without cloning."""
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        with patch("sync.core.local_git.get_repo_path", return_value=repo_dir):
            with patch("sync.core.local_git.REPOS_BASE", tmp_path):
                path = ensure_cloned("org", "my-repo")
                assert path == repo_dir

    @patch("sync.core.local_git._run_git")
    def test_clones_if_missing(self, mock_git, tmp_path):
        repo_dir = tmp_path / "my-repo"
        # Repo dir does NOT exist

        with patch("sync.core.local_git.get_repo_path", return_value=repo_dir):
            with patch("sync.core.local_git.REPOS_BASE", tmp_path):
                ensure_cloned("org", "my-repo")
                mock_git.assert_called_once()
                args = mock_git.call_args[0][0]
                assert "clone" in args
                assert "https://github.com/org/my-repo.git" in args


# ---------------------------------------------------------------------------
# pull_main
# ---------------------------------------------------------------------------

class TestPullMain:
    @patch("sync.core.local_git._run_git")
    def test_success(self, mock_git):
        # symbolic-ref succeeds -> branch is "main"
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout="refs/remotes/origin/main\n"),  # symbolic-ref
            MagicMock(returncode=0),  # stash
            MagicMock(returncode=0),  # checkout
            MagicMock(returncode=0, stdout="", stderr=""),  # pull
        ]
        result = pull_main(Path("/repo"))
        assert result.success is True
        assert "main" in result.message

    @patch("sync.core.local_git._run_git")
    def test_pull_failure(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout="refs/remotes/origin/main\n"),
            MagicMock(returncode=0),  # stash
            MagicMock(returncode=0),  # checkout
            MagicMock(returncode=1, stdout="", stderr="conflict"),  # pull fails
        ]
        result = pull_main(Path("/repo"))
        assert result.success is False
        assert result.needs_manual is True

    @patch("sync.core.local_git._run_git")
    def test_fallback_to_master(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),  # symbolic-ref fails
            MagicMock(returncode=1, stdout="", stderr=""),  # origin/main check fails
            MagicMock(returncode=0),  # stash
            MagicMock(returncode=0),  # checkout master
            MagicMock(returncode=0, stdout="", stderr=""),  # pull
        ]
        result = pull_main(Path("/repo"))
        assert result.success is True
        assert "master" in result.message


# ---------------------------------------------------------------------------
# create_branch
# ---------------------------------------------------------------------------

class TestCreateBranch:
    @patch("sync.core.local_git._run_git")
    def test_new_branch(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=1),  # local check fails (branch doesn't exist)
            MagicMock(returncode=0, stdout=""),  # remote check (no remote branch)
            MagicMock(returncode=0),  # checkout -b
        ]
        result = create_branch(Path("/repo"), "sync/framework-1.2.5")
        assert result.success is True
        assert "created" in result.message

    @patch("sync.core.local_git._run_git")
    def test_existing_local_branch(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=0),  # local check succeeds
            MagicMock(returncode=0),  # checkout
        ]
        result = create_branch(Path("/repo"), "sync/framework-1.2.5")
        assert result.success is True
        assert "existing" in result.message

    @patch("sync.core.local_git._run_git")
    def test_remote_branch_exists_no_force(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=1),  # local doesn't exist
            MagicMock(returncode=0, stdout="abc123 refs/heads/sync/framework-1.2.5\n"),  # remote exists
        ]
        result = create_branch(Path("/repo"), "sync/framework-1.2.5")
        assert result.success is False
        assert "already exists on remote" in result.message


# ---------------------------------------------------------------------------
# has_changes
# ---------------------------------------------------------------------------

class TestHasChanges:
    @patch("sync.core.local_git._run_git")
    def test_no_changes(self, mock_git):
        mock_git.return_value = MagicMock(returncode=0, stdout="")
        assert has_changes(Path("/repo")) is False

    @patch("sync.core.local_git._run_git")
    def test_has_changes(self, mock_git):
        mock_git.return_value = MagicMock(returncode=0, stdout=" M file.txt\n")
        assert has_changes(Path("/repo")) is True


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------

class TestCommit:
    @patch("sync.core.local_git._run_git")
    def test_commit_with_changes(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=0),  # add -A
            MagicMock(returncode=1),  # diff --cached --quiet (has changes)
            MagicMock(returncode=0),  # commit
        ]
        result = commit(Path("/repo"), "test commit")
        assert result.success is True
        assert "committed" in result.message

    @patch("sync.core.local_git._run_git")
    def test_no_changes_to_commit(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=0),  # add -A
            MagicMock(returncode=0),  # diff --cached --quiet (no changes)
        ]
        result = commit(Path("/repo"), "test commit")
        assert result.success is True
        assert "no changes" in result.message


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

class TestPush:
    @patch("sync.core.local_git._run_git")
    def test_success(self, mock_git):
        mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = push(Path("/repo"), "my-branch")
        assert result.success is True
        assert "pushed" in result.message

    @patch("sync.core.local_git._run_git")
    def test_failure(self, mock_git):
        mock_git.return_value = MagicMock(returncode=1, stdout="", stderr="denied")
        result = push(Path("/repo"), "my-branch")
        assert result.success is False
        assert result.needs_manual is True


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------

class TestCreatePR:
    @patch("sync.core.local_git.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/org/repo/pull/42\n",
            stderr="",
        )
        result = create_pr("org", "repo", Path("/repo"), "title", "body")
        assert result.success is True
        assert "pull/42" in result.message

    @patch("sync.core.local_git.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error creating PR",
        )
        result = create_pr("org", "repo", Path("/repo"), "title", "body")
        assert result.success is False
        assert result.needs_manual is True


# ---------------------------------------------------------------------------
# enable_auto_merge
# ---------------------------------------------------------------------------

class TestEnableAutoMerge:
    @patch("sync.core.local_git.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        # Should not raise
        enable_auto_merge("org", "repo", "https://github.com/org/repo/pull/42")

    @patch("sync.core.local_git.subprocess.run")
    def test_handles_exception(self, mock_run):
        mock_run.side_effect = Exception("network error")
        # Should not raise (best-effort)
        enable_auto_merge("org", "repo", "https://github.com/org/repo/pull/42")


# ---------------------------------------------------------------------------
# get_current_branch / get_default_branch
# ---------------------------------------------------------------------------

class TestBranchHelpers:
    @patch("sync.core.local_git._run_git")
    def test_get_current_branch(self, mock_git):
        mock_git.return_value = MagicMock(stdout="feature/test\n")
        assert get_current_branch(Path("/repo")) == "feature/test"

    @patch("sync.core.local_git._run_git")
    def test_get_default_branch_main(self, mock_git):
        mock_git.return_value = MagicMock(returncode=0, stdout="refs/remotes/origin/main\n")
        assert get_default_branch(Path("/repo")) == "main"

    @patch("sync.core.local_git._run_git")
    def test_get_default_branch_fallback_main(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=1, stdout=""),  # symbolic-ref fails
            MagicMock(returncode=0),  # origin/main exists
        ]
        assert get_default_branch(Path("/repo")) == "main"

    @patch("sync.core.local_git._run_git")
    def test_get_default_branch_fallback_master(self, mock_git):
        mock_git.side_effect = [
            MagicMock(returncode=1, stdout=""),  # symbolic-ref fails
            MagicMock(returncode=1),  # origin/main doesn't exist
        ]
        assert get_default_branch(Path("/repo")) == "master"

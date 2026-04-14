"""Tests for sync.commands.release — version bump, tag, release."""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from sync.commands.release import (
    _get_latest_tag,
    _get_previous_tag,
    _categorize_commits,
    _get_changelog,
    run,
)


# ---------------------------------------------------------------------------
# _get_latest_tag
# ---------------------------------------------------------------------------

class TestGetLatestTag:
    @patch("sync.commands.release._git")
    def test_finds_semver(self, mock_git):
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="1.2.7\n1.2.6\n1.2.5\nv1.2.5_1.0.0\n",
        )
        assert _get_latest_tag() == "1.2.7"

    @patch("sync.commands.release._git")
    def test_skips_combined_tags(self, mock_git):
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="v1.2.7_1.0.0\n1.2.5\n",
        )
        assert _get_latest_tag() == "1.2.5"

    @patch("sync.commands.release._git")
    def test_no_tags(self, mock_git):
        mock_git.return_value = MagicMock(returncode=1, stdout="")
        assert _get_latest_tag() is None

    @patch("sync.commands.release._git")
    def test_no_semver_tags(self, mock_git):
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="latest\nstable\n",
        )
        assert _get_latest_tag() is None


# ---------------------------------------------------------------------------
# _get_previous_tag
# ---------------------------------------------------------------------------

class TestGetPreviousTag:
    @patch("sync.commands.release._git")
    def test_finds_previous(self, mock_git):
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="1.2.7\n1.2.6\n1.2.5\n",
        )
        assert _get_previous_tag("1.2.7") == "1.2.6"

    @patch("sync.commands.release._git")
    def test_no_previous(self, mock_git):
        mock_git.return_value = MagicMock(
            returncode=0,
            stdout="1.0.0\n",
        )
        assert _get_previous_tag("1.0.0") is None


# ---------------------------------------------------------------------------
# _categorize_commits
# ---------------------------------------------------------------------------

class TestCategorizeCommits:
    def test_feat_commits(self):
        commits = ["abc1234 feat: add new feature"]
        cats = _categorize_commits(commits)
        assert any("Features" in k for k in cats)

    def test_fix_commits(self):
        commits = ["abc1234 fix: broken thing"]
        cats = _categorize_commits(commits)
        assert any("Bug Fixes" in k for k in cats)

    def test_chore_commits(self):
        commits = ["abc1234 chore: update deps"]
        cats = _categorize_commits(commits)
        assert any("Maintenance" in k for k in cats)

    def test_scoped_prefix(self):
        commits = ["abc1234 feat(cli): add flag"]
        cats = _categorize_commits(commits)
        assert any("Features" in k for k in cats)

    def test_other_category(self):
        commits = ["abc1234 random commit message"]
        cats = _categorize_commits(commits)
        assert any("Other" in k for k in cats)

    def test_multiple_categories(self):
        commits = [
            "abc1234 feat: feature",
            "def5678 fix: bugfix",
            "ghi9012 docs: update readme",
            "jkl3456 random message",
        ]
        cats = _categorize_commits(commits)
        assert len(cats) >= 3

    def test_empty_commits(self):
        cats = _categorize_commits([])
        assert cats == {}

    def test_case_insensitive(self):
        commits = ["abc1234 FEAT: uppercase feature"]
        cats = _categorize_commits(commits)
        assert any("Features" in k for k in cats)


# ---------------------------------------------------------------------------
# run (release command)
# ---------------------------------------------------------------------------

class TestReleaseCommand:
    @patch("sync.commands.release._create_github_release")
    @patch("sync.commands.release._get_changelog")
    @patch("sync.commands.release._get_latest_tag")
    def test_dry_run_with_bump(self, mock_tag, mock_cl, mock_release, capsys):
        mock_tag.return_value = "1.2.5"
        mock_cl.return_value = (["abc feat: new"], [])

        args = SimpleNamespace(part="patch", dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "1.2.6" in out
        assert "Would commit" in out

    @patch("sync.commands.release._get_latest_tag")
    def test_no_tags_exits(self, mock_tag):
        mock_tag.return_value = None
        args = SimpleNamespace(part="patch", dry_run=False)
        with pytest.raises(SystemExit):
            run(args)

    @patch("sync.commands.release._create_github_release")
    @patch("sync.commands.release._get_previous_tag")
    @patch("sync.commands.release._get_changelog")
    @patch("sync.commands.release._get_latest_tag")
    def test_release_current_tag_dry_run(self, mock_tag, mock_cl, mock_prev, mock_release, capsys):
        """No --part means release current tag without bumping."""
        mock_tag.return_value = "1.2.7"
        mock_prev.return_value = "1.2.6"
        mock_cl.return_value = (["abc fix: something"], [])

        args = SimpleNamespace(part=None, dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "1.2.7" in out
        assert "Would create GitHub Release" in out

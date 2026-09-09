"""Tests for miscellaneous commands: bump-repo-version, cleanup-branches, revert, protect-main, list-issues, migrate-mkdocs, generate-registry."""

import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from sync.core.github_api import GHAPIError


# ---------------------------------------------------------------------------
# bump-repo-version
# ---------------------------------------------------------------------------

class TestBumpRepoVersion:
    @patch("sync.commands.bump_repo_version.create_tag")
    @patch("sync.commands.bump_repo_version.get_branch_sha", return_value="abc123")
    @patch("sync.commands.bump_repo_version.get_default_branch", return_value="main")
    @patch("sync.commands.bump_repo_version.get_latest_tags")
    def test_bump_patch(self, mock_tags, mock_branch, mock_sha, mock_create, capsys):
        from sync.commands.bump_repo_version import run
        mock_tags.return_value = ["v1.2.7_1.0.0"]

        args = SimpleNamespace(part="patch", repo="org/my-repo")
        run(args)
        out = capsys.readouterr().out
        assert "v1.2.7_1.0.1" in out
        mock_create.assert_called_once()

    @patch("sync.commands.bump_repo_version.get_latest_tags")
    def test_no_combined_tags(self, mock_tags, capsys):
        from sync.commands.bump_repo_version import run
        mock_tags.return_value = ["1.0.0"]  # no combined tag

        args = SimpleNamespace(part="patch", repo="org/my-repo")
        with pytest.raises(SystemExit):
            run(args)

    def test_invalid_repo_format(self, capsys):
        from sync.commands.bump_repo_version import run
        args = SimpleNamespace(part="patch", repo="noslash")
        with pytest.raises(SystemExit):
            run(args)

    @patch("sync.commands.bump_repo_version.get_latest_tags")
    def test_tag_already_exists(self, mock_tags, capsys):
        from sync.commands.bump_repo_version import run
        mock_tags.return_value = ["v1.2.7_1.0.0", "v1.2.7_1.0.1"]

        args = SimpleNamespace(part="patch", repo="org/my-repo")
        with pytest.raises(SystemExit):
            run(args)


# ---------------------------------------------------------------------------
# cleanup-branches
# ---------------------------------------------------------------------------

class TestCleanupBranches:
    @patch("sync.commands.cleanup_branches.subprocess.run")
    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_dry_run(self, mock_load, mock_resolve, mock_subp, make_repo_entry, capsys, tmp_path):
        from sync.commands.cleanup_branches import run, _get_merged_local, _get_merged_remote
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir
        mock_subp.return_value = MagicMock(
            returncode=0,
            stdout="  sync/framework-1.2.5\n* main\n",
        )

        with patch("sync.commands.cleanup_branches._get_merged_local", return_value=["sync/framework-1.2.5"]):
            with patch("sync.commands.cleanup_branches._get_merged_remote", return_value=[]):
                args = SimpleNamespace(repo=None, dry_run=True)
                run(args)
                out = capsys.readouterr().out
                assert "DRY RUN" in out

    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_missing_clone(self, mock_load, mock_resolve, make_repo_entry, capsys, tmp_path):
        from sync.commands.cleanup_branches import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_resolve.return_value = tmp_path / "nonexistent"

        args = SimpleNamespace(repo=None, dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "not found" in out

    # ── Enumeration failure must never read as "clean" ──
    #
    # Regression guard for the false all-clear: the command reported 23 consumer
    # repos as clean while 278 stale sync/framework-* branches sat on their
    # remotes, because _get_merged_remote returned [] on failure exactly as it
    # did on genuine emptiness.

    @patch("sync.commands.cleanup_branches._resolve_default_branch", return_value="main")
    @patch("sync.commands.cleanup_branches._git")
    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_remote_enumeration_failure_is_loud_not_clean(
        self, mock_load, mock_resolve, mock_git, mock_branch,
        make_repo_entry, capsys, tmp_path,
    ):
        from sync.commands.cleanup_branches import run
        mock_load.return_value = [make_repo_entry(name="lab1")]
        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir
        mock_git.return_value = MagicMock(returncode=0, stdout="")

        with patch("sync.commands.cleanup_branches._get_merged_local", return_value=[]):
            with patch("sync.commands.cleanup_branches._get_merged_remote", return_value=None):
                args = SimpleNamespace(repo=None, dry_run=True)
                with pytest.raises(SystemExit) as exc:
                    run(args)
                assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "could not enumerate REMOTE" in out
        assert "NOT proven clean" in out
        assert "✅ clean" not in out

    @patch("sync.commands.cleanup_branches._resolve_default_branch", return_value="main")
    @patch("sync.commands.cleanup_branches._git")
    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_local_enumeration_failure_is_loud_not_clean(
        self, mock_load, mock_resolve, mock_git, mock_branch,
        make_repo_entry, capsys, tmp_path,
    ):
        from sync.commands.cleanup_branches import run
        mock_load.return_value = [make_repo_entry(name="lab1")]
        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir
        mock_git.return_value = MagicMock(returncode=0, stdout="")

        with patch("sync.commands.cleanup_branches._get_merged_local", return_value=None):
            with patch("sync.commands.cleanup_branches._get_merged_remote", return_value=[]):
                args = SimpleNamespace(repo=None, dry_run=True)
                with pytest.raises(SystemExit) as exc:
                    run(args)
                assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "could not enumerate LOCAL" in out
        assert "✅ clean" not in out

    @patch("sync.commands.cleanup_branches._resolve_default_branch", return_value="main")
    @patch("sync.commands.cleanup_branches._git")
    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_clean_only_when_enumeration_succeeded(
        self, mock_load, mock_resolve, mock_git, mock_branch,
        make_repo_entry, capsys, tmp_path,
    ):
        from sync.commands.cleanup_branches import run
        mock_load.return_value = [make_repo_entry(name="lab1")]
        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir
        mock_git.return_value = MagicMock(returncode=0, stdout="")

        with patch("sync.commands.cleanup_branches._get_merged_local", return_value=[]):
            with patch("sync.commands.cleanup_branches._get_merged_remote", return_value=[]):
                args = SimpleNamespace(repo=None, dry_run=True)
                run(args)  # must NOT exit non-zero
        out = capsys.readouterr().out
        assert "✅ clean" in out
        assert "could not enumerate" not in out

    @patch("sync.commands.cleanup_branches._resolve_default_branch", return_value=None)
    @patch("sync.commands.cleanup_branches._resolve_repo_path")
    @patch("sync.commands.cleanup_branches.load_repos")
    def test_undeterminable_default_branch_is_loud(
        self, mock_load, mock_resolve, mock_branch, make_repo_entry, capsys, tmp_path,
    ):
        from sync.commands.cleanup_branches import run
        mock_load.return_value = [make_repo_entry(name="lab1")]
        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir

        args = SimpleNamespace(repo=None, dry_run=True)
        with pytest.raises(SystemExit) as exc:
            run(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "could not determine the default branch" in out
        assert "✅ clean" not in out

    # ── The fetch must not trust the clone's configured refspec ──

    @patch("sync.commands.cleanup_branches._git")
    def test_fetch_passes_full_refspec_explicitly(self, mock_git, tmp_path):
        from sync.commands.cleanup_branches import _fetch_all_heads, ALL_HEADS_REFSPEC
        mock_git.return_value = MagicMock(returncode=0, stdout="")

        assert _fetch_all_heads(tmp_path) is True
        args = mock_git.call_args[0][1]
        assert args[0] == "fetch"
        assert ALL_HEADS_REFSPEC in args, (
            "must pass +refs/heads/*:refs/remotes/origin/* on the command line — "
            "a single-branch clone configures a narrow remote.origin.fetch"
        )

    @patch("sync.commands.cleanup_branches._fetch_all_heads", return_value=False)
    def test_get_merged_remote_none_when_fetch_fails(self, mock_fetch, tmp_path):
        from sync.commands.cleanup_branches import _get_merged_remote
        assert _get_merged_remote(tmp_path, "main") is None

    @patch("sync.commands.cleanup_branches._fetch_all_heads", return_value=True)
    @patch("sync.commands.cleanup_branches._git")
    def test_get_merged_remote_none_when_branch_cmd_fails(self, mock_git, mock_fetch, tmp_path):
        from sync.commands.cleanup_branches import _get_merged_remote
        # rev-parse of origin/main succeeds, `branch -r --merged` fails
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout="sha"),
            MagicMock(returncode=128, stdout=""),
        ]
        assert _get_merged_remote(tmp_path, "main") is None

    @patch("sync.commands.cleanup_branches._fetch_all_heads", return_value=True)
    @patch("sync.commands.cleanup_branches._git")
    def test_get_merged_remote_none_when_ref_missing(self, mock_git, mock_fetch, tmp_path):
        from sync.commands.cleanup_branches import _get_merged_remote
        mock_git.return_value = MagicMock(returncode=128, stdout="")
        assert _get_merged_remote(tmp_path, "main") is None

    @patch("sync.commands.cleanup_branches._fetch_all_heads", return_value=True)
    @patch("sync.commands.cleanup_branches._git")
    def test_get_merged_remote_strips_and_filters(self, mock_git, mock_fetch, tmp_path):
        from sync.commands.cleanup_branches import _get_merged_remote
        mock_git.side_effect = [
            MagicMock(returncode=0, stdout="sha"),
            MagicMock(returncode=0, stdout=(
                "  origin/HEAD -> origin/main\n"
                "  origin/main\n"
                "  origin/gh-pages\n"
                "  origin/sync/framework-1.9.5\n"
                "  origin/sync/framework-1.10.1\n"
            )),
        ]
        assert _get_merged_remote(tmp_path, "main") == [
            "sync/framework-1.9.5", "sync/framework-1.10.1",
        ]

    @patch("sync.commands.cleanup_branches._git")
    def test_get_merged_local_none_on_git_failure(self, mock_git, tmp_path):
        from sync.commands.cleanup_branches import _get_merged_local
        mock_git.return_value = MagicMock(returncode=128, stdout="")
        assert _get_merged_local(tmp_path, "main") is None

    @patch("sync.commands.cleanup_branches._git")
    def test_get_merged_local_empty_is_a_real_answer(self, mock_git, tmp_path):
        from sync.commands.cleanup_branches import _get_merged_local
        mock_git.return_value = MagicMock(returncode=0, stdout="* main\n")
        assert _get_merged_local(tmp_path, "main") == []

    # ── Default branch is resolved, not assumed to be "main" ──

    @patch("sync.commands.cleanup_branches._git")
    def test_default_branch_from_symbolic_ref(self, mock_git, tmp_path):
        from sync.commands.cleanup_branches import _resolve_default_branch
        mock_git.return_value = MagicMock(returncode=0, stdout="refs/remotes/origin/master\n")
        assert _resolve_default_branch(tmp_path) == "master"

    @patch("sync.commands.cleanup_branches._git")
    def test_default_branch_falls_back_to_master(self, mock_git, tmp_path):
        from sync.commands.cleanup_branches import _resolve_default_branch
        mock_git.side_effect = [
            MagicMock(returncode=128, stdout=""),   # symbolic-ref fails
            MagicMock(returncode=128, stdout=""),   # origin/main missing
            MagicMock(returncode=0, stdout="sha"),  # origin/master exists
        ]
        assert _resolve_default_branch(tmp_path) == "master"

    @patch("sync.commands.cleanup_branches._git")
    def test_default_branch_none_when_undeterminable(self, mock_git, tmp_path):
        from sync.commands.cleanup_branches import _resolve_default_branch
        mock_git.return_value = MagicMock(returncode=128, stdout="")
        assert _resolve_default_branch(tmp_path) is None


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------

class TestRevert:
    @patch("sync.commands.revert.subprocess.run")
    @patch("sync.commands.revert._resolve_repo_path")
    @patch("sync.commands.revert.filter_sync_targets")
    @patch("sync.commands.revert.load_repos")
    def test_clean_repo(self, mock_load, mock_filter, mock_resolve, mock_subp,
                         make_repo_entry, capsys, tmp_path):
        from sync.commands.revert import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]

        repo_dir = tmp_path / "lab1"
        repo_dir.mkdir()
        mock_resolve.return_value = repo_dir
        mock_subp.return_value = MagicMock(returncode=0, stdout="")

        args = SimpleNamespace(repo=None)
        run(args)
        out = capsys.readouterr().out
        assert "clean" in out

    @patch("sync.commands.revert._resolve_repo_path")
    @patch("sync.commands.revert.filter_sync_targets")
    @patch("sync.commands.revert.load_repos")
    def test_missing_clone(self, mock_load, mock_filter, mock_resolve,
                            make_repo_entry, capsys, tmp_path):
        from sync.commands.revert import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_filter.return_value = [repo]
        mock_resolve.return_value = tmp_path / "nonexistent"

        args = SimpleNamespace(repo=None)
        run(args)
        out = capsys.readouterr().out
        assert "not found" in out


# ---------------------------------------------------------------------------
# protect-main
# ---------------------------------------------------------------------------

class TestProtectMain:
    @patch("sync.commands.protect_main.subprocess.run")
    @patch("sync.commands.protect_main.load_repos")
    def test_dry_run(self, mock_load, mock_subp, make_repo_entry, capsys):
        from sync.commands.protect_main import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        # First call = _get_default_branch (returns "main"), second = _get_protection (fails → unprotected)
        mock_subp.side_effect = [
            MagicMock(returncode=0, stdout="main\n"),  # _get_default_branch
            MagicMock(returncode=1, stdout="", stderr=""),  # _get_protection
        ]

        args = SimpleNamespace(repo=None, branch=None, check=None, dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "would apply" in out

    @patch("sync.commands.protect_main.subprocess.run")
    @patch("sync.commands.protect_main.load_repos")
    def test_default_branch_resolved_not_hardcoded(self, mock_load, mock_subp, make_repo_entry, capsys):
        """Default branch is fetched from GitHub, not assumed to be 'main'."""
        from sync.commands.protect_main import run, DEFAULT_CONTEXTS
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_subp.side_effect = [
            MagicMock(returncode=0, stdout="master\n"),   # _get_default_branch → "master"
            MagicMock(returncode=1, stdout="", stderr=""),  # _get_protection → unprotected
            MagicMock(returncode=0, stdout="{}", stderr=""),  # _protect → success
        ]

        args = SimpleNamespace(repo=None, branch=None, check=None, dry_run=False)
        run(args)

        # The PUT call must hit "master", not "main"
        put_call = mock_subp.call_args_list[2]
        url_part = next(s for s in put_call.args[0] if "branches" in s)
        assert "/master/" in url_part
        # Default contexts unchanged
        payload = json.loads(put_call.kwargs["input"])
        assert payload["required_status_checks"]["contexts"] == DEFAULT_CONTEXTS

    @patch("sync.commands.protect_main.subprocess.run")
    @patch("sync.commands.protect_main.load_repos")
    def test_custom_contexts_passed_through(self, mock_load, mock_subp, make_repo_entry, capsys):
        """--check contexts replace the default; payload reflects them exactly."""
        from sync.commands.protect_main import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_subp.side_effect = [
            MagicMock(returncode=0, stdout="main\n"),    # _get_default_branch
            MagicMock(returncode=1, stdout="", stderr=""),  # _get_protection → unprotected
            MagicMock(returncode=0, stdout="{}", stderr=""),  # _protect
        ]

        args = SimpleNamespace(repo=None, branch=None, check=["unit-tests"], dry_run=False)
        run(args)

        put_call = mock_subp.call_args_list[2]
        payload = json.loads(put_call.kwargs["input"])
        assert payload["required_status_checks"]["contexts"] == ["unit-tests"]

    @patch("sync.commands.protect_main.subprocess.run")
    @patch("sync.commands.protect_main.load_repos")
    def test_training_repo_path_unchanged(self, mock_load, mock_subp, make_repo_entry, capsys):
        """Training repos still use default branch + default context when no overrides given."""
        from sync.commands.protect_main import run, DEFAULT_CONTEXTS
        repo = make_repo_entry(name="k8s-lab", repo="dynatrace-wwse/k8s-lab")
        mock_load.return_value = [repo]
        mock_subp.side_effect = [
            MagicMock(returncode=0, stdout="main\n"),      # _get_default_branch
            MagicMock(returncode=1, stdout="", stderr=""),  # _get_protection → unprotected
            MagicMock(returncode=0, stdout="{}", stderr=""),  # _protect
        ]

        args = SimpleNamespace(repo=None, branch=None, check=None, dry_run=False)
        run(args)

        put_call = mock_subp.call_args_list[2]
        url_part = next(s for s in put_call.args[0] if "branches" in s)
        assert "/main/" in url_part
        payload = json.loads(put_call.kwargs["input"])
        assert payload["required_status_checks"]["contexts"] == DEFAULT_CONTEXTS
        assert payload["enforce_admins"] is True
        assert payload["allow_deletions"] is False

    @patch("sync.commands.protect_main.subprocess.run")
    @patch("sync.commands.protect_main.load_repos")
    def test_out_of_yaml_repo_accepted(self, mock_load, mock_subp, make_repo_entry, capsys):
        """owner/name not in repos.yaml is accepted as a synthetic entry."""
        from sync.commands.protect_main import run
        mock_load.return_value = []  # nothing in yaml
        mock_subp.side_effect = [
            MagicMock(returncode=0, stdout="main\n"),      # _get_default_branch
            MagicMock(returncode=1, stdout="", stderr=""),  # _get_protection → unprotected
            MagicMock(returncode=0, stdout="{}", stderr=""),  # _protect
        ]

        args = SimpleNamespace(repo="dynatrace-wwse/dynatrace-app-enablements",
                               branch=None, check=["unit-tests"], dry_run=False)
        run(args)

        put_call = mock_subp.call_args_list[2]
        url_part = next(s for s in put_call.args[0] if "branches" in s)
        assert "dynatrace-wwse/dynatrace-app-enablements" in url_part


# ---------------------------------------------------------------------------
# list-issues
# ---------------------------------------------------------------------------

class TestListIssues:
    @patch("sync.commands.list_issues._get_issues")
    @patch("sync.commands.list_issues.load_repos")
    def test_no_issues(self, mock_load, mock_issues, make_repo_entry, capsys):
        from sync.commands.list_issues import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_issues.return_value = []

        args = SimpleNamespace(repo=None, label=None)
        run(args)
        out = capsys.readouterr().out
        assert "No open issues" in out

    @patch("sync.commands.list_issues._get_issues")
    @patch("sync.commands.list_issues.load_repos")
    def test_with_issues(self, mock_load, mock_issues, make_repo_entry, capsys):
        from sync.commands.list_issues import run
        repo = make_repo_entry(name="lab1")
        mock_load.return_value = [repo]
        mock_issues.return_value = [
            {"number": 1, "title": "Bug report", "url": "http://issue/1",
             "labels": [{"name": "bug"}], "author": {"login": "user1"},
             "createdAt": "2024-01-15T00:00:00Z"},
        ]

        args = SimpleNamespace(repo=None, label=None)
        run(args)
        out = capsys.readouterr().out
        assert "#1" in out
        assert "Bug report" in out
        assert "1 open issues" in out


# ---------------------------------------------------------------------------
# migrate-mkdocs
# ---------------------------------------------------------------------------

class TestMigrateMkdocs:
    @patch("sync.commands.migrate_mkdocs.get_file_content")
    def test_already_migrated(self, mock_content, capsys):
        from sync.commands.migrate_mkdocs import run
        mock_content.return_value = "INHERIT: mkdocs-base.yaml\nsite_name: test"

        args = SimpleNamespace(repo="org/my-repo", dry_run=False)
        run(args)
        out = capsys.readouterr().out
        assert "already using INHERIT" in out

    @patch("sync.commands.migrate_mkdocs.get_file_content")
    def test_dry_run(self, mock_content, capsys):
        from sync.commands.migrate_mkdocs import run
        mock_content.return_value = "site_name: My Lab\nrepo_name: my-lab\nnav:\n  - Home: index.md\n"

        args = SimpleNamespace(repo="org/my-repo", dry_run=True)
        run(args)
        out = capsys.readouterr().out
        assert "Would migrate" in out
        assert "INHERIT: mkdocs-base.yaml" in out

    @patch("sync.commands.migrate_mkdocs.get_file_content")
    def test_api_error(self, mock_content, capsys):
        from sync.commands.migrate_mkdocs import run
        mock_content.side_effect = GHAPIError("contents", 404, "Not Found")

        args = SimpleNamespace(repo="org/my-repo", dry_run=False)
        with pytest.raises(SystemExit):
            run(args)

    def test_invalid_repo_format(self, capsys):
        from sync.commands.migrate_mkdocs import run
        args = SimpleNamespace(repo="noslash", dry_run=False)
        with pytest.raises(SystemExit):
            run(args)


# ---------------------------------------------------------------------------
# generate-registry
# ---------------------------------------------------------------------------

class TestGenerateRegistry:
    @patch("sync.commands.generate_registry.get_latest_tags")
    @patch("sync.commands.generate_registry.load_repos")
    def test_generates_html(self, mock_load, mock_tags, make_repo_entry, tmp_path, capsys):
        from sync.commands.generate_registry import run
        repos = [make_repo_entry(name="lab1", repo="org/lab1")]
        mock_load.return_value = repos
        mock_tags.return_value = ["v1.2.7_1.0.0"]

        out_path = tmp_path / "registry.html"
        args = SimpleNamespace(output=str(out_path))
        run(args)

        content = out_path.read_text()
        assert "lab1" in content
        assert "REGISTRY-START" in content
        assert "REGISTRY-END" in content
        assert "Generated registry" in capsys.readouterr().out

    @patch("sync.commands.generate_registry.get_latest_tags")
    @patch("sync.commands.generate_registry.load_repos")
    def test_injects_into_existing(self, mock_load, mock_tags, make_repo_entry, tmp_path, capsys):
        from sync.commands.generate_registry import run
        repos = [make_repo_entry(name="lab1", repo="org/lab1")]
        mock_load.return_value = repos
        mock_tags.return_value = []

        out_path = tmp_path / "registry.html"
        out_path.write_text(
            "<html><!-- REGISTRY-START -->old<!-- REGISTRY-END --></html>"
        )

        args = SimpleNamespace(output=str(out_path))
        run(args)

        content = out_path.read_text()
        assert "old" not in content
        assert "lab1" in content
        assert "Updated registry" in capsys.readouterr().out

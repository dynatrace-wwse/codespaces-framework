"""sync checkout — Checkout main and show status across repos."""

import subprocess
import sys
from pathlib import Path

from sync.core.repos import load_repos, filter_sync_targets
from sync.core.local_git import get_repo_path, pull_main, get_current_branch, GitError


def _repo_status(repo_path: Path) -> str:
    """Get short git status summary."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True,
    )
    lines = [l for l in result.stdout.strip().split("\n") if l]
    if not lines:
        return "clean"
    return f"{len(lines)} changed file(s)"


def _last_commit(repo_path: Path) -> str:
    """Get one-line last commit info."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%h %s (%cr)"],
        cwd=repo_path, capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def run(args):
    target_repo = getattr(args, "repo", None)
    pull = getattr(args, "pull", False)

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"❌ '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = filter_sync_targets(repos)

    print(f"Checking out {len(repos)} repos\n")

    ok = 0
    missing = 0
    errors = 0

    for entry in repos:
        repo_path = get_repo_path(entry.repo_name)
        print(f"── {entry.url} ──")

        if not repo_path.is_dir() or not (repo_path / ".git").is_dir():
            print(f"  📭 not cloned — run sync clone first")
            missing += 1
            print()
            continue

        # Checkout main and optionally pull
        if pull:
            result = pull_main(repo_path)
            if not result.success:
                print(f"  ❌ {result.message}")
                errors += 1
                print()
                continue
            print(f"  🔄 {result.message}")
        else:
            # Just checkout default branch without pulling
            try:
                subprocess.run(
                    ["git", "checkout", "main"],
                    cwd=repo_path, capture_output=True, text=True,
                )
            except Exception:
                pass

        branch = get_current_branch(repo_path)
        status = _repo_status(repo_path)
        commit = _last_commit(repo_path)

        print(f"  📍 branch: {branch}")
        print(f"  📋 status: {status}")
        print(f"  📝 last commit: {commit}")

        ok += 1
        print()

    print(f"📊 {ok} checked out, {missing} not cloned, {errors} errors")

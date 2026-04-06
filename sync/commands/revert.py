"""sync revert — Revert uncommitted changes in repos.

Undoes migrate or any other uncommitted local changes by running
git checkout -- . and git clean -fd on each repo.
"""

import subprocess
import sys
from pathlib import Path

from sync.core.repos import load_repos, filter_sync_targets
from sync.commands.migrate import _resolve_repo_path


def _revert_repo(repo_path: Path) -> tuple[bool, str]:
    """Revert a repo to its last committed state. Returns (success, message)."""
    # Check for uncommitted changes first
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True,
    )
    changes = result.stdout.strip()
    if not changes:
        return True, "clean — nothing to revert"

    change_count = len(changes.splitlines())

    # Revert tracked files
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=repo_path, capture_output=True, text=True,
    )

    # Remove untracked files/dirs created by migrate
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=repo_path, capture_output=True, text=True,
    )

    return True, f"reverted {change_count} changes"


def run(args):
    target_repo = getattr(args, "repo", None)

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"x '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = filter_sync_targets(repos)

    print(f"Reverting {len(repos)} repos\n")

    for entry in repos:
        repo_path = _resolve_repo_path(entry.repo_name)
        print(f"── {entry.repo} ──")

        if not repo_path.is_dir():
            print(f"  ⊘ local clone not found")
            print()
            continue

        success, msg = _revert_repo(repo_path)
        print(f"  {msg}")
        print()

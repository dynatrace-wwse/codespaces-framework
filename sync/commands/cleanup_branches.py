"""sync cleanup-branches — Delete merged branches across repos.

Deletes local and remote branches that have been merged into main.
Skips main, master, and gh-pages branches.
"""

import subprocess
import sys
from pathlib import Path

from sync.core.repos import load_repos, filter_sync_targets
from sync.commands.migrate import _resolve_repo_path


PROTECTED_BRANCHES = {"main", "master", "gh-pages"}


def _get_merged_local(repo_path: Path) -> list[str]:
    """Get local branches merged into main."""
    result = subprocess.run(
        ["git", "branch", "--merged", "main"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    branches = []
    for line in result.stdout.strip().split("\n"):
        branch = line.strip().lstrip("* ")
        if branch and branch not in PROTECTED_BRANCHES:
            branches.append(branch)
    return branches


def _get_merged_remote(repo_path: Path) -> list[str]:
    """Get remote branches merged into origin/main."""
    # Fetch and prune first
    subprocess.run(
        ["git", "fetch", "-p"],
        cwd=repo_path, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["git", "branch", "-r", "--merged", "origin/main"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    branches = []
    for line in result.stdout.strip().split("\n"):
        branch = line.strip()
        if not branch:
            continue
        # Strip origin/ prefix
        if branch.startswith("origin/"):
            name = branch[len("origin/"):]
        else:
            continue
        if name not in PROTECTED_BRANCHES and "->" not in branch:
            branches.append(name)
    return branches


def _delete_local(repo_path: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "branch", "-d", branch],
        cwd=repo_path, capture_output=True, text=True,
    )
    return result.returncode == 0


def _delete_remote(repo_path: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        cwd=repo_path, capture_output=True, text=True,
    )
    return result.returncode == 0


def run(args):
    target_repo = getattr(args, "repo", None)
    dry_run = args.dry_run

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"❌ '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = filter_sync_targets(repos)

    print(f"{'[DRY RUN] ' if dry_run else ''}Cleaning up merged branches across {len(repos)} repos\n")

    total_local = 0
    total_remote = 0

    for entry in repos:
        repo_path = _resolve_repo_path(entry.repo_name)
        print(f"── {entry.repo} ──")

        if not repo_path.is_dir():
            print(f"  📭 local clone not found")
            print()
            continue

        # Make sure we're on main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=repo_path, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=repo_path, capture_output=True, text=True,
        )

        local = _get_merged_local(repo_path)
        remote = _get_merged_remote(repo_path)

        if not local and not remote:
            print(f"  ✅ clean — no merged branches to delete")
            print()
            continue

        if local:
            print(f"  🗑️  local ({len(local)}):")
            for branch in local:
                if dry_run:
                    print(f"    ⏳ {branch}")
                else:
                    ok = _delete_local(repo_path, branch)
                    print(f"    {'🗑️' if ok else '❌'}  {branch}")
                total_local += 1

        if remote:
            print(f"  🗑️  remote ({len(remote)}):")
            for branch in remote:
                if dry_run:
                    print(f"    ⏳ origin/{branch}")
                else:
                    ok = _delete_remote(repo_path, branch)
                    print(f"    {'🗑️' if ok else '❌'}  origin/{branch}")
                total_remote += 1

        print()

    action = "Would delete" if dry_run else "Deleted"
    print(f"📊 {action} {total_local} local + {total_remote} remote branches")

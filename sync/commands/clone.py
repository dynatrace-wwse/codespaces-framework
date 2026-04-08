"""sync clone — Clone all repos from repos.yaml that aren't local yet."""

import sys

from sync.core.repos import load_repos, filter_sync_targets
from sync.core.local_git import ensure_cloned, get_repo_path, GitError


def run(args):
    target_repo = getattr(args, "repo", None)
    clone_all = getattr(args, "clone_all", False)

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"❌ '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    elif not clone_all:
        repos = filter_sync_targets(repos)

    print(f"Cloning {len(repos)} repos\n")

    cloned = 0
    skipped = 0
    errors = 0

    for entry in repos:
        path = get_repo_path(entry.repo_name)

        if path.is_dir() and (path / ".git").is_dir():
            print(f"  ✅ {entry.url} — already cloned")
            skipped += 1
            continue

        try:
            ensure_cloned(entry.owner, entry.repo_name)
            print(f"  📥 {entry.url} — cloned")
            cloned += 1
        except GitError as e:
            print(f"  ❌ {entry.url} — {e.message}")
            errors += 1

    print(f"\n📊 {cloned} cloned, {skipped} already local, {errors} errors")

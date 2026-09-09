"""sync cleanup-branches — Delete merged branches across repos.

Deletes local and remote branches that have been merged into the default branch.
Skips main, master, and gh-pages branches.

⚠️ Enumeration failure is NOT emptiness. Every helper here returns ``None`` when it
could not determine an answer, and an empty list ONLY when it looked and found
nothing. The two were previously indistinguishable: ``_get_merged_remote`` returned
``[]`` both when a repo had no merged remote branches and when the git call failed
outright, and the caller printed "✅ clean — no merged branches to delete" either way.

That false all-clear hid a real backlog. The clones this command runs against are
single-branch::

    $ git config --get-all remote.origin.fetch
    +refs/heads/main:refs/remotes/origin/main

so ``git branch -r --merged origin/main`` could only ever return ``origin/main``
itself. The command reported every consumer repo as clean while 278 stale
``sync/framework-*`` branches sat on their remotes. Hence ``_fetch_all_heads``, which
passes a full refspec explicitly rather than trusting whatever the clone was
configured with.
"""

import subprocess
import sys
from pathlib import Path

from sync.core.repos import load_repos, filter_sync_targets
from sync.commands.migrate import _resolve_repo_path


PROTECTED_BRANCHES = {"main", "master", "gh-pages"}

# Full refspec, passed on the command line so it does NOT depend on
# remote.origin.fetch — a single-branch clone configures a narrow one.
ALL_HEADS_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def _git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=repo_path, capture_output=True, text=True,
    )


def _resolve_default_branch(repo_path: Path) -> str | None:
    """Return the repo's default branch, or None if it cannot be determined.

    Never guesses. The old code hardcoded "main", so on a master-based repo every
    `--merged main` call failed and read as "clean".
    """
    result = _git(repo_path, ["symbolic-ref", "refs/remotes/origin/HEAD"])
    if result.returncode == 0 and result.stdout.strip():
        name = result.stdout.strip().rsplit("/", 1)[-1]
        if name:
            return name
    for candidate in ("main", "master"):
        if _git(repo_path, ["rev-parse", "--verify", f"origin/{candidate}"]).returncode == 0:
            return candidate
    return None


def _fetch_all_heads(repo_path: Path) -> bool:
    """Fetch every remote head with an explicit full refspec. False on failure."""
    return _git(repo_path, ["fetch", "origin", ALL_HEADS_REFSPEC, "--prune"]).returncode == 0


def _get_merged_local(repo_path: Path, default_branch: str) -> list[str] | None:
    """Local branches merged into the default branch. None if git failed."""
    result = _git(repo_path, ["branch", "--merged", default_branch])
    if result.returncode != 0:
        return None
    branches = []
    for line in result.stdout.strip().split("\n"):
        branch = line.strip().lstrip("* ").strip()
        if branch and branch not in PROTECTED_BRANCHES:
            branches.append(branch)
    return branches


def _get_merged_remote(repo_path: Path, default_branch: str) -> list[str] | None:
    """Remote branches merged into origin/<default_branch>. None if it could not look.

    Returns None — never [] — when the fetch fails or the ref is missing, so the
    caller can say "could not enumerate" instead of "clean".
    """
    if not _fetch_all_heads(repo_path):
        return None
    ref = f"origin/{default_branch}"
    if _git(repo_path, ["rev-parse", "--verify", ref]).returncode != 0:
        return None
    result = _git(repo_path, ["branch", "-r", "--merged", ref])
    if result.returncode != 0:
        return None
    branches = []
    for line in result.stdout.strip().split("\n"):
        branch = line.strip()
        if not branch or "->" in branch:
            continue
        if not branch.startswith("origin/"):
            continue
        name = branch[len("origin/"):]
        if name and name not in PROTECTED_BRANCHES:
            branches.append(name)
    return branches


def _delete_local(repo_path: Path, branch: str) -> bool:
    return _git(repo_path, ["branch", "-d", branch]).returncode == 0


def _delete_remote(repo_path: Path, branch: str) -> bool:
    return _git(repo_path, ["push", "origin", "--delete", branch]).returncode == 0


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
        repos = [r for r in repos if r.status == "active"]

    print(f"{'[DRY RUN] ' if dry_run else ''}Cleaning up merged branches across {len(repos)} repos\n")

    total_local = 0
    total_remote = 0
    total_failed = 0
    unreadable = []

    for entry in repos:
        repo_path = _resolve_repo_path(entry.repo_name)
        print(f"── {entry.url} ──")

        if not repo_path.is_dir():
            print(f"  📭 local clone not found")
            print()
            continue

        default_branch = _resolve_default_branch(repo_path)
        if default_branch is None:
            print(f"  ⚠️  could not determine the default branch — SKIPPED, not clean")
            unreadable.append(f"{entry.repo_name} (default branch)")
            print()
            continue

        _git(repo_path, ["checkout", default_branch])
        _git(repo_path, ["pull", "origin", default_branch])

        local = _get_merged_local(repo_path, default_branch)
        remote = _get_merged_remote(repo_path, default_branch)

        if local is None:
            print(f"  ⚠️  could not enumerate LOCAL branches — SKIPPED, not clean")
            unreadable.append(f"{entry.repo_name} (local)")
        if remote is None:
            print(f"  ⚠️  could not enumerate REMOTE branches — SKIPPED, not clean")
            unreadable.append(f"{entry.repo_name} (remote)")
        if local is None or remote is None:
            print()
            continue

        if not local and not remote:
            print(f"  ✅ clean — no merged branches to delete (branch: {default_branch})")
            print()
            continue

        if local:
            print(f"  🗑️  local ({len(local)}):")
            for branch in local:
                if dry_run:
                    print(f"    ⏳ {branch}")
                    total_local += 1
                else:
                    ok = _delete_local(repo_path, branch)
                    print(f"    {'🗑️' if ok else '❌'}  {branch}")
                    if ok:
                        total_local += 1
                    else:
                        total_failed += 1

        if remote:
            print(f"  🗑️  remote ({len(remote)}):")
            for branch in remote:
                if dry_run:
                    print(f"    ⏳ origin/{branch}")
                    total_remote += 1
                else:
                    ok = _delete_remote(repo_path, branch)
                    print(f"    {'🗑️' if ok else '❌'}  origin/{branch}")
                    if ok:
                        total_remote += 1
                    else:
                        total_failed += 1

        print()

    action = "Would delete" if dry_run else "Deleted"
    print(f"📊 {action} {total_local} local + {total_remote} remote branches")
    if total_failed:
        print(f"❌ {total_failed} deletion(s) failed")
    if unreadable:
        print(f"⚠️  {len(unreadable)} repo(s) could not be read — NOT proven clean:")
        for item in unreadable:
            print(f"     {item}")
    if unreadable or total_failed:
        sys.exit(1)

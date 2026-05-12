"""Local git operations for sync workflows.

All sync operations follow a local-first pattern:
  1. Ensure the repo is cloned locally (sibling to codespaces-framework)
  2. Pull latest main
  3. Create a working branch
  4. Make changes (migrate, version bump, etc.)
  5. Commit and push
  6. Create PR via gh CLI

On failure at any step, the branch is left in place for manual intervention.
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Base directory: parent of codespaces-framework (e.g. /home/ubuntu/enablement-framework/)
REPOS_BASE = Path(__file__).parent.parent.parent.parent


@dataclass
class GitResult:
    success: bool
    message: str
    path: Optional[Path] = None
    branch: Optional[str] = None
    needs_manual: bool = False


def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the given directory."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(args, cwd, result.stderr.strip())
    return result


class GitError(Exception):
    def __init__(self, cmd: list[str], cwd: Path, message: str):
        self.cmd = cmd
        self.cwd = cwd
        self.message = message
        super().__init__(f"git {' '.join(cmd)} in {cwd}: {message}")


def get_repo_path(repo_name: str) -> Path:
    """Get the expected local path for a repo."""
    return REPOS_BASE / repo_name


def ensure_cloned(owner: str, repo_name: str) -> Path:
    """Clone the repo if not already present. Returns the local path."""
    path = get_repo_path(repo_name)
    if path.is_dir() and (path / ".git").is_dir():
        return path

    url = f"https://github.com/{owner}/{repo_name}.git"
    print(f"  Cloning {owner}/{repo_name}...")
    _run_git(["clone", url, str(path)], cwd=REPOS_BASE)
    return path


def pull_main(path: Path) -> GitResult:
    """Checkout main/master and pull latest. Returns result with conflict info."""
    # Detect default branch
    result = _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], path, check=False)
    if result.returncode == 0:
        default_branch = result.stdout.strip().split("/")[-1]
    else:
        # Fallback: try main, then master
        check = _run_git(["rev-parse", "--verify", "origin/main"], path, check=False)
        default_branch = "main" if check.returncode == 0 else "master"

    # Stash any uncommitted changes
    _run_git(["stash", "--include-untracked"], path, check=False)

    # Checkout and pull
    _run_git(["checkout", default_branch], path)
    pull = _run_git(["pull", "origin", default_branch], path, check=False)

    if pull.returncode != 0:
        return GitResult(
            success=False,
            message=f"pull failed: {pull.stderr.strip()}",
            path=path,
            needs_manual=True,
        )

    return GitResult(success=True, message=f"up to date on {default_branch}", path=path)


def create_branch(path: Path, branch_name: str, force: bool = False) -> GitResult:
    """Create and checkout a new branch from current HEAD."""
    # Check if branch already exists locally
    local = _run_git(["rev-parse", "--verify", branch_name], path, check=False)
    if local.returncode == 0:
        if force:
            # Delete and recreate from current HEAD
            _run_git(["branch", "-D", branch_name], path, check=False)
        else:
            _run_git(["checkout", branch_name], path)
            return GitResult(
                success=True,
                message=f"checked out existing branch {branch_name}",
                path=path,
                branch=branch_name,
            )

    # Check if branch exists on remote
    remote = _run_git(["ls-remote", "--heads", "origin", branch_name], path, check=False)
    if remote.stdout.strip():
        if force:
            # Delete remote branch so we can push fresh
            _run_git(["push", "origin", "--delete", branch_name], path, check=False)
        else:
            return GitResult(
                success=False,
                message=f"branch {branch_name} already exists on remote (PR may be open)",
                path=path,
                branch=branch_name,
            )

    _run_git(["checkout", "-b", branch_name], path)
    return GitResult(success=True, message=f"created {branch_name}", path=path, branch=branch_name)


def has_changes(path: Path) -> bool:
    """Check if there are staged or unstaged changes."""
    result = _run_git(["status", "--porcelain"], path, check=False)
    return bool(result.stdout.strip())


def commit(path: Path, message: str) -> GitResult:
    """Stage all changes and commit."""
    _run_git(["add", "-A"], path)

    # Check if there's anything to commit
    result = _run_git(["diff", "--cached", "--quiet"], path, check=False)
    if result.returncode == 0:
        return GitResult(success=True, message="no changes to commit", path=path)

    _run_git(["commit", "-m", message], path)
    return GitResult(success=True, message=f"committed: {message}", path=path)


def push(path: Path, branch_name: str) -> GitResult:
    """Push branch to origin."""
    result = _run_git(["push", "-u", "origin", branch_name], path, check=False)
    if result.returncode != 0:
        return GitResult(
            success=False,
            message=f"push failed: {result.stderr.strip()}",
            path=path,
            branch=branch_name,
            needs_manual=True,
        )
    return GitResult(success=True, message=f"pushed {branch_name}", path=path, branch=branch_name)


def create_pr(owner: str, repo_name: str, path: Path, title: str, body: str, base: str = "main") -> GitResult:
    """Create a PR via gh CLI."""
    # Detect current branch for --head so gh doesn't abort when it can't infer upstream
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path, capture_output=True, text=True,
    )
    head_branch = branch_result.stdout.strip() or ""
    cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--base", base]
    if head_branch:
        cmd += ["--head", head_branch]
    result = subprocess.run(cmd, cwd=path, capture_output=True, text=True)
    if result.returncode != 0:
        return GitResult(
            success=False,
            message=f"PR creation failed: {result.stderr.strip()}",
            path=path,
            needs_manual=True,
        )
    pr_url = result.stdout.strip()
    return GitResult(success=True, message=pr_url, path=path)


def enable_auto_merge(owner: str, repo_name: str, pr_url: str) -> None:
    """Enable auto-merge on a PR. Best-effort, no error on failure."""
    # Extract PR number from URL
    try:
        pr_number = pr_url.rstrip("/").split("/")[-1]
        subprocess.run(
            ["gh", "pr", "merge", pr_number, "--auto", "--merge", "-R", f"{owner}/{repo_name}"],
            capture_output=True,
            text=True,
        )
    except Exception:
        pass


def get_current_branch(path: Path) -> str:
    """Get the current branch name."""
    result = _run_git(["branch", "--show-current"], path)
    return result.stdout.strip()


def get_default_branch(path: Path) -> str:
    """Detect the default branch (main or master)."""
    result = _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], path, check=False)
    if result.returncode == 0:
        return result.stdout.strip().split("/")[-1]
    check = _run_git(["rev-parse", "--verify", "origin/main"], path, check=False)
    return "main" if check.returncode == 0 else "master"

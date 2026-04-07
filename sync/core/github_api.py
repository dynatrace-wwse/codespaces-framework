"""GitHub API wrapper for sync operations."""

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass
class PRResult:
    repo: str
    url: str
    number: int
    status: str  # "created", "skipped", "error"
    message: str = ""


def get_token() -> str:
    """Get SYNC_TOKEN from env, falling back to gh auth token."""
    token = os.environ.get("SYNC_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: SYNC_TOKEN not set and 'gh auth token' failed.", file=sys.stderr)
        print("Export SYNC_TOKEN or run 'gh auth login'.", file=sys.stderr)
        sys.exit(1)


def _gh_api(method: str, endpoint: str, data: Optional[dict] = None) -> dict:
    """Call GitHub API via gh cli."""
    cmd = ["gh", "api", "-X", method, endpoint]
    if data:
        result = subprocess.run(
            ["gh", "api", "-X", method, endpoint, "--input", "-"],
            input=json.dumps(data),
            capture_output=True,
            text=True,
        )
    else:
        result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        error_msg = result.stderr.strip()
        raise GHAPIError(endpoint, result.returncode, error_msg)

    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


class GHAPIError(Exception):
    def __init__(self, endpoint: str, code: int, message: str):
        self.endpoint = endpoint
        self.code = code
        self.message = message
        super().__init__(f"GitHub API error on {endpoint}: {message}")


def check_rate_limit() -> dict:
    """Check GitHub API rate limit. Returns rate limit info."""
    return _gh_api("GET", "rate_limit")


def check_repo_exists(owner: str, repo: str) -> tuple[bool, bool]:
    """Check if repo exists and if archived. Returns (exists, archived)."""
    try:
        data = _gh_api("GET", f"repos/{owner}/{repo}")
        return True, data.get("archived", False)
    except GHAPIError:
        return False, False


def get_file_content(owner: str, repo: str, path: str, ref: str = "") -> str:
    """Get file content from a repo."""
    endpoint = f"repos/{owner}/{repo}/contents/{path}"
    if ref:
        endpoint += f"?ref={ref}"
    data = _gh_api("GET", endpoint)
    content = data.get("content", "")
    return base64.b64decode(content).decode("utf-8")


def get_file_sha(owner: str, repo: str, path: str, ref: str = "") -> str:
    """Get file SHA for update operations."""
    endpoint = f"repos/{owner}/{repo}/contents/{path}"
    if ref:
        endpoint += f"?ref={ref}"
    data = _gh_api("GET", endpoint)
    return data.get("sha", "")


def get_default_branch(owner: str, repo: str) -> str:
    """Get the default branch name for a repo."""
    data = _gh_api("GET", f"repos/{owner}/{repo}")
    return data.get("default_branch", "main")


def get_latest_tags(owner: str, repo: str) -> list[str]:
    """Get tag names for a repo, newest first."""
    try:
        data = _gh_api("GET", f"repos/{owner}/{repo}/tags?per_page=10")
        return [t["name"] for t in data]
    except GHAPIError:
        return []


def create_branch(owner: str, repo: str, branch: str, from_sha: str) -> None:
    """Create a new branch."""
    _gh_api(
        "POST",
        f"repos/{owner}/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": from_sha},
    )


def get_branch_sha(owner: str, repo: str, branch: str) -> str:
    """Get the HEAD SHA of a branch."""
    data = _gh_api("GET", f"repos/{owner}/{repo}/git/ref/heads/{branch}")
    return data["object"]["sha"]


def update_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str,
    sha: str,
) -> None:
    """Update a file on a branch."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    _gh_api(
        "PUT",
        f"repos/{owner}/{repo}/contents/{path}",
        {
            "message": message,
            "content": encoded,
            "sha": sha,
            "branch": branch,
        },
    )


def create_pr(
    owner: str, repo: str, title: str, body: str, head: str, base: str
) -> dict:
    """Create a pull request. Returns PR data."""
    return _gh_api(
        "POST",
        f"repos/{owner}/{repo}/pulls",
        {"title": title, "body": body, "head": head, "base": base},
    )


def enable_auto_merge(owner: str, repo: str, pr_number: int) -> None:
    """Enable auto-merge on a PR via GraphQL."""
    # Get the PR node ID first
    pr_data = _gh_api("GET", f"repos/{owner}/{repo}/pulls/{pr_number}")
    node_id = pr_data.get("node_id", "")
    if not node_id:
        return

    query = """
    mutation($prId: ID!) {
      enablePullRequestAutoMerge(input: {pullRequestId: $prId, mergeMethod: MERGE}) {
        pullRequest { number }
      }
    }
    """
    try:
        subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"prId={node_id}"],
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        pass  # Auto-merge may not be enabled on the repo


def create_tag(owner: str, repo: str, tag: str, sha: str) -> None:
    """Create a lightweight tag."""
    _gh_api(
        "POST",
        f"repos/{owner}/{repo}/git/refs",
        {"ref": f"refs/tags/{tag}", "sha": sha},
    )


def branch_exists(owner: str, repo: str, branch: str) -> bool:
    """Check if a branch exists."""
    try:
        _gh_api("GET", f"repos/{owner}/{repo}/git/ref/heads/{branch}")
        return True
    except GHAPIError:
        return False

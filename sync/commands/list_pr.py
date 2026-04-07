"""sync list-pr — List open PRs across repos, optionally approve/merge.

By default lists all open PRs for sync-managed repos.
Use --framework-version to filter to sync branch PRs only.
Use --approve to approve PRs with passing CI.
Use --merge to also merge approved PRs.
"""

import json
import subprocess
import sys

from sync.core.repos import load_repos, filter_sync_targets

SYNC_BRANCH_PREFIX = "sync/framework-"


def _gh(args: list[str], repo: str) -> subprocess.CompletedProcess:
    """Run gh CLI command for a specific repo."""
    return subprocess.run(
        ["gh"] + args + ["-R", repo],
        capture_output=True,
        text=True,
    )


def _get_prs(repo: str, head: str = None) -> list[dict]:
    """List open PRs. Optionally filter by head branch."""
    cmd = ["pr", "list", "--state", "open", "--json", "number,url,title,headRefName,statusCheckRollup"]
    if head:
        cmd.extend(["--head", head])
    result = _gh(cmd, repo)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return json.loads(result.stdout)


def _get_ci_status(pr: dict) -> str:
    """Determine CI status from PR check rollup."""
    checks = pr.get("statusCheckRollup", [])
    if not checks:
        return "none"

    states = set()
    for check in checks:
        conclusion = check.get("conclusion", "")
        status = check.get("status", "")

        if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            states.add("pass")
        elif conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            states.add("fail")
        elif status in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING"):
            states.add("pending")
        elif conclusion == "":
            states.add("pending")
        else:
            states.add("unknown")

    if "fail" in states:
        return "failing"
    if "pending" in states:
        return "pending"
    if "pass" in states:
        return "passing"
    return "none"


def _approve_pr(repo: str, pr_number: int) -> bool:
    result = _gh(["pr", "review", str(pr_number), "--approve"], repo)
    return result.returncode == 0


def _merge_pr(repo: str, pr_number: int) -> bool:
    result = _gh(["pr", "merge", str(pr_number), "--merge"], repo)
    return result.returncode == 0


CI_ICONS = {
    "passing": "✅",
    "failing": "❌",
    "pending": "⏳",
    "none": "❓",
}


def run(args):
    version = getattr(args, "framework_version", None)
    target_repo = getattr(args, "repo", None)
    do_approve = args.approve
    do_merge = args.merge

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"x '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = [r for r in repos if r.status == "active"]

    head_filter = f"{SYNC_BRANCH_PREFIX}{version}" if version else None
    print(f"Listing open PRs{f' for branch {head_filter}' if head_filter else ''} across {len(repos)} repos\n")

    counts = {"passing": 0, "failing": 0, "pending": 0, "no_pr": 0, "approved": 0, "merged": 0}

    for entry in repos:
        print(f"── {entry.repo} ──")

        prs = _get_prs(entry.repo, head=head_filter)
        if not prs:
            print(f"  📭 no open PRs")
            counts["no_pr"] += 1
            print()
            continue

        for pr in prs:
            pr_number = pr["number"]
            pr_url = pr["url"]
            title = pr["title"]
            branch = pr["headRefName"]
            ci = _get_ci_status(pr)
            icon = CI_ICONS.get(ci, "?")

            print(f"  {icon} #{pr_number} [{ci}] {branch}")
            print(f"    {title}")
            print(f"    {pr_url}")

            if ci == "passing":
                counts["passing"] += 1
            elif ci == "failing":
                counts["failing"] += 1
            elif ci == "pending":
                counts["pending"] += 1

            if do_approve and ci == "passing":
                ok = _approve_pr(entry.repo, pr_number)
                if ok:
                    print(f"    🟢 approved")
                    counts["approved"] += 1
                else:
                    print(f"    ⚠️  approve skipped (can't approve own PR)")

            if do_merge and ci == "passing":
                merged = _merge_pr(entry.repo, pr_number)
                if merged:
                    print(f"    🚀 merged")
                    counts["merged"] += 1
                else:
                    print(f"    ❌ merge failed")

        print()

    # Summary
    parts = []
    if counts["approved"]:
        parts.append(f"{counts['approved']} approved")
    if counts["merged"]:
        parts.append(f"{counts['merged']} merged")
    if counts["passing"]:
        parts.append(f"{counts['passing']} passing")
    if counts["pending"]:
        parts.append(f"{counts['pending']} pending")
    if counts["failing"]:
        parts.append(f"{counts['failing']} failing")
    if counts["no_pr"]:
        parts.append(f"{counts['no_pr']} no open PRs")
    print(f"Summary: {', '.join(parts)}")

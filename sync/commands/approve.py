"""sync approve — Check sync PRs and approve those with passing CI.

Finds open PRs on the sync/framework-<version> branch across all repos,
checks their CI status, and approves + optionally merges those that pass.
"""

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


def _get_pr(repo: str, branch: str) -> dict | None:
    """Find an open PR for the given branch. Returns {number, url, title} or None."""
    import json
    result = _gh(
        ["pr", "list", "--head", branch, "--state", "open", "--json", "number,url,title,statusCheckRollup"],
        repo,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    prs = json.loads(result.stdout)
    return prs[0] if prs else None


def _get_ci_status(pr: dict) -> str:
    """Determine CI status from PR check rollup. Returns 'passing', 'failing', 'pending', or 'none'."""
    checks = pr.get("statusCheckRollup", [])
    if not checks:
        return "none"

    states = set()
    for check in checks:
        # Handle both CheckRun and StatusContext
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
    """Approve a PR. Returns True on success."""
    result = _gh(["pr", "review", str(pr_number), "--approve"], repo)
    return result.returncode == 0


def _merge_pr(repo: str, pr_number: int) -> bool:
    """Merge a PR. Returns True on success."""
    result = _gh(["pr", "merge", str(pr_number), "--merge"], repo)
    return result.returncode == 0


def run(args):
    target = args.framework_version
    target_repo = getattr(args, "repo", None)
    do_merge = args.merge

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"x '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = filter_sync_targets(repos)

    branch_name = f"{SYNC_BRANCH_PREFIX}{target}"
    print(f"Checking PRs for branch {branch_name} across {len(repos)} repos\n")

    counts = {"passing": 0, "failing": 0, "pending": 0, "no_pr": 0, "approved": 0, "merged": 0}

    for entry in repos:
        print(f"── {entry.repo} ──")

        pr = _get_pr(entry.repo, branch_name)
        if not pr:
            print(f"  - no open PR")
            counts["no_pr"] += 1
            print()
            continue

        pr_number = pr["number"]
        pr_url = pr["url"]
        ci_status = _get_ci_status(pr)

        if ci_status == "passing":
            print(f"  ✓ PR #{pr_number} — CI passing")
            counts["passing"] += 1

            ok = _approve_pr(entry.repo, pr_number)
            if ok:
                print(f"    approved")
                counts["approved"] += 1
            else:
                print(f"    ✗ approve failed")

            if do_merge and ok:
                merged = _merge_pr(entry.repo, pr_number)
                if merged:
                    print(f"    merged")
                    counts["merged"] += 1
                else:
                    print(f"    ✗ merge failed")

        elif ci_status == "failing":
            print(f"  ✗ PR #{pr_number} — CI failing")
            print(f"    {pr_url}")
            counts["failing"] += 1

        elif ci_status == "pending":
            print(f"  ~ PR #{pr_number} — CI pending")
            counts["pending"] += 1

        else:
            print(f"  ? PR #{pr_number} — no CI checks")
            counts["no_pr"] += 1

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
        parts.append(f"{counts['no_pr']} no PR")
    print(f"Summary: {', '.join(parts)}")

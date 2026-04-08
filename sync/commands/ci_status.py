"""sync ci-status — Show latest CI run status across repos."""

import json
import subprocess
import sys

from sync.core.repos import load_repos

CI_ICONS = {
    "completed/success": "✅",
    "completed/failure": "❌",
    "completed/cancelled": "⚪",
    "in_progress/": "⏳",
    "queued/": "🔄",
}


def _get_latest_run(repo: str, workflow: str = None) -> list[dict]:
    """Get latest workflow runs for a repo."""
    cmd = ["gh", "run", "list", "-R", repo, "--limit", "1",
           "--json", "status,conclusion,name,workflowName,headBranch,updatedAt,url,databaseId"]
    if workflow:
        cmd.extend(["--workflow", workflow])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return json.loads(result.stdout)


def _get_all_latest_runs(repo: str) -> list[dict]:
    """Get latest run per workflow for a repo."""
    cmd = ["gh", "run", "list", "-R", repo, "--limit", "20",
           "--json", "status,conclusion,name,workflowName,headBranch,updatedAt,url,databaseId"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    runs = json.loads(result.stdout)
    # Deduplicate: keep latest per workflow name
    seen = {}
    for run in runs:
        name = run.get("workflowName", run["name"])
        if name not in seen:
            seen[name] = run
    return list(seen.values())


def _icon(run: dict) -> str:
    status = run.get("status", "")
    conclusion = run.get("conclusion", "")
    return CI_ICONS.get(f"{status}/{conclusion}", CI_ICONS.get(f"{status}/", "❓"))


def run(args):
    target_repo = getattr(args, "repo", None)
    show_all = getattr(args, "all_workflows", False)

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"❌ '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = [r for r in repos if r.status == "active" and r.ci]

    print(f"Checking CI status across {len(repos)} repos\n")

    passing = 0
    failing = 0
    other = 0
    failed_repos = []

    for entry in repos:
        print(f"── {entry.url} ──")

        if show_all:
            runs = _get_all_latest_runs(entry.repo)
        else:
            runs = _get_all_latest_runs(entry.repo)

        if not runs:
            print(f"  📭 no workflow runs found")
            other += 1
            print()
            continue

        repo_ok = True
        for r in runs:
            icon = _icon(r)
            name = r.get("workflowName", r["name"])
            branch = r.get("headBranch", "")
            url = r.get("url", "")
            conclusion = r.get("conclusion", r.get("status", ""))

            print(f"  {icon} {name} [{conclusion}] on {branch}")
            if url:
                print(f"    {url}")

            if r.get("conclusion") == "failure":
                repo_ok = False

        if repo_ok:
            passing += 1
        else:
            failing += 1
            failed_repos.append(entry)

        print()

    print(f"📊 {passing} passing, {failing} failing, {other} no runs")

    if failed_repos:
        print(f"\nFailing repos:")
        for entry in failed_repos:
            print(f"  ❌ {entry.url}")

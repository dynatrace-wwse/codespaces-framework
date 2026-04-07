"""sync list-issues — List open issues across repos."""

import json
import subprocess
import sys

from sync.core.repos import load_repos, filter_sync_targets


LABEL_ICONS = {
    "bug": "🐛",
    "enhancement": "✨",
    "documentation": "📝",
    "question": "❓",
    "help wanted": "🙋",
    "good first issue": "🌱",
}


def _gh(args: list[str], repo: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh"] + args + ["-R", repo],
        capture_output=True,
        text=True,
    )


def _get_issues(repo: str, label: str = None) -> list[dict]:
    cmd = ["issue", "list", "--state", "open", "--json", "number,title,url,labels,createdAt,author"]
    if label:
        cmd.extend(["--label", label])
    result = _gh(cmd, repo)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return json.loads(result.stdout)


def run(args):
    target_repo = getattr(args, "repo", None)
    label_filter = getattr(args, "label", None)

    repos = load_repos()
    if target_repo:
        repos = [r for r in repos if r.repo_name == target_repo or r.name == target_repo
                 or r.repo == target_repo]
        if not repos:
            print(f"❌ '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)
    else:
        repos = filter_sync_targets(repos)

    print(f"Listing open issues{f' (label: {label_filter})' if label_filter else ''} across {len(repos)} repos\n")

    total_issues = 0

    for entry in repos:
        issues = _get_issues(entry.repo, label=label_filter)

        if not issues:
            continue

        print(f"── {entry.repo} ({len(issues)} issues) ──")
        total_issues += len(issues)

        for issue in issues:
            number = issue["number"]
            title = issue["title"]
            url = issue["url"]
            labels = [l["name"] for l in issue.get("labels", [])]
            author = issue.get("author", {}).get("login", "")
            created = issue.get("createdAt", "")[:10]

            # Pick icon from first matching label
            icon = "📋"
            for lbl in labels:
                if lbl.lower() in LABEL_ICONS:
                    icon = LABEL_ICONS[lbl.lower()]
                    break

            label_str = f" [{', '.join(labels)}]" if labels else ""
            print(f"  {icon} #{number}{label_str} {title}")
            print(f"    {url}  @{author} {created}")

        print()

    if total_issues == 0:
        print("✅ No open issues across repos")
    else:
        print(f"📊 Total: {total_issues} open issues across {len(repos)} repos")

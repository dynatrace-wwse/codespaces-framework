"""sync tag — Create combined version tags (vFramework_RepoVersion) on consumer repos.

After all repos are synced to a framework version and PRs merged,
this creates a combined tag on each repo's default branch.

Format: v1.2.5_1.0.0 (framework version _ repo version)

Repo version is auto-detected from the latest combined tag, or starts at 1.0.0.
Use --bump to increment the repo version part (patch/minor/major).
"""

import re
import subprocess
import sys

from sync.core.repos import load_repos, filter_sync_targets
from sync.core.github_api import (
    get_file_content,
    get_latest_tags,
    get_default_branch,
    get_branch_sha,
    create_tag,
    create_release,
    GHAPIError,
)
from sync.core.version import extract_framework_version, parse_version, parse_combined_tag

SOURCE_FW_PATH = ".devcontainer/util/source_framework.sh"


def _get_changelog(owner: str, name: str, previous_tag: str, new_tag: str) -> str:
    """Generate changelog between two tags using gh CLI."""
    # Get merged PRs since previous tag
    result = subprocess.run(
        ["gh", "api", f"repos/{owner}/{name}/compare/{previous_tag}...HEAD",
         "--jq", ".commits[].commit.message"],
        capture_output=True, text=True,
    )
    commits = []
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("Merge"):
                commits.append(line)

    # Get PRs merged since previous tag
    result = subprocess.run(
        ["gh", "pr", "list", "--state", "merged", "--base", "main",
         "--json", "number,title,author,mergedAt",
         "-R", f"{owner}/{name}"],
        capture_output=True, text=True,
    )
    prs = []
    if result.returncode == 0 and result.stdout.strip():
        import json
        try:
            pr_data = json.loads(result.stdout)
            for pr in pr_data[:20]:  # Last 20 merged PRs
                author = pr.get("author", {}).get("login", "")
                prs.append(f"* {pr['title']} by @{author} in #{pr['number']}")
        except json.JSONDecodeError:
            pass

    return commits, prs


def run(args):
    target = args.framework_version
    force = args.force
    bump_part = getattr(args, "bump", None)
    dry_run = getattr(args, "dry_run", False)
    do_release = getattr(args, "release", False)

    repos = filter_sync_targets(load_repos())

    # Pre-flight: verify all repos at target version
    print(f"🔍 Pre-flight: checking all repos are at framework {target}\n")
    if not force:
        behind = []
        for repo_entry in repos:
            owner, name = repo_entry.owner, repo_entry.repo_name
            try:
                content = get_file_content(owner, name, SOURCE_FW_PATH)
                try:
                    current = extract_framework_version(content)
                except ValueError:
                    current = "not-migrated"
                if current != target:
                    behind.append(f"{repo_entry.repo} @ {current}")
                else:
                    print(f"  ✅ {repo_entry.repo} @ {current}")
            except GHAPIError as e:
                behind.append(f"{repo_entry.repo}: {e.message}")

        if behind:
            print(f"\n  ❌ Repos not at {target}:")
            for b in behind:
                print(f"    ❌ {b}")
            print("\n  Use --force to override.")
            sys.exit(1)
    else:
        print(f"  ⚠️  --force: skipping pre-flight checks")

    print(f"\n🏷️  Creating combined tags\n")

    # Create tags
    tagged = []
    errors = []
    for repo_entry in repos:
        owner, name = repo_entry.owner, repo_entry.repo_name
        try:
            # Get current repo version from latest combined tag
            tags = get_latest_tags(owner, name)
            combined = [t for t in tags if "_" in t]

            previous_tag = combined[0] if combined else None

            if combined:
                ct = parse_combined_tag(combined[0])
                repo_version = ct.repo
            else:
                repo_version = parse_version("1.0.0")

            # Optionally bump repo version
            if bump_part:
                repo_version = repo_version.bump(bump_part)

            new_tag = f"v{target}_{repo_version}"

            # Check if tag already exists
            already_exists = new_tag in tags
            if already_exists and not do_release:
                print(f"  ⏭️  {repo_entry.repo}: {new_tag} already exists")
                continue

            if dry_run:
                action = "would tag + release" if do_release else "would tag"
                print(f"  ⏳ {repo_entry.repo}: {action} {new_tag}")
                tagged.append(f"{repo_entry.repo}: {new_tag}")
                continue

            # Create tag if it doesn't exist yet
            if not already_exists:
                default_branch = get_default_branch(owner, name)
                sha = get_branch_sha(owner, name, default_branch)
                create_tag(owner, name, new_tag, sha)
                print(f"  🏷️  {repo_entry.repo}: {new_tag}")

            # Create GitHub Release
            if do_release:
                # Release name: version only (no repo name, no v prefix)
                release_name = f"{target}_{repo_version}"

                # Build changelog
                commits, prs = _get_changelog(owner, name, previous_tag or "HEAD~10", new_tag)

                body_parts = [
                    f"## 📋 Release {release_name}\n",
                    f"| | |",
                    f"|---|---|",
                    f"| **Framework version** | `{target}` |",
                    f"| **Repository version** | `{repo_version}` |",
                    f"| **Previous release** | `{previous_tag or 'initial'}` |",
                    f"",
                    f"### 🔄 What's Changed\n",
                ]

                if bump_part:
                    body_parts.append(f"- **Repo version bump**: `{bump_part}` ({previous_tag or '1.0.0'} → {new_tag})")

                body_parts.extend([
                    f"- Synced to framework **{target}** ([versioned pull model](https://dynatrace-wwse.github.io/codespaces-framework/framework/#versioned-pull-model))",
                    f"- Templates updated (`source_framework.sh`, `Makefile`)",
                    f"- README badges refreshed",
                    f"",
                ])

                # Categorize commits
                if commits:
                    from sync.commands.release import _categorize_commits
                    categories = _categorize_commits(commits)
                    for label, items in categories.items():
                        body_parts.append(f"### {label}\n")
                        for item in items:
                            body_parts.append(f"- {item}")
                        body_parts.append("")

                if prs:
                    body_parts.append("### 📝 Merged Pull Requests\n")
                    body_parts.extend(prs)
                    body_parts.append("")

                if previous_tag:
                    body_parts.append(
                        f"**Full Changelog**: https://github.com/{owner}/{name}/compare/{previous_tag}...{new_tag}"
                    )

                release_body = "\n".join(body_parts)

                try:
                    rel = create_release(owner, name, new_tag, release_name, release_body)
                    rel_url = rel.get("html_url", "")
                    print(f"  📦 {repo_entry.repo}: release {release_name} → {rel_url}")
                except GHAPIError as e:
                    print(f"  ⚠️  {repo_entry.repo}: release failed — {e.message}")

            tagged.append(f"{repo_entry.repo}: {new_tag}")

        except GHAPIError as e:
            errors.append(f"{repo_entry.repo}: {e.message}")
            print(f"  ❌ {repo_entry.repo}: {e.message}", file=sys.stderr)

    print(f"\n📊 {'Would tag' if dry_run else 'Tagged'} {len(tagged)} repos, {len(errors)} errors")

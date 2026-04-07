"""sync release — Bump framework version, tag, push, create GitHub Release.

Flow:
  1. Detect current version from latest git tag
  2. Bump (patch/minor/major)
  3. Update cli.py default --framework-version
  4. Commit the version bump
  5. Create git tag
  6. Push branch + tag
  7. Create GitHub Release with changelog
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from sync.core.version import parse_version


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run git command in the framework repo."""
    framework_dir = Path(__file__).parent.parent.parent
    result = subprocess.run(
        ["git"] + args,
        cwd=framework_dir,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        print(f"❌ git {' '.join(args)}: {result.stderr.strip()}", file=sys.stderr)
    return result


def _get_latest_tag() -> str:
    """Get the latest semver tag (X.Y.Z format, no v prefix)."""
    result = _git(["tag", "--sort=-v:refname"])
    if result.returncode != 0:
        return None
    for line in result.stdout.strip().split("\n"):
        tag = line.strip()
        if re.match(r"^\d+\.\d+\.\d+$", tag):
            return tag
    return None


def _get_changelog(previous_tag: str) -> tuple[list[str], list[str]]:
    """Get commits and PRs since previous tag."""
    # Commits since previous tag
    result = _git(["log", f"{previous_tag}..HEAD", "--oneline", "--no-merges"])
    commits = []
    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                commits.append(line)

    # Merged PRs since previous tag
    prs = []
    result = subprocess.run(
        ["gh", "pr", "list", "--state", "merged", "--base", "main",
         "--json", "number,title,author,mergedAt",
         "-R", "dynatrace-wwse/codespaces-framework"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            pr_data = json.loads(result.stdout)
            for pr in pr_data[:20]:
                author = pr.get("author", {}).get("login", "")
                prs.append(f"* {pr['title']} by @{author} in #{pr['number']}")
        except json.JSONDecodeError:
            pass

    return commits, prs


def _create_github_release(tag: str, previous_tag: str) -> str | None:
    """Create a GitHub Release with changelog. Returns release URL or None."""
    commits, prs = _get_changelog(previous_tag)

    body_parts = [
        f"## 📋 Framework Release {tag}\n",
    ]

    if commits:
        body_parts.append("### 🔄 Changes\n")
        for commit in commits[:30]:
            body_parts.append(f"- {commit}")
        body_parts.append("")

    if prs:
        body_parts.append("### 📝 Merged Pull Requests\n")
        body_parts.extend(prs)
        body_parts.append("")

    body_parts.append(
        f"**Full Changelog**: https://github.com/dynatrace-wwse/codespaces-framework/compare/{previous_tag}...{tag}"
    )

    body = "\n".join(body_parts)

    result = subprocess.run(
        ["gh", "release", "create", tag,
         "--title", tag,
         "--notes", body,
         "-R", "dynatrace-wwse/codespaces-framework"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def run(args):
    part = args.part
    dry_run = args.dry_run

    # 1. Detect current version
    current_tag = _get_latest_tag()
    if not current_tag:
        print("❌ No semver tags found (expected X.Y.Z format)")
        sys.exit(1)

    current = parse_version(current_tag)
    new = current.bump(part)
    new_tag = str(new)

    print(f"📦 Current version: {current_tag}")
    print(f"🆕 New version:     {new_tag} ({part} bump)")
    print()

    # 2. Check for uncommitted changes
    status = _git(["status", "--porcelain"], check=False)
    uncommitted = [l for l in status.stdout.strip().split("\n") if l.strip()]
    if uncommitted:
        print(f"📋 Uncommitted changes ({len(uncommitted)} files):")
        for line in uncommitted[:10]:
            print(f"    {line}")
        if len(uncommitted) > 10:
            print(f"    ... and {len(uncommitted) - 10} more")
        print()

    # 3. Preview changelog
    commits, prs = _get_changelog(current_tag)
    if commits:
        print(f"📝 Changes since {current_tag} ({len(commits)} commits):")
        for c in commits[:10]:
            print(f"    {c}")
        if len(commits) > 10:
            print(f"    ... and {len(commits) - 10} more")
        print()

    # 4. Update cli.py default version
    cli_path = Path(__file__).parent.parent / "cli.py"
    cli_content = cli_path.read_text()
    old_default = f'default="{current_tag}"'
    new_default = f'default="{new_tag}"'
    if old_default in cli_content:
        cli_content = cli_content.replace(old_default, new_default)
        cli_content = cli_content.replace(
            f'help="Framework version to pin (default: {current_tag})"',
            f'help="Framework version to pin (default: {new_tag})"',
        )
        if not dry_run:
            cli_path.write_text(cli_content)
            print(f"✅ Updated cli.py default version to {new_tag}")
        else:
            print(f"⏳ Would update cli.py default version to {new_tag}")
    else:
        print(f"⚠️  cli.py default version not found as {current_tag} — skipping update")

    if dry_run:
        print(f"\n⏳ Would commit, tag {new_tag}, push, and create GitHub Release")
        return

    # 5. Commit
    _git(["add", str(cli_path)])
    result = _git(["diff", "--cached", "--quiet"], check=False)
    if result.returncode != 0:
        _git(["commit", "-m", f"chore: bump framework version to {new_tag}"])
        print(f"✅ Committed version bump")
    else:
        print(f"⏭️  No cli.py changes to commit")

    # 6. Tag
    tag_result = _git(["tag", new_tag])
    if tag_result.returncode != 0:
        print(f"❌ Failed to create tag {new_tag}")
        sys.exit(1)
    print(f"🏷️  Tagged {new_tag}")

    # 7. Push
    branch = _git(["branch", "--show-current"]).stdout.strip()
    push_result = _git(["push", "origin", branch, "--tags"])
    if push_result.returncode != 0:
        print(f"❌ Push failed")
        sys.exit(1)
    print(f"🚀 Pushed {branch} + tag {new_tag}")

    # 8. Create GitHub Release
    print(f"\n📦 Creating GitHub Release...")
    release_url = _create_github_release(new_tag, current_tag)
    if release_url:
        print(f"📦 Release created: {release_url}")
    else:
        print(f"⚠️  Release creation failed — create manually on GitHub")

    print(f"\n✅ Framework {new_tag} released!")
    print(f"   Next: sync push-update --framework-version {new_tag}")

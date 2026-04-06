"""sync tag — Create combined version tags after PRs merge."""

import sys

from sync.core.repos import load_repos, filter_sync_targets
from sync.core.github_api import (
    get_file_content,
    get_latest_tags,
    get_default_branch,
    get_branch_sha,
    create_tag,
    GHAPIError,
)
from sync.core.version import extract_framework_version, parse_combined_tag

SOURCE_FW_PATH = ".devcontainer/util/source_framework.sh"


def run(args):
    target = args.framework_version
    force = args.force

    repos = filter_sync_targets(load_repos())

    # Pre-flight: verify all repos at target version
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
            except GHAPIError as e:
                behind.append(f"{repo_entry.repo}: {e.message}")

        if behind:
            print(f"Pre-flight failed: repos not at {target}:")
            for b in behind:
                print(f"  ! {b}")
            print("\nUse --force to override.")
            sys.exit(1)

    print(f"Pre-flight passed: all repos at {target}\n")

    # Create tags
    tagged = []
    errors = []
    for repo_entry in repos:
        owner, name = repo_entry.owner, repo_entry.repo_name
        try:
            # Get current repo version from latest combined tag
            tags = get_latest_tags(owner, name)
            combined = [t for t in tags if "_" in t]

            if combined:
                ct = parse_combined_tag(combined[0])
                repo_version = str(ct.repo)
            else:
                repo_version = "1.0.0"

            new_tag = f"v{target}_{repo_version}"

            # Check if tag already exists
            if new_tag in tags:
                print(f"  - {repo_entry.repo}: {new_tag} already exists")
                continue

            default_branch = get_default_branch(owner, name)
            sha = get_branch_sha(owner, name, default_branch)
            create_tag(owner, name, new_tag, sha)

            tagged.append(f"{repo_entry.repo}: {new_tag}")
            print(f"  + {repo_entry.repo}: {new_tag}")

        except GHAPIError as e:
            errors.append(f"{repo_entry.repo}: {e.message}")
            print(f"  x {repo_entry.repo}: {e.message}", file=sys.stderr)

    print(f"\nTagged {len(tagged)} repos, {len(errors)} errors")

"""sync bump-repo-version — Bump the repo version component of a combined tag."""

import sys

from sync.core.github_api import (
    get_latest_tags,
    get_default_branch,
    get_branch_sha,
    create_tag,
    GHAPIError,
)
from sync.core.version import parse_combined_tag


def run(args):
    part = args.part
    repo_full = args.repo

    if "/" not in repo_full:
        print(f"Repo must be in owner/name format, got: {repo_full}", file=sys.stderr)
        sys.exit(1)

    owner, name = repo_full.split("/", 1)

    try:
        tags = get_latest_tags(owner, name)
        combined = [t for t in tags if "_" in t]

        if not combined:
            print(f"No combined tags found on {repo_full}.", file=sys.stderr)
            print("Create an initial tag first with: sync tag --framework-version X.Y.Z")
            sys.exit(1)

        ct = parse_combined_tag(combined[0])
        new_repo = ct.repo.bump(part)
        new_tag = f"v{ct.framework}_{new_repo}"

        if new_tag in tags:
            print(f"Tag {new_tag} already exists on {repo_full}.")
            sys.exit(1)

        default_branch = get_default_branch(owner, name)
        sha = get_branch_sha(owner, name, default_branch)
        create_tag(owner, name, new_tag, sha)

        print(f"+ Bumped: {combined[0]} -> {new_tag}")
        print(f"+ Tag pushed to {repo_full}")

    except GHAPIError as e:
        print(f"Error: {e.message}", file=sys.stderr)
        sys.exit(1)

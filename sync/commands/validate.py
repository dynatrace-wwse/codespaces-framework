"""sync validate — Validate repos.yaml entries."""

import sys

from sync.core.repos import load_repos, validate_repos
from sync.core.github_api import check_repo_exists, GHAPIError


def run(args):
    target_repo = args.repo

    try:
        repos = load_repos()
    except Exception as e:
        print(f"x Failed to load repos.yaml: {e}", file=sys.stderr)
        sys.exit(1)

    # Schema validation
    errors = validate_repos(repos)
    if errors:
        print("x Schema validation failed:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    print("+ Schema validation passed")

    # Filter to specific repo if requested
    if target_repo:
        repos = [r for r in repos if r.repo == target_repo or r.name == target_repo]
        if not repos:
            print(f"x Repo '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)

    # GitHub accessibility check
    gh_errors = []
    for repo_entry in repos:
        owner, name = repo_entry.owner, repo_entry.repo_name
        try:
            exists, archived = check_repo_exists(owner, name)
            if not exists:
                gh_errors.append(
                    f"{repo_entry.name}: repo '{repo_entry.repo}' not found (404)"
                )
            elif archived and repo_entry.status != "archived":
                gh_errors.append(
                    f"{repo_entry.name}: repo is archived on GitHub but status is '{repo_entry.status}'"
                )
        except GHAPIError as e:
            gh_errors.append(f"{repo_entry.name}: API error: {e.message}")

    if gh_errors:
        print("x GitHub accessibility check failed:")
        for err in gh_errors:
            print(f"  - {err}")
        sys.exit(1)

    print(f"+ All {len(repos)} repos accessible on GitHub")
    print("+ repos.yaml valid")

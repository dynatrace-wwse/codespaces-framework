#!/usr/bin/env python3
"""Sync CLI — framework version management across dynatrace-wwse repos."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="sync",
        description="Manage framework versions across dynatrace-wwse repos",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # push-update
    pu = subparsers.add_parser(
        "push-update", help="Pull main, branch, migrate, commit, push, create PR"
    )
    pu.add_argument(
        "--framework-version", required=True, help="Target framework version (X.Y.Z)"
    )
    pu.add_argument("--repo", help="Target a specific repo (default: all sync-managed)")
    pu.add_argument("--dry-run", action="store_true", help="Preview changes without creating PRs")
    pu.add_argument("--force", action="store_true", help="Update even if already at target version")
    pu.add_argument("--auto-merge", action="store_true", help="Enable auto-merge on created PRs")
    pu.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    # status
    st = subparsers.add_parser("status", help="Show version drift across repos")
    st.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    # diff
    di = subparsers.add_parser("diff", help="Preview what push-update would change")
    di.add_argument("--framework-version", help="Target version (default: latest tag)")
    di.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    # validate
    va = subparsers.add_parser("validate", help="Validate repos.yaml and local repo state")
    va.add_argument("--repo", help="Validate a specific repo entry")

    # tag
    tg = subparsers.add_parser("tag", help="Create combined version tags after PRs merge")
    tg.add_argument(
        "--framework-version", required=True, help="Framework version for tags"
    )
    tg.add_argument("--force", action="store_true", help="Skip pre-flight checks")

    # bump-repo-version
    br = subparsers.add_parser("bump-repo-version", help="Bump repo version component")
    br.add_argument(
        "--part",
        choices=["patch", "minor", "major"],
        default="patch",
        help="Version part to bump (default: patch)",
    )
    br.add_argument("--repo", required=True, help="Target repo (owner/name)")

    # migrate-mkdocs
    mm = subparsers.add_parser(
        "migrate-mkdocs", help="Migrate mkdocs.yaml to INHERIT pattern"
    )
    mm.add_argument("--repo", required=True, help="Target repo (owner/name)")
    mm.add_argument("--dry-run", action="store_true", help="Preview without changes")

    # list
    ls = subparsers.add_parser("list", help="List registered repos")
    ls.add_argument("--ci-enabled", action="store_true", help="Only repos with ci: true")
    ls.add_argument("--sync-managed", action="store_true", help="Only sync-managed repos")
    ls.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    # migrate
    mi = subparsers.add_parser(
        "migrate",
        help="Migrate repos from repos.yaml to versioned pull model",
    )
    mi.add_argument(
        "--repo",
        help="Migrate a specific repo (default: all sync-managed repos)",
    )
    mi.add_argument(
        "--framework-version",
        default="1.2.5",
        help="Framework version to pin (default: 1.2.5)",
    )
    mi.add_argument("--dry-run", action="store_true", help="Audit only, no changes")

    # release
    rl = subparsers.add_parser(
        "release",
        help="Bump framework version, tag, push, and update default version",
    )
    rl.add_argument(
        "--part",
        choices=["patch", "minor", "major"],
        default="patch",
        help="Version part to bump (default: patch)",
    )
    rl.add_argument("--dry-run", action="store_true", help="Preview without tagging/pushing")

    # list-pr
    lp = subparsers.add_parser(
        "list-pr",
        help="List open PRs across repos, optionally approve/merge passing ones",
    )
    lp.add_argument("--framework-version", help="Filter PRs by sync branch version")
    lp.add_argument("--repo", help="Target a specific repo (default: all sync-managed)")
    lp.add_argument("--approve", action="store_true", help="Approve PRs with passing CI")
    lp.add_argument("--merge", action="store_true", help="Merge approved PRs")

    # list-issues
    li = subparsers.add_parser(
        "list-issues",
        help="List open issues across repos",
    )
    li.add_argument("--repo", help="Target a specific repo (default: all sync-managed)")
    li.add_argument("--label", help="Filter by label")

    # revert
    rv = subparsers.add_parser(
        "revert",
        help="Revert uncommitted changes in repos (undo migrate, etc.)",
    )
    rv.add_argument("--repo", help="Revert a specific repo (default: all sync-managed repos)")

    # generate-registry
    gr = subparsers.add_parser(
        "generate-registry", help="Generate HTML registry page"
    )
    gr.add_argument("--output", required=True, help="Output HTML file path")

    args = parser.parse_args()

    # Import and dispatch to command handlers
    if args.command == "push-update":
        from sync.commands.push_update import run
    elif args.command == "status":
        from sync.commands.status import run
    elif args.command == "diff":
        from sync.commands.diff_cmd import run
    elif args.command == "validate":
        from sync.commands.validate import run
    elif args.command == "tag":
        from sync.commands.tag import run
    elif args.command == "bump-repo-version":
        from sync.commands.bump_repo_version import run
    elif args.command == "migrate-mkdocs":
        from sync.commands.migrate_mkdocs import run
    elif args.command == "list":
        from sync.commands.list_cmd import run
    elif args.command == "migrate":
        from sync.commands.migrate import run
    elif args.command == "release":
        from sync.commands.release import run
    elif args.command == "list-pr":
        from sync.commands.list_pr import run
    elif args.command == "list-issues":
        from sync.commands.list_issues import run
    elif args.command == "revert":
        from sync.commands.revert import run
    elif args.command == "generate-registry":
        from sync.commands.generate_registry import run
    else:
        parser.print_help()
        sys.exit(1)

    run(args)


if __name__ == "__main__":
    main()

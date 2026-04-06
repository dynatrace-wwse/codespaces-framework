"""sync migrate-mkdocs — Migrate mkdocs.yaml to INHERIT pattern."""

import sys
import yaml

from sync.core.github_api import get_file_content, GHAPIError


def run(args):
    repo_full = args.repo
    dry_run = args.dry_run

    if "/" not in repo_full:
        print(f"Repo must be in owner/name format, got: {repo_full}", file=sys.stderr)
        sys.exit(1)

    owner, name = repo_full.split("/", 1)

    try:
        content = get_file_content(owner, name, "mkdocs.yaml")
    except GHAPIError as e:
        print(f"Could not read mkdocs.yaml: {e.message}", file=sys.stderr)
        sys.exit(1)

    # Check if already migrated
    if "INHERIT:" in content:
        print(f"{repo_full}: already using INHERIT pattern. Nothing to do.")
        return

    try:
        config = yaml.safe_load(content)
    except yaml.YAMLError as e:
        print(f"Failed to parse mkdocs.yaml: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract repo-specific fields
    site_name = config.get("site_name", "")
    repo_name = config.get("repo_name", "")
    repo_url = config.get("repo_url", "")
    nav = config.get("nav", [])
    extra = config.get("extra", {})
    rum_snippet = extra.get("rum_snippet", "")

    # Build new mkdocs.yaml
    lines = ["INHERIT: mkdocs-base.yaml", ""]
    if site_name:
        lines.append(f'site_name: "{site_name}"')
    if repo_name:
        lines.append(f'repo_name: "{repo_name}"')
    if repo_url:
        lines.append(f'repo_url: "{repo_url}"')

    if nav:
        lines.append("nav:")
        for item in nav:
            if isinstance(item, dict):
                for k, v in item.items():
                    lines.append(f"  - '{k}': {v}")
            else:
                lines.append(f"  - {item}")

    if rum_snippet:
        lines.append("")
        lines.append("extra:")
        lines.append(f'  rum_snippet: "{rum_snippet}"')

    new_content = "\n".join(lines) + "\n"

    if dry_run:
        print(f"Would migrate {repo_full}/mkdocs.yaml:\n")
        print(new_content)
        return

    print(f"+ Extracted nav ({len(nav)} sections)")
    if rum_snippet:
        print("+ Extracted RUM snippet")
    print(f"\nMigrated mkdocs.yaml for {repo_full}:")
    print(new_content)
    print("NOTE: Apply this content manually or via push-update with --force.")

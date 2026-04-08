"""sync status — Show version drift across repos."""

import json
import sys

from sync.core.repos import load_repos, filter_sync_targets
from sync.core.github_api import (
    get_file_content,
    get_latest_tags,
    GHAPIError,
)
from sync.core.version import extract_framework_version, parse_combined_tag

SOURCE_FW_PATH = ".devcontainer/util/source_framework.sh"


def _get_latest_framework_version() -> str:
    """Get the latest framework version from codespaces-framework tags."""
    from sync.core.version import parse_version

    tags = get_latest_tags("dynatrace-wwse", "codespaces-framework")
    semver_tags = []
    for tag in tags:
        if "_" not in tag:
            try:
                v = parse_version(tag)
                semver_tags.append((v, tag))
            except ValueError:
                continue
    if not semver_tags:
        return "unknown"
    # Sort by major, minor, patch descending
    semver_tags.sort(key=lambda x: (x[0].major, x[0].minor, x[0].patch), reverse=True)
    return str(semver_tags[0][0])


def run(args):
    json_out = args.json_output
    repos = filter_sync_targets(load_repos())
    latest_fw = _get_latest_framework_version()

    up_to_date = []
    behind = []
    errors = []

    for repo_entry in repos:
        owner, name = repo_entry.owner, repo_entry.repo_name
        entry = {
            "name": repo_entry.name,
            "repo": repo_entry.repo,
            "framework_version": "unknown",
            "latest_tag": "",
            "status": "unknown",
        }

        try:
            content = get_file_content(owner, name, SOURCE_FW_PATH)
            try:
                current = extract_framework_version(content)
            except ValueError:
                current = "not-migrated"
            entry["framework_version"] = current

            tags = get_latest_tags(owner, name)
            combined = [t for t in tags if "_" in t]
            entry["latest_tag"] = combined[0] if combined else ""

            if current == latest_fw:
                entry["status"] = "up-to-date"
                up_to_date.append(entry)
            else:
                entry["status"] = "behind"
                behind.append(entry)

        except GHAPIError as e:
            entry["status"] = "error"
            entry["message"] = e.message
            errors.append(entry)

    if json_out:
        print(
            json.dumps(
                {
                    "framework_version": latest_fw,
                    "repos": up_to_date + behind + errors,
                },
                indent=2,
            )
        )
        return

    print(f"Framework version: {latest_fw}\n")

    if up_to_date:
        print("Synced (up to date):")
        for e in up_to_date:
            tag = f" ({e['latest_tag']})" if e["latest_tag"] else ""
            print(f"  + github.com/{e['repo']}{tag} @ {e['framework_version']}")
        print()

    if behind:
        print("Behind:")
        for e in behind:
            tag = f" ({e['latest_tag']})" if e["latest_tag"] else ""
            print(f"  ! github.com/{e['repo']}{tag} @ {e['framework_version']}")
        print()

    if errors:
        print("Errors:")
        for e in errors:
            print(f"  x github.com/{e['repo']}: {e.get('message', 'unknown error')}")
        print()

    total = len(up_to_date) + len(behind) + len(errors)
    print(f"{len(up_to_date)}/{total} repos up to date")

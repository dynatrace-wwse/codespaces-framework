"""sync diff — Preview what push-update would change."""

import json
import sys

from sync.core.repos import load_repos, filter_sync_targets
from sync.core.github_api import get_file_content, get_latest_tags, GHAPIError
from sync.core.version import extract_framework_version

SOURCE_FW_PATH = ".devcontainer/util/source_framework.sh"


def run(args):
    json_out = args.json_output
    target = args.framework_version

    if not target:
        from sync.core.version import parse_version

        tags = get_latest_tags("dynatrace-wwse", "codespaces-framework")
        versions = []
        for t in tags:
            if "_" not in t:
                try:
                    v = parse_version(t)
                    versions.append(v)
                except ValueError:
                    continue
        if versions:
            versions.sort(key=lambda v: (v.major, v.minor, v.patch), reverse=True)
            target = str(versions[0])
        else:
            print("Could not determine latest framework version.", file=sys.stderr)
            sys.exit(1)

    repos = filter_sync_targets(load_repos())
    diffs = []

    for repo_entry in repos:
        owner, name = repo_entry.owner, repo_entry.repo_name
        try:
            content = get_file_content(owner, name, SOURCE_FW_PATH)
            try:
                current = extract_framework_version(content)
            except ValueError:
                current = "not-migrated"

            if current != target:
                diffs.append(
                    {
                        "repo": repo_entry.repo,
                        "current": current,
                        "target": target,
                        "source_framework": f"{current} -> {target}",
                    }
                )
        except GHAPIError as e:
            diffs.append(
                {
                    "repo": repo_entry.repo,
                    "current": "error",
                    "target": target,
                    "error": e.message,
                }
            )

    if json_out:
        print(json.dumps({"target_version": target, "diffs": diffs}, indent=2))
        return

    if not diffs:
        print(f"All repos already at {target}. Nothing to update.")
        return

    print(f"Target: {target}\n")
    print(f"{'Repo':<55} {'source_framework.sh':<25}")
    print(f"{'-'*55} {'-'*25}")
    for d in diffs:
        if "error" in d:
            print(f"{d['repo']:<55} {'ERROR: ' + d['error']:<25}")
        else:
            print(f"{d['repo']:<55} {d['source_framework']:<25}")
    print(f"\n{len(diffs)} repo(s) need updating")

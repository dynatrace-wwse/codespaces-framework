"""sync list — List registered repos."""

import json

from sync.core.repos import load_repos


def run(args):
    json_out = args.json_output
    repos = load_repos()

    if args.ci_enabled:
        repos = [r for r in repos if r.ci]
    if args.sync_managed:
        repos = [r for r in repos if r.sync_managed]

    if json_out:
        data = [
            {
                "name": r.name,
                "repo": r.repo,
                "status": r.status,
                "maintainer": r.maintainer,
                "sync_managed": r.sync_managed,
                "ci": r.ci,
                "tags": r.tags,
            }
            for r in repos
        ]
        print(json.dumps({"repos": data}, indent=2))
        return

    print(f"{'Name':<45} {'Status':<12} {'Sync':<6} {'CI':<4} {'Maintainer':<20}")
    print(f"{'-'*45} {'-'*12} {'-'*6} {'-'*4} {'-'*20}")
    for r in repos:
        sync = "yes" if r.sync_managed else "no"
        ci = "yes" if r.ci else "no"
        print(f"{r.name:<45} {r.status:<12} {sync:<6} {ci:<4} {r.maintainer:<20}")
    print(f"\n{len(repos)} repo(s)")

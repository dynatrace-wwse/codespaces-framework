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
                "url": r.url,
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

    # Compute column widths from data
    w_repo = max(len(r.url) for r in repos)
    w_maint = max(len(r.maintainer) for r in repos)
    w_repo = max(w_repo, len("Repository"))
    w_maint = max(w_maint, len("Maintainer"))

    header = f"{'#':<4} {'Repository':<{w_repo}}  {'Status':<10} {'Sync':<6} {'CI':<4} {'Maintainer':<{w_maint}}"
    print(header)
    print(f"{'-'*4} {'-'*w_repo}  {'-'*10} {'-'*6} {'-'*4} {'-'*w_maint}")
    for i, r in enumerate(repos, 1):
        sync = "yes" if r.sync_managed else "no"
        ci = "yes" if r.ci else "no"
        print(f"{i:<4} {r.url:<{w_repo}}  {r.status:<10} {sync:<6} {ci:<4} {r.maintainer:<{w_maint}}")
    print(f"\n{len(repos)} repo(s)")

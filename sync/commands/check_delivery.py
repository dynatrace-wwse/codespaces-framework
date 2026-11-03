"""sync check-delivery — refuse an invalid training delivery block (ENH-152).

Runs against one training checkout (default: the current directory), so a
training repo's own CI can call it. Exit 0 = valid (including no block at all,
which means today's behaviour); exit 1 = invalid, with every issue printed.
"""

import sys

from sync.core.delivery import check_repo


def run(args):
    path = getattr(args, "path", ".") or "."
    effective, issues = check_repo(path)
    if issues:
        print(f"x delivery: {len(issues)} issue(s) in {path}")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)
    print(
        f"+ delivery: {effective['availability']}, tenancy {effective['tenancy']}, "
        f"shared infra {'yes' if effective['sharedInfra'] else 'no'}"
    )

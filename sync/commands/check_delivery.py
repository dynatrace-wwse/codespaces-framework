"""sync check-delivery — refuse an invalid training delivery declaration (ENH-152, ENH-163).

Reads .devcontainer/delivery.yaml when present, else the docs/index.md block.

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
    for warning in effective.get("warnings") or []:
        print(f"! delivery: {warning}")
    line = (f"+ delivery: {effective['availability']}, tenancy {effective['tenancy']}, "
            f"shared infra {'yes' if effective['sharedInfra'] else 'no'}")
    if "seats" in effective:   # .devcontainer/delivery.yaml (ENH-163)
        seats = effective["seats"]
        line += (f", personal seats {'yes' if seats['personal'] else 'no'} "
                 f"({seats['mode']})")
        if effective.get("sharedInstance"):
            line += (f", shared instance {effective['sharedInstance']} "
                     "(Orbital checks it against its allowlist at booking)")
    print(line)

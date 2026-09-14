"""Fail if any workflow can publish the public site from a branch other than main.

A pull request must validate and must not publish; publication happens only
after merge. This used not to hold: deploy-ghpages.yaml triggered on push to
'docs/*' and 'rfe/*', so any working branch attempted to publish, and shared the
'pages' concurrency group with cancel-in-progress, so it could cancel an
in-flight main deploy on the way.

The github-pages environment allowlist rejected those runs, but only after the
job was queued. This check keeps the cause from coming back.

Run from the repository root.
"""

import sys
from pathlib import Path

import yaml

PUBLISH_ALLOWED_BRANCHES = {"main"}
WORKFLOW_DIR = Path(".github/workflows")


def _triggers(doc):
    # YAML 1.1 parses a bare `on:` key as the boolean True.
    raw = doc.get("on", doc.get(True)) or {}
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {t: None for t in raw}
    return raw


def _publishes(doc, body):
    return (
        "actions/deploy-pages" in body
        or "gh-deploy" in body
        or (doc.get("permissions") or {}).get("pages") == "write"
    )


def main() -> int:
    failures = []
    inspected = []

    for wf in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        body = wf.read_text()
        doc = yaml.safe_load(body) or {}
        if not _publishes(doc, body):
            continue
        inspected.append(wf.name)

        triggers = _triggers(doc)
        push = triggers.get("push") or {}
        branches = push.get("branches", []) if isinstance(push, dict) else []
        disallowed = [b for b in branches if b not in PUBLISH_ALLOWED_BRANCHES]
        if disallowed:
            failures.append(
                f"{wf.name}: publishes on push to {disallowed} - "
                f"only {sorted(PUBLISH_ALLOWED_BRANCHES)} may publish"
            )
        if "pull_request" in triggers or "pull_request_target" in triggers:
            failures.append(f"{wf.name}: publishes on a pull_request trigger")

    if failures:
        print("Publication trigger policy violated:")
        for f in failures:
            print(f"  - {f}")
        print(
            "A pull request must validate and must not publish; "
            "publication happens only after merge."
        )
        return 1

    print(
        "Publication trigger policy OK - publishing workflows inspected: "
        + (", ".join(inspected) or "none")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

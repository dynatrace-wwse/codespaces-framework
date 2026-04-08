"""sync generate-json — Generate repos.json for the org GitHub Pages registry."""

import json
import sys

from sync.core.repos import load_repos


def run(args):
    repos = load_repos()

    # Only include listed, active repos
    listed = [r for r in repos if r.listed and r.status == "active"]

    output = []
    for r in listed:
        entry = {
            "repo": r.repo_name,
            "title": r.title or r.name,
            "desc": r.description,
            "tags": r.tags,
            "primaryTag": r.primary_tag or (r.tags[0] if r.tags else ""),
            "duration": r.duration,
        }
        if r.icon_key:
            entry["iconKey"] = r.icon_key
        if r.is_template:
            entry["isTemplate"] = True

        output.append(entry)

    data = json.dumps(output, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(data + "\n")
        print(f"Wrote {len(output)} repos to {args.output}")
    else:
        print(data)

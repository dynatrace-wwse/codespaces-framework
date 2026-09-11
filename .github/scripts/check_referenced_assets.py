"""Fail if mkdocs references an extra_css/extra_javascript file the build did not produce.

MkDocs does not warn about a missing extra_javascript file - not with a plain
build, and not with --strict either (measured on mkdocs-material 9.5.42). The
page silently ships a <script> tag pointing at a 404, and for mermaid that means
Material falls back to fetching the unpinned CDN copy. Nothing in the build log
says so. This check is the part --strict does not cover.

Run from the repository root, with the built site directory as the argument.
"""

import sys
from pathlib import Path

from mkdocs.config import load_config


def main(site_dir: str) -> int:
    site = Path(site_dir)
    if not site.is_dir():
        print(f"Site directory not found: {site}")
        return 1

    cfg = load_config()
    missing = []
    checked = 0
    for key in ("extra_css", "extra_javascript"):
        for entry in cfg[key]:
            ref = str(entry)
            if ref.startswith(("http://", "https://", "//")):
                continue  # remote by intent, not our problem to resolve
            checked += 1
            if not (site / ref).is_file():
                missing.append(f"{key}: {ref}")

    if missing:
        print("Referenced asset(s) missing from the built site:")
        for m in missing:
            print(f"  - {m}")
        print("MkDocs does not warn on these, not even with --strict.")
        return 1

    print(f"All {checked} referenced extra_css/extra_javascript asset(s) present in {site}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "site"))

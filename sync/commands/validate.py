"""sync validate — Validate repos.yaml entries and local repo state."""

import re
import sys

from sync.core.repos import load_repos, validate_repos
from sync.core.github_api import check_repo_exists, GHAPIError
from sync.commands.migrate import (
    _validate_devcontainer, _validate_readme, _resolve_repo_path, _get_category_a,
    SOURCE_FRAMEWORK_TEMPLATE, THIN_MAKEFILE,
)


def _validate_local(repo_entry, repo_path):
    """Validate a local repo clone against framework expectations."""
    from pathlib import Path

    path = Path(repo_path)
    if not path.is_dir() or not (path / ".devcontainer").is_dir():
        print(f"  📭 {repo_entry.name}: local clone not found at {path}")
        return

    print(f"\n── {repo_entry.repo} ({path}) ──")

    # devcontainer.json validation
    dc_issues = _validate_devcontainer(path)
    if dc_issues:
        print(f"  ❌ devcontainer.json: {len(dc_issues)} issue(s)")
        for issue in dc_issues:
            print(f"    ❌ {issue}")
    else:
        print(f"  ✅ devcontainer.json valid")

    # Category A files that should NOT be present (migration leftovers)
    cat_a_files, cat_a_dirs = _get_category_a(repo_entry.image_tier)
    leftovers = []
    for f in cat_a_files:
        if (path / f).exists():
            leftovers.append(f)
    for d in cat_a_dirs:
        if (path / d).is_dir():
            leftovers.append(f"{d}/")

    if leftovers:
        print(f"  ⚠️  migration: {len(leftovers)} Category A leftover(s) — run sync migrate")
        for f in leftovers:
            print(f"    ❌ {f}")
    else:
        print(f"  ✅ migration clean (no Category A files)")

    # Verify source_framework.sh matches the framework template
    sf = path / ".devcontainer/util/source_framework.sh"
    if not sf.exists():
        print(f"  ❌ source_framework.sh missing")
    else:
        content = sf.read_text()
        m = re.search(r'FRAMEWORK_VERSION="\$\{FRAMEWORK_VERSION:-([^}]+)\}"', content)
        if not m:
            print(f"  ❌ source_framework.sh — no FRAMEWORK_VERSION pin found")
        else:
            pinned = m.group(1)
            expected = SOURCE_FRAMEWORK_TEMPLATE % pinned
            if content == expected:
                print(f"  ✅ source_framework.sh matches template (v{pinned})")
            else:
                print(f"  ⚠️  source_framework.sh outdated — run sync migrate to update")

    # Verify Makefile matches the framework template
    mf = path / ".devcontainer/Makefile"
    if not mf.exists():
        print(f"  ❌ Makefile missing")
    else:
        if mf.read_text() == THIN_MAKEFILE:
            print(f"  ✅ Makefile matches template")
        else:
            print(f"  ⚠️  Makefile outdated — run sync migrate to update")

    # Verify README badges and footer
    _validate_readme(repo_entry, path)


def run(args):
    target_repo = args.repo

    try:
        repos = load_repos()
    except Exception as e:
        print(f"x Failed to load repos.yaml: {e}", file=sys.stderr)
        sys.exit(1)

    # Schema validation
    errors = validate_repos(repos)
    if errors:
        print("x Schema validation failed:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    print("+ Schema validation passed")

    # Filter to specific repo if requested
    if target_repo:
        repos = [r for r in repos if r.repo == target_repo or r.name == target_repo]
        if not repos:
            print(f"x Repo '{target_repo}' not found in repos.yaml", file=sys.stderr)
            sys.exit(1)

    # GitHub accessibility check
    gh_errors = []
    for repo_entry in repos:
        owner, name = repo_entry.owner, repo_entry.repo_name
        try:
            exists, archived = check_repo_exists(owner, name)
            if not exists:
                gh_errors.append(
                    f"{repo_entry.name}: repo '{repo_entry.repo}' not found (404)"
                )
            elif archived and repo_entry.status != "archived":
                gh_errors.append(
                    f"{repo_entry.name}: repo is archived on GitHub but status is '{repo_entry.status}'"
                )
        except GHAPIError as e:
            gh_errors.append(f"{repo_entry.name}: API error: {e.message}")

    if gh_errors:
        print("x GitHub accessibility check failed:")
        for err in gh_errors:
            print(f"  - {err}")
        sys.exit(1)

    print(f"+ All {len(repos)} repos accessible on GitHub")

    # Local clone validation (devcontainer.json, migration status, templates)
    for repo_entry in repos:
        try:
            repo_path = _resolve_repo_path(repo_entry.repo_name)
        except Exception:
            repo_path = None
        _validate_local(repo_entry, repo_path)

    print("\n+ validation complete")

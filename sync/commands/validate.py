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
        print(f"  📭 {repo_entry.url}: local clone not found at {path}")
        return

    print(f"\n── {repo_entry.url} ({path}) ──")

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
                print(f"  ✅ source_framework.sh matches template ({pinned})")
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

    # Verify docs structure
    _validate_docs(repo_entry, path)


def _validate_docs(repo_entry, path):
    """Validate documentation structure for MkDocs GitHub Pages."""
    from pathlib import Path
    path = Path(path)
    issues = []

    # mkdocs.yaml
    mk = path / "mkdocs.yaml"
    if not mk.exists():
        issues.append("mkdocs.yaml missing")
    else:
        content = mk.read_text()
        if "INHERIT:" not in content:
            issues.append("mkdocs.yaml not using INHERIT pattern")
        if "rum_snippet" not in content:
            issues.append("mkdocs.yaml missing rum_snippet in extra (RUM tracking disabled)")

    # docs/ directory
    docs = path / "docs"
    if not docs.is_dir():
        issues.append("docs/ directory missing")
    else:
        # index.md
        if not (docs / "index.md").exists():
            issues.append("docs/index.md missing")

        # overrides/main.html (for RUM)
        overrides = docs / "overrides"
        if not overrides.is_dir():
            issues.append("docs/overrides/ missing")
        elif not (overrides / "main.html").exists():
            issues.append("docs/overrides/main.html missing")

        # requirements
        reqs = docs / "requirements"
        if not reqs.is_dir():
            issues.append("docs/requirements/ missing")
        elif not (reqs / "requirements-mkdocs.txt").exists():
            issues.append("docs/requirements/requirements-mkdocs.txt missing")

    # GitHub Actions workflow for docs deployment
    ghpages = path / ".github/workflows/deploy-ghpages.yaml"
    if not ghpages.exists():
        issues.append(".github/workflows/deploy-ghpages.yaml missing")

    # Integration tests workflow
    ci = path / ".github/workflows/integration-tests.yaml"
    if not ci.exists():
        issues.append(".github/workflows/integration-tests.yaml missing")

    # .vscode/mcp.json
    mcp = path / ".vscode/mcp.json"
    if not mcp.exists():
        issues.append(".vscode/mcp.json missing")

    # test/integration.sh
    ti = path / ".devcontainer/test/integration.sh"
    if not ti.exists():
        issues.append(".devcontainer/test/integration.sh missing")

    # my_functions.sh
    mf = path / ".devcontainer/util/my_functions.sh"
    if not mf.exists():
        issues.append(".devcontainer/util/my_functions.sh missing")

    if issues:
        print(f"  ⚠️  repo structure: {len(issues)} issue(s)")
        for issue in issues:
            print(f"    ❌ {issue}")
    else:
        print(f"  ✅ repo structure complete")


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

    # Always skip codespaces-framework (it's the source of truth, not a consumer)
    repos = [r for r in repos if r.name != "codespaces-framework"]

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
                    f"{repo_entry.url}: not found (404)"
                )
            elif archived and repo_entry.status != "archived":
                gh_errors.append(
                    f"{repo_entry.url}: repo is archived on GitHub but status is '{repo_entry.status}'"
                )
        except GHAPIError as e:
            gh_errors.append(f"{repo_entry.url}: API error: {e.message}")

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
